"""Skill packs for agent runs: instructions and reference files an agent loads when it chooses to.

A pack is a manifest, docker/agent/skills/<name>.json, naming a git repository, the commit to take,
the skills to take from it and the sha256 of the files they hold. ``prepare`` fetches that commit
once, on the host, copies the named skills to .cache/agent-skills/ and refuses files whose hash
differs. The agent's container gets the pack read-only, outside its workspace, in opencode's
global skills directory: opencode shows the model each skill's name and description and loads one
when the model asks for it (its "skill" tool). A run records the pack and is never resumed with
another one, or none.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forcebench import CACHE_DIR, REPO_ROOT

PACKS_DIR = REPO_ROOT / "docker" / "agent" / "skills"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class SkillPack:
    """A pinned selection of skills from one repository."""

    name: str
    version: str
    source: str
    commit: str
    skills: tuple[str, ...]
    sha256: str

    def describe(self) -> dict[str, Any]:
        """What a run records about its pack (a resume must match it)."""
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "commit": self.commit,
            "skills": list(self.skills),
            "sha256": self.sha256,
        }

    def directory(self, cache: Path = CACHE_DIR) -> Path:
        return cache / "agent-skills" / f"{self.name}-{self.version}"


def load_pack(name: str, packs: Path = PACKS_DIR) -> SkillPack:
    """The pack docker/agent/skills/<name>.json describes, or a ValueError naming those there are."""
    path = packs / f"{name}.json"
    if not _NAME.fullmatch(name) or not path.is_file():
        known = sorted(p.stem for p in packs.glob("*.json"))
        raise ValueError(f"no skill pack {name!r} (known: {', '.join(known) or 'none'})")
    d = json.loads(path.read_text())
    skills = tuple(d["skills"])
    if not _COMMIT.fullmatch(d["commit"]):
        raise ValueError(f"{path.name}: commit must be a full 40-character sha")
    if not skills or any(not _NAME.fullmatch(s) for s in skills) or len(set(skills)) < len(skills):
        raise ValueError(f"{path.name}: skills must be distinct directory names")
    return SkillPack(name, d["version"], d["source"], d["commit"], skills, d["sha256"])


def tree_sha256(root: Path) -> str:
    """One hash over every file's path and content under root. A symlink is refused: copied, it
    could bring in a file from anywhere on the host."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if p.is_symlink():
            raise RuntimeError(f"symlink in a skill pack: {p.relative_to(root)}")
        if p.is_file():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            h.update(f"{p.relative_to(root).as_posix()}\0{digest}\n".encode())
    return h.hexdigest()


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def prepare(pack: SkillPack, cache: Path = CACHE_DIR) -> Path:
    """The pack's directory, fetched and copied on first use, its hash checked every time. A
    RuntimeError when it cannot be fetched or its files are not the manifest's."""
    dest = pack.directory(cache)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            repo, out = Path(tmp) / "repo", Path(tmp) / "pack"
            repo.mkdir()
            _git("init", "-q", cwd=repo)
            _git("fetch", "-q", "--depth", "1", pack.source, pack.commit, cwd=repo)
            _git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=repo)
            if _git("rev-parse", "HEAD", cwd=repo) != pack.commit:
                raise RuntimeError(f"{pack.name}: fetched the wrong commit")
            out.mkdir()
            for s in pack.skills:
                src = repo / "skills" / s
                if not (src / "SKILL.md").is_file():
                    raise RuntimeError(f"{pack.name}: no skill {s} at {pack.commit[:12]}")
                shutil.copytree(src, out / s, symlinks=True)
            _check(pack, out)
            out.rename(dest)
    _check(pack, dest)
    return dest


def _check(pack: SkillPack, root: Path) -> None:
    got = tree_sha256(root)
    if got != pack.sha256:
        raise RuntimeError(
            f"{pack.name}: the files in {root} hash to {got}, not the manifest's {pack.sha256}"
        )
