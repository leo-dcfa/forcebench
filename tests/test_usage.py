"""Tokens, time and list-price cost per task (forcebench.usage): what a task's cost counts."""

from datetime import date

import pytest

from forcebench.prices import Price
from forcebench.usage import block, follow, price_by_run, split_by_run, usage


PRICE = Price(
    input=2,
    cached_input=0.2,
    output=10,
    reasoning="output",
    currency="USD",
    source="https://example.com/pricing",
    reasoning_source="https://example.com/reasoning",
    as_of=date(2026, 10, 10),
)


def _case(task: str, passed: bool, **kw) -> dict:
    return {
        "task_id": task,
        "sample": 0,
        "passed": passed,
        "input_tokens": 1000,
        "output_tokens": 500,
        "reasoning_tokens": 300,
        "latency_s": 10.0,
        "finish_reason": "stop",
        **kw,
    }


def _chains(within: int = 3):
    """Task a passes at once; b fails, fails again, then passes; c fails and is never retried."""
    answers = [("r", _case("a", True)), ("r", _case("b", False)), ("r", _case("c", False))]
    later = {
        ("r", 2): {("b", 0): _case("b", False, input_tokens=3000, latency_s=20.0)},
        ("r", 3): {("b", 0): _case("b", True, input_tokens=5000, latency_s=30.0)},
    }
    return follow(answers, later, within)


def test_cost_per_task_includes_every_attempt_the_score_counts():
    chains = _chains()
    one, three = block(chains, 1, PRICE, True), block(chains, 3, PRICE, True)
    # pass@1 counts the first answers only; c@3 also b's two retries (failed and passed alike).
    assert (one["answers"], three["answers"]) == (3, 5)
    first = 1000 * 2 + 500 * 10  # one answer at list price, in millionths of a dollar
    assert one["cost"]["total"] == pytest.approx(3 * first / 1e6)
    retries = (3000 + 5000) * 2 + 2 * 500 * 10
    assert three["cost"]["total"] == pytest.approx((3 * first + retries) / 1e6)
    assert three["cost"]["per_task"] == pytest.approx(three["cost"]["total"] / 3, rel=1e-3)
    assert three["tokens"]["input"] == pytest.approx((3 * 1000 + 3000 + 5000) / 3, rel=1e-3)


def test_cost_per_solved_task_is_total_cost_over_tasks_solved():
    chains = _chains()
    one, three = block(chains, 1, PRICE, True), block(chains, 3, PRICE, True)
    assert (one["solved"], three["solved"]) == (1, 2)
    assert one["cost"]["per_solved"] == pytest.approx(one["cost"]["total"] / 1, rel=1e-3)
    assert three["cost"]["per_solved"] == pytest.approx(three["cost"]["total"] / 2, rel=1e-3)


def test_cost_per_solved_task_is_hidden_when_nothing_was_solved():
    chains = follow([("r", _case("c", False))], {}, 1)
    out = block(chains, 1, PRICE, True)
    assert out["cost"]["per_task"] is not None
    assert out["cost"]["per_solved"] is None


def test_time_per_task_adds_up_its_attempts():
    three = block(_chains(), 3, PRICE, True)
    # a: 10 s, b: 10 + 20 + 30 s, c: 10 s.
    assert three["time_s"]["median"] == 10.0
    assert three["time_s"]["mean"] == pytest.approx(80 / 3, rel=1e-3)
    assert three["time_s"]["p90"] == pytest.approx(10 + (60 - 10) * 0.8)


def test_an_answer_without_token_counts_leaves_no_tokens_and_no_cost():
    unreported = _case("c", False, input_tokens=0, output_tokens=0, reasoning_tokens=0)
    chains = follow([("r", _case("a", True)), ("r", unreported)], {}, 1)
    out = block(chains, 1, PRICE, True)
    assert (out["tokens"], out["cost"], out["no_cost"], out["unreported"]) == (
        None,
        None,
        "unreported",
        1,
    )
    assert out["time_s"]["median"] == 10.0, "its time is still known"


def test_without_a_list_price_there_are_tokens_but_no_cost():
    out = block(_chains(), 1, None, True)
    assert out["tokens"]["output"] == 500
    assert (out["cost"], out["no_cost"]) == (None, "no_price")


def test_a_prompt_longer_than_its_price_holds_for_has_no_cost():
    tiered = PRICE.model_copy(update={"prompt_tokens_max": 4000})
    assert block(_chains(), 1, tiered, True)["cost"] is not None
    out = block(_chains(), 3, tiered, True)  # b's third attempt sends 5,000 input tokens
    assert (out["cost"], out["no_cost"]) == (None, "price_tier")


def test_reasoning_is_split_from_the_answer_only_where_it_was_reported():
    budget = _case("c", False, output_tokens=32768, reasoning_tokens=0, finish_reason="error: x")
    chains = follow([("r", _case("a", True)), ("r", budget)], {}, 1)
    out = block(chains, 1, PRICE, True)["tokens"]
    # a: 300 reasoning, 200 answer. c ran out of budget, recorded without its reasoning count.
    assert (out["reasoning"], out["answer"], out["unsplit"]) == (150, 100, 32768 / 2)
    assert block(chains, 1, PRICE, False)["tokens"]["unsplit"] == (500 + 32768) / 2
    parts = block(chains, 1, PRICE, True)["cost"]["parts"]
    assert parts["cached_input"] is None, "no answer recorded its cached input"


def test_cached_input_is_billed_at_the_full_input_price():
    chains = follow([("r", _case("a", True, cached_input_tokens=600))], {}, 1)
    out = block(chains, 1, PRICE, True)
    assert out["tokens"]["cached_input"] == 600
    assert out["cost"]["parts"]["cached_input"] == pytest.approx(600 * 2 / 1e6)
    assert out["cost"]["parts"]["input"] == pytest.approx(400 * 2 / 1e6)
    assert out["cost"]["total"] == pytest.approx((1000 * 2 + 500 * 10) / 1e6)


def test_usage_by_attempts_counted_and_suite():
    out = usage(_chains(), {"a": "s1", "b": "s1", "c": "s2"}, [1, 2, 3], PRICE, True)
    assert sorted(out) == ["1", "2", "3"]
    assert sorted(out["1"]["suites"]) == sorted(out["3"]["suites"]) == ["s1", "s2"]
    assert "suites" not in out["2"], "c@2 is shown overall only"
    assert out["3"]["suites"]["s1"]["answers"] == 4
    assert out["3"]["overall"]["recorded"] == {"cached_input": 0, "served_by": 0}


def test_an_answer_the_endpoint_never_returned_counts_as_failed_without_tokens_or_time():
    # gemini-3.8-flash@medium's shape: one retry recorded as failed (runner.record_failed: no
    # tokens, the last try's latency). It is a failure in the score, and leaves the cost intact.
    never = _case("b", False, input_tokens=0, output_tokens=0, reasoning_tokens=0, latency_s=69.0)
    never["finish_reason"] = "failed: the endpoint cut every response"
    answers = [("r", _case("a", True)), ("r", _case("b", False))]
    later = {("r", 2): {("b", 0): never}, ("r", 3): {}}
    two = block(follow(answers, later, 3), 2, PRICE, True)
    assert (two["answers"], two["failed"], two["unreported"], two["no_cost"]) == (3, 1, 0, None)
    one_answer = 1000 * 2 + 500 * 10
    assert two["cost"]["total"] == pytest.approx(2 * one_answer / 1e6), "no tokens for it"
    assert two["time_s"]["mean"] == 10.0, "and no time: the failed try's latency is left out"
    assert two["solved"] == 1


def test_a_task_with_several_samples_counts_once_as_the_mean_of_its_samples():
    # Task a answered twice (one pass, one fail, the second with twice the tokens and time); b once.
    a0, a1 = _case("a", True), _case("a", False, sample=1, input_tokens=2000, latency_s=20.0)
    chains = follow([("r", a0), ("r", a1), ("r", _case("b", True))], {}, 1)
    out = block(chains, 1, PRICE, True)
    assert (out["tasks"], out["answers"], out["solved"]) == (2, 3, 1.5)
    # a: mean input 1,500, b: 1,000; per task (1,500 + 1,000) / 2.
    assert out["tokens"]["input"] == 1250
    assert out["time_s"]["mean"] == pytest.approx((15 + 10) / 2)


def test_older_answers_stay_unsplit_when_newer_ones_join_the_entry():
    # Attempt 1 recorded before 2026-10-10 (Anthropic's thinking not counted: reasoning 0, no
    # cached_input_tokens field); attempt 2 recorded after, with its thinking counted.
    old = _case("a", False, reasoning_tokens=0)
    new = _case("a", True, reasoning_tokens=300, cached_input_tokens=0)
    chains = follow([("r", old)], {("r", 2): {("a", 0): new}}, 2)
    before = block(chains, 1, PRICE, split_by_run([[old]]))
    after_units = split_by_run([[old], [new]])
    assert block(chains, 1, PRICE, after_units)["tokens"] == before["tokens"], "pass@1 is unchanged"
    assert before["tokens"]["unsplit"] == 500
    two = block(chains, 2, PRICE, after_units)["tokens"]
    assert (two["reasoning"], two["answer"], two["unsplit"]) == (300, 200, 500)


def test_a_server_that_reports_reasoning_splits_its_older_answers_too():
    # Older OpenAI-compatible answers kept their reasoning count: they split as before.
    a, b = _case("a", True), _case("b", False, reasoning_tokens=0)
    split = split_by_run([[a, b]])
    assert split(a)
    assert split(b)


def test_each_answer_is_billed_at_its_own_runs_price():
    # A configuration whose newer run was pinned to a dearer route: each answer at its run's price.
    dear = PRICE.model_copy(update={"input": 4, "output": 20})
    old, new = _case("a", True), _case("b", True)
    prices = price_by_run([(PRICE, [old]), (dear, [new])])
    out = block(follow([("r1", old), ("r2", new)], {}, 1), 1, prices, True)
    assert out["cost"]["total"] == pytest.approx((1000 * 2 + 500 * 10 + 1000 * 4 + 500 * 20) / 1e6)
    assert price_by_run([(PRICE, [old]), (PRICE, [new])]) is PRICE, "one price when all agree"
    unpriced = price_by_run([(PRICE, [old]), (None, [new])])
    assert (
        block(follow([("r1", old), ("r2", new)], {}, 1), 1, unpriced, True)["no_cost"] == "no_price"
    )
