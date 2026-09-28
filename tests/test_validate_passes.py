"""`make validate` runs like `make grade`: every suite but LWC in the networked sandbox, LWC in the
offline container (no network), where the authors' outputs get no in-process exception."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from forcebench import REPO_ROOT
from forcebench import validate as validate_mod
from forcebench.cli import app
from forcebench.graders import GradeEnv, lwc
from forcebench.tasks import all_tasks, load_suites


def _docker_lines(target: str, args: str) -> list[list[str]]:
    if not shutil.which("make"):
        pytest.skip("make not installed")
    out = subprocess.run(
        ["make", "-n", "-C", str(REPO_ROOT), target, f"ARGS={args}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [shlex.split(ln) for ln in out.splitlines() if ln.startswith("docker run")]


def test_make_validate_runs_lwc_only_in_the_offline_container():
    sandbox, offline = _docker_lines("validate", "--suite apex -v")
    assert "--network" not in sandbox and "FORCEBENCH_LWC_OFFLINE=1" not in sandbox
    assert sandbox[-6:] == ["validate", "--suite", "apex", "-v", "--exclude-suite", "lwc"]
    assert offline[offline.index("--network") + 1] == "none"
    assert "FORCEBENCH_LWC_OFFLINE=1" in offline
    assert offline[-7:] == [
        "validate", "--suite", "apex", "-v", "--only-suite", "lwc", "--no-org",
    ]  # fmt: skip
    # the offline container is the same one make grade uses for LWC
    (_, grade_offline) = _docker_lines("grade", "results/runs/x")
    assert offline[: offline.index("-m")] == grade_offline[: grade_offline.index("-m")]


@pytest.fixture
def captured(monkeypatch):
    """Runs `forcebench validate` with validate_tasks replaced: records what it was given."""
    seen: dict = {}

    async def fake(tasks, env, concurrency=8, on_done=None, *, authored=True):
        seen.update(tasks=tasks, authored=authored)
        return []

    monkeypatch.setattr(validate_mod, "validate_tasks", fake)
    monkeypatch.delenv(lwc.OFFLINE_MARKER, raising=False)
    return seen


def test_suite_filters(captured):
    assert CliRunner().invoke(app, ["validate", "--no-org", "--only-suite", "lwc"]).exit_code == 0
    assert captured["tasks"] and {t.suite for t in captured["tasks"]} == {"lwc"}
    CliRunner().invoke(app, ["validate", "--no-org", "--exclude-suite", "lwc"])
    assert captured["tasks"] and "lwc" not in {t.suite for t in captured["tasks"]}
    # --only-suite narrows a selection; it never adds to it
    CliRunner().invoke(app, ["validate", "--no-org", "--suite", "apex", "--only-suite", "lwc"])
    assert captured["tasks"] == []


def test_the_offline_container_validates_without_the_authored_exception(captured, monkeypatch):
    CliRunner().invoke(app, ["validate", "--no-org", "--only-suite", "lwc"])
    assert captured["authored"] is True  # CI's validate --no-org, outside any container
    monkeypatch.setenv(lwc.OFFLINE_MARKER, "1")
    CliRunner().invoke(app, ["validate", "--no-org", "--only-suite", "lwc"])
    assert captured["authored"] is False


async def test_validate_tasks_without_authored_grades_as_model_answers(monkeypatch):
    seen: list[bool] = []

    async def fake_validate_task(task, env):
        seen.append(lwc.grading_authored_answers())
        return validate_mod.TaskValidation(task=task)

    monkeypatch.setattr(validate_mod, "validate_task", fake_validate_task)
    tasks = all_tasks(load_suites(["lwc"]))[:2]
    await validate_mod.validate_tasks(tasks, GradeEnv(), authored=False)
    await validate_mod.validate_tasks(tasks, GradeEnv())
    assert seen == [False, False, True, True]


def test_the_offline_pass_needs_only_the_offline_mounts(tmp_path):
    """`validate --only-suite lwc --no-org` with only src/ and suites/ in /work (no models/,
    orgs/, .env), the marker set: it runs, and on this machine (not the sandbox container) every
    LWC task is skipped by the gate, with a warning that make grade would skip them too."""
    work = tmp_path / "work"
    for name in ("src", "suites"):
        shutil.copytree(REPO_ROOT / name, work / name, ignore=shutil.ignore_patterns("__pycache__"))
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(work / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FORCEBENCH_CACHE_DIR": str(tmp_path / "cache"),
        lwc.OFFLINE_MARKER: "1",
        "NO_COLOR": "1",
        "COLUMNS": "400",
    }
    done = subprocess.run(
        [sys.executable, "-m", "forcebench", "validate", "--only-suite", "lwc", "--no-org"],
        cwd=work, env=env, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    n = len(all_tasks(load_suites(["lwc"])))
    assert f"{n} tasks: 0 ok, 0 failing, {n} skipped" in done.stdout
    assert "make grade would skip these answers too" in done.stdout
    assert "sandbox" in done.stdout
