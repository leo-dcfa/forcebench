"""The client builds a real pydantic-ai agent (catches API drift) and classifies failures."""

import json

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


class _FakeServer:
    """An OpenAI-compatible endpoint that streams a scripted reply, as vLLM does."""

    def __init__(self, deltas: list[dict], finish_reason: str):
        import http.server
        import threading

        chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
        chunks.append({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 64}})
        body = (
            "".join(
                f"data: {json.dumps({'id': 'x', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'm', **c})}\n\n"
                for c in chunks
            )
            + "data: [DONE]\n\n"
        )

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self):
        self.httpd.shutdown()


def _client(monkeypatch, url):
    reg = load_registry()
    m = reg.get("qwen3.8-27b-awq-int4")
    p = reg.provider_for(m)
    monkeypatch.setenv(p.base_url_env, url)
    return Client(m, p, "low", retries=1)


@pytest.mark.asyncio
async def test_budget_exhausted_while_thinking_is_scored_and_keeps_the_reasoning(monkeypatch):
    server = _FakeServer(
        [{"role": "assistant", "reasoning_content": "Let me think. "}]
        + [{"reasoning_content": "Still thinking. "}] * 3,
        finish_reason="length",
    )
    try:
        gen = await _client(monkeypatch, server.url).generate("system", "hi")
    finally:
        server.close()
    assert gen.error is None, "running out of budget is the model's failure, so it is scored"
    assert "token limit" in (gen.finish_reason or "")
    assert gen.text == ""
    assert gen.reasoning == "Let me think. " + "Still thinking. " * 3
    assert gen.output_tokens == 32768


@pytest.mark.asyncio
async def test_streamed_answer_keeps_text_reasoning_and_usage(monkeypatch):
    server = _FakeServer(
        [
            {"role": "assistant", "reasoning_content": "Plan it."},
            {"content": "Answer: "},
            {"content": "B"},
        ],
        finish_reason="stop",
    )
    try:
        gen = await _client(monkeypatch, server.url).generate("system", "hi")
    finally:
        server.close()
    assert (gen.text, gen.reasoning, gen.finish_reason) == ("Answer: B", "Plan it.", "stop")
    assert (gen.input_tokens, gen.output_tokens) == (10, 64)
