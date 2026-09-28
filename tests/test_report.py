"""The leaderboard (results/leaderboard.json, the website's data contract): which answers count,
when a suite and an entry are complete, what a partial entry's score covers."""

import json

import pytest

from forcebench import BENCHMARK_VERSION, GENERATION_PROTOCOL, RESULTS_DIR, run_protocol
from forcebench.report import build_entry, build_leaderboard, render_markdown
from forcebench.stats import mean
from forcebench.tasks import Suite, load_suites

# Schema v1 as the website reads it: fields may be added, never removed or renamed.
V1_TOP = {"schema_version", "benchmark", "version", "generated_at", "suites", "tasks", "entries"}
V1_ENTRY = {
    "config_id", "subset", "model", "model_family", "base_model", "quant", "engine", "effort",
    "effort_tier", "open_weights", "local", "overall", "suites", "per_task", "tokens", "outcomes",
    "no_answer_rate", "latency_s_mean", "samples", "pending", "date", "complete", "runs",
}  # fmt: skip
# Fields added since, removed before comparing with older builds.
ADDED = ("progress", "legacy")


@pytest.fixture
def suites(make_task):
    """Suite a with tasks a-0, a-1 and suite b with tasks b-0, b-1."""

    def suite(sid: str, n: int = 2) -> Suite:
        tasks = [make_task({"format": "text"}, id=f"{sid}-{i}", suite=sid) for i in range(n)]
        return Suite(id=sid, name=sid, description=sid, grading="deterministic", tasks=tasks)

    return [suite("a"), suite("b")]


def _case(task_id: str, passed: bool = True, sample: int = 0, **kw) -> dict:
    return {
        "task_id": task_id,
        "sample": sample,
        "suite": task_id.split("-")[0],
        "task_version": 1,
        "passed": passed,
        "skipped": None,
        "infra_error": None,
        "answer_error": None,
        "output_tokens": 100,
        "reasoning_tokens": 0,
        "finish_reason": "stop",
        "latency_s": 1.0,
        **kw,
    }


def _meta(run_id: str = "r2", **kw) -> dict:
    return {
        "run_id": run_id,
        "benchmark_version": BENCHMARK_VERSION,
        "config_id": "m@low",
        "subset": "full",
        "effort": "low",
        "effort_tier": "low",
        "model": {
            "display": "M",
            "family": "F",
            "base_model": "B",
            "quant": "Q",
            "engine": "E",
            "open_weights": True,
            "local": True,
        },
        "started_at": "2026-09-28T00:00:00+00:00",
        "protocol": 2,
        **kw,
    }


def _all(passed: bool = True) -> list[dict]:
    return [_case(t, passed) for t in ("a-0", "a-1", "b-0", "b-1")]


# --------------------------------------------------------------------------- completeness


def test_complete_entry(suites):
    cases = [_case("a-0"), _case("a-1", False), _case("b-0"), _case("b-1")]
    e = build_entry([(_meta(), cases)], suites)
    assert e["complete"] and e["pending"] == 0
    assert e["overall"]["score"] == mean([0.5, 1.0])
    assert all("complete" not in s for s in e["suites"].values()), "unchanged for complete suites"
    assert e["progress"] == {
        "tasks_graded": 4, "tasks_total": 4, "suites_complete": 2, "suites_total": 2,
    }  # fmt: skip
    assert (e["legacy"], e["outcomes"]["retried"]) == (0, None)


def test_suite_with_an_ungraded_task_is_incomplete(suites):
    cases = [_case("a-0"), _case("a-1"), _case("b-0", False)]
    e = build_entry([(_meta(), cases)], suites)
    assert not e["complete"]
    assert e["suites"]["b"] == {
        "score": 0.0,
        "ci_low": 0.0,
        "ci_high": 0.0,
        "n": 1,
        "complete": False,
    }
    assert "complete" not in e["suites"]["a"]
    assert e["progress"] == {
        "tasks_graded": 3, "tasks_total": 4, "suites_complete": 1, "suites_total": 2,
    }  # fmt: skip


def test_partial_entry_scores_only_its_complete_suites(suites):
    # Suite b has one graded task (a failure) and one missing: averaging "what was graded so
    # far" would mix a finished suite with a fraction of another.
    cases = [_case("a-0"), _case("a-1", False), _case("b-0", False)]
    e = build_entry([(_meta(), cases)], suites)
    assert e["overall"]["score"] == 0.5, "suite a only"
    assert e["overall"]["ci_low"] <= 0.5 <= e["overall"]["ci_high"]
    assert e["suites"]["b"]["score"] == 0.0, "an incomplete suite keeps its score, marked"


def test_a_pending_answer_makes_its_suite_incomplete(suites):
    # b-0 has a graded sample and a pending one. Pending answers are more often failures, so
    # the suite is not complete until it is graded.
    cases = [*_all(), _case("b-0", sample=1, infra_error="org unavailable")]
    e = build_entry([(_meta(), cases)], suites)
    assert (e["complete"], e["pending"]) == (False, 1)
    assert e["progress"]["suites_complete"] == 1
    assert e["suites"]["b"]["complete"] is False
    assert e["overall"]["score"] == 1.0


def test_entry_without_a_complete_suite_is_not_scored(suites):
    e = build_entry([(_meta(), [_case("a-0"), _case("b-0")])], suites)
    assert e["overall"] == {"score": None, "ci_low": None, "ci_high": None}


# --------------------------------------------------------------------------- versions


def test_answers_to_older_task_versions_are_left_out(make_task, suites):
    suites[0].tasks[1] = make_task({"format": "text"}, id="a-1", suite="a", version=2)
    stale = _case("a-1", task_version=1, stale=True, skipped="stale: ...")
    e = build_entry([(_meta(), [_case("a-0"), stale, _case("b-0"), _case("b-1")])], suites)
    assert "a-1" not in e["per_task"]
    assert (e["complete"], e["pending"], e["progress"]["suites_complete"]) == (False, 0, 1)
    e = build_entry([(_meta(), [*_all()[:1], _case("a-1", task_version=2), *_all()[2:]])], suites)
    assert e["complete"]


# --------------------------------------------------------------------------- protocols


def test_legacy_answers_are_replaced_or_pending_never_merged(suites):
    legacy_run = (_meta("r1", protocol=1), _all(passed=True))
    fresh_run = (_meta("r2"), [_case("a-0", False), _case("a-1", False)])
    e = build_entry([legacy_run, fresh_run], suites)
    assert e["per_task"] == {"a-0": 0.0, "a-1": 0.0}, "the protocol-1 passes are not merged in"
    assert (e["legacy"], e["pending"], e["samples"]) == (2, 2, 2)
    assert not e["complete"]
    assert "b" not in e["suites"]
    assert e["overall"]["score"] == 0.0


def test_legacy_only_configuration_is_listed_apart(tmp_path, suites):
    runs = tmp_path / "runs"
    _write_run(runs, _meta("r1", protocol=1), _all())
    _write_run(runs, _meta("r2", config_id="n@low"), _all(passed=False))
    data = build_leaderboard(suites, runs)
    assert [e["config_id"] for e in data["entries"]] == ["n@low"]
    (u,) = data["unscored"]
    assert (u["config_id"], u["legacy"], u["pending"]) == ("m@low", 4, 4)
    assert u["progress"]["tasks_graded"] == 0


def test_protocol_is_derived_for_runs_that_did_not_record_it(tmp_path, suites):
    old = _meta("r1", request={"max_tokens": 1})  # not streamed: before protocol 2
    del old["protocol"]
    _write_run(tmp_path, old, _all())
    data = build_leaderboard(suites, tmp_path)
    assert data["entries"] == [] and data["unscored"][0]["legacy"] == 4


def _write_run(runs_dir, meta, cases) -> None:
    run = runs_dir / meta["run_id"]
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps(meta))
    (run / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases))


# --------------------------------------------------------------------------- outcomes


def test_retried_answers_are_counted_once_every_case_records_attempts(suites):
    cases = [_case("a-0", attempts=1), _case("a-1", attempts=2), *_all()[2:]]
    assert build_entry([(_meta(), cases)], suites)["outcomes"]["retried"] is None
    cases = [{**c, "attempts": c.get("attempts", 1)} for c in cases]
    assert build_entry([(_meta(), cases)], suites)["outcomes"]["retried"] == 1


# --------------------------------------------------------------------------- output


def test_markdown_labels_progress_by_complete_suites(tmp_path, suites):
    _write_run(tmp_path, _meta("r1"), [_case("a-0"), _case("a-1"), _case("b-0")])
    _write_run(tmp_path, _meta("r2", config_id="n@low"), [_case("a-0")])
    md = render_markdown(build_leaderboard(suites, tmp_path))
    row = next(line for line in md.splitlines() if "partial" in line)
    assert "partial (1/2 suites complete)" in row
    assert "| 100 | 100\\* |" in row, "suite b is in progress"
    assert "complete suites" in md
    assert "Not scored yet" in md and "0/4 tasks graded" not in md and "1/4 tasks graded" in md


@pytest.fixture(scope="module")
def rebuilt():
    """The leaderboard rebuilt from the committed results."""
    return build_leaderboard(load_suites())


def test_rebuilt_leaderboard_keeps_schema_v1(rebuilt):
    data = rebuilt
    assert set(data) >= V1_TOP
    for e in data["entries"]:
        assert set(e) >= V1_ENTRY
        assert all(isinstance(e["overall"][k], float) for k in ("score", "ci_low", "ci_high"))
        assert e["tokens"]["output_mean"] is not None and e["date"]


def _strip(e: dict) -> dict:
    e = json.loads(json.dumps(e))
    for k in ADDED:
        e.pop(k, None)
    e["outcomes"].pop("retried", None)
    for s in e["suites"].values():
        s.pop("complete", None)
    return e


def test_current_results_rebuild_identically_for_complete_entries(rebuilt):
    """Complete entries whose answers all come from the current protocol are unchanged by a
    rebuild, apart from added fields. A failure here usually means results/leaderboard.json is
    out of date (a task changed, runs were added): run `forcebench report`."""
    committed = json.loads((RESULTS_DIR / "leaderboard.json").read_text())
    data = rebuilt
    if data["tasks"] != committed["tasks"]:
        pytest.skip("results/leaderboard.json predates the current task set")
    by_config = {(e["config_id"], e["subset"]): e for e in data["entries"]}
    checked = 0
    for old in committed["entries"]:
        if not old["complete"] or any(_protocol(r) != GENERATION_PROTOCOL for r in old["runs"]):
            continue  # entries with protocol-1 runs are rebuilt without those answers, by design
        new = by_config.get((old["config_id"], old["subset"]))
        assert new is not None, old["config_id"]
        assert _strip(new) == _strip(old), old["config_id"]
        checked += 1
    assert checked >= 1


def _protocol(run_id: str) -> int | None:
    path = RESULTS_DIR / "runs" / run_id / "run.json"  # None: the run has been moved away since
    return run_protocol(json.loads(path.read_text())) if path.exists() else None
