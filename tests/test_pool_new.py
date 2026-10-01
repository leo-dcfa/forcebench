"""Starting a private task from the template (forcebench private new)."""

import pytest
from typer.testing import CliRunner

from forcebench.cli import app
from forcebench.pool import init_private_dir, load_private_pool
from forcebench.tasks import EVERY_STATUS, all_tasks, load_suites


@pytest.fixture
def pool(tmp_path, monkeypatch):
    root = tmp_path / "pool"
    root.mkdir()
    init_private_dir(root)
    monkeypatch.setenv("FORCEBENCH_PRIVATE_DIR", str(root))
    monkeypatch.setattr("forcebench.models.load_dotenv", lambda *a, **k: None)
    return root


def test_a_new_task_is_a_loadable_draft_with_the_pools_canary(pool):
    result = CliRunner().invoke(app, ["private", "new", "docs-fresh-question", "--author", "leo"])
    assert result.exit_code == 0, result.output
    loaded = load_private_pool(pool)
    [task] = all_tasks(load_suites(pool="private", private=loaded, statuses=EVERY_STATUS))
    assert (task.id, task.suite, task.status, task.tier) == (
        "docs-fresh-question",
        "docs",
        "draft",
        "private",
    )
    assert loaded.canary_guid in task.canary and task.authors == ["leo"]
    assert loaded.exposure["docs-fresh-question"] == []
    assert all_tasks(load_suites(pool="private", private=loaded)) == [], "a draft is never run"
    checked = CliRunner().invoke(app, ["validate", "--pool", "private", "--no-org"])
    assert checked.exit_code == 0, checked.output


def test_a_new_task_needs_a_known_suite_and_a_fresh_id(pool):
    assert CliRunner().invoke(app, ["private", "new", "nosuch-question"]).exit_code == 2
    assert CliRunner().invoke(app, ["private", "new", "docs-twice"]).exit_code == 0
    again = CliRunner().invoke(app, ["private", "new", "docs-twice"])
    assert again.exit_code == 1 and "already exists" in again.output
