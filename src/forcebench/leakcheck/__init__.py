"""Leak checks: nothing from the private pool may appear in what this repository publishes.

``forcebench leakcheck`` reads every file git tracks here and applies allowlist rules: what
public files may contain, rather than a list of what they may not. They need no secrets, so they
run anywhere, CI included. A finding names the file, the line and the rule, never the text it
matched, which could be private.

Each module in this package holds rules, registered with ``@rule``; every module is imported on
first use, so adding rules never means editing a shared list.
"""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from forcebench import REPO_ROOT


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
    return decode_text(path.read_bytes())


def decode_text(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def all_rules() -> list[Rule]:
    """Every registered rule, once each module of this package has been imported."""
    for mod in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{mod.name}")
    return RULES


def check(files: Iterable[tuple[str, str]], rules: Iterable[Rule] | None = None) -> list[Finding]:
    rules = all_rules() if rules is None else list(rules)
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


def check_staged(root: Path = REPO_ROOT) -> list[Finding]:
    """Every rule over every file staged for the next commit, as staged (the pre-commit hook):
    what the commit would contain, not the working tree. A staged symbolic link is checked as
    the path it points to."""
    git = ["git", "-C", str(root)]
    names = subprocess.run(
        [*git, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
        capture_output=True, check=True,
    ).stdout.decode()  # fmt: skip

    def files() -> Iterator[tuple[str, str]]:
        for path in (p for p in names.split("\0") if p):
            blob = subprocess.run([*git, "show", f":{path}"], capture_output=True, check=True)
            text = decode_text(blob.stdout)
            if text is not None:
                yield path, text

    return check(files())


def line_of(text: str, pos: int) -> int:
    """The 1-based line of ``pos`` in ``text``."""
    return text.count("\n", 0, pos) + 1
