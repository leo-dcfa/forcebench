"""Defects an adversarial review found in the second round of fixes: grading several runs in
one command, answers that could still cause an OSError, invalidate racing a resume, and
symbolic links inside a run directory.
"""

import asyncio
import fcntl
import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forcebench import SUITES_DIR, runner
from forcebench.answer_files import AnswerPathError
from forcebench.answers import extract
from forcebench.cli import app
from forcebench.fsutil import exclusive_lock
from forcebench.graders import _REGISTRY, Grade, GradeEnv, grade, lwc, scratch_def
from forcebench.llm import Generation
from forcebench.runner import RunDirError, check_run_dir
from forcebench.tasks import load_task


RUN_A = "20260928T000000Z_m@low"
RUN_B = "20260928T000001Z_m@low"


def _finished_run(runs: Path, name: str, task_id: str, samples: int = 1) -> Path:
    run = runs / name
    (run / "raw").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"task_ids": [task_id], "samples": samples}))
    (run / "cases.jsonl").write_text("")
    gen = Generation(text="Answer: x", finish_reason="stop").model_dump()
    (run / "raw" / "generations.jsonl").write_text(
        "".join(
            json.dumps({"key": f"{task_id}#{s}", "generation": gen}) + "\n" for s in range(samples)
        )
    )
    return run


@pytest.fixture
def contended_task(make_task, monkeypatch):
    """A task whose grader waits on the per-org semaphore, as org graders do (more cases than
    the semaphore has slots, so some wait).
    """

    async def org_like(task, answer, env):
        async with env.lock("fb-grader-1"):
            await asyncio.sleep(0.005)
        return Grade(passed=True, score=1.0)

    monkeypatch.setitem(_REGISTRY, "review_org_like", org_like)
    return make_task({"format": "text"}, {"type": "review_org_like"})


# --------------------------------------------------------------------------- 1. one event loop


def test_grade_all_grades_every_run_like_the_first(tmp_path, contended_task, monkeypatch):
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr("forcebench.cli.select_tasks", lambda *a, **k: ([], [contended_task]))
    for name in (RUN_A, RUN_B):
        _finished_run(tmp_path, name, contended_task.id, samples=12)
    result = CliRunner().invoke(app, ["grade", "--all", "--no-org"])
    assert result.exit_code == 0, result.output
    for name in (RUN_A, RUN_B):
        cases = [json.loads(x) for x in (tmp_path / name / "cases.jsonl").read_text().splitlines()]
        assert len(cases) == 12
        assert all(c["passed"] for c in cases), [c["checks"] for c in cases if not c["passed"]]


# --------------------------------------------------------------------------- 2. scratch defs


def test_a_settings_name_no_file_can_have_is_refused_before_writing(tmp_path):
    with pytest.raises(AnswerPathError, match="longer than 255 bytes"):
        scratch_def.build_shape(
            tmp_path / "shape", {"a" * 300 + "Settings": {"x": True}}, {}, "67.0"
        )
    assert not (tmp_path / "shape").exists()


async def test_such_a_scratch_def_answer_fails_its_format_not_infra(monkeypatch):
    task = load_task(next(SUITES_DIR.glob("*/tasks/scratch-def-person-accounts.yaml")))
    doc = extract(task, task.reference_output).json_value
    doc.setdefault("settings", {})["a" * 300 + "Settings"] = {"enableSomething": True}

    async def api_version(alias):
        return "67.0"

    async def no_sf(*a, **k):
        pytest.fail("sf ran")

    monkeypatch.setattr(scratch_def, "_org_api_version", api_version)
    monkeypatch.setattr(scratch_def, "sf_json", no_sf)
    profile = task.grader.params.get("profile", scratch_def.DEFAULT_PROFILE)
    env = GradeEnv(orgs={profile: ["fb-scratchdef-1"]})
    g = await grade(task, extract(task, f"```json\n{json.dumps(doc)}\n```"), env)
    assert g.infra_error is None
    assert not g.passed
    assert [c.name for c in g.checks] == ["format"]


# --------------------------------------------------------------------------- 3. LWC output


async def test_lwc_code_that_replaces_jests_result_file_fails_the_answer(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()

    async def component_made_a_folder(cmd, cwd, timeout, env):
        (run / ".fb-jest.json").mkdir()  # what model code may do inside its run directory
        return 1, "", ""

    monkeypatch.setattr(lwc, "_run", component_made_a_folder)
    res = await lwc._jest("node", tmp_path / "ws", run, ["x.test.js"], 10, False)
    assert isinstance(res, list)
    [check] = res
    assert check.name == "tests pass"
    assert not check.passed
    assert "replaced" in check.detail


# --------------------------------------------------------------------------- 4. invalidate


def _locked_elsewhere(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def test_invalidate_chooses_its_answers_under_the_run_lock(tmp_path):
    run = _finished_run(tmp_path, RUN_A, "t", samples=2)
    seen: dict = {}

    def select(records):
        seen["locked"] = _locked_elsewhere(run / runner.LOCK_FILE)
        seen["keys"] = [r["key"] for r in records]
        return ["t#1"]

    assert runner.invalidate(run, select, "test") == 1
    assert seen == {"locked": True, "keys": ["t#0", "t#1"]}


def test_the_invalidate_command_selects_under_the_lock(tmp_path, monkeypatch):
    run = _finished_run(tmp_path, RUN_A, "t")
    got: dict = {}

    def fake(run_dir, keys, reason):
        got["callable"] = callable(keys)
        return 0

    monkeypatch.setattr(runner, "invalidate", fake)
    result = CliRunner().invoke(app, ["invalidate", str(run), "--reason", "x", "--task", "t"])
    assert result.exit_code == 0, result.output
    assert got == {"callable": True}


# --------------------------------------------------------------------------- 5. symlinks


@pytest.mark.parametrize("entry", ["raw", "artifacts", "run.json", "cases.jsonl", ".lock"])
def test_symlinks_inside_a_run_are_refused(tmp_path, entry):
    run = _finished_run(tmp_path, RUN_A, "t")
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = run / entry
    if link.is_dir():
        shutil.rmtree(link)
    elif link.exists():
        link.unlink()
    link.symlink_to(target)
    with pytest.raises(RunDirError, match="symbolic links in a run"):
        check_run_dir(run)


def test_grading_never_rewrites_artifacts_through_a_symlink(tmp_path, make_task):
    task = make_task({"format": "text"})
    run = _finished_run(tmp_path, RUN_A, task.id)
    victim = tmp_path / "victim"
    (victim / "0").mkdir(parents=True)
    (victim / "0" / "precious.txt").write_text("keep me")
    (run / "artifacts").mkdir()
    (run / "artifacts" / task.id).symlink_to(victim)
    with pytest.raises(RunDirError, match="leads out of the run directory"):
        asyncio.run(runner.grade(run, [task], GradeEnv(work_dir=tmp_path / "w"), progress=False))
    assert (victim / "0" / "precious.txt").read_text() == "keep me"


def test_the_lock_is_never_taken_through_a_symlink(tmp_path):
    (tmp_path / ".lock").symlink_to(tmp_path / "somewhere-else")
    with pytest.raises(OSError), exclusive_lock(tmp_path / ".lock"):
        pass
    assert not (tmp_path / "somewhere-else").exists()
