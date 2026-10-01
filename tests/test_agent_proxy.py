"""The model proxy for agent runs (forcebench.agent.proxy): what it changes in each request, the
budget it enforces, and what it records."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib import error, request

import pytest

from forcebench.agent.proxy import Budget, Proxy, make_handler, merge, rewrite, usage_from_sse


def test_the_configuration_wins_over_the_harness():
    body = {
        "model": "model",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.0,
        "top_p": 0.1,
        "max_completion_tokens": 999_999,
        "max_tokens": 999_999,
        "chat_template_kwargs": {"reasoning_effort": "low", "other": 1},
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
    assert out["chat_template_kwargs"] == {"reasoning_effort": 75, "other": 1, "thinking": True}
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
