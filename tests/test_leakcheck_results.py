"""Results hold only public runs of public tasks, and only the files results/ is meant to hold."""

import json

import pytest

from forcebench import CANARY
from forcebench.leakcheck import check
from forcebench.leakcheck.results import only_public_results, public_task_ids

UNKNOWN = "apex-" + "not-a-public-task"  # a task id no public task has ever had
RUN = "results/runs/20260930T000000Z_m@low/run.json"
CASES = "results/runs/20260930T000000Z_m@low/cases.jsonl"


def _public_id() -> str:
    return sorted(public_task_ids())[0]


def _check(path: str, text: str):
    return check([(path, text)], [only_public_results])


def test_public_runs_and_answers_pass():
    run = {"visibility": "public", "canary": CANARY, "task_ids": [_public_id()]}
    assert not _check(RUN, json.dumps(run))
    assert not _check(RUN, json.dumps({"task_ids": [_public_id()]}))  # from before visibility
    assert not _check(CASES, json.dumps({"task_id": _public_id()}) + "\n")


@pytest.mark.parametrize(
    ("run", "detail"),
    [
        ({"visibility": "private"}, "not a public run"),
        ({"canary": "forcebench private canary GUID 1"}, "another canary"),
        ({"task_ids": [UNKNOWN]}, "names 1 tasks that are not public"),
    ],
)
def test_a_run_that_is_not_public_is_found(run, detail):
    [finding] = _check(RUN, json.dumps(run))
    assert detail in finding.detail
    assert UNKNOWN not in str(finding), "a task id that is not public is never named"


def test_answers_that_are_not_public_are_found_by_line():
    lines = [
        {"task_id": _public_id()},
        {"task_id": _public_id(), "visibility": "private"},
        {"task_id": UNKNOWN},
    ]
    findings = _check(CASES, "".join(json.dumps(x) + "\n" for x in lines))
    assert [(f.line, f.detail) for f in findings] == [
        (2, "not a public answer"),
        (3, "names 1 tasks that are not public"),
    ]


def test_the_leaderboard_must_say_it_is_public_and_name_public_tasks():
    good = {"visibility": "public", "tasks": [{"id": _public_id()}], "entries": []}
    assert not _check("results/leaderboard.json", json.dumps(good))
    older = {**good, "visibility": None}
    assert "visibility" in _check("results/leaderboard.json", json.dumps(older))[0].detail
    leaky = {**good, "entries": [{"per_task": {UNKNOWN: 1.0}}]}
    [finding] = _check("results/leaderboard.json", json.dumps(leaky))
    assert "not public" in finding.detail


@pytest.mark.parametrize(
    "path",
    [
        "results/runs/20260930T000000Z_m@low/raw/generations.jsonl",  # full replies, reasoning
        "results/runs/20260930T000000Z_m@low/artifacts/t/0/grade.json",
        "results/apex-held-out.yaml",
        "results/private/leaderboard.json",
    ],
)
def test_results_hold_only_their_own_files(path):
    [finding] = _check(path, "{}")
    assert "holds only" in finding.detail


def test_malformed_json_is_a_finding_not_a_crash():
    assert _check(RUN, "{not json")[0].detail == "not valid JSON"


def test_the_agent_tracks_files_are_results_too():
    """The agent track keeps its runs and leaderboard under results/agent/ (docs/agent-track.md),
    checked like the single-turn track's."""
    run = json.dumps({"task_ids": [_public_id()]})
    assert not _check("results/agent/runs/20260930T000000Z_m@low/run.json", run)
    good = {"visibility": "public", "tasks": [{"id": _public_id()}], "entries": []}
    assert not _check("results/agent/leaderboard.json", json.dumps(good))
    assert not _check("results/agent/LEADERBOARD.md", "# results\n")
    leaky = json.dumps({"visibility": "private"})
    assert _check("results/agent/runs/20260930T000000Z_m@low/run.json", leaky)
    assert _check("results/agent/leaderboard.json", json.dumps({**good, "visibility": None}))
    assert _check("results/agent/notes.txt", "x")
