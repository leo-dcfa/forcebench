"""Skill packs for agent runs (forcebench.agent.skills): the manifest, and that only the pinned
files are ever mounted. Packs are fetched here from a local repository, never the network.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from forcebench.agent.skills import load_pack, prepare, skill_block, tree_sha256


def test_the_shipped_pack_is_pinned():
    pack = load_pack("sf-skills")
    assert len(pack.commit) == 40 and len(pack.sha256) == 64
    assert "platform-apex-generate" in pack.skills and len(pack.skills) == 16
    assert set(pack.describe()) == {"name", "version", "source", "commit", "skills", "sha256"}


def test_an_unknown_pack_names_the_known_ones():
    with pytest.raises(ValueError, match="known: sf-skills"):
        load_pack("nope")
    with pytest.raises(ValueError, match="no skill pack"):
        load_pack("../sf-skills")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path):
    """A skills repository with two skills, and a manifest that takes one of them."""
    repo = tmp_path / "upstream"
    for name in ("apex", "other"):
        (repo / "skills" / name / "references").mkdir(parents=True)
        (repo / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\nBody.\n")
        (repo / "skills" / name / "references" / "a.md").write_text("ref")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "skills")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    packs = tmp_path / "packs"
    packs.mkdir()

    def write(**over):
        d = {
            "name": "test-pack", "version": "1.0.0", "source": f"file://{repo}",
            "commit": commit, "skills": ["apex"], "sha256": "",
        } | over  # fmt: skip
        (packs / "test-pack.json").write_text(json.dumps(d))
        return load_pack("test-pack", packs)

    return repo, write, tmp_path / "cache"


def _hash_of_pack(repo: Path, names: list[str]) -> str:
    """What the manifest would pin: the hash of those skills' files, copied as they are."""
    out = repo.parent / "expected"
    shutil.rmtree(out, ignore_errors=True)
    for n in names:
        shutil.copytree(repo / "skills" / n, out / n)
    return tree_sha256(out)


def test_only_the_named_skills_at_the_pinned_commit_are_prepared(upstream):
    repo, write, cache = upstream
    pack = write(sha256=_hash_of_pack(repo, ["apex"]))
    d = prepare(pack, cache)
    assert sorted(p.name for p in d.iterdir()) == ["apex"]
    assert (d / "apex" / "references" / "a.md").read_text() == "ref"
    assert prepare(pack, cache) == d, "fetched once, then reused"


def test_a_pack_whose_files_differ_is_refused_and_not_kept(upstream):
    _, write, cache = upstream
    pack = write(sha256="0" * 64)
    with pytest.raises(RuntimeError, match="hash to"):
        prepare(pack, cache)
    assert not pack.directory(cache).exists()


def test_files_changed_after_preparing_are_refused(upstream):
    repo, write, cache = upstream
    pack = write(sha256=_hash_of_pack(repo, ["apex"]))
    d = prepare(pack, cache)
    (d / "apex" / "SKILL.md").write_text("tampered")
    with pytest.raises(RuntimeError, match="hash to"):
        prepare(pack, cache)


def test_a_skill_missing_at_the_commit_is_refused(upstream):
    _, write, cache = upstream
    with pytest.raises(RuntimeError, match="no skill missing"):
        prepare(write(skills=["apex", "missing"]), cache)


def test_a_symlink_in_a_pack_is_refused(upstream):
    repo, write, cache = upstream
    (repo / "skills" / "apex" / "link").symlink_to("/etc/passwd")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "link")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    with pytest.raises(RuntimeError, match="symlink"):
        prepare(write(commit=commit, sha256="0" * 64), cache)


def test_preloading_names_skills_by_suite(upstream):
    _, write, _ = upstream
    pack = write(preload={"apex": ["apex"]})
    assert pack.preload_for("apex") == ("apex",) and pack.preload_for("lwc") == ()
    with pytest.raises(ValueError, match="preload for apex"):
        write(preload={"apex": ["other"]})  # in the repository, not in the pack
    assert load_pack("sf-skills").preload_for("permissions") == (
        "platform-permission-set-generate",
    )


def test_a_skill_block_is_what_opencode_hands_the_model(tmp_path):
    skill = tmp_path / "apex"
    (skill / "assets").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: apex\ndescription: d\n---\n\n## Use\n\nWrite Apex.\n"
    )
    for i in range(12):
        (skill / "assets" / f"f{i:02}.cls").write_text("x")
    block = skill_block(tmp_path, "apex", "/m")
    assert block.startswith(
        '<skill_content name="apex">\n# Skill: apex\n\n## Use\n\nWrite Apex.\n\n'
    )
    assert "name: apex" not in block, "front matter is left out"
    assert "Base directory for this skill: /m/apex\n" in block
    assert block.count("<file>") == 10 and "<file>/m/apex/assets/f00.cls</file>" in block
    assert "SKILL.md</file>" not in block and block.endswith("</skill_files>\n</skill_content>")
