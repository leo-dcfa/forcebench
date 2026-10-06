"""The reasoning-traces dataset: built from public runs only, pushed only to a private or gated
dataset. Runs on a copy of the report fixture, with made-up raw replies.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
import yaml
from huggingface_hub.errors import HfHubHTTPError
from typer.testing import CliRunner

from forcebench import CANARY, PACKAGE_DIR
from forcebench.answers import prompt_sha
from forcebench.cli import app
from forcebench.llm import Generation
from forcebench.report import RunDataError
from forcebench.tasks import load_suite
from forcebench.traces import build, published_replies, reply_digest
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
    summary = build(_suites(tree), tree / "results", out, CARD, TERMS, set())
    records = _records(out)
    assert summary["runs"] == {RUN: len(records)} and records, "only the run with raw replies"
    rec = records[0]
    assert rec["canary"] == CANARY and rec["reasoning"] == "I think it is x."
    assert not {"provider", "endpoint_model", "request"} & set(rec), "no provider or endpoint"
    cases = (tree / "results" / "runs" / RUN / "cases.jsonl").read_text().splitlines()
    case = next(c for c in map(json.loads, cases) if c["task_id"] == rec["task_id"])
    assert "prompt" not in rec and rec["prompt_sha"] == case["prompt_sha"], "the hash, no prompt"
    assert rec["open_material"] is False and rec["hosted_model_output"] is False
    card = (out / "README.md").read_text()
    assert "@@" not in card and CANARY in card and "extra_gated_fields" in card
    assert "https://forcebench.ai/privacy/" in card
    terms = (out / "LICENSE.md").read_text()
    assert "Azul Labs Pty Ltd (ACN 662 440 913)" in terms and "DRAFT" not in terms


def test_a_private_run_in_the_results_refuses_the_whole_build(tree, tmp_path):
    meta_path = tree / "results" / "runs" / RUN / "run.json"
    meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "visibility": "private"}))
    with pytest.raises(RunDataError, match="refusing to publish anything"):
        build(_suites(tree), tree / "results", tmp_path / "dataset", CARD, TERMS, set())
    assert not (tmp_path / "dataset" / "data").exists() or not _records(tmp_path / "dataset")


@pytest.fixture
def built(tree, tmp_path):
    out = tmp_path / "dataset"
    build(_suites(tree), tree / "results", out, CARD, TERMS, set())
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
        (lambda r: r.update(prompt="the task"), "reproduces a task's prompt"),
        (lambda r: r.pop("open_material"), "lacks open_material"),
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
    def __init__(self, private: bool = False, gated: bool | str = False, status: int = 200):
        self.info = SimpleNamespace(private=private, gated=gated)
        self.status = status
        self.refuse = False
        self.uploads: list[dict] = []

    def dataset_info(self, repo_id):
        if self.status != 200:
            response = httpx2.Response(self.status, request=httpx2.Request("GET", "https://hub"))
            raise HfHubHTTPError("Client Error", response=response)
        return self.info

    def upload_folder(self, **kwargs):
        if self.refuse:
            raise ValueError("Invalid metadata in README.md.")
        self.uploads.append(kwargs)


@pytest.mark.parametrize(
    ("hub", "allowed", "state"),
    [
        (FakeHub(private=True), True, "private"),
        (FakeHub(gated="manual"), True, "public, gated (manual approval)"),
        (FakeHub(gated=True), True, "public, gated"),
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


@pytest.mark.parametrize("status", [401, 404])
def test_a_dataset_the_hub_will_not_describe_refuses_the_push(built, status):
    out, public = built
    hub = FakeHub(private=True, status=status)
    with pytest.raises(PushError, match=f"would not describe someone/traces \\({status}\\)"):
        push(out, "someone/traces", public, hub, "msg")
    assert hub.uploads == []


def test_without_the_traces_extra_the_push_says_how_to_get_it(built, monkeypatch):
    out, _ = built
    monkeypatch.setitem(sys.modules, "forcebench.traces_push", None)
    result = CliRunner().invoke(app, ["traces", "push", "--dir", str(out)])
    assert result.exit_code == 1 and "--extra traces" in result.output


def _keys(node) -> list[str]:
    if isinstance(node, dict):
        return [k for k, v in node.items() for k in (k, *_keys(v))]
    if isinstance(node, list):
        return [k for v in node for k in _keys(v)]
    return []


def test_the_card_metadata_is_what_hugging_face_accepts():
    """Hugging Face rejects the whole upload if any metadata key holds a dot or a dollar sign."""
    header = yaml.safe_load(CARD.split("---\n")[1])
    assert header["extra_gated_fields"] and header["license"] == "other"
    assert not [k for k in _keys(header) if "." in str(k) or "$" in str(k)]


def test_an_upload_the_hub_refuses_is_reported_not_raised(built):
    out, public = built
    hub = FakeHub(private=True)
    hub.refuse = True
    with pytest.raises(PushError, match="nothing was committed: Invalid metadata"):
        push(out, "someone/traces", public, hub, "msg")


def test_an_answer_recorded_without_its_prompt_hash_gets_the_tasks(tree, tmp_path):
    path = tree / "results" / "runs" / RUN / "cases.jsonl"
    cases = [json.loads(x) for x in path.read_text().splitlines()]
    path.write_text("".join(json.dumps({**c, "prompt_sha": None}) + "\n" for c in cases))
    raw = tree / "results" / "runs" / RUN / "raw" / "generations.jsonl"
    lines = [json.loads(x) for x in raw.read_text().splitlines()]
    raw.write_text("".join(json.dumps({**x, "prompt_sha": None}) + "\n" for x in lines))
    out = tmp_path / "dataset"
    build(_suites(tree), tree / "results", out, CARD, TERMS, set())
    tasks = {t.id: t for s in _suites(tree) for t in s.tasks}
    records = _records(out)
    assert records and all(r["prompt_sha"] == prompt_sha(tasks[r["task_id"]]) for r in records)


def test_a_reply_once_committed_to_the_repository_is_open_material(tree, tmp_path):
    raw = tree / "results" / "runs" / RUN / "raw" / "generations.jsonl"
    first = json.loads(raw.read_text().splitlines()[0])
    gen = Generation.model_validate(first["generation"])
    out = tmp_path / "dataset"
    summary = build(
        _suites(tree), tree / "results", out, CARD, TERMS, {reply_digest(RUN, first["key"], gen)}
    )
    flagged = [r for r in _records(out) if r["open_material"]]
    assert summary["open_material"] == 1 and len(flagged) == 1
    assert f"{flagged[0]['task_id']}#{flagged[0]['sample']}" == first["key"]
    elsewhere = {reply_digest("20260901T000000Z_other@low", first["key"], gen)}
    summary = build(_suites(tree), tree / "results", out, CARD, TERMS, elsewhere)
    assert summary["open_material"] == 0, "the same reply published by another run is not this one"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_published_replies_reads_every_version_ever_committed(tmp_path):
    repo = tmp_path / "repo"
    raw = repo / "results" / "runs" / RUN / "raw" / "generations.jsonl"
    raw.parent.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    replies = [Generation(text=t, reasoning="why", finish_reason="stop") for t in ("a", "b")]
    for gen in replies:  # two versions of the file, then it is untracked
        raw.write_text(json.dumps({"key": "t#0", "generation": gen.model_dump()}) + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", gen.text)
    _git(repo, "rm", "-q", "--cached", str(raw))
    _git(repo, "commit", "-q", "-m", "untrack")
    assert published_replies(repo) == {reply_digest(RUN, "t#0", g) for g in replies}
