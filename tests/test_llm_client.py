"""The client builds a real pydantic-ai agent (catches API drift) and classifies failures."""

import json
from dataclasses import dataclass

import pytest

from forcebench.llm import Client
from forcebench.models import load_registry


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr("forcebench.llm.asyncio.sleep", lambda s: _noop())


@pytest.mark.asyncio
async def test_endpoint_errors_are_unscored_and_retried(monkeypatch, no_backoff):
    reg = load_registry()
    m = reg.get("qwen3.8-27b-awq-int4")
    p = reg.provider_for(m)
    monkeypatch.setenv(p.base_url_env, "http://127.0.0.1:9/v1")  # nothing listens here
    client = Client(m, p, "low", retries=2)
    gen = await client.generate("system", "hi")
    assert gen.error is not None, "an unreachable endpoint must not be scored"
    assert gen.attempts == 2


async def _noop():
    return None


def _sse(deltas: list[dict], finish_reason: str | None = "stop") -> str:
    """A streamed reply as vLLM sends it. Without a finish reason the stream just stops, as when
    the server dies mid-answer: no final chunk, no usage, no [DONE]."""
    chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    if finish_reason is not None:
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
        chunks.append({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 64}})
    body = "".join(
        f"data: {json.dumps({'id': 'x', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'm', **c})}\n\n"
        for c in chunks
    )
    return body + ("data: [DONE]\n\n" if finish_reason is not None else "")


@dataclass
class _Drop:
    """Send the start of a reply, then drop the connection mid-stream."""

    body: str


class _FakeServer:
    """An OpenAI-compatible endpoint that streams scripted replies, one per request (the last
    one repeats)."""

    def __init__(self, *replies: str | _Drop):
        import http.server
        import threading

        self.requests = 0
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                reply = replies[min(server.requests, len(replies) - 1)]
                server.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if isinstance(reply, _Drop):
                    # Promise more than is sent, so the client sees a broken body, not an end.
                    self.send_header("Content-Length", str(len(reply.body) + 10_000))
                    self.end_headers()
                    self.wfile.write(reply.body.encode())
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.end_headers()
                self.wfile.write(reply.encode())

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self):
        self.httpd.shutdown()


def _client(monkeypatch, url, retries: int = 1):
    reg = load_registry()
    m = reg.get("qwen3.8-27b-awq-int4")
    p = reg.provider_for(m)
    monkeypatch.setenv(p.base_url_env, url)
    return Client(m, p, "low", retries=retries)


async def _generate(monkeypatch, *replies: str | _Drop, retries: int = 1):
    server = _FakeServer(*replies)
    try:
        gen = await _client(monkeypatch, server.url, retries).generate("system", "hi")
    finally:
        server.close()
    return gen, server.requests


THINKING = [{"role": "assistant", "reasoning_content": "Let me think. "}] + [
    {"reasoning_content": "Still thinking. "}
] * 3


@pytest.mark.asyncio
async def test_budget_exhausted_while_thinking_is_scored_and_keeps_the_reasoning(monkeypatch):
    gen, _ = await _generate(monkeypatch, _sse(THINKING, finish_reason="length"))
    assert gen.error is None, "running out of budget is the model's failure, so it is scored"
    assert "token limit" in (gen.finish_reason or "")
    assert gen.text == ""
    assert gen.reasoning == "Let me think. " + "Still thinking. " * 3
    assert gen.output_tokens == 32768


@pytest.mark.asyncio
async def test_streamed_answer_keeps_text_reasoning_and_usage(monkeypatch):
    gen, _ = await _generate(
        monkeypatch,
        _sse(
            [
                {"role": "assistant", "reasoning_content": "Plan it."},
                {"content": "Answer: "},
                {"content": "B"},
            ]
        ),
    )
    assert (gen.text, gen.reasoning, gen.finish_reason) == ("Answer: B", "Plan it.", "stop")
    assert (gen.input_tokens, gen.output_tokens) == (10, 64)
    assert (gen.error, gen.attempts) == (None, 1)


@pytest.mark.asyncio
async def test_empty_reply_the_model_finished_is_scored_as_no_answer(monkeypatch):
    gen, _ = await _generate(
        monkeypatch, _sse([{"role": "assistant", "content": ""}], finish_reason="stop")
    )
    assert gen.error is None, "the model ended its turn without answering: its own failure"
    assert (gen.finish_reason or "").startswith("error: UnexpectedModelBehavior")
    assert "token limit" not in (gen.finish_reason or "")
    assert (gen.text, gen.output_tokens) == ("", 0)


@pytest.mark.asyncio
async def test_stream_that_stops_without_a_finish_reason_is_not_scored(monkeypatch, no_backoff):
    # The server died mid-thought: pydantic-ai sees a thinking-only reply, as for an empty answer.
    gen, requests = await _generate(monkeypatch, _sse(THINKING, finish_reason=None), retries=2)
    assert gen.error is not None, "a reply the server never finished is the endpoint's failure"
    assert "not finished" in gen.error
    assert (gen.attempts, requests) == (2, 2), "retried from scratch like any endpoint failure"


@pytest.mark.asyncio
async def test_answer_cut_off_without_a_finish_reason_is_not_scored(monkeypatch):
    gen, _ = await _generate(
        monkeypatch, _sse([{"role": "assistant", "content": "Answer: "}], finish_reason=None)
    )
    assert gen.error is not None, "half an answer the server never finished must not be graded"
    assert gen.text == ""


@pytest.mark.asyncio
async def test_empty_stream_is_not_scored(monkeypatch):
    gen, _ = await _generate(monkeypatch, "data: [DONE]\n\n")
    assert gen.error is not None
    assert "not finished" in gen.error


@pytest.mark.asyncio
async def test_connection_dropped_mid_stream_is_retried_and_counted(monkeypatch, no_backoff):
    answer = _sse([{"role": "assistant", "content": "Answer: B"}])
    gen, requests = await _generate(monkeypatch, _Drop(answer[:120]), answer, retries=4)
    assert (gen.error, gen.text) == (None, "Answer: B")
    assert (gen.attempts, requests) == (2, 2), "the retry is recorded, not hidden"
