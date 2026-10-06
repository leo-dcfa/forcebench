"""Canary and task-file rules: only the public canary, only public task files."""

import re
from collections.abc import Iterator

from forcebench import CANARY_GUID
from forcebench.leakcheck import Finding, line_of, rule


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
    a private pool's: some private file, or part of one, was copied in.
    """
    allowed = {CANARY_GUID, *(TEST_GUIDS if path.startswith("tests/") else ())}
    for m in _CANARY_RE.finditer(text):
        if m.group(1).lower() not in allowed:
            yield Finding(path, line_of(text, m.start()), "canary", "not the public canary GUID")


_TASKS_DIR_RE = re.compile(r"suites/[^/]+/tasks/[^/]+")


@rule
def public_task_files(path: str, text: str) -> Iterator[Finding]:
    """A task directory holds only task files, and each carries the public canary on its first
    line, says ``visibility: public`` once, and has no ``tier`` (which only private tasks have).
    """
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
