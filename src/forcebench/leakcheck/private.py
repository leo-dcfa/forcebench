"""The denylist: what the private pool itself says must never appear here.

Where the private pool is configured (FORCEBENCH_PRIVATE_DIR: the maintainer's machine, and so
the pre-commit hook), every file is also checked for the pool's task ids, its canary, its
directory, its repository's name and URL, its Hugging Face dataset, the hidden-test class names
only private tasks use, its private providers (as a configuration names them, their public
label, their address) and private configurations (their ids, and their model ids no public
configuration shares), and exact copies of its files. Without a pool (CI) there is nothing to
check against, and this rule finds nothing; the allowlist rules still apply. A finding says which
kind of thing matched, never what.
"""

import functools
import hashlib
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from forcebench import SUITES_DIR
from forcebench.leakcheck import Finding, line_of, rule
from forcebench.models import load_registry
from forcebench.pool import configured_private_dir, load_private_pool


_CLASS_RE = re.compile(r"\bFB_[A-Za-z0-9_]+")
_REMOTE_RE = re.compile(r"[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_MIN_COPY = 64  # bytes: shorter files (.gitkeep, "{}") say nothing about the pool


@dataclass(frozen=True)
class Denylist:
    words: re.Pattern[str] | None  # task ids, repository names, hidden classes: whole words
    anywhere: re.Pattern[str] | None  # the canary and the directory: anywhere, any case
    kinds: dict[str, str]  # matched text (lower case for `anywhere`) -> what it is
    copies: frozenset[str]  # sha256 of every file the pool's repository tracks


def _alternation(tokens: list[str], template: str, flags: int = 0) -> re.Pattern[str] | None:
    if not tokens:
        return None
    body = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    return re.compile(template.format(body), flags)


def _git(root: Path, *args: str) -> str | None:
    """Git in the pool's own repository.

    Inside a git hook, git sets GIT_DIR, GIT_INDEX_FILE and the like for the public repository;
    left in place, they would make `-C root` read that one.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False, env=env
    )
    return out.stdout if out.returncode == 0 else None


def _hidden_classes(task_files: list[Path]) -> set[str]:
    """FB_ class names in the private tasks' hidden files and test lists.

    Names a public task uses too are left out (they are not the pool's to give away).
    """
    names: set[str] = set()
    for path in task_files:
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        grader = data.get("grader") if isinstance(data, dict) else None
        if isinstance(grader, dict):
            for item in [*(grader.get("hidden_files") or {}), *(grader.get("tests") or [])]:
                names |= set(_CLASS_RE.findall(str(item)))
    public = "\n".join(p.read_text() for p in SUITES_DIR.glob("*/tasks/*.yaml"))
    return {n for n in names if not re.search(rf"\b{re.escape(n)}\b", public)}


def _private_registry(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """What the pool's private providers and configurations would give away (models.load_registry).

    Whole words: each private configuration's id, its model id where no public configuration
    shares it, each private provider as a configuration or a run names it (``provider: x``) and its
    public label. Anywhere, in any case: each private provider's address and host, as this
    machine's environment resolves them.
    """
    public = load_registry(private_dir=None)
    registry = load_registry(private_dir=root)
    words: dict[str, str] = {}
    anywhere: dict[str, str] = {}
    shared = {m.model_id for m in public.models.values()}
    for m in registry.models.values():
        if m.private:
            words[m.id] = "a private configuration's id"
            if m.model_id not in shared:
                words[m.model_id] = "a private configuration's model id"
    for name, p in registry.providers.items():
        if not p.private:
            continue
        words[f"provider: {name}"] = words[f'"provider": "{name}"'] = "a private provider"
        if p.label:
            words[p.label] = "a private provider's name"
        url = p.resolved_base_url()
        if url:
            anywhere[url.lower().rstrip("/")] = "a private provider's address"
            if host := urlparse(url).hostname:
                anywhere[host.lower()] = "a private provider's address"
    return words, anywhere


def _copies(root: Path) -> frozenset[str]:
    """Every file of the pool's repository that its .gitignore does not leave out.

    Files count whether tracked or not yet (a task being written).
    """
    listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    files = [root / p for p in listed.split("\0") if p] if listed else []
    hashes = set()
    for f in files:
        if f.is_file() and not f.is_symlink() and f.stat().st_size >= _MIN_COPY:
            hashes.add(hashlib.sha256(f.read_bytes()).hexdigest())
    return frozenset(hashes)


@functools.cache
def denylist() -> Denylist | None:
    """The configured private pool's denylist, or None where there is no pool.

    A pool that is configured but cannot be loaded raises PrivatePoolError: better no check than
    a silent one.
    """
    configured = configured_private_dir()
    if configured is None:
        return None
    pool = load_private_pool()
    root = pool.root
    task_files = sorted(root.glob("suites/*/tasks/*.yaml"))
    kinds = {f.stem: "a private task id" for f in task_files}
    kinds |= dict.fromkeys(_hidden_classes(task_files), "a hidden class of a private task")
    remote = _git(root, "remote", "get-url", "origin")
    if remote and (m := _REMOTE_RE.search(remote.strip())):
        kinds[f"{m.group(1)}/{m.group(2)}"] = "the private repository"
        kinds[m.group(2)] = "the private repository's name"
    if pool.hf_dataset:
        kinds[pool.hf_dataset] = "the private dataset"
        kinds[pool.hf_dataset.split("/")[1]] = "the private dataset's name"
    paths = {str(root), os.path.normpath(configured)}
    home = str(Path.home())
    paths |= {"~" + p[len(home) :] for p in list(paths) if p.startswith(home + os.sep)}
    anywhere = {pool.canary_guid.lower(): "the private canary"}
    anywhere |= {p.lower(): "the private pool's directory" for p in paths}
    configs, addresses = _private_registry(root)
    kinds |= configs
    anywhere |= addresses
    return Denylist(
        words=_alternation(list(kinds), r"(?<![\w-])(?:{})(?![\w-])"),
        anywhere=_alternation(list(anywhere), "(?:{})", re.I),
        kinds={**kinds, **anywhere},
        copies=_copies(root),
    )


@rule
def nothing_from_the_private_pool(path: str, text: str) -> Iterator[Finding]:
    deny = denylist()
    if deny is None:
        return
    if len(text) >= _MIN_COPY and hashlib.sha256(text.encode()).hexdigest() in deny.copies:
        yield Finding(path, 1, "private", "a copy of a file in the private pool")
    for pattern, fold in ((deny.words, False), (deny.anywhere, True)):
        for m in pattern.finditer(text) if pattern else ():
            kind = deny.kinds[m.group(0).lower() if fold else m.group(0)]
            yield Finding(path, line_of(text, m.start()), "private", f"names {kind}")
