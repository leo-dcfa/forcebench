"""Calling models. One user turn, the fixed system prompt, no tools."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from pydantic import BaseModel

from forcebench.models import ModelConfig, Provider

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


def _build_model(m: ModelConfig, p: Provider):
    match p.kind:
        case "openai_compatible" | "openai":
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            base_url = p.resolved_base_url()
            if p.kind == "openai_compatible" and not base_url:
                raise RuntimeError(f"no base URL: set {p.base_url_env}")
            return OpenAIChatModel(
                m.endpoint_model, provider=OpenAIProvider(base_url=base_url, api_key=p.api_key())
            )
        case "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(m.endpoint_model, provider=AnthropicProvider(api_key=p.api_key()))
        case "google":
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            return GoogleModel(m.endpoint_model, provider=GoogleProvider(api_key=p.api_key()))
    raise ValueError(p.kind)


def _settings(m: ModelConfig, effort: str, timeout: float) -> dict[str, Any]:
    sampling = dict(m.sampling)
    settings: dict[str, Any] = {"max_tokens": m.max_tokens, "timeout": timeout}
    for key in ("temperature", "top_p", "seed"):
        if key in sampling:
            settings[key] = sampling.pop(key)
    extra = {**sampling, **m.efforts[effort]}
    if extra:
        settings["extra_body"] = extra
    return settings


def _retryable(e: Exception) -> bool:
    name = type(e).__name__
    text = str(e).lower()
    if name in {"APIConnectionError", "ConnectError", "RemoteProtocolError", "ReadError"}:
        return True
    status = getattr(e, "status_code", None) or getattr(
        getattr(e, "response", None), "status_code", None
    )
    if isinstance(status, int) and (status >= 500 or status == 429):
        return True
    return "connection" in text or "overloaded" in text


class Client:
    def __init__(
        self, m: ModelConfig, p: Provider, effort: str, timeout: float = 3600, retries: int = 4
    ):
        from pydantic_ai import Agent

        if effort not in m.efforts:
            raise KeyError(f"{m.id} has no effort {effort!r}; have {sorted(m.efforts)}")
        self.m, self.effort, self.retries = m, effort, retries
        self.settings = _settings(m, effort, timeout)
        self._model = _build_model(m, p)
        self._agent_cls = Agent

    async def generate(self, system: str, user: str) -> Generation:
        from pydantic_ai.messages import ThinkingPart

        agent = self._agent_cls(self._model, system_prompt=system)
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            t0 = time.monotonic()
            try:
                r = await agent.run(user, model_settings=self.settings)
            except Exception as e:
                last_err = e
                # The budget is tokens (max_tokens), not wall-clock time: slow hardware must not
                # cost a model points. A timeout is an endpoint problem; the case is re-run on resume.
                if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                    return Generation(
                        error=f"timed out after {time.monotonic() - t0:.0f}s (not scored; re-run with --resume)",
                        finish_reason="timeout", latency_s=time.monotonic() - t0, attempts=attempt,
                    )  # fmt: skip
                if attempt < self.retries and _retryable(e):
                    await asyncio.sleep(min(30 * 2 ** (attempt - 1), 300))
                    continue
                if _retryable(e):
                    return Generation(error=f"{type(e).__name__}: {e}"[:1000], attempts=attempt)
                # Anything else (400s, empty output, parse errors) counts as a failed answer.
                return Generation(
                    finish_reason=f"error: {type(e).__name__}: {e}"[:500],
                    latency_s=time.monotonic() - t0,
                    attempts=attempt,
                )
            latency = time.monotonic() - t0
            resp = r.all_messages()[-1]
            reasoning = "\n".join(
                p.content for p in getattr(resp, "parts", []) if isinstance(p, ThinkingPart)
            )
            u = r.usage
            return Generation(
                text=r.output if isinstance(r.output, str) else str(r.output),
                reasoning=reasoning,
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                reasoning_tokens=int((u.details or {}).get("reasoning_tokens", 0)),
                finish_reason=getattr(resp, "finish_reason", None),
                latency_s=latency,
                attempts=attempt,
            )
        return Generation(
            error=f"{type(last_err).__name__}: {last_err}"[:1000], attempts=self.retries
        )
