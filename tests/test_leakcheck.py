"""Leak checks: allowlist rules over what this repository publishes."""

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forcebench import CANARY, REPO_ROOT
from forcebench.cli import app
from forcebench.leakcheck import Finding, all_rules, check, check_tracked, read_text
from forcebench.leakcheck.tasks import only_the_public_canary, public_task_files


PRIVATE_GUID = "5d2c9a41-7f3e-4b8a-9c1d-2e6f0a4b8c3d"  # stands in for a real pool's canary


def test_this_repository_passes():
    assert check_tracked() == []


def test_every_rule_module_is_loaded():
    assert {only_the_public_canary, public_task_files} <= set(all_rules())


@pytest.mark.parametrize(
    "text",
    [
        f"notes: forcebench private canary GUID {PRIVATE_GUID}\n",
        f"canary_guid: {PRIVATE_GUID}\n",  # a private pool's pool.yaml
        f"# canary GUID {PRIVATE_GUID.upper()}\n",
    ],
)
def test_another_canary_is_found_anywhere(text):
    [finding] = check([("docs/notes.md", "line one\n" + text)], [only_the_public_canary])
    assert (finding.line, finding.rule) == (2, "canary")
    assert PRIVATE_GUID not in str(finding).lower(), "a finding never quotes what it matched"


def test_the_public_canary_and_test_guids_in_tests_are_allowed():
    fake = "11111111-2222-4333-8444-555555555555"
    assert not check([("README.md", CANARY)], [only_the_public_canary])
    assert not check([("tests/test_x.py", f"canary GUID {fake}")], [only_the_public_canary])
    assert check([("docs/x.md", f"canary GUID {fake}")], [only_the_public_canary])


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        (lambda t: t.split("\n", 1)[1], "first line"),
        (lambda t: t.replace("visibility: public", "visibility: private"), "visibility: public"),
        (lambda t: t.replace("visibility: public\n", ""), "visibility: public"),
        (lambda t: t.replace("visibility: public", "visibility: public\ntier: private"), "tier"),
    ],
)
def test_a_task_file_must_look_public(text, detail):
    src = next((REPO_ROOT / "suites" / "docs" / "tasks").glob("*.yaml")).read_text()
    findings = check([("suites/docs/tasks/docs-x.yaml", text(src))], [public_task_files])
    assert [f for f in findings if detail in f.detail], findings


def test_a_task_directory_holds_only_task_files():
    for path in ("suites/docs/tasks/docs-x.yml", "suites/docs/tasks/docs-x.json"):
        [finding] = check([(path, "{}")], [public_task_files])
        assert "only <task id>.yaml" in finding.detail
    assert not check([("tests/fixtures/x/tasks/a.json", "{}")], [public_task_files])


def test_binary_files_are_not_read(tmp_path: Path):
    (tmp_path / "a.bin").write_bytes(b"canary GUID \x00\xff")
    (tmp_path / "b.txt").write_text("text")
    assert read_text(tmp_path / "a.bin") is None
    assert read_text(tmp_path / "b.txt") == "text"


def test_only_tracked_files_are_read(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.md").write_text(f"canary GUID {PRIVATE_GUID}\n")
    (tmp_path / "untracked.md").write_text(f"canary GUID {PRIVATE_GUID}\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.md"], check=True)
    assert [f.path for f in check_tracked(tmp_path)] == ["tracked.md"]


def test_the_command_fails_on_a_finding(monkeypatch):
    monkeypatch.setattr(
        "forcebench.leakcheck.check_tracked", lambda: [Finding("x.md", 3, "canary", "not it")]
    )
    result = CliRunner().invoke(app, ["leakcheck"])
    assert result.exit_code == 1
    assert "x.md:3: canary: not it" in result.output
