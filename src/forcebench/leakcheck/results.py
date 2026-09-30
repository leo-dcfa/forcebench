"""Results: only the files results/ is meant to hold, all public, naming only public tasks.

The report refuses to build the leaderboard from anything else (report.py). This checks what is
actually committed, including runs the leaderboard leaves out (results/invalid/). Task ids that
are not public are counted, never named: they may be private.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterator
from typing import Any

from forcebench import CANARY_GUID
from forcebench.leakcheck import Finding, rule
from forcebench.report import known_task_ids
from forcebench.tasks import EVERY_STATUS, load_suites

_RUN_FILE_RE = re.compile(r"results/(?:runs|invalid)/[^/]+/(?:run\.json|cases\.jsonl)")
_OTHER_FILES = frozenset(
    {"results/leaderboard.json", "results/LEADERBOARD.md", "results/invalid/README.md"}
)


@functools.cache
def public_task_ids() -> frozenset[str]:
    """Every public task id, removed ones (suites/prompt-hashes.json) included."""
    return frozenset(known_task_ids(load_suites(statuses=EVERY_STATUS)))


def _json(path: str, text: str, line: int = 1) -> tuple[Any, list[Finding]]:
    try:
        return json.loads(text), []
    except ValueError:
        return None, [Finding(path, line, "results", "not valid JSON")]


def _unknown(path: str, line: int, ids: set[str]) -> Iterator[Finding]:
    unknown = ids - public_task_ids()
    if unknown:
        yield Finding(path, line, "results", f"names {len(unknown)} tasks that are not public")


def _run(path: str, text: str) -> Iterator[Finding]:
    meta, bad = _json(path, text)
    yield from bad
    if not isinstance(meta, dict):
        return
    if meta.get("visibility", "public") != "public":
        yield Finding(path, 1, "results", "not a public run")
    canary = str(meta.get("canary") or "")
    if canary and CANARY_GUID not in canary:
        yield Finding(path, 1, "results", "carries another canary")
    yield from _unknown(path, 1, {str(t) for t in meta.get("task_ids") or []})


def _cases(path: str, text: str) -> Iterator[Finding]:
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        case, bad = _json(path, line, n)
        yield from bad
        if not isinstance(case, dict):
            continue
        if case.get("visibility", "public") != "public":
            yield Finding(path, n, "results", "not a public answer")
        yield from _unknown(path, n, {str(case.get("task_id"))})


def _leaderboard(path: str, text: str) -> Iterator[Finding]:
    data, bad = _json(path, text)
    yield from bad
    if not isinstance(data, dict):
        return
    if data.get("visibility") != "public":
        yield Finding(path, 1, "results", 'it must say "visibility": "public"')
    ids = {str(t.get("id")) for t in data.get("tasks") or [] if isinstance(t, dict)}
    for e in data.get("entries") or []:
        if isinstance(e, dict):
            ids |= set(e.get("per_task") or {})
    yield from _unknown(path, 1, ids)


@rule
def only_public_results(path: str, text: str) -> Iterator[Finding]:
    if not path.startswith("results/"):
        return
    if not _RUN_FILE_RE.fullmatch(path) and path not in _OTHER_FILES:
        yield Finding(
            path, 1, "results",
            "results/ holds only the leaderboard, and run.json and cases.jsonl of each run",
        )  # fmt: skip
    elif path.endswith("/run.json"):
        yield from _run(path, text)
    elif path.endswith("/cases.jsonl"):
        yield from _cases(path, text)
    elif path == "results/leaderboard.json":
        yield from _leaderboard(path, text)
