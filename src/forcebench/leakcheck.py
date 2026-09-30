"""Leak checks: nothing from the private pool may appear in what this repository publishes.

``forcebench leakcheck`` reads every file git tracks here and applies allowlist rules: what
public files may contain, rather than a list of what they may not. They need no secrets, so they
run anywhere, CI included. A finding names the file, the line and the rule, never the text it
matched, which could be private.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from forcebench import CANARY_GUID, REPO_ROOT


@dataclass(frozen=True)
class Finding:
    path: str  # relative to the repository
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


Rule = Callable[[str, str], Iterator[Finding]]  # (path, text) -> findings
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    """Register an allowlist rule."""
    RULES.append(fn)
    return fn


def tracked_files(root: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    )
    return [p for p in out.stdout.decode().split("\0") if p]


def read_text(path: Path) -> str | None:
    """A file's text, or None for a binary one (a NUL byte, or not UTF-8), which no rule reads."""
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def check(files: Iterable[tuple[str, str]], rules: Iterable[Rule] = RULES) -> list[Finding]:
    rules = list(rules)
    return [f for path, text in files for r in rules for f in r(path, text)]


def check_tracked(root: Path = REPO_ROOT) -> list[Finding]:
    """Every rule over every file git tracks in ``root``, as it is in the working tree."""

    def files() -> Iterator[tuple[str, str]]:
        for path in tracked_files(root):
            full = root / path
            if full.is_symlink() or not full.is_file():  # a link, or deleted but not yet staged
                continue
            text = read_text(full)
            if text is not None:
                yield path, text

    return check(files())


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# --------------------------------------------------------------------------- rules

_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# "canary GUID <guid>", as task files and runs carry it, and "canary_guid: <guid>", as a private
# pool's pool.yaml has it.
_CANARY_RE = re.compile(rf"canary[\W_]{{0,3}}(?:guid[\W_]{{0,3}})?({_GUID})", re.I)
# Made-up GUIDs the tests give a private pool; never a real pool's.
TEST_GUIDS = frozenset(
    {"11111111-2222-4333-8444-555555555555", "99999999-2222-4333-8444-555555555555"}
)


@rule
def only_the_public_canary(path: str, text: str) -> Iterator[Finding]:
    """The only canary GUID here is the public one (and, in tests, the made-up ones). Another is
    a private pool's: some private file, or part of one, was copied in."""
    allowed = {CANARY_GUID, *(TEST_GUIDS if path.startswith("tests/") else ())}
    for m in _CANARY_RE.finditer(text):
        if m.group(1).lower() not in allowed:
            yield Finding(path, _line(text, m.start()), "canary", "not the public canary GUID")


_TASKS_DIR_RE = re.compile(r"suites/[^/]+/tasks/[^/]+")


@rule
def public_task_files(path: str, text: str) -> Iterator[Finding]:
    """A task directory holds only task files, and each carries the public canary on its first
    line, says ``visibility: public`` once, and has no ``tier`` (which only private tasks have)."""
    if not _TASKS_DIR_RE.fullmatch(path):
        return
    if not path.endswith(".yaml"):
        yield Finding(path, 1, "task", "a task directory holds only <task id>.yaml files")
        return
    lines = text.split("\n")
    if CANARY_GUID not in lines[0]:
        yield Finding(path, 1, "task", "the first line does not carry the public canary")
    said = [(i, line.strip()) for i, line in enumerate(lines, 1) if line.startswith("visibility:")]
    if [s for _, s in said] != ["visibility: public"]:
        where = said[0][0] if said else 1
        yield Finding(path, where, "task", "it must say `visibility: public`, once")
    for i, line in enumerate(lines, 1):
        if line.startswith("tier:"):
            yield Finding(path, i, "task", "`tier` is only for private tasks")
