"""The reasoning-traces dataset: built from public runs only, pushed only to a private or gated
dataset. Runs on a copy of the report fixture, with made-up raw replies."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from forcebench import CANARY, PACKAGE_DIR
from forcebench.answers import render_prompt
from forcebench.cli import app
from forcebench.llm import Generation
from forcebench.report import RunDataError
from forcebench.tasks import load_suite
from forcebench.traces import build
from forcebench.traces_push import PushError, check_folder, push, repo_state

FIXTURE = Path(__file__).parent / "fixtures" / "report"
RUN = "20260928T010000Z_model-a@low"
CARD = (PACKAGE_DIR / "data" / "traces-card.md").read_text()
TERMS = (PACKAGE_DIR / "data" / "traces-terms.md").read_text()


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "report"
    shutil.copytree(FIXTURE, root)
    cases = [
        json.loads(x)
        for x in (root / "results" / "runs" / RUN / "cases.jsonl").read_text().splitlines()
    ]
    raw = root / "results" / "runs" / RUN / "raw"
    raw.mkdir()
    lines = []
    for c in cases:
        gen = Generation(text=c["output"], reasoning="I think it is x.", finish_reason="stop")
        lines.append(
            json.dumps(
                {
                    "key": f"{c['task_id']}#{c['sample']}",
                    "generation": gen.model_dump(),
                    "prompt_sha": c["prompt_sha"],
                }
            )
        )
    (raw / "generations.jsonl").write_text("\n".join(lines) + "\n")
    return root


def _suites(root: Path):
    return [load_suite(d) for d in sorted((root / "suites").iterdir())]


def _records(out: Path) -> list[dict]:
    return [
        json.loads(x)
        for p in sorted((out / "data").rglob("*.jsonl"))
        for x in p.read_text().splitlines()
    ]


def test_the_dataset_holds_public_runs_graded_answers_with_their_canary(tree, tmp_path):
    out = tmp_path / "dataset"
    summary = build(_suites(tree), tree / "results", out, CARD, TERMS)
    records = _records(out)
    assert summary["runs"] == {RUN: len(records)} and records, "only the run with raw replies"
    rec = records[0]
    assert rec["canary"] == CANARY and rec["reasoning"] == "I think it is x."
    assert not {"provider", "endpoint_model", "request"} & set(rec), "no provider or endpoint"
    task = next(t for s in _suites(tree) for t in s.tasks if t.id == rec["task_id"])
    assert rec["prompt"] == render_prompt(task)
    card = (out / "README.md").read_text()
    assert "@@" not in card and CANARY in card and "extra_gated_fields" in card
    assert "https://forcebench.ai/privacy/" in card
    assert "DRAFT, pending legal review" in (out / "LICENSE.md").read_text()


def test_a_private_run_in_the_results_refuses_the_whole_build(tree, tmp_path):
    meta_path = tree / "results" / "runs" / RUN / "run.json"
    meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "visibility": "private"}))
    with pytest.raises(RunDataError, match="refusing to publish anything"):
        build(_suites(tree), tree / "results", tmp_path / "dataset", CARD, TERMS)
    assert not (tmp_path / "dataset" / "data").exists() or not _records(tmp_path / "dataset")


@pytest.fixture
def built(tree, tmp_path):
    out = tmp_path / "dataset"
    build(_suites(tree), tree / "results", out, CARD, TERMS)
    public = {t.id for s in _suites(tree) for t in s.tasks}
    return out, public


def test_push_checks_every_file_and_record(built):
    out, public = built
    assert check_folder(out, public) == len(_records(out))
    path = next((out / "data").rglob("*.jsonl"))
    good = path.read_text()
    for change, why in (
        (lambda r: r.pop("canary"), "canary"),
        (lambda r: r.update(task_id="apex-not-public"), "public task"),
        (lambda r: r.update(provider="somewhere"), "provider"),
    ):
        rec = json.loads(good.splitlines()[0])
        change(rec)
        path.write_text(json.dumps(rec) + "\n")
        with pytest.raises(PushError, match=why):
            check_folder(out, public)
    path.write_text(good)
    (out / "notes.txt").write_text("x")
    with pytest.raises(PushError, match="not part of the dataset"):
        check_folder(out, public)


class FakeHub:
    def __init__(self, private=False, gated=False):
        self.info = SimpleNamespace(private=private, gated=gated)
        self.uploads: list[dict] = []

    def dataset_info(self, repo_id):
        return self.info

    def upload_folder(self, **kwargs):
        self.uploads.append(kwargs)


@pytest.mark.parametrize(
    ("hub", "allowed", "state"),
    [
        (FakeHub(private=True), True, "private"),
        (FakeHub(gated="manual"), True, "public, gated (manual approval)"),
        (FakeHub(), False, "public and not gated"),
    ],
)
def test_only_a_private_or_gated_dataset_receives_the_traces(built, hub, allowed, state):
    out, public = built
    assert repo_state(hub, "someone/traces") == (allowed, state)
    if allowed:
        assert push(out, "someone/traces", public, hub, "msg") > 0 and hub.uploads
    else:
        with pytest.raises(PushError, match="not gated"):
            push(out, "someone/traces", public, hub, "msg")
        assert hub.uploads == [], "nothing was uploaded"


def test_the_push_command_needs_its_settings_and_never_prints_the_token(built, monkeypatch):
    out, _ = built
    monkeypatch.setattr("forcebench.models.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HF_DATASET_REPO", raising=False)
    assert CliRunner().invoke(app, ["traces", "push", "--dir", str(out)]).exit_code == 1
    monkeypatch.setenv("HF_TOKEN", "hf_secret_token_value")
    monkeypatch.setenv("HF_DATASET_REPO", "someone/traces")
    hub = FakeHub()
    monkeypatch.setattr("forcebench.traces_push.hub", lambda token: hub)
    refused = CliRunner().invoke(app, ["traces", "push", "--dir", str(out), "--yes"])
    assert refused.exit_code == 1 and "public and not gated" in refused.output
    assert "hf_secret_token_value" not in refused.output and hub.uploads == []
