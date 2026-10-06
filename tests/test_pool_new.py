"""Starting a private task from the template (forcebench private new).

The public suites here are the small alpha and beta fixtures: a real suite's numbered ids
(<suite>-p001, ...) are real private task ids, which leakcheck rightly refuses to see in this
repository.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from forcebench.cli import app
from forcebench.pool import init_private_dir, load_private_pool
from forcebench.tasks import EVERY_STATUS, all_tasks, load_suites


PUBLIC_FIXTURE = Path(__file__).parent / "fixtures" / "report" / "suites"


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr("forcebench.tasks.SUITES_DIR", PUBLIC_FIXTURE)
    monkeypatch.setattr("forcebench.tasks._manifest_ids", lambda: {"alpha-removed"})
    root = tmp_path / "pool"
    root.mkdir()
    init_private_dir(root)
    monkeypatch.setenv("FORCEBENCH_PRIVATE_DIR", str(root))
    monkeypatch.setattr("forcebench.models.load_dotenv", lambda *a, **k: None)
    return root


def test_a_new_task_is_a_loadable_draft_with_the_pools_canary(pool):
    result = CliRunner().invoke(app, ["private", "new", "alpha-fresh-question", "--author", "leo"])
    assert result.exit_code == 0, result.output
    loaded = load_private_pool(pool)
    [task] = all_tasks(load_suites(pool="private", private=loaded, statuses=EVERY_STATUS))
    assert (task.id, task.suite, task.status, task.tier) == (
        "alpha-fresh-question",
        "alpha",
        "draft",
        "private",
    )
    assert loaded.canary_guid in task.canary
    assert task.authors == ["leo"]
    assert loaded.exposure["alpha-fresh-question"] == []
    assert all_tasks(load_suites(pool="private", private=loaded)) == [], "a draft is never run"
    checked = CliRunner().invoke(app, ["validate", "--pool", "private", "--no-org"])
    assert checked.exit_code == 0, checked.output


def test_a_new_task_needs_a_known_suite_and_a_fresh_id(pool):
    assert CliRunner().invoke(app, ["private", "new", "nosuch-question"]).exit_code == 2
    assert CliRunner().invoke(app, ["private", "new", "alpha-twice"]).exit_code == 0
    again = CliRunner().invoke(app, ["private", "new", "alpha-twice"])
    assert again.exit_code == 1
    assert "already exists" in again.output


def test_without_an_id_the_suite_gets_its_next_numbered_one(pool):
    for _ in range(2):
        made = CliRunner().invoke(
            app, ["private", "new", "--suite", "beta", "--difficulty", "hard"]
        )
        assert made.exit_code == 0, made.output
    semi = CliRunner().invoke(app, ["private", "new", "--suite", "beta", "--tier", "semi-private"])
    assert semi.exit_code == 0, semi.output
    loaded = load_private_pool(pool)
    tasks = all_tasks(load_suites(pool="private", private=loaded, statuses=EVERY_STATUS))
    assert [(t.id, t.difficulty, t.tier) for t in tasks] == [
        ("beta-p001", "hard", "private"),
        ("beta-p002", "hard", "private"),
        ("beta-p003", "medium", "semi-private"),
    ]
    assert set(loaded.exposure) == {"beta-p001", "beta-p002", "beta-p003"}


def test_a_new_task_needs_an_id_or_a_suite_and_known_values(pool):
    assert CliRunner().invoke(app, ["private", "new"]).exit_code == 2
    bad = ["--suite", "beta", "--difficulty", "brutal"], ["--suite", "beta", "--tier", "public"]
    for args in bad:
        assert CliRunner().invoke(app, ["private", "new", *args]).exit_code == 2


def test_a_public_id_is_refused(pool):
    public = all_tasks(load_suites(["alpha"]))[0].id
    result = CliRunner().invoke(app, ["private", "new", public])
    assert result.exit_code == 1
    assert "public task id" in result.output
    assert not list((pool / "suites").glob("*/tasks/*.yaml"))


def test_a_numbered_id_a_past_run_used_is_never_given_out_again(pool):
    run = pool / "results" / "runs" / "20261005T000000Z_some-model@low"
    run.mkdir(parents=True)
    (run / "run.json").write_text('{"task_ids": ["beta-p001", "beta-p004"]}')
    assert CliRunner().invoke(app, ["private", "new", "--suite", "beta"]).exit_code == 0
    assert (pool / "suites" / "beta" / "tasks" / "beta-p005.yaml").exists()


def test_an_id_another_suite_has_is_refused(pool):
    assert CliRunner().invoke(app, ["private", "new", "alpha-shared-name"]).exit_code == 0
    again = CliRunner().invoke(app, ["private", "new", "alpha-shared-name", "--suite", "beta"])
    assert again.exit_code == 1
    assert "already exists" in again.output


def test_a_pool_it_cannot_write_is_reported_without_its_path(pool):
    tasks = pool / "suites" / "beta" / "tasks"
    tasks.mkdir(parents=True)
    tasks.chmod(0o500)
    try:
        result = CliRunner().invoke(app, ["private", "new", "--suite", "beta"])
    finally:
        tasks.chmod(0o700)
    assert result.exit_code == 1
    assert "could not read or write the private pool" in result.output
    assert str(pool) not in result.output
