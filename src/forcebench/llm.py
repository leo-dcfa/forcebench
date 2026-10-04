"""Calling models. One user turn, the fixed system prompt, no tools."""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from forcebench.models import ModelConfig, Provider

if TYPE_CHECKING:
    from pydantic_ai import Agent, AgentRunResult
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.settings import ModelSettings

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
    # Set when the endpoint failed (connection, 5xx, ...) after retries, or with a client error
    # that no retry fixes (400, 401, 404, ...). Such cases are not scored; they are re-run with
    # `forcebench run --resume`.
    error: str | None = None


def _count_hidden_output() -> None:
    """Keep the thinking a server leaves out of completion_tokens. pydantic-ai keeps prompt and
    completion tokens but drops total_tokens, and some OpenAI-compatible servers (Gemini's) count
    the model's thinking only in the total. The difference is recorded in the usage details as
    hidden_output_tokens, so an answer's output includes everything the model generated."""
    import pydantic_ai.models.openai as oai

    if getattr(oai._map_usage, "forcebench_hidden_output", False):
        return
    original = oai._map_usage

    def mapped(response: Any, *args: Any, **kwargs: Any) -> Any:
        used = original(response, *args, **kwargs)
        raw = getattr(response, "usage", None)
        total, prompt, completion = (
            getattr(raw, k, None) for k in ("total_tokens", "prompt_tokens", "completion_tokens")
        )
        if total and prompt is not None and completion is not None and total > prompt + completion:
            hidden = used.details.get("hidden_output_tokens", 0) + total - prompt - completion
            used.details["hidden_output_tokens"] = hidden
        return used

    mapped.forcebench_hidden_output = True  # type: ignore[attr-defined]
    oai._map_usage = mapped


def _hidden(used: Any) -> int:
    return int((getattr(used, "details", None) or {}).get("hidden_output_tokens", 0))


def _build_model(m: ModelConfig, p: Provider, timeout: float):
    """Build the pydantic-ai model with SDK retries OFF: a retry silently restarts the answer,
    and retrying only the answers that take long biases results towards short answers."""
    match p.kind:
        case "openai_compatible" | "openai":
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            _count_hidden_output()
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


def _check_settings(m: ModelConfig, p: Provider, effort: str, settings: dict[str, Any]) -> None:
    """Refuse settings the provider would silently drop. pydantic-ai's Google model has no
    ``extra_body``: sampling keys other than temperature/top_p/seed and every effort field
    would never reach the API, while the run records them as sent."""
    if p.kind == "google" and settings.get("extra_body"):
        raise ValueError(
            f"{m.id}@{effort}: a Google provider cannot send extra request-body fields "
            f"{sorted(settings['extra_body'])} (from `sampling` and `efforts.{effort}`); "
            "pydantic-ai's Google model ignores extra_body. Map them to Google model settings "
            "(e.g. google_thinking_config) in llm.py before using this configuration."
        )


def recorded_request(m: ModelConfig, effort: str) -> dict[str, Any]:
    """The request as a run records it (run.json ``request``): the model settings sent, and
    how they are sent (streamed, SDK retries off; see Client)."""
    return {**_settings(m, effort), "stream": True, "sdk_retries": 0}


# Client errors that a later try can get past: request timeout, conflict, too early, rate
# limited. Any other 4xx (bad request, unauthorised, forbidden, not found, payload too large...)
# fails the same way every time, so it is not retried.
RETRYABLE_CLIENT_ERRORS = frozenset({408, 409, 425, 429})


def _status_code(e: BaseException) -> int | None:
    """The HTTP status of an endpoint error, from the exception or the ones it was raised from
    (pydantic-ai's ModelHTTPError wraps the SDK's status error)."""
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for status in (_attr(cur, "status_code"), _attr(_attr(cur, "response"), "status_code")):
            if isinstance(status, int):
                return status
        cur = cur.__cause__ or cur.__context__
    return None


def _attr(obj: object, name: str) -> Any:
    """obj.name, or None; some SDK exceptions raise from properties they cannot fill."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _permanent_client_error(e: BaseException) -> int | None:
    """The status of a client error no retry will fix (4xx other than 408/409/425/429)."""
    status = _status_code(e)
    if status is not None and 400 <= status < 500 and status not in RETRYABLE_CLIENT_ERRORS:
        return status
    return None


class _UnfinishedReplyError(Exception):
    """The server ended a reply without saying why: it was cut off, not finished."""


# Raw finish reasons with which a server says it gave up on the request itself (vLLM, SGLang:
# abort, error; OpenAI Responses: cancelled, failed). The reply was cut off, not finished.
_ABORTED = {"abort", "aborted", "error", "cancelled", "failed"}


def _raw_finish_reason(resp: ModelResponse) -> str | None:
    raw = (resp.provider_details or {}).get("finish_reason")
    return str(raw) if raw else None


def _finished(resp: ModelResponse | None) -> bool:
    """Whether the server finished the reply, i.e. reported how it ended (stop, length, content
    filter...). A raw reason pydantic-ai does not map counts too, unless it says the server
    aborted the request."""
    if resp is None:
        return False
    raw = _raw_finish_reason(resp)
    if raw is not None and raw.lower() in _ABORTED:
        return False
    return resp.finish_reason is not None or raw is not None


def _endpoint_error(e: Exception | None, resp: ModelResponse | None = None) -> str:
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    what = f"{type(e).__name__}: {e}"
    if e is not None and (status := _permanent_client_error(e)) is not None:
        what = f"client error {status}, not retried: {what}"
    elif isinstance(e, _UnfinishedReplyError | UnexpectedModelBehavior):
        raw = _raw_finish_reason(resp) if resp is not None else None
        why = f"finish reason {raw!r}" if raw else "no finish reason"
        what = f"reply not finished by the server ({why}): {what}"
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
        _check_settings(m, p, effort, self.settings)
        self._model = _build_model(m, p, timeout)
        self._agent_cls = Agent

    async def _run(self, agent: Agent[None, str], user: str, streamed: _Streamed) -> AgentRunResult:
        """One model call. Driving the run node by node streams every model request through
        `streamed` and keeps the last response, so a failure can be classified by how the
        reply ended (its finish reason)."""
        settings = cast("ModelSettings", self.settings)  # plus provider fields (extra_body)
        async with agent.iter(user, model_settings=settings) as run:
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
                    raise _UnfinishedReplyError("the answer was cut off")
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
                    assert streamed.response is not None
                    # The server stops at exactly max_tokens when the budget runs out.
                    budget = "token limit" in str(e)
                    used = streamed.response.usage
                    return Generation(
                        text=streamed.text(),
                        reasoning=streamed.thinking(),
                        input_tokens=used.input_tokens or 0,
                        output_tokens=(self.settings["max_tokens"] or 0)
                        if budget
                        else (used.output_tokens or 0) + _hidden(used),
                        finish_reason=f"error: {type(e).__name__}: {e}"[:500],
                        latency_s=elapsed,
                        attempts=attempt,
                    )
                # Anything else came from the endpoint (5xx, overload, out of memory, rate
                # limiting, a dropped or unfinished stream...): start the answer again from
                # scratch with backoff, then leave it unscored for --resume. A client error that
                # a retry cannot fix (400, 401, 404...) ends the answer unscored at once.
                if attempt < self.retries and _permanent_client_error(e) is None:
                    await asyncio.sleep(min(30 * 2 ** (attempt - 1), 300))
                    continue
                return Generation(
                    error=_endpoint_error(e, streamed.response), latency_s=elapsed, attempts=attempt
                )
            resp = r.response
            usage = r.usage
            reasoning = "\n".join(p.content for p in resp.parts if isinstance(p, ThinkingPart))
            return Generation(
                text=r.output if isinstance(r.output, str) else str(r.output),
                reasoning=reasoning,
                input_tokens=usage.input_tokens or 0,
                # Everything the model generated, the thinking a server left out of its
                # completion tokens included (_count_hidden_output).
                output_tokens=(usage.output_tokens or 0) + _hidden(usage),
                reasoning_tokens=max(
                    int((usage.details or {}).get("reasoning_tokens", 0)), _hidden(usage)
                ),
                # The server's own reason where pydantic-ai has no name for it.
                finish_reason=resp.finish_reason or _raw_finish_reason(resp),
                latency_s=time.monotonic() - t0,
                attempts=attempt,
            )
        return Generation(error=_endpoint_error(last_err), attempts=self.retries)
