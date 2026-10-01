"""The model proxy for agent runs (forcebench.agent.proxy): what it changes in each request, the
budget it enforces, and what it records."""

from forcebench.agent.proxy import Budget, merge, rewrite, usage_from_sse


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
