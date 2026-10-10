"""Private providers and configurations (docs/private-pool.md).

They are loaded from the private pool's models/ only where FORCEBENCH_PRIVATE_DIR is set, marked
private, their runs kept only in the pool's results/runs, refused by the public report, and named
nowhere in this repository (leakcheck).

The made-up names are put together at run time: spelt out, they would be found in this very file
when the repository is checked against the made-up pool.
"""

import asyncio
import json
import subprocess

import pytest
import yaml

from forcebench import models, report, runner
from forcebench.leakcheck import check
from forcebench.leakcheck.private import denylist, nothing_from_the_private_pool
from forcebench.pool import init_private_dir
from forcebench.private_backup import collect
from forcebench.runner import RunDirError, check_private_config


PROVIDER = "hush" + "net"
LABEL = "Hush" + "net relay"
CONFIG = "hush" + "-model-1"
MODEL_ID = "hush" + "-model"
URL_ENV = "FORCEBENCH_TEST_HUSH_BASE_URL"
ADDRESS = "https://relay." + "hush-example.test/v1"


def _private_files(root, *, provider=PROVIDER, config=CONFIG) -> None:
    """A private provider and one configuration of it, copied from a public one."""
    base = models.load_registry(private_dir=None).get("claude-haiku-4.5").model_dump()
    entry = {**base, "id": config, "provider": provider, "model_id": MODEL_ID, "display": "Hush 1"}
    entry.pop("private")
    folder = root / "models"
    folder.mkdir(exist_ok=True)
    (folder / "providers.yaml").write_text(
        yaml.safe_dump(
            {provider: {"kind": "openai_compatible", "label": LABEL, "base_url_env": URL_ENV}}
        )
    )
    (folder / "hush.yaml").write_text(yaml.safe_dump({"models": [entry]}))


@pytest.fixture
def pool(tmp_path, monkeypatch):
    root = tmp_path / "pool"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    made = init_private_dir(root)
    _private_files(made.root)
    monkeypatch.setattr("forcebench.models.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("FORCEBENCH_PRIVATE_DIR", str(root))
    monkeypatch.setenv(URL_ENV, ADDRESS)
    denylist.cache_clear()
    yield made
    denylist.cache_clear()


def test_the_private_pools_providers_and_configurations_are_loaded_and_marked_private(pool):
    reg = models.load_registry()
    m = reg.get(CONFIG)
    assert m.private
    assert reg.provider_for(m).private
    assert m.public_dict()["private"] is True
    public = [x for x in reg.models.values() if x.id != CONFIG]
    assert public
    assert not any(x.private for x in public)
    assert "private" not in public[0].public_dict(), (
        "a public configuration's run.json is unchanged"
    )


def test_without_the_private_pool_nothing_changes(pool, monkeypatch):
    monkeypatch.delenv("FORCEBENCH_PRIVATE_DIR")
    reg = models.load_registry()
    assert CONFIG not in reg.models
    assert PROVIDER not in reg.providers
    assert reg.models.keys() == models.load_registry(private_dir=None).models.keys()


def test_a_private_name_clashing_with_a_public_one_is_refused(pool):
    _private_files(pool.root, config="claude-haiku-4.5")
    with pytest.raises(ValueError, match=r"duplicate the private configuration claude-haiku-4\.5"):
        models.load_registry()
    _private_files(pool.root, provider="anthropic")
    with pytest.raises(
        ValueError, match="the private provider 'anthropic' has the name of a public one"
    ):
        models.load_registry()


def test_a_run_of_a_private_configuration_is_kept_only_in_the_private_pool(pool, tmp_path):
    reg = models.load_registry()
    m = reg.get(CONFIG)
    public_run = tmp_path / "results" / "runs" / runner.run_id_for(m, m.default_effort)
    with pytest.raises(RunDirError, match="kept only in the private pool's results/runs"):
        check_private_config(m, reg.provider_for(m), public_run)
    check_private_config(m, reg.provider_for(m), pool.runs_dir / "anything")
    # Generation refuses before the run directory exists, whatever the tasks.
    with pytest.raises(RunDirError, match="kept only in the private pool's results/runs"):
        asyncio.run(runner.generate(reg, CONFIG, None, [], run_dir=public_run))
    assert not public_run.exists()
    # A public configuration is not affected.
    haiku = reg.get("claude-haiku-4.5")
    check_private_config(haiku, reg.provider_for(haiku), public_run)


def test_the_public_report_refuses_a_run_of_a_private_configuration():
    meta = {"visibility": "public", "model": {"id": CONFIG, "private": True}}
    assert report._foreign(meta, [], "public", None) == "it is a run of a private configuration"
    assert report._foreign({**meta, "model": {"id": "x"}}, [], "public", None) is None


def test_the_backup_takes_a_private_configurations_runs_along(pool):
    run = pool.runs_dir / "20261010T000000Z_hush-model-1@high"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"visibility": "public", "model": {"private": True}}))
    (run / "cases.jsonl").write_text("{}\n")
    assert collect(pool).runs == [run.name]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"run {CONFIG}@high", "a private configuration's id"),
        (f"the {MODEL_ID} family", "a private configuration's model id"),
        (f"  provider: {PROVIDER}\n", "a private provider"),
        (f'{{"provider": "{PROVIDER}"}}', "a private provider"),
        (f"served by {LABEL}", "a private provider's name"),
        (f"BASE={ADDRESS.upper()}", "a private provider's address"),
    ],
)
def test_leakcheck_finds_the_private_names_in_public_files(pool, text, kind):
    found = [f.detail for f in check([("docs/x.md", text)], [nothing_from_the_private_pool])]
    assert f"names {kind}" in found
