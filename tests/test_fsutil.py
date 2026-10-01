"""Crash-safe writes of run files and the per-run lock (forcebench.fsutil, runner.run_lock)."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import textwrap

import pytest

from forcebench import fsutil
from forcebench.fsutil import LockBusyError, atomic_write_text, exclusive_lock


def _leftovers(directory) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


# --------------------------------------------------------------------------- atomic writes


def test_atomic_write_creates_and_replaces(tmp_path):
    path = tmp_path / "cases.jsonl"
    atomic_write_text(path, "one\n")
    assert path.read_text() == "one\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    atomic_write_text(path, "two\n")
    assert path.read_text() == "two\n"
    assert _leftovers(tmp_path) == []


def test_atomic_write_keeps_the_permission_bits(tmp_path):
    path = tmp_path / "run.json"
    path.write_text("{}")
    path.chmod(0o600)
    atomic_write_text(path, '{"a": 1}')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_target_is_untouched_until_the_new_content_is_on_disk(tmp_path, monkeypatch):
    """The content is written and synced to another file; the target changes only by the
    rename, so a reader at any moment before it sees the complete old file."""
    path = tmp_path / "cases.jsonl"
    path.write_text("old\n")
    seen: list[str] = []
    real_fsync = os.fsync

    def fsync(fd):
        seen.append(path.read_text())
        real_fsync(fd)

    monkeypatch.setattr(fsutil.os, "fsync", fsync)
    atomic_write_text(path, "new\n" * 1000)
    assert seen[0] == "old\n", "the temp file is synced before the target changes"
    assert path.read_text() == "new\n" * 1000


@pytest.mark.parametrize("fail_in", ["write", "replace"])
def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch, fail_in):
    path = tmp_path / "run.json"
    path.write_text('{"old": true}\n')

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    if fail_in == "write":
        monkeypatch.setattr(fsutil.os, "fsync", boom)
    else:
        monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError, match="No space left"):
        atomic_write_text(path, '{"new": true}\n')
    assert path.read_text() == '{"old": true}\n'
    assert _leftovers(tmp_path) == []


def test_a_crash_mid_write_leaves_the_old_file(tmp_path):
    """A process killed while writing (here: os._exit from inside the write) never leaves a
    truncated target behind."""
    path = tmp_path / "cases.jsonl"
    path.write_text("complete old content\n")
    code = textwrap.dedent(
        f"""
        import os
        from forcebench import fsutil
        def die(fd):
            os._exit(9)
        fsutil.os.fsync = die
        fsutil.atomic_write_text(__import__("pathlib").Path({str(path)!r}), "partial" * 10000)
        """
    )
    done = subprocess.run([sys.executable, "-c", code], check=False)
    assert done.returncode == 9
    assert path.read_text() == "complete old content\n"


# --------------------------------------------------------------------------- locks


def _locked_elsewhere(path) -> bool:
    """Whether another open file description can take the lock right now (as another process
    would: flock locks belong to the open file, not the process)."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def test_exclusive_lock_excludes_others_and_is_released(tmp_path):
    lock = tmp_path / ".lock"
    with exclusive_lock(lock):
        assert _locked_elsewhere(lock)
    assert not _locked_elsewhere(lock)


def test_taking_a_held_lock_again_in_this_process_is_refused_not_a_deadlock(tmp_path):
    lock = tmp_path / ".lock"
    with (
        exclusive_lock(lock),
        pytest.raises(RuntimeError, match="already holds"),
        exclusive_lock(lock),
    ):
        pass
    with exclusive_lock(lock):  # released despite the error
        pass


def test_without_waiting_a_held_lock_is_refused_and_nothing_is_taken(tmp_path):
    lock = tmp_path / ".lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)  # another open file: as another process holds it
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LockBusyError), exclusive_lock(lock, wait=False):
            pytest.fail("the block ran without the lock")
    finally:
        os.close(fd)
    with exclusive_lock(lock, wait=False):  # free again: taken at once
        assert _locked_elsewhere(lock)


def test_a_second_process_waits_for_the_lock(tmp_path):
    lock = tmp_path / ".lock"
    code = textwrap.dedent(
        f"""
        import sys, time
        from forcebench.fsutil import exclusive_lock
        t0 = time.monotonic()
        with exclusive_lock(__import__("pathlib").Path({str(lock)!r}), waiting="WAITING"):
            print(round(time.monotonic() - t0, 1))
        """
    )
    with exclusive_lock(lock):
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        # The other process reports that it is waiting, then blocks until we let go.
        assert proc.stderr is not None
        assert proc.stderr.readline().strip() == "WAITING"
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0 and float(out) >= 0


# --------------------------------------------------------------------------- runs


def test_grading_holds_the_run_lock_and_writes_cases_atomically(tmp_path, make_task, monkeypatch):
    import asyncio

    from forcebench import runner
    from forcebench.graders import GradeEnv
    from forcebench.llm import Generation

    task = make_task({"format": "text"})
    run_dir = tmp_path / "20260928T000000Z_qwen3.8-27b-awq-int4@low"
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"task_ids": [task.id], "samples": 1}))
    gen = Generation(text="Answer: x", finish_reason="stop")
    (run_dir / "raw" / "generations.jsonl").write_text(
        json.dumps({"key": f"{task.id}#0", "generation": gen.model_dump()}) + "\n"
    )
    held: list[bool] = []
    writes: list[str] = []
    real_write = runner.atomic_write_text

    def spy(path, text):
        held.append(_locked_elsewhere(run_dir / runner.LOCK_FILE))
        writes.append(path.name)
        real_write(path, text)

    monkeypatch.setattr(runner, "atomic_write_text", spy)
    asyncio.run(runner.grade(run_dir, [task], GradeEnv(work_dir=tmp_path / "w"), progress=False))
    # The grading pass's timing summary (artifacts/grading/<start>.json), then the run's files.
    assert re.fullmatch(r"\d{8}T\d{6}Z\.json", writes[0]), writes
    assert writes[1:] == ["cases.jsonl", "run.json"]
    assert held == [True, True, True], "every write happens under the run lock"
    assert not _locked_elsewhere(run_dir / runner.LOCK_FILE), "released afterwards"
    [case] = [json.loads(x) for x in (run_dir / "cases.jsonl").read_text().splitlines()]
    assert case["passed"] is True
