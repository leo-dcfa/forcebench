"""Provisional scores: every entry of a set on the same, common complete suites."""

from forcebench.provisional import MIN_SUITES, provisional


def _data(entries, n_suites=MIN_SUITES + 2):
    suites = [{"id": f"s{i}"} for i in range(n_suites)]
    tasks = [{"id": f"s{i}-t{j}", "suite": f"s{i}"} for i in range(n_suites) for j in range(3)]
    return {"suites": suites, "tasks": tasks, "entries": entries}


def _entry(config_id, score, incomplete=(), subset="full", n_suites=MIN_SUITES + 2):
    suites = {f"s{i}": {"score": score, "n": 3} for i in range(n_suites)}
    for s in incomplete:
        suites[s]["complete"] = False
    per_task = {f"s{i}-t{j}": score for i in range(n_suites) for j in range(3)}
    return {
        "config_id": config_id,
        "subset": subset,
        "complete": not incomplete,
        "suites": suites,
        "per_task": per_task,
    }


def test_every_entry_is_scored_on_the_common_complete_suites():
    data = _data([_entry("a", 0.5, incomplete=["s0"]), _entry("b", 0.8, incomplete=["s1"])])
    prov = provisional(data)
    assert prov is not None
    assert "lite" not in prov
    full = prov["full"]
    assert full["suites"] == [f"s{i}" for i in range(2, MIN_SUITES + 2)]
    assert full["n_tasks"] == 3 * MIN_SUITES
    assert full["entries"]["b"]["rank"] == 1
    assert full["entries"]["a"]["rank"] == 2
    assert full["entries"]["a"]["score"] == 0.5


def test_nothing_provisional_when_every_entry_is_complete_or_too_few_suites_are_common():
    assert provisional(_data([_entry("a", 0.5), _entry("b", 0.6)])) is None
    many = [f"s{i}" for i in range(3)]  # leaves fewer than MIN_SUITES common suites
    assert provisional(_data([_entry("a", 0.5, incomplete=many), _entry("b", 0.6)])) is None


def test_ties_share_a_rank():
    data = _data([_entry("a", 0.5, incomplete=["s0"]), _entry("b", 0.5), _entry("c", 0.4)])
    prov = provisional(data)
    assert prov is not None
    ranks = {k: v["rank"] for k, v in prov["full"]["entries"].items()}
    assert ranks == {"a": 1, "b": 1, "c": 3}
