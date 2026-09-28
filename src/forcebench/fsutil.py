"""Crash-safe file writes and advisory locks.

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

__all__ = ["atomic_write_text", "exclusive_lock"]


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


# Locks this process holds, by path. flock locks belong to an open file description, so taking
# the same lock again in this process would wait for itself forever: that is refused instead.
_held: set[str] = set()


@contextlib.contextmanager
def exclusive_lock(path: Path, waiting: str | None = None) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``path`` (created if missing) for the ``with`` block.

    While another process holds it, this waits, saying so once on stderr (``waiting``). Taking a
    lock this process already holds raises RuntimeError rather than deadlocking.
    """
    key = os.path.realpath(path)
    if key in _held:
        raise RuntimeError(f"this process already holds the lock {path}")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
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
