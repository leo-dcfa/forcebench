"""Grading throughput: each grading pass records its timing, and `forcebench throughput` sums it."""

import asyncio
import json

from typer.testing import CliRunner

from forcebench import runner
from forcebench.cli import app
from forcebench.graders import GradeEnv
from forcebench.llm import Generation
from forcebench.throughput import summarise


RUN = "20261001T000000Z_m@low"


def _stored_run(tmp_path, tasks):
    run_dir = tmp_path / RUN
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"task_ids": [t.id for t in tasks], "samples": 1}))
    lines = [
        json.dumps(
            {
                "key": f"{t.id}#0",
                "generation": Generation(text="Answer: x", finish_reason="stop").model_dump(),
            }
        )
        for t in tasks
    ]
    (run_dir / "raw" / "generations.jsonl").write_text("\n".join(lines) + "\n")
    return run_dir


def test_each_grading_pass_records_its_timing_locally(tmp_path, make_task):
    tasks = [make_task({"format": "text"}, id=f"docs-t{i}") for i in range(3)]
    run_dir = _stored_run(tmp_path, tasks)
    asyncio.run(runner.grade(run_dir, tasks, GradeEnv(work_dir=tmp_path / "w"), progress=False))
    [timing] = list((run_dir / "artifacts" / "grading").glob("*.json"))
    data = json.loads(timing.read_text())
    assert set(data["cases"]) == {f"docs-t{i}#0" for i in range(3)}
    case = data["cases"]["docs-t0#0"]
    assert (case["grader"], case["sf_calls"], case["deploys"], case["graded"]) == (
        "short_answer",
        0,
        0,
        True,
    )
    assert "x" not in json.dumps(data["cases"]), "timing only: no answer, no reasoning"
    found = summarise(run_dir)
    assert found["passes"][0]["graded"] == 3
    assert found["graders"]["short_answer"]["graded"] == 3


def test_the_command_reports_or_says_there_is_nothing(tmp_path, make_task):
    empty = tmp_path / "20261001T000001Z_m@low"
    empty.mkdir()
    assert CliRunner().invoke(app, ["throughput", str(empty)]).exit_code == 1
    tasks = [make_task({"format": "text"}, id="docs-t0")]
    run_dir = _stored_run(tmp_path, tasks)
    asyncio.run(runner.grade(run_dir, tasks, GradeEnv(work_dir=tmp_path / "w"), progress=False))
    result = CliRunner().invoke(app, ["throughput", str(run_dir), "--json"])
    assert (
        result.exit_code == 0
        and json.loads(result.output)["graders"]["short_answer"]["graded"] == 1
    )


def _graded(tmp_path, name, verdicts):
    run = tmp_path / name
    run.mkdir()
    lines = [
        json.dumps(
            {"task_id": t, "sample": 0, "passed": v == "pass", "skipped": v == "skip" or None}
        )
        for t, v in verdicts.items()
    ]
    (run / "cases.jsonl").write_text("\n".join(lines) + "\n")
    return run


def test_compare_grades_lists_every_verdict_that_differs(tmp_path):
    pooled = _graded(tmp_path, "pooled", {"apex-a": "pass", "apex-b": "fail", "soql-c": "pass"})
    fresh = _graded(tmp_path, "fresh", {"apex-a": "pass", "apex-b": "pass", "soql-c": "skip"})
    same = _graded(tmp_path, "same", {"apex-a": "pass", "apex-b": "fail", "soql-c": "pass"})
    differ = CliRunner().invoke(app, ["compare-grades", str(pooled), str(fresh)])
    assert differ.exit_code == 1
    assert (
        "apex-b#0: fail -> pass" in differ.output and "soql-c#0: pass -> skipped" in differ.output
    )
    assert "2 verdicts differ" in differ.output
    assert CliRunner().invoke(app, ["compare-grades", str(pooled), str(same)]).exit_code == 0


def test_grading_can_be_pinned_only_to_registered_orgs(tmp_path, make_task, monkeypatch):
    tasks = [make_task({"format": "text"}, id="docs-t0")]
    run_dir = _stored_run(tmp_path, tasks)
    monkeypatch.setattr("forcebench.cli.select_tasks", lambda *a, **k: ([], tasks))
    result = CliRunner().invoke(
        app, ["grade", str(run_dir), "--no-org", "--org", "base=fb-not-registered"]
    )
    assert result.exit_code == 2 and "not a registered grader org" in " ".join(
        result.output.split()
    )
