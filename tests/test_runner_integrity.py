"""Run bookkeeping that the leaderboard's integrity rests on: the generations store, resuming,
task versions and per-case retry counts."""

import json

import pytest

from forcebench.llm import Generation
from forcebench.runner import CorruptStoreError, GenerationStore, invalidate, read_records


def _line(key: str, **gen) -> str:
    return json.dumps({"key": key, "generation": Generation(**gen).model_dump()}) + "\n"


@pytest.fixture
def raw(tmp_path):
    path = tmp_path / "run" / "raw" / "generations.jsonl"
    path.parent.mkdir(parents=True)
    return path


# --------------------------------------------------------------------------- store


def test_torn_last_line_is_skipped_with_a_warning(raw):
    raw.write_text(_line("a#0", text="A") + _line("b#0", text="B")[:25])
    with pytest.warns(RuntimeWarning, match="torn last line"):
        store = GenerationStore(raw)
    assert set(store.done) == {"a#0"}, "the torn answer is generated again on resume"


def test_corrupt_line_in_the_middle_fails_loudly(raw):
    raw.write_text(_line("a#0", text="A") + '{"key": "b#0", "gen\n' + _line("c#0", text="C"))
    with pytest.raises(CorruptStoreError, match="line 2"):
        GenerationStore(raw)
    with pytest.raises(CorruptStoreError):
        read_records(raw)


@pytest.mark.asyncio
async def test_append_after_a_torn_line_drops_the_fragment(raw):
    raw.write_text(_line("a#0", text="A") + _line("b#0", text="B")[:25])
    with pytest.warns(RuntimeWarning):
        store = GenerationStore(raw)
    await store.add("b#0", Generation(text="B again"))
    assert raw.read_text() == _line("a#0", text="A") + _line("b#0", text="B again")
    assert GenerationStore(raw).done["b#0"].text == "B again"


@pytest.mark.asyncio
async def test_append_after_a_final_line_without_newline_starts_a_new_line(raw):
    raw.write_text(_line("a#0", text="A").rstrip("\n"))
    store = GenerationStore(raw)
    await store.add("b#0", Generation(text="B"))
    assert [r["key"] for r in read_records(raw)] == ["a#0", "b#0"]


def test_invalidate_survives_a_torn_line(raw):
    raw.write_text(_line("a#0", text="A") + _line("b#0", text="B")[:25])
    with pytest.warns(RuntimeWarning):
        assert invalidate(raw.parent.parent, ["a#0", "b#0"], "test") == 1
    assert GenerationStore(raw).done == {}
    assert [r["key"] for r in read_records(raw)] == ["a#0", "a#0"]


# --------------------------------------------------------------------------- runs

MODEL = "qwen3.8-27b-awq-int4"


class _FakeModel:
    """Stands in for the model: every call is recorded and answered from a script."""

    def __init__(self, registry):
        self.registry = registry
        self.prompts: list[str] = []
        self.reply = Generation(text="Answer: x", finish_reason="stop")


@pytest.fixture
def model(monkeypatch):
    from forcebench import runner
    from forcebench.llm import Client
    from forcebench.models import load_registry

    reg = load_registry()
    fake = _FakeModel(reg)

    class FakeClient(Client):
        async def generate(self, system: str, user: str) -> Generation:
            fake.prompts.append(user)
            return fake.reply.model_copy()

    monkeypatch.setenv(reg.provider_for(reg.get(MODEL)).base_url_env, "http://127.0.0.1:9/v1")
    monkeypatch.setattr(runner, "Client", FakeClient)
    return fake


def _generate(model, run_dir, tasks, model_id=MODEL, effort="low", **kw):
    import asyncio

    from forcebench.runner import generate

    return asyncio.run(
        generate(model.registry, model_id, effort, tasks, run_dir=run_dir, progress=False, **kw)
    )


def _grade(run_dir, tasks):
    import asyncio

    from forcebench.graders import GradeEnv
    from forcebench.runner import grade

    asyncio.run(grade(run_dir, tasks, GradeEnv(work_dir=run_dir / "work"), progress=False))
    return [json.loads(x) for x in (run_dir / "cases.jsonl").read_text().splitlines()]


def _task(make_task, version: int = 1):
    return make_task({"format": "text"}, version=version)


def test_generation_records_the_task_version_and_prompt(model, make_task, tmp_path):
    from forcebench.answers import render_prompt
    from forcebench.runner import _sha

    run_dir = _generate(model, tmp_path / "run", [_task(make_task)])
    (rec,) = read_records(run_dir / "raw" / "generations.jsonl")
    assert rec["task_version"] == 1
    assert rec["prompt_sha"] == _sha(render_prompt(_task(make_task)))
    (case,) = _grade(run_dir, [_task(make_task)])
    assert (case["passed"], case["task_version"], case["stale"]) == (True, 1, False)
    assert case["prompt_sha"] == rec["prompt_sha"]


def test_answer_to_an_older_task_version_is_not_graded_against_the_new_one(
    model, make_task, tmp_path
):
    run_dir = _generate(model, tmp_path / "run", [_task(make_task)])
    (case,) = _grade(run_dir, [_task(make_task, version=2)])
    assert case["stale"] and "stale" in case["skipped"]
    assert case["task_version"] == 1, "the answer's own version, which the report leaves out"
    assert (case["passed"], case["checks"]) == (False, [])


def test_older_records_fall_back_to_the_run_task_versions(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / "run", [_task(make_task)])
    raw = run_dir / "raw" / "generations.jsonl"
    (rec,) = read_records(raw)
    raw.write_text(json.dumps({"key": rec["key"], "generation": rec["generation"]}) + "\n")
    (case,) = _grade(run_dir, [_task(make_task, version=2)])
    assert (case["stale"], case["task_version"]) == (True, 1)
    (case,) = _grade(run_dir, [_task(make_task, version=1)])
    assert (case["stale"], case["passed"]) == (False, True)


def test_resume_regenerates_answers_to_an_older_task_version(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / "run", [_task(make_task)])
    _generate(model, run_dir, [_task(make_task)])
    assert len(model.prompts) == 1, "a current answer is reused"
    _generate(model, run_dir, [_task(make_task, version=2)])
    assert len(model.prompts) == 2, "an answer to version 1 is not reused for version 2"
    assert read_records(run_dir / "raw" / "generations.jsonl")[-1]["task_version"] == 2
    assert json.loads((run_dir / "run.json").read_text())["task_versions"] == {"test-task": 1}
    (case,) = _grade(run_dir, [_task(make_task, version=2)])
    assert (case["stale"], case["task_version"], case["passed"]) == (False, 2, True)
