"""Backing up private runs: only private runs of this pool, only to a private dataset. Runs on a
made-up pool in a temporary directory; the dataset's name is put together at run time."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from forcebench.cli import app
from forcebench.pool import init_private_dir, load_private_pool
from forcebench.private_backup import BackupError, collect, push

DATASET = "someone/" + "held-out" + "-traces"
RUN = "20261005T000000Z_model-a@low"


@pytest.fixture
def pool(tmp_path, monkeypatch):
    root = tmp_path / "pool"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    made = init_private_dir(root)
    run = made.runs_dir / RUN
    (run / "raw").mkdir(parents=True)
    (run / "artifacts").mkdir()
    meta = {"run_id": RUN, "visibility": "private", "canary": made.canary}
    (run / "run.json").write_text(json.dumps(meta))
    (run / "cases.jsonl").write_text('{"task_id": "x"}\n')
    (run / "raw" / "generations.jsonl").write_text('{"key": "x#0"}\n')
    (run / "artifacts" / "grading.json").write_text("{}")
    (run / ".lock").write_text("")
    monkeypatch.setattr("forcebench.models.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("FORCEBENCH_PRIVATE_DIR", str(root))
    return made


def test_a_backup_holds_each_runs_metadata_cases_and_full_replies_only(pool):
    backup = collect(pool)
    assert backup.runs == [RUN]
    assert sorted(backup.files) == [
        f"runs/{RUN}/cases.jsonl",
        f"runs/{RUN}/raw/generations.jsonl",
        f"runs/{RUN}/run.json",
    ], "no grading artifacts, no locks"


@pytest.mark.parametrize(
    "change",
    [{"visibility": "public"}, {"canary": "another pool's canary"}, {"visibility": None}],
)
def test_a_run_that_is_not_this_pools_private_run_refuses_the_backup(pool, change):
    path = pool.runs_dir / RUN / "run.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **change}))
    with pytest.raises(BackupError, match="not a private run of this pool"):
        collect(pool)


def test_a_symbolic_link_or_an_empty_pool_refuses_the_backup(pool, tmp_path):
    raw = pool.runs_dir / RUN / "raw" / "generations.jsonl"
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("{}\n")
    raw.unlink()
    raw.symlink_to(elsewhere)
    with pytest.raises(BackupError, match="symbolic link"):
        collect(pool)
    subprocess.run(["rm", "-r", str(pool.runs_dir / RUN)], check=True)
    with pytest.raises(BackupError, match="no runs"):
        collect(pool)


class FakeHub:
    def __init__(self, private: bool, gated: bool | str = False):
        self.info = SimpleNamespace(private=private, gated=gated)
        self.commits: list[list[str]] = []

    def dataset_info(self, repo_id):
        return self.info

    def create_commit(self, repo_id, operations, *, commit_message, repo_type):
        self.commits.append(sorted(op.path_in_repo for op in operations))


@pytest.mark.parametrize(
    ("hub", "refusal"),
    [
        (FakeHub(private=False), "the dataset is public"),
        (FakeHub(private=False, gated="manual"), "public, even if gated"),
    ],
)
def test_only_a_private_dataset_receives_private_runs(pool, hub, refusal):
    with pytest.raises(BackupError, match=refusal):
        push(hub, DATASET, collect(pool), None)
    assert hub.commits == []


def test_the_public_traces_dataset_never_receives_private_runs(pool):
    hub = FakeHub(private=True)
    with pytest.raises(BackupError, match="public traces dataset"):
        push(hub, DATASET, collect(pool), DATASET.upper())
    assert hub.commits == []


def test_the_command_uploads_to_the_dataset_named_in_pool_yaml(pool, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret_token_value")
    missing = CliRunner().invoke(app, ["private", "backup", "--yes"])
    assert missing.exit_code == 1 and "hf_dataset" in missing.output
    (pool.root / "pool.yaml").write_text(
        (pool.root / "pool.yaml").read_text() + f"hf_dataset: {DATASET}\n"
    )
    assert load_private_pool(pool.root).hf_dataset == DATASET
    hub = FakeHub(private=True)
    monkeypatch.setattr("forcebench.private_backup.hub", lambda token: hub)
    done = CliRunner().invoke(app, ["private", "backup", "--yes"])
    assert done.exit_code == 0, done.output
    assert hub.commits == [sorted(collect(pool).files)]
    assert "hf_secret_token_value" not in done.output and DATASET not in done.output
