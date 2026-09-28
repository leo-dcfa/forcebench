"""Crash-safe file writes, advisory locks, and the results-directory symlink check.

A run's ``cases.jsonl`` and ``run.json`` (and the leaderboard) are rewritten as a whole, and are
read by other processes while that happens (``forcebench report``, the publisher). They are
never written in place: :func:`atomic_write_text` writes a temporary file next to the target and
renames it over the target, so a reader sees the old file or the new one, never a truncated or
half-written one, and a crash leaves the old file intact.

Writers of the same run serialise on :func:`exclusive_lock` (``runner.run_lock``), so two graders
cannot interleave their read-merge-write of ``cases.jsonl``. The lock is ``flock(2)``: advisory,
released when the process exits however it exits, and shared by every process on one kernel
(two containers of one Docker VM, or two processes on one machine), not between the host and a
container.

The results tree is data (runs can be contributed), so none of the directories the harness
reads runs from and writes them and the leaderboard to may be a symbolic link:
:func:`check_results_dir` refuses ``results/`` or ``results/runs`` if either is one, as
``runner.check_run_dir`` refuses a run directory, and its files, that is one.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

__all__ = [
    "LockBusyError",
    "ResultsDirError",
    "atomic_write_text",
    "check_results_dir",
    "exclusive_lock",
]


class ResultsDirError(ValueError):
    """The results directory, or its ``runs/``, is a symbolic link (see check_results_dir)."""


def check_results_dir(results_dir: Path, runs_dir: Path | None = None) -> None:
    """Refuse ``results_dir`` (``results/``) and ``runs_dir`` (default ``results_dir/runs``) if
    either is a symbolic link: runs would be read and graded, and grades and the leaderboard
    written, wherever it points. A missing directory is not refused (a new checkout)."""
    runs_dir = results_dir / "runs" if runs_dir is None else runs_dir
    links = [str(d) for d in (results_dir, runs_dir) if d.is_symlink()]
    if links:
        raise ResultsDirError(
            f"refusing to read or write results: {' and '.join(links)} "
            f"{'is a symbolic link' if len(links) == 1 else 'are symbolic links'}; results/ "
            "and results/runs must be plain directories (docs/sandbox.md)"
        )


def _fsync_dir(directory: Path) -> None:
    """Make a rename in ``directory`` durable. Not every file system can sync a directory (some
    Docker bind mounts cannot); the rename itself is atomic either way."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` (UTF-8) atomically and durably.

    The text goes to a temporary file in the same directory (a rename is atomic only within one
    file system), is flushed and fsynced, then renamed over ``path`` with ``os.replace``. On any
    error the temporary file is removed and ``path`` is left as it was. An existing file keeps
    its permission bits; a new one gets 0644, as a plain write with the usual umask would.
    """
    path = Path(path)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(mode)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


class LockBusyError(RuntimeError):
    """Another process holds the lock, and the caller asked not to wait for it
    (``exclusive_lock(..., wait=False)``)."""


# Locks this process holds, by path. flock locks belong to an open file description, so taking
# the same lock again in this process would wait for itself forever: that is refused instead.
_held: set[str] = set()


@contextlib.contextmanager
def exclusive_lock(path: Path, waiting: str | None = None, *, wait: bool = True) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``path`` (created if missing) for the ``with`` block.

    While another process holds it, this waits, saying so once on stderr (``waiting``); with
    ``wait=False`` it raises LockBusyError instead, having taken nothing. Taking a lock this
    process already holds raises RuntimeError rather than deadlocking.
    """
    key = os.path.realpath(path)
    if key in _held:
        raise RuntimeError(f"this process already holds the lock {path}")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                raise LockBusyError(f"another process holds the lock {path}") from None
            if waiting:
                print(waiting, file=sys.stderr, flush=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
        _held.add(key)
        try:
            yield
        finally:
            _held.discard(key)
    finally:
        os.close(fd)  # releases the lock
