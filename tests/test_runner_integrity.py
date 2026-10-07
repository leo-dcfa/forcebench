"""Run bookkeeping that the leaderboard's integrity rests on.

The generations store, resuming, task versions and per-case retry counts.
"""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from forcebench import GENERATION_PROTOCOL, run_protocol, runner
from forcebench.answers import render_prompt
from forcebench.cli import app
from forcebench.graders import GradeEnv
from forcebench.llm import Client, Generation
from forcebench.models import load_registry
from forcebench.runner import (
    CorruptStoreError,
    GenerationStore,
    ResumeError,
    _sha,
    generate,
    grade,
    invalidate,
    read_records,
    record_failed,
    sample_seed,
)


# A run directory name as runner.run_id_for makes them (commands refuse any other name).
RUN = "20260928T000000Z_qwen3.8-27b-awq-int4@low"


def _line(key: str, **gen) -> str:
    return json.dumps({"key": key, "generation": Generation(**gen).model_dump()}) + "\n"


@pytest.fixture
def raw(tmp_path):
    path = tmp_path / RUN / "raw" / "generations.jsonl"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("same_size", [False, True])
async def test_what_another_writer_added_since_is_never_cut(raw, same_size):
    torn = _line("b#0", text="B" * 100)[:-10]
    raw.write_text(_line("a#0", text="A") + torn)
    with pytest.warns(RuntimeWarning):
        store = GenerationStore(raw)
    # Meanwhile another process drops the torn line and appends its own record, which may even
    # leave the file exactly as long as it was.
    pad = len(torn) - len(_line("c#0", text="")) if same_size else 5
    other = _line("c#0", text="C" * pad)
    raw.write_text(_line("a#0", text="A") + other)
    await store.add("b#0", Generation(text="B again"))
    assert [r["key"] for r in read_records(raw)] == ["a#0", "c#0", "b#0"]


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
        self.seeds: list[int | None] = []
        self.reply = Generation(text="Answer: x", finish_reason="stop")


@pytest.fixture
def model(monkeypatch):
    reg = load_registry()
    fake = _FakeModel(reg)

    class FakeClient(Client):
        async def generate(self, system: str, user: str, seed: int | None = None) -> Generation:
            fake.prompts.append(user)
            fake.seeds.append(seed)
            return fake.reply.model_copy()

    monkeypatch.setenv(reg.provider_for(reg.get(MODEL)).base_url_env, "http://127.0.0.1:9/v1")
    monkeypatch.setattr(runner, "Client", FakeClient)
    return fake


def _generate(
    model, run_dir, tasks, model_id: str | None = MODEL, effort: str | None = "low", **kw
):
    return asyncio.run(
        generate(model.registry, model_id, effort, tasks, run_dir=run_dir, progress=False, **kw)
    )


def _grade(run_dir, tasks):
    asyncio.run(grade(run_dir, tasks, GradeEnv(work_dir=run_dir / "work"), progress=False))
    return [json.loads(x) for x in (run_dir / "cases.jsonl").read_text().splitlines()]


def _task(make_task, version: int = 1):
    return make_task({"format": "text"}, version=version)


def test_sample_seeds_give_every_answer_a_seed_of_its_own(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)], samples=3, sample_seeds=True)
    assert sorted(model.seeds) == sorted(sample_seed(f"test-task#{s}") for s in range(3))
    assert len(set(model.seeds)) == 3
    assert json.loads((run_dir / "run.json").read_text())["sample_seeds"] is True


def test_without_sample_seeds_no_seed_is_sent(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)], samples=2)
    assert model.seeds == [None, None]
    assert "sample_seeds" not in json.loads((run_dir / "run.json").read_text())
    with pytest.raises(ResumeError, match="sample seeds"):
        _generate(model, run_dir, [_task(make_task)], samples=2, sample_seeds=True)


def test_generation_records_the_task_version_and_prompt(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    (rec,) = read_records(run_dir / "raw" / "generations.jsonl")
    assert rec["task_version"] == 1
    assert rec["prompt_sha"] == _sha(render_prompt(_task(make_task)))
    (case,) = _grade(run_dir, [_task(make_task)])
    assert (case["passed"], case["task_version"], case["stale"]) == (True, 1, False)
    assert case["prompt_sha"] == rec["prompt_sha"]


def test_answer_to_an_older_task_version_is_not_graded_against_the_new_one(
    model, make_task, tmp_path
):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    (case,) = _grade(run_dir, [_task(make_task, version=2)])
    assert case["stale"]
    assert "stale" in case["skipped"]
    assert case["task_version"] == 1, "the answer's own version, which the report leaves out"
    assert (case["passed"], case["checks"]) == (False, [])


def test_older_records_fall_back_to_the_run_task_versions(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    raw = run_dir / "raw" / "generations.jsonl"
    (rec,) = read_records(raw)
    raw.write_text(json.dumps({"key": rec["key"], "generation": rec["generation"]}) + "\n")
    (case,) = _grade(run_dir, [_task(make_task, version=2)])
    assert (case["stale"], case["task_version"]) == (True, 1)
    (case,) = _grade(run_dir, [_task(make_task, version=1)])
    assert (case["stale"], case["passed"]) == (False, True)


def test_resume_regenerates_answers_to_an_older_task_version(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    _generate(model, run_dir, [_task(make_task)])
    assert len(model.prompts) == 1, "a current answer is reused"
    _generate(model, run_dir, [_task(make_task, version=2)])
    assert len(model.prompts) == 2, "an answer to version 1 is not reused for version 2"
    assert read_records(run_dir / "raw" / "generations.jsonl")[-1]["task_version"] == 2
    assert json.loads((run_dir / "run.json").read_text())["task_versions"] == {"test-task": 1}
    (case,) = _grade(run_dir, [_task(make_task, version=2)])
    assert (case["stale"], case["task_version"], case["passed"]) == (False, 2, True)


def test_cases_publish_how_many_attempts_each_answer_took(model, make_task, tmp_path):
    model.reply = Generation(text="Answer: x", finish_reason="stop", attempts=3)
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    (case,) = _grade(run_dir, [_task(make_task)])
    assert (case["passed"], case["attempts"]) == (True, 3)


# --------------------------------------------------------------------------- resume


def _meta(run_dir):
    return json.loads((run_dir / "run.json").read_text())


def test_new_run_records_the_generation_protocol(model, make_task, tmp_path):
    meta = _meta(_generate(model, tmp_path / RUN, [_task(make_task)]))
    assert meta["protocol"] == GENERATION_PROTOCOL == run_protocol(meta)
    assert (meta["request"]["stream"], meta["request"]["sdk_retries"]) == (True, 0)


def test_resume_takes_omitted_settings_from_the_run(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)], samples=2, subset="lite")
    before = _meta(run_dir)
    _generate(model, run_dir, [_task(make_task)], model_id=None, effort=None)
    after = _meta(run_dir)
    assert len(model.prompts) == 2, "nothing is regenerated"
    for k in ("config_id", "effort", "samples", "subset", "request", "started_at"):
        assert after[k] == before[k]


@pytest.mark.parametrize(
    "change",
    [
        {"effort": "xhigh"},
        {"samples": 3},
        {"subset": "lite"},
        {"model_id": "qwen3.8-27b-mlx-4bit"},
    ],
    ids=lambda c: next(iter(c)),
)
def test_resume_refuses_other_settings(model, make_task, tmp_path, change):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    before = (run_dir / "run.json").read_text()
    with pytest.raises(ResumeError, match="cannot resume"):
        _generate(model, run_dir, [_task(make_task)], **change)
    assert len(model.prompts) == 1, "no answer is generated or relabelled"
    assert (run_dir / "run.json").read_text() == before


def test_resume_refuses_changed_request_settings(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    meta = _meta(run_dir)
    meta["request"]["temperature"] = 0.2  # e.g. the model's sampling config changed since
    (run_dir / "run.json").write_text(json.dumps(meta))
    with pytest.raises(ResumeError, match="request"):
        _generate(model, run_dir, [_task(make_task)])


def test_resume_refuses_a_changed_system_prompt(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    meta = _meta(run_dir)
    meta["system_prompt_sha"] = "0123456789ab"  # the run was started with another prompt
    (run_dir / "run.json").write_text(json.dumps(meta))
    with pytest.raises(ResumeError, match="system prompt"):
        _generate(model, run_dir, [_task(make_task)])


def test_resume_regenerates_an_answer_to_another_prompt(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    raw = run_dir / "raw" / "generations.jsonl"
    (rec,) = read_records(raw)
    raw.write_text(json.dumps({**rec, "prompt_sha": "0123456789ab"}) + "\n")
    _generate(model, run_dir, [_task(make_task)])
    assert len(model.prompts) == 2, "the answer was to a prompt the task no longer renders"


def test_answers_without_run_json_are_not_resumed(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    (run_dir / "run.json").unlink()
    with pytest.raises(ResumeError, match=r"no run\.json"):
        _generate(model, run_dir, [_task(make_task)], effort="xhigh")
    assert len(model.prompts) == 1


def test_resume_refuses_a_run_from_an_older_protocol(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    meta = _meta(run_dir)
    del meta["protocol"], meta["request"]["stream"], meta["request"]["sdk_retries"]
    (run_dir / "run.json").write_text(json.dumps(meta))
    with pytest.raises(ResumeError, match="protocol 1"):
        _generate(model, run_dir, [_task(make_task)])


def test_resume_with_fewer_tasks_keeps_the_others(model, make_task, tmp_path):
    a, b = _task(make_task), make_task({"format": "text"}, id="other-task")
    run_dir = _generate(model, tmp_path / RUN, [a, b])
    _generate(model, run_dir, [a])
    assert _meta(run_dir)["task_ids"] == ["other-task", "test-task"]


def test_cli_resume_with_another_effort_fails_clearly(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    result = CliRunner().invoke(app, ["run", "--resume", str(run_dir), "-e", "xhigh", "--no-grade"])
    assert result.exit_code == 1
    assert "cannot resume" in result.output
    assert "'low'" in result.output
    assert len(model.prompts) == 1


@pytest.mark.parametrize(
    ("meta", "protocol"),
    [
        ({"protocol": 2}, 2),
        ({"request": {"max_tokens": 1}}, 1),  # before streaming
        (
            {
                "request": {"stream": True, "sdk_retries": 0},
                "started_at": "2026-09-27T00:00:00+00:00",
            },
            1,
        ),
        (
            {  # started before protocol 2 existed, then resumed: run.json was rewritten
                "request": {"stream": True, "sdk_retries": 0},
                "provider": "mtplx",
                "started_at": "2026-09-26T19:29:33.656074+00:00",
            },
            1,
        ),
        (
            {
                "request": {"stream": True, "sdk_retries": 0},
                "provider": "direct-gpu",
                "started_at": "2026-09-26T21:47:13.1+00:00",
            },
            2,
        ),
    ],
)
def test_protocol_of_runs_from_before_it_was_recorded(meta, protocol):
    assert run_protocol(meta) == protocol


def test_a_run_records_the_endpoint_model_it_called(model, make_task, tmp_path):
    reg = model.registry
    meta = _meta(_generate(model, tmp_path / RUN, [_task(make_task)]))
    assert meta["endpoint_model"] == reg.get(MODEL).endpoint_model
    other = tmp_path / RUN.replace("000000Z", "000001Z")
    meta = _meta(_generate(model, other, [_task(make_task)], endpoint_model="same-weights-x3"))
    assert meta["endpoint_model"] == "same-weights-x3"
    assert meta["config_id"] == f"{MODEL}@low", "the same configuration, served another way"


def test_resume_keeps_the_endpoint_model_and_refuses_another(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)], endpoint_model="x3")
    _generate(model, run_dir, [_task(make_task)], model_id=None, effort=None)
    assert _meta(run_dir)["endpoint_model"] == "x3", "omitted: the run's"
    with pytest.raises(ResumeError, match="endpoint model"):
        _generate(model, run_dir, [_task(make_task)], endpoint_model="x2")


def test_a_run_from_before_endpoint_names_resumes_with_its_configs(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    meta = _meta(run_dir)
    del meta["endpoint_model"]  # recorded before endpoint names were
    (run_dir / "run.json").write_text(json.dumps(meta))
    with pytest.raises(ResumeError, match="endpoint model"):
        _generate(model, run_dir, [_task(make_task)], endpoint_model="x3")
    _generate(model, run_dir, [_task(make_task)])
    assert _meta(run_dir)["endpoint_model"] == model.registry.get(MODEL).endpoint_model


def test_an_answer_the_endpoint_never_returned_can_be_recorded_as_failed(
    model, make_task, tmp_path
):
    model.reply = Generation(error="ModelHTTPError: status_code: 503", attempts=4, latency_s=31)
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    assert record_failed(run_dir, ["test-task#0"], "the endpoint cuts responses at 30 s") == 1
    (case,) = _grade(run_dir, [_task(make_task)])
    assert (case["passed"], case["infra_error"], case["skipped"]) == (False, None, None)
    assert case["finish_reason"] == "failed: the endpoint cuts responses at 30 s"
    assert case["attempts"] == 4
    assert case["prompt_sha"] == _sha(render_prompt(_task(make_task)))
    _generate(model, run_dir, [_task(make_task)])
    assert len(model.prompts) == 1, "a resume does not ask again"


def test_record_failed_leaves_real_and_invalidated_answers_alone(model, make_task, tmp_path):
    run_dir = _generate(model, tmp_path / RUN, [_task(make_task)])
    assert record_failed(run_dir, ["test-task#0"], "x") == 0, "a delivered answer stays"
    invalidate(run_dir, ["test-task#0"], "regenerate it")
    assert record_failed(run_dir, ["test-task#0"], "x") == 0, "an invalidated one is regenerated"
