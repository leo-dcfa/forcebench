"""Calling models. One user turn, the fixed system prompt, no tools."""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from forcebench.models import ModelConfig, Provider

if TYPE_CHECKING:
    from pydantic_ai import Agent, AgentRunResult
    from pydantic_ai.messages import ModelResponse

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


class Generation(BaseModel):
    text: str = ""
    reasoning: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str | None = None
    latency_s: float = 0.0
    attempts: int = 1
    # Set when the endpoint failed (connection, 5xx) after retries. Such cases are not
    # scored; they are re-run with `forcebench run --resume`.
    error: str | None = None


def _build_model(m: ModelConfig, p: Provider, timeout: float):
    """Build the pydantic-ai model with SDK retries OFF: a retry silently restarts the answer,
    and retrying only the answers that take long biases results towards short answers."""
    match p.kind:
        case "openai_compatible" | "openai":
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            base_url = p.resolved_base_url()
            if p.kind == "openai_compatible" and not base_url:
                raise RuntimeError(f"no base URL: set {p.base_url_env}")
            client = AsyncOpenAI(
                base_url=base_url, api_key=p.api_key() or "none", max_retries=0, timeout=timeout
            )
            return OpenAIChatModel(m.endpoint_model, provider=OpenAIProvider(openai_client=client))
        case "anthropic":
            from anthropic import AsyncAnthropic
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            client = AsyncAnthropic(api_key=p.api_key(), max_retries=0, timeout=timeout)
            return AnthropicModel(
                m.endpoint_model, provider=AnthropicProvider(anthropic_client=client)
            )
        case "google":
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            return GoogleModel(m.endpoint_model, provider=GoogleProvider(api_key=p.api_key()))
    raise ValueError(p.kind)


def _settings(m: ModelConfig, effort: str) -> dict[str, Any]:
    sampling = dict(m.sampling)
    settings: dict[str, Any] = {"max_tokens": m.max_tokens}
    for key in ("temperature", "top_p", "seed"):
        if key in sampling:
            settings[key] = sampling.pop(key)
    extra = {**sampling, **m.efforts[effort]}
    if extra:
        settings["extra_body"] = extra
    return settings


def _retryable(e: Exception) -> bool:
    """Endpoint failures worth retrying. Timeouts are never retried (see Client.generate)."""
    name = type(e).__name__
    text = str(e).lower()
    if "timeout" in name.lower() or "timed out" in text:
        return False
    if name in {"APIConnectionError", "ConnectError", "RemoteProtocolError", "ReadError"}:
        return True
    status = getattr(e, "status_code", None) or getattr(
        getattr(e, "response", None), "status_code", None
    )
    if isinstance(status, int) and (status >= 500 or status == 429):
        return True
    return "connection" in text or "overloaded" in text


class _UnfinishedReplyError(Exception):
    """The server ended a reply without saying why: it was cut off, not finished."""


def _finished(resp: ModelResponse | None) -> bool:
    """Whether the server finished the reply, i.e. reported a finish reason (stop, length,
    content filter...). The raw reason counts too, for values pydantic-ai does not map."""
    if resp is None:
        return False
    raw = (resp.provider_details or {}).get("finish_reason")
    return resp.finish_reason is not None or bool(raw)


def _endpoint_error(e: Exception | None) -> str:
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    what = f"{type(e).__name__}: {e}"
    if isinstance(e, _UnfinishedReplyError | UnexpectedModelBehavior):
        what = f"reply not finished by the server: {what}"
    return what[:1000]


class _Streamed:
    """What the model has streamed so far, per response part. A reply that fails still leaves a
    record of what the model wrote, e.g. the reasoning that used up the token budget."""

    def __init__(self) -> None:
        self.parts: dict[int, tuple[bool, list[str]]] = {}  # index -> (is_thinking, chunks)
        # The last complete response, kept so a failed run can still tell how the reply ended.
        self.response: ModelResponse | None = None

    async def collect(self, _ctx: Any, events: Any) -> None:
        from pydantic_ai.messages import (
            PartDeltaEvent,
            PartStartEvent,
            ThinkingPart,
            ThinkingPartDelta,
        )

        async for ev in events:
            if isinstance(ev, PartStartEvent):
                content = getattr(ev.part, "content", None)
                self.parts[ev.index] = (
                    isinstance(ev.part, ThinkingPart),
                    [content] if isinstance(content, str) else [],
                )
            elif isinstance(ev, PartDeltaEvent):
                delta = getattr(ev.delta, "content_delta", None)
                if isinstance(delta, str):
                    thinking = isinstance(ev.delta, ThinkingPartDelta)
                    self.parts.setdefault(ev.index, (thinking, []))[1].append(delta)

    def _join(self, thinking: bool) -> str:
        return "".join(
            "".join(chunks) for _, (t, chunks) in sorted(self.parts.items()) if t is thinking
        )

    def thinking(self) -> str:
        return self._join(True)

    def text(self) -> str:
        return self._join(False)


class Client:
    """One model configuration. Responses are streamed: proxies commonly cap the time to a
    complete (non-streamed) response, which would cut off slow machines' long answers."""

    def __init__(
        self, m: ModelConfig, p: Provider, effort: str, timeout: float = 4 * 3600, retries: int = 4
    ):
        from pydantic_ai import Agent

        if effort not in m.efforts:
            raise KeyError(f"{m.id} has no effort {effort!r}; have {sorted(m.efforts)}")
        self.m, self.effort, self.retries = m, effort, retries
        self.settings = _settings(m, effort)
        self._model = _build_model(m, p, timeout)
        self._agent_cls = Agent

    async def _run(self, agent: Agent[None, str], user: str, streamed: _Streamed) -> AgentRunResult:
        """One model call. Driving the run node by node streams every model request through
        `streamed` and keeps the last response, so a failure can be classified by how the
        reply ended (its finish reason)."""
        async with agent.iter(user, model_settings=self.settings) as run:
            async for node in run:
                if agent.is_model_request_node(node):
                    async with node.stream(run.ctx) as events:
                        await streamed.collect(run.ctx, events)
                elif agent.is_call_tools_node(node):
                    streamed.response = node.model_response
            assert run.result is not None, "the agent run did not finish"
            return run.result

    async def generate(self, system: str, user: str) -> Generation:
        from pydantic_ai.exceptions import UnexpectedModelBehavior
        from pydantic_ai.messages import ThinkingPart

        # retries=0: pydantic-ai would otherwise re-prompt the model after an empty
        # answer, a hidden retry that would change the conversation being measured.
        agent = self._agent_cls(self._model, system_prompt=system, retries=0)
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            t0 = time.monotonic()
            streamed = _Streamed()
            try:
                r = await self._run(agent, user, streamed)
                if not _finished(r.response):
                    raise _UnfinishedReplyError("the stream ended without a finish reason")
            except Exception as e:
                last_err = e
                elapsed = time.monotonic() - t0
                # The budget is tokens (max_tokens), not wall-clock time: slow hardware must not
                # cost a model points. A timeout is an endpoint problem; re-run with --resume.
                if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                    return Generation(
                        error=f"timed out after {elapsed:.0f}s (not scored; re-run with --resume)",
                        finish_reason="timeout", latency_s=elapsed, attempts=attempt,
                    )  # fmt: skip
                # Only the model's own failures are scored as failed answers: it exhausted the
                # token budget, or it ended its reply without an answer. What it wrote is kept.
                # A reply the server never finished (an empty stream, or one that stopped with no
                # finish reason because the server died mid-answer) is the endpoint's failure.
                if isinstance(e, UnexpectedModelBehavior) and _finished(streamed.response):
                    # The server stops at exactly max_tokens when the budget runs out.
                    budget = "token limit" in str(e)
                    return Generation(
                        text=streamed.text(),
                        reasoning=streamed.thinking(),
                        output_tokens=(self.settings["max_tokens"] or 0) if budget else 0,
                        finish_reason=f"error: {type(e).__name__}: {e}"[:500],
                        latency_s=elapsed,
                        attempts=attempt,
                    )
                # Anything else came from the endpoint (5xx, overload, out of memory, bad
                # request, a dropped or unfinished stream...): start the answer again from
                # scratch with backoff, then leave it unscored for --resume.
                if attempt < self.retries:
                    await asyncio.sleep(min(30 * 2 ** (attempt - 1), 300))
                    continue
                return Generation(error=_endpoint_error(e), latency_s=elapsed, attempts=attempt)
            resp = r.response
            usage = r.usage
            reasoning = "\n".join(p.content for p in resp.parts if isinstance(p, ThinkingPart))
            return Generation(
                text=r.output if isinstance(r.output, str) else str(r.output),
                reasoning=reasoning,
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                reasoning_tokens=int((usage.details or {}).get("reasoning_tokens", 0)),
                finish_reason=resp.finish_reason,
                latency_s=time.monotonic() - t0,
                attempts=attempt,
            )
        return Generation(error=_endpoint_error(last_err), attempts=self.retries)
