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
