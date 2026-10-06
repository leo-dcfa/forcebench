"""The org lock: sf never runs outside the sandbox, and inside it only against scratch orgs."""

import datetime as dt
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forcebench import org
from forcebench.cli import app
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
    monkeypatch.setattr(org, "REGISTRY", tmp_path / "cache" / "orgs.json")
    monkeypatch.setattr(org, "PENDING", tmp_path / "cache" / "orgs-pending.json")
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
    # Provisioning mode lets the Dev Hub stay in the store, but only a provisioning command
    # (orgs create / register / import) may then run anything.
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        check_command(("org", "create", "scratch", "--target-dev-hub", "azul"))
    with org._provisioning_operation():
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


# --------------------------------------------------------------------------- provisioning mode

HUB_AND_GRADERS = (
    {"hub@example.com": PROD, "grader@example.com": SCRATCH, "new@example.com": SCRATCH},
    {"azul": "hub@example.com", "fb-grader-1": "grader@example.com", "fb-new": "new@example.com"},
)
DEPLOY = ("project", "deploy", "start", "--dry-run", "--target-org", "fb-grader-1")


@pytest.fixture
def provisioning(sandbox, monkeypatch):
    """Provisioning mode with the Dev Hub logged in next to scratch orgs."""
    monkeypatch.setenv("FORCEBENCH_PROVISION", "1")
    _store(sandbox, *HUB_AND_GRADERS)
    return sandbox


def test_grading_refuses_while_a_devhub_is_logged_in(provisioning):
    with pytest.raises(OrgError, match="a Dev Hub is logged in to the sandbox"):
        check_command(DEPLOY)
    with pytest.raises(OrgError, match="a Dev Hub is logged in to the sandbox"):
        check_command(("data", "query", "--query", "SELECT Id FROM Account", "-o", "fb-grader-1"))
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        check_command(("org", "login", "sfdx-url", "--sfdx-url-file", "x", "--alias", "y"))


def test_grading_and_validation_get_no_orgs_while_a_devhub_is_logged_in(provisioning, monkeypatch):
    org._set_pending("fb-new", "base")
    org.save_registry({"base": ["fb-grader-1"]})
    monkeypatch.setattr(org, "verify_scratch", lambda a: pytest.fail("sf org display ran"))
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        org.available_orgs()


def test_grading_runs_again_once_the_devhub_is_logged_out(provisioning):
    hub_file = provisioning / ".sfdx" / "hub@example.com.json"
    hub_file.unlink()
    check_command(DEPLOY)


def test_only_the_org_being_created_may_be_set_up_meanwhile(provisioning):
    """Orgs create runs the profile's setup in another process: its lock check (and seed.py's
    sf calls) may target the org being created, and nothing else.
    """
    org._set_pending("fb-new", "base")
    check_command(("org", "display", "--target-org", "fb-new"))
    check_command(("apex", "run", "--file", "wipe.apex", "--target-org", "new@example.com"))
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        check_command(DEPLOY)
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        check_command(("org", "create", "scratch", "--target-dev-hub", "azul", "-o", "fb-new"))
    org._set_pending("fb-new", None)  # what register() does once the setup has finished
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        check_command(("org", "display", "--target-org", "fb-new"))


def _fake_sf(calls):
    def sf(*args, cwd=None):
        check_command(args)  # the real lock decides; only the CLI itself is faked
        calls.append(args)
        if args[:2] == ("org", "display"):
            return {"status": 0, "result": {"instanceUrl": SCRATCH, "status": "Active"}}
        return {"status": 0, "result": {}}

    return sf


def test_create_and_register_run_while_the_devhub_is_logged_in(provisioning, monkeypatch, tmp_path):
    orgs_dir = tmp_path / "profiles"
    (orgs_dir / "plain" / "config").mkdir(parents=True)  # no setup.sh, no source to deploy
    monkeypatch.setattr(org, "ORGS_DIR", orgs_dir)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(org, "sf_json_sync", _fake_sf(calls))
    org.create("plain", "fb-new", "azul")
    assert [c[:3] for c in calls] == [
        ("org", "create", "scratch"),
        ("org", "display", "--target-org"),
        ("org", "display", "--target-org"),  # register() verifies again
    ]
    assert org.load_registry() == {"plain": ["fb-new"]}
    org.register("plain", "fb-grader-1")  # the explicit register command
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        check_command(DEPLOY)  # nothing is left allowed once they return


# --------------------------------------------------------------------------- pending orgs expire


@pytest.fixture
def clock(monkeypatch):
    """org._now, moved forward by setting clock["later"] (a timedelta)."""
    start = org._now()
    state = {"later": dt.timedelta(0)}
    monkeypatch.setattr(org, "_now", lambda: start + state["later"])
    return state


def test_a_pending_entry_records_when_it_was_made(sandbox, clock):
    org._set_pending("fb-new", "base")
    [p] = org.pending_orgs()
    assert (p.alias, p.profile, p.created) == ("fb-new", "base", org._now().replace(microsecond=0))
    assert not p.expired()
    assert json.loads(org.PENDING.read_text())["fb-new"]["created"].endswith("+00:00")


def test_a_pending_entry_expires_after_a_day(provisioning, clock):
    """An orgs create whose setup failed or was abandoned must not leave its org usable with
    the Dev Hub logged in (or wipeable as a grader org) for good.
    """
    display = ("org", "display", "--target-org", "fb-new")
    org._set_pending("fb-new", "base")
    clock["later"] = dt.timedelta(hours=23, minutes=59)
    check_command(display)
    assert org.is_grader_org("fb-new", "base")
    clock["later"] = dt.timedelta(hours=24, minutes=1)
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        check_command(display)
    assert not org.is_grader_org("fb-new", "base")
    [p] = org.pending_orgs()
    assert p.expired(), "it stays listed, as expired"


@pytest.mark.parametrize(
    "entry",
    [
        "base",  # the format before creation times were recorded
        {"profile": "base"},
        {"profile": "base", "created": "yesterday"},
        {"profile": "base", "created": "2026-09-28T10:00:00"},  # no time zone
        {"profile": "base", "created": "2999-01-01T00:00:00+00:00"},  # far in the future
    ],
    ids=["old-format", "no-time", "unreadable", "naive", "future"],
)
def test_a_pending_entry_of_unknown_age_is_expired(provisioning, entry):
    org.PENDING.parent.mkdir(parents=True, exist_ok=True)
    org.PENDING.write_text(json.dumps({"fb-new": entry}))
    [p] = org.pending_orgs()
    assert (p.alias, p.profile) == ("fb-new", "base")
    assert p.expired()
    with pytest.raises(OrgError, match="a Dev Hub is logged in"):
        check_command(("org", "display", "--target-org", "fb-new"))


def test_orgs_list_shows_pending_and_expired_entries(sandbox, clock):
    org._set_pending("fb-old", "base")
    clock["later"] = dt.timedelta(days=2)
    org._set_pending("fb-new", "npsp")
    result = CliRunner().invoke(app, ["orgs", "list"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "pending: fb-new (npsp), being set up by orgs create since" in out
    assert "expired: fb-old (base), pending since" in out
    assert "no longer used" in out
    assert "forcebench orgs register base fb-old" in out


def test_orgs_list_shows_pending_entries_even_while_a_devhub_is_logged_in(provisioning):
    """Pending entries exist while provisioning, when listing the registered orgs refuses."""
    org._set_pending("fb-new", "base")
    result = CliRunner().invoke(app, ["orgs", "list"])
    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "pending: fb-new (base)" in out
    assert "a Dev Hub is logged in to the sandbox" in out


def test_create_refuses_names_that_are_not_plain(provisioning):
    with pytest.raises(OrgError, match="not a valid org profile"):
        org.create("../../etc", "fb-new", "azul")
    with pytest.raises(OrgError, match="not a valid org alias"):
        org.create("base", "--target-org=client", "azul")


# --------------------------------------------------------------------------- aliases


@pytest.mark.parametrize("alias", ["-h", "--help", "", "a b", "x;y", "../x", "a\nb", "é", "-o"])
def test_aliases_must_be_plain_names(alias):
    with pytest.raises(OrgError, match="not a valid org alias"):
        org.check_alias(alias)


@pytest.mark.parametrize("alias", ["fb-grader-1", "fb_npsp.2", "A1", "devhub"])
def test_plain_aliases_are_accepted(alias):
    assert org.check_alias(alias) == alias


def test_the_check_prints_its_confirmation_only_when_it_passes(sandbox, monkeypatch, capsys):
    _store(sandbox, {"grader@example.com": SCRATCH}, {"fb-grader-1": "grader@example.com"})
    monkeypatch.setattr(org, "verify_scratch", lambda a: {"instanceUrl": SCRATCH})
    assert org.main(["check", "--", "fb-grader-1"]) == 0
    assert capsys.readouterr().out == "FORCEBENCH_ORG_LOCK_OK fb-grader-1\n"
    org.save_registry({"base": ["fb-grader-1"]})
    assert org.main(["check", "--profile", "base", "--", "fb-grader-1"]) == 0
    assert capsys.readouterr().out == "FORCEBENCH_ORG_LOCK_OK fb-grader-1 base\n"
    for argv in (["check", "--", "-h"], ["check", "--", "client-prod"]):
        assert org.main(argv) == 1
        out = capsys.readouterr()
        assert out.out == ""
        assert "refusing to touch" in out.err


# --------------------------------------------------------------------------- auth URLs

# Not a real token. Assembled at runtime so secret scanners don't mistake it for one.
FAKE_REFRESH = "5Aep" + "861" + "NotARealRefreshTokenForTests" + "0" * 24 + ".x"
GOOD_URL = f"force://PlatformCLI::{FAKE_REFRESH}@fun-1234-dev-ed.scratch.my.salesforce.com"
TOKEN = f"force://PlatformCLI::{FAKE_REFRESH[:32]}"


def test_auth_url_of_a_scratch_org_is_accepted():
    assert org.auth_url_host(GOOD_URL) == "fun-1234-dev-ed.scratch.my.salesforce.com"
    assert (
        org.auth_url_host(f"{TOKEN}@X.Scratch.My.Salesforce.com") == "x.scratch.my.salesforce.com"
    )
    assert org.auth_url_host("force://id:secret==:tok=@x.scratch.my.salesforce.com")


@pytest.mark.parametrize(
    "url",
    [
        # the review's case: the CLI stops at the first '@' host and logs in to client.my...
        f"{TOKEN}@client.my.salesforce.com@x.scratch.my.salesforce.com",
        f"{TOKEN}@x.scratch.my.salesforce.com@client.my.salesforce.com",
        "force://PlatformCLI::tok@en@x.scratch.my.salesforce.com",
        f"{TOKEN}@client.my.salesforce.com",
        f"{TOKEN}@scratch.my.salesforce.com",
        f"{TOKEN}@x.scratch.my.salesforce.com.evil.com",
        f"{TOKEN}@evil.com/x.scratch.my.salesforce.com",
        f"{TOKEN}@evil.com#.scratch.my.salesforce.com",
        f"{TOKEN}@evil.com?.scratch.my.salesforce.com",
        f"{TOKEN}@evil.com\\.scratch.my.salesforce.com",
        f"{TOKEN}@x.scratch.my.salesforce.com:443",
        f"{TOKEN}@x.scratch.my.salesforce.com/",
        f"{TOKEN}@x.scratch.my.salesforce.com.",
        f"{TOKEN}@https://x.scratch.my.salesforce.com",
        f"{TOKEN}@x.scratch.my.salesforce.com\nforce://a::b",
        f"{TOKEN}@x.scratch.my.salesforce.com x",
        f"{TOKEN}@x.scr\u0430tch.my.salesforce.com",  # Cyrillic a
        f"{TOKEN}@\u212a.scratch.my.salesforce.com",  # Kelvin sign, matches k ignoring case
        f"{TOKEN}@-x.scratch.my.salesforce.com",
        f"{TOKEN}@",
        "force://PlatformCLI:tok@x.scratch.my.salesforce.com",  # two fields
        "force://a:b:c:d@x.scratch.my.salesforce.com",
        "force://a::t!k@x.scratch.my.salesforce.com",
        "https://x.scratch.my.salesforce.com",
        "FORCE://PlatformCLI::tok@x.scratch.my.salesforce.com",
        '{"sfdxAuthUrl": "force://PlatformCLI::tok@client.my.salesforce.com"}',
        "",
    ],
)
def test_adversarial_auth_urls_are_refused(url):
    with pytest.raises(OrgError, match="refusing to import"):
        org.auth_url_host(url)


def test_import_logs_in_with_a_private_copy_of_the_checked_url(sandbox, monkeypatch, tmp_path):
    src = tmp_path / "base__fb-grader-2.url"
    src.write_text(f"  {GOOD_URL}\n\n")
    seen: dict = {}

    def fake_sf(*args, cwd=None):
        path = Path(args[args.index("--sfdx-url-file") + 1])
        seen.update(args=args, path=path, text=path.read_text(), mode=path.stat().st_mode & 0o777)
        return {"status": 0}

    monkeypatch.setattr(org, "sf_json_sync", fake_sf)
    monkeypatch.setattr(org, "register", lambda profile, alias: seen.update(registered=alias))
    org.import_auth("base", "fb-grader-2", src)
    assert seen["path"] != src, "a temporary copy, removed after"
    assert not seen["path"].exists(), "a temporary copy, removed after"
    assert (seen["text"], seen["mode"]) == (GOOD_URL + "\n", 0o600)
    assert seen["args"][-2:] == ("--alias", "fb-grader-2")
    assert seen["registered"] == "fb-grader-2"


def test_import_runs_nothing_for_a_refused_url_or_alias(sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(org, "sf_json_sync", lambda *a, **k: pytest.fail("sf ran"))
    bad = tmp_path / "bad.url"
    bad.write_text(f"{TOKEN}@client.my.salesforce.com@x.scratch.my.salesforce.com")
    with pytest.raises(OrgError, match="exactly one '@'"):
        org.import_auth("base", "fb-grader-2", bad)
    good = tmp_path / "good.url"
    good.write_text(GOOD_URL)
    with pytest.raises(OrgError, match="not a valid org alias"):
        org.import_auth("base", "--target-org=client-prod", good)


def test_scratch_instance_urls_are_parsed_strictly():
    assert org.is_scratch_url(SCRATCH)
    assert org.is_scratch_url(SCRATCH + "/")
    for url in (
        "http://fun-1234-dev-ed.scratch.my.salesforce.com",
        "https://client.my.salesforce.com@fun.scratch.my.salesforce.com",
        "https://fun.scratch.my.salesforce.com:8443",
        "https://fun.scratch.my.salesforce.com:bad",
        "fun.scratch.my.salesforce.com",
    ):
        assert not org.is_scratch_url(url), url


def test_grading_commands_stop_with_a_clear_message_while_a_devhub_is_logged_in(provisioning):
    for argv in (["validate", "--suite", "apex"], ["orgs", "list"]):
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 1, argv
        assert "a Dev Hub is logged in to the sandbox" in result.output
