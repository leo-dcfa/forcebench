"""The client builds a real pydantic-ai agent (catches API drift) and classifies failures."""

import pytest

from forcebench.llm import Client
from forcebench.models import load_registry


@pytest.mark.asyncio
async def test_endpoint_errors_are_unscored_and_retried(monkeypatch):
    reg = load_registry()
    m = reg.get("qwen3.8-27b-awq-int4")
    p = reg.provider_for(m)
    monkeypatch.setenv(p.base_url_env, "http://127.0.0.1:9/v1")  # nothing listens here
    client = Client(m, p, "low", retries=2)
    monkeypatch.setattr("forcebench.llm.asyncio.sleep", lambda s: _noop())
    gen = await client.generate("system", "hi")
    assert gen.error is not None, "an unreachable endpoint must not be scored"
    assert gen.attempts == 2


async def _noop():
    return None
