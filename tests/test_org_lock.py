"""The org lock: sf never runs outside the sandbox, and inside it only against scratch orgs."""

import json

import pytest

from forcebench import org
from forcebench.org import OrgError, check_command


def _store(home, orgs, aliases=None):
    sfdx = home / ".sfdx"
    sfdx.mkdir(parents=True, exist_ok=True)
    for username, url in orgs.items():
        (sfdx / f"{username}.json").write_text(
            json.dumps({"username": username, "instanceUrl": url, "accessToken": "x"})
        )
    (sfdx / "alias.json").write_text(json.dumps({"orgs": aliases or {}}))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(org.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("FORCEBENCH_SANDBOX", "1")
    monkeypatch.delenv("FORCEBENCH_PROVISION", raising=False)
    monkeypatch.setenv("FORCEBENCH_DEVHUB_USERNAME", "hub@example.com")
    monkeypatch.setattr(org, "in_sandbox", lambda: True)
    return tmp_path


SCRATCH = "https://fun-1234-dev-ed.scratch.my.salesforce.com"
PROD = "https://client.my.salesforce.com"


def test_refuses_outside_sandbox(monkeypatch):
    monkeypatch.setattr(org, "in_sandbox", lambda: False)
    with pytest.raises(OrgError, match="outside the Forcebench sandbox"):
        check_command(("data", "query", "-q", "SELECT Id FROM Account", "--target-org", "fb"))


def test_scratch_target_allowed(sandbox):
    _store(sandbox, {"test-a@example.com": SCRATCH}, {"fb-grader-1": "test-a@example.com"})
    check_command(("project", "deploy", "start", "--dry-run", "--target-org", "fb-grader-1"))


def test_any_non_scratch_org_in_store_blocks_everything(sandbox):
    _store(
        sandbox,
        {"test-a@example.com": SCRATCH, "admin@client.com": PROD},
        {"fb-grader-1": "test-a@example.com"},
    )
    with pytest.raises(OrgError, match="not Forcebench scratch orgs"):
        check_command(("data", "query", "--target-org", "fb-grader-1"))


def test_missing_or_unknown_target(sandbox):
    _store(sandbox, {"test-a@example.com": SCRATCH}, {"fb-grader-1": "test-a@example.com"})
    with pytest.raises(OrgError, match="must name its target"):
        check_command(("project", "deploy", "start"))
    with pytest.raises(OrgError, match="not a scratch org"):
        check_command(("data", "query", "--target-org", "somebody-else"))
    with pytest.raises(OrgError, match="never runs it"):
        check_command(("org", "list"))


def test_devhub_only_while_provisioning(sandbox, monkeypatch):
    _store(sandbox, {"hub@example.com": PROD}, {"azul": "hub@example.com"})
    with pytest.raises(OrgError, match="not Forcebench scratch orgs"):
        check_command(("org", "create", "scratch", "--target-dev-hub", "azul"))
    monkeypatch.setenv("FORCEBENCH_PROVISION", "1")
    monkeypatch.setattr(org, "provisioning", lambda: True)
    check_command(("org", "create", "scratch", "--target-dev-hub", "azul"))
    with pytest.raises(OrgError, match="not a scratch org"):
        check_command(("data", "query", "--target-org", "azul"))


def test_import_rejects_non_scratch_url(sandbox, tmp_path):
    f = tmp_path / "auth.txt"
    f.write_text("force://PlatformCLI::token@client.my.salesforce.com")
    with pytest.raises(OrgError, match="not for a scratch org"):
        org.import_auth("base", "x", f)


def test_scratch_url_check():
    assert org.is_scratch_url(SCRATCH)
    assert not org.is_scratch_url(PROD)
    assert not org.is_scratch_url("https://evil.com/.scratch.my.salesforce.com")
