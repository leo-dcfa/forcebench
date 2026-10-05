"""The model proxy for agent runs (forcebench.agent.proxy): what it changes in each request, the
budget it enforces, and what it records."""

import ast
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib import error, request

import pytest

import forcebench.agent.proxy as proxy_module
from forcebench.agent.proxy import (
    AnthropicStream,
    Budget,
    Proxy,
    anthropic_error,
    make_handler,
    merge,
    rewrite,
    to_chat,
    usage_from_sse,
)


def test_the_proxy_runs_in_the_agent_image_s_python():
    # The proxy runs as the agent image's python3 (Debian bookworm: 3.11), not the harness's.
    ast.parse(Path(proxy_module.__file__).read_text(), feature_version=(3, 11))


def test_the_configuration_wins_over_the_harness():
    body = {
        "model": "model",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.0,
        "top_p": 0.1,
        "max_completion_tokens": 999_999,
        "max_tokens": 999_999,
        "chat_template_kwargs": {"reasoning_effort": "low", "other": 1},
        "reasoning_effort": "high",
        "seed": 7,
        "stream": True,
    }
    inject = {
        "temperature": 1.0,
        "chat_template_kwargs": {"thinking": True, "reasoning_effort": 75},
    }
    out = rewrite(body, "served-name", inject, 32768)
    assert out["model"] == "served-name"
    assert out["temperature"] == 1.0 and "top_p" not in out  # sampling is the config's only
    assert out["max_tokens"] == 32768 and "max_completion_tokens" not in out
    # How the model reasons is the configuration's alone: none of the harness's switches survive.
    assert out["chat_template_kwargs"] == {"reasoning_effort": 75, "thinking": True}
    assert "reasoning_effort" not in out and "seed" not in out
    assert out["stream_options"] == {"include_usage": True}
    assert out["messages"] == body["messages"]


def test_merge_is_recursive_and_the_extra_side_wins():
    assert merge({"a": {"b": 1, "c": 2}, "d": 1}, {"a": {"b": 3}, "e": 4}) == {
        "a": {"b": 3, "c": 2},
        "d": 1,
        "e": 4,
    }


def test_the_budget_refuses_once_requests_or_tokens_are_spent():
    b = Budget(max_requests=2, max_output_tokens=100)
    assert b.take() is None and b.take() is None
    assert "request budget" in (b.take() or "")
    b = Budget(max_requests=0, max_output_tokens=100)
    assert b.take() is None
    b.spend(100)
    assert "token budget" in (b.take() or "")


def test_usage_and_finish_reason_are_read_from_a_stream():
    lines = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
        b"\n",
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7}}\n',
        b"data: not json\n",
        b"data: [DONE]\n",
    ]
    usage, finish = usage_from_sse(lines)
    assert usage == {"prompt_tokens": 5, "completion_tokens": 7}
    assert finish == "stop"


class _Upstream(BaseHTTPRequestHandler):
    """A model server that records what it was asked and streams a short reply."""

    seen: ClassVar[list[dict]] = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        self.seen.append(
            {"path": self.path, "auth": self.headers.get("Authorization"),
             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))}
        )  # fmt: skip
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in (
            {"choices": [{"delta": {"content": "Answer: B"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3}},
        ):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def servers(tmp_path):
    _Upstream.seen = []
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    log = tmp_path / "requests.jsonl"
    proxy = Proxy(
        {
            "FB_UPSTREAM": f"http://127.0.0.1:{up.server_port}/v1",
            "FB_UPSTREAM_KEY": "secret",
            "FB_MODEL": "served-name",
            "FB_INJECT": json.dumps({"chat_template_kwargs": {"reasoning_effort": "high"}}),
            "FB_MAX_TOKENS": "32768",
            "FB_MAX_REQUESTS": "1",
            "FB_LOG": str(log),
        }
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(proxy))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1", log
    srv.shutdown()
    up.shutdown()


def _post(url: str, body: dict) -> tuple[int, bytes]:
    req = request.Request(
        url + "/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    try:
        with request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except error.HTTPError as e:
        return e.code, e.read()


def test_a_request_goes_through_rewritten_and_is_logged(servers):
    url, log = servers
    status, body = _post(url, {"model": "model", "messages": [], "stream": True, "temperature": 0})
    assert status == 200 and b"Answer: B" in body
    sent = _Upstream.seen[0]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["auth"] == "Bearer secret"  # the key is added here; the agent never has it
    assert sent["body"]["model"] == "served-name"
    assert sent["body"]["chat_template_kwargs"] == {"reasoning_effort": "high"}
    assert "temperature" not in sent["body"]
    rec = json.loads(log.read_text().splitlines()[0])
    assert (rec["status"], rec["prompt_tokens"], rec["completion_tokens"], rec["finish_reason"]) == (
        200, 11, 3, "stop",
    )  # fmt: skip
    assert rec["dropped"] == ["temperature"], "the log says what the harness tried to set"


def test_a_spent_budget_ends_the_session(servers):
    url, log = servers
    assert _post(url, {"messages": [], "stream": True})[0] == 200
    status, body = _post(url, {"messages": [], "stream": True})
    assert status == 400 and b"budget exhausted" in body
    assert len(_Upstream.seen) == 1, "nothing more reaches the model"
    assert "refused" in json.loads(log.read_text().splitlines()[-1])


def test_only_chat_completions_are_proxied(servers):
    url, _ = servers
    req = request.Request(url.replace("/v1", "/admin"), b"{}", {"Content-Type": "application/json"})
    with pytest.raises(error.HTTPError) as e:
        request.urlopen(req, timeout=10)
    assert e.value.code == 404 and not _Upstream.seen
    with request.urlopen(url + "/models", timeout=10) as r:
        assert json.loads(r.read())["data"][0]["id"] == "model"


def _events(raw: bytes) -> list[dict]:
    return [json.loads(x[5:]) for x in raw.decode().splitlines() if x.startswith("data:")]


def test_an_anthropic_request_reaches_the_server_as_a_chat_completion(servers):
    url, log = servers
    req = request.Request(
        url + "/messages?beta=true",
        json.dumps({"model": "claude-x", "system": "Be brief.", "stream": True, "max_tokens": 4096,
                    "messages": [{"role": "user", "content": "hi"}], "temperature": 1,
                    "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}).encode(),
        {"Content-Type": "application/json", "anthropic-version": "2023-06-01",
         "x-claude-code-request-class": "main"},
    )  # fmt: skip
    with request.urlopen(req, timeout=10) as r:
        events = _events(r.read())
    sent = _Upstream.seen[0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == "Bearer secret"
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
    ]
    assert sent["body"]["model"] == "served-name" and sent["body"]["max_tokens"] == 32768
    assert sent["body"]["chat_template_kwargs"] == {"reasoning_effort": "high"}
    for k in ("thinking", "output_config", "temperature", "system"):
        assert k not in sent["body"], "the harness can't change how the model thinks"
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["message_start", "ping"] and kinds[-2:] == [
        "message_delta",
        "message_stop",
    ]
    text = "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta")
    assert text == "Answer: B"
    assert events[-2]["delta"]["stop_reason"] == "end_turn"
    assert events[-2]["usage"] == {"input_tokens": 11, "output_tokens": 3}
    rec = json.loads(log.read_text().splitlines()[0])
    assert (rec["api"], rec["request_class"], rec["prompt_tokens"], rec["completion_tokens"]) == (
        "messages", "main", 11, 3,
    )  # fmt: skip
    assert rec["finish_reason"] == "stop"


def test_a_spent_budget_answers_an_anthropic_harness_in_its_own_shape(servers):
    url, _ = servers
    _post(url, {"messages": [], "stream": True})
    req = request.Request(
        url + "/messages", b'{"messages": []}', {"Content-Type": "application/json"}
    )
    with pytest.raises(error.HTTPError) as e:
        request.urlopen(req, timeout=10)
    body = json.loads(e.value.read())
    assert e.value.code == 400 and body["type"] == "error"
    assert "budget exhausted" in body["error"]["message"]


def test_a_conversation_is_translated_block_by_block():
    body = {
        "system": [{"type": "text", "text": "You are an agent.", "cache_control": {"type": "x"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "<reminder>"},
                                         {"type": "text", "text": "Fix it."}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Look first.", "signature": "s"},
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.js"}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "false"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "x=1"}]},
                {"type": "tool_result", "tool_use_id": "t2", "content": "exit 1", "is_error": True},
                {"type": "text", "text": "Keep going."},
            ]},
            {"role": "system", "content": "Mind the budget."},
        ],
        "tools": [
            {"name": "Read", "description": "Read a file", "input_schema": {"type": "object"},
             "cache_control": {"type": "ephemeral"}},
            {"type": "web_search_20250305", "name": "web_search"},
        ],
        "tool_choice": {"type": "any"},
        "stop_sequences": ["END"],
    }  # fmt: skip
    out, folded = to_chat(body)
    assert folded == 1
    assert out["messages"] == [
        {"role": "system", "content": "You are an agent.\n\nMind the budget."},
        {"role": "user", "content": "<reminder>\n\nFix it."},
        {"role": "assistant", "content": "Reading.", "reasoning_content": "Look first.",
         "tool_calls": [
             {"id": "t1", "type": "function",
              "function": {"name": "Read", "arguments": '{"file_path": "a.js"}'}},
             {"id": "t2", "type": "function",
              "function": {"name": "Bash", "arguments": '{"command": "false"}'}},
         ]},
        {"role": "tool", "tool_call_id": "t1", "content": "x=1"},
        {"role": "tool", "tool_call_id": "t2", "content": "Error: exit 1"},
        {"role": "user", "content": "Keep going."},
    ]  # fmt: skip
    assert out["tools"] == [
        {"type": "function", "function": {"name": "Read", "description": "Read a file",
                                          "parameters": {"type": "object"}}},
    ], "server tools need Anthropic's servers: not offered"  # fmt: skip
    assert out["tool_choice"] == "required" and out["stop"] == ["END"]


def test_a_stream_is_translated_one_block_at_a_time():
    s = AnthropicStream("claude-x")
    raw = s.start("msg_1")
    for chunk in (
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"reasoning": "Think"}}]},
        {"choices": [{"delta": {"reasoning": "ing."}}]},
        {"choices": [{"delta": {"content": "Let me look."}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "type": "function", "function": {"name": "Read"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"file'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '_path": "a"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 12}},
    ):  # fmt: skip
        raw += s.chunk(chunk)
    events = _events(raw + s.end())
    starts = [e["content_block"]["type"] for e in events if e["type"] == "content_block_start"]
    assert starts == ["thinking", "text", "tool_use"]
    stops = [e["index"] for e in events if e["type"] == "content_block_stop"]
    assert stops == [0, 1, 2], "each block is closed before the next opens"
    deltas = [e["delta"] for e in events if e["type"] == "content_block_delta"]
    assert "".join(d.get("thinking", "") for d in deltas) == "Thinking."
    assert json.loads("".join(d.get("partial_json", "") for d in deltas)) == {"file_path": "a"}
    tool = next(e for e in events if e["type"] == "content_block_start" and e["index"] == 2)
    assert tool["content_block"] == {"type": "tool_use", "id": "c1", "name": "Read", "input": {}}
    end = events[-2]
    assert end["delta"]["stop_reason"] == "tool_use"
    assert end["usage"] == {"input_tokens": 40, "output_tokens": 12}


def test_a_context_overflow_reads_as_anthropic_s_so_the_harness_compacts():
    vllm = (
        "This model's maximum context length is 163840 tokens. However, you requested "
        "170000 tokens (137232 in the messages, 32768 in the completion)."
    )
    err = anthropic_error(400, vllm)
    assert err == {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "prompt is too long: 170000 tokens > 163840 maximum",
        },
    }
    assert anthropic_error(500, "boom")["error"] == {"type": "api_error", "message": "boom"}
