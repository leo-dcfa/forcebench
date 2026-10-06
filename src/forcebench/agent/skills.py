"""Skill packs for agent runs: instructions and reference files an agent loads when it chooses to.

A pack is a manifest, docker/agent/skills/<name>.json, naming a git repository, the commit to take,
the skills to take from it and the sha256 of the files they hold. ``prepare`` fetches that commit
once, on the host, copies the named skills to .cache/agent-skills/ and refuses files whose hash
differs. The agent's container gets the pack read-only, outside its workspace, in opencode's
global skills directory: opencode shows the model each skill's name and description and loads one
when the model asks for it (its "skill" tool). A run records the pack and is never resumed with
another one, or none.

A run can also preload skills: the manifest maps each suite to the skills on its subject, and each
task's message then starts with those skills exactly as opencode's skill tool returns them (a
``<skill_content>`` block), so the model has them whether or not it would have loaded them.
"""

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
    # Suite -> the skills on its subject, for runs that preload them (chosen by subject, never by task).
    preload: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def preload_for(self, suite: str) -> tuple[str, ...]:
        return dict(self.preload).get(suite, ())

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
    preload = tuple((suite, tuple(ids)) for suite, ids in sorted((d.get("preload") or {}).items()))
    for suite, ids in preload:
        if not ids or any(s not in skills for s in ids):
            raise ValueError(f"{path.name}: preload for {suite} must name skills of the pack")
    return SkillPack(name, d["version"], d["source"], d["commit"], skills, d["sha256"], preload)


def tree_sha256(root: Path) -> str:
    """One hash over every file's path and content under root. A symlink is refused: copied, it
    could bring in a file from anywhere on the host.
    """
    h = hashlib.sha256()
    for p in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if p.is_symlink():
            raise RuntimeError(f"symlink in a skill pack: {p.relative_to(root)}")
        if p.is_file():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            h.update(f"{p.relative_to(root).as_posix()}\0{digest}\n".encode())
    return h.hexdigest()


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def prepare(pack: SkillPack, cache: Path = CACHE_DIR) -> Path:
    """The pack's directory, fetched and copied on first use, its hash checked every time. A
    RuntimeError when it cannot be fetched or its files are not the manifest's.
    """
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


def _body(skill_md: str) -> str:
    """SKILL.md without its front matter."""
    if skill_md.startswith("---\n"):
        end = skill_md.find("\n---", 4)
        if end >= 0:
            skill_md = skill_md[end + 4 :]
    return skill_md.strip()


def skill_block(pack_dir: Path, skill: str, mount: str, max_files: int = 10) -> str:
    """A skill as opencode's skill tool hands it to the model (opencode 2.0.21): its instructions,
    where it lives in the container, and up to ``max_files`` of its other files.
    """
    root = pack_dir / skill
    files = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "SKILL.md"
    )[:max_files]
    listing = "".join(f"<file>{mount}/{skill}/{f}</file>\n" for f in files)
    return (
        f'<skill_content name="{skill}">\n# Skill: {skill}\n\n'
        f"{_body((root / 'SKILL.md').read_text(encoding='utf-8'))}\n\n"
        f"Base directory for this skill: {mount}/{skill}\n"
        "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.\n"
        "Note: file list is sampled.\n\n"
        f"<skill_files>\n{listing}</skill_files>\n</skill_content>"
    )
