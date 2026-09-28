"""Answer extraction regressions from the second review: choice answers outside the strict
`Answer: <letter>` form, `#` comments in http blocks, and Windows line endings."""

from __future__ import annotations

import pytest

from forcebench.answers import extract
from forcebench.tasks import AnswerFormat, Task, all_tasks, load_suites


@pytest.fixture
def five(make_task):
    return make_task(
        {"format": "choice", "choices": {k: f"option {k}" for k in "ABCDE"}, "multiple": True}
    )


@pytest.fixture
def nine(make_task):
    """A task with an option I, which the pronoun must never select."""
    return make_task(
        {"format": "choice", "choices": {k: f"option {k}" for k in "ABCDEFGHI"}, "multiple": True}
    )


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Reasoning.\n\nFinal Answer: C", ["C"]),
        ("Reasoning.\n\n**Final Answer:** C", ["C"]),
        ("Reasoning.\n\nAnswer:\nC", ["C"]),
        ("Reasoning.\n\n**Answer:**\n\n**C**", ["C"]),
        ("Reasoning.\n\n## Answer:\n\nC) the Bulk API", ["C"]),
        ("Reasoning.\n\n\\boxed{C}", ["C"]),
        ("Reasoning.\n\n$\\boxed{\\text{C}}$", ["C"]),
        ("Reasoning.\n\n$$\\boxed{A, C}$$", ["A", "C"]),
        ("Answer: $\\boxed{C}$", ["C"]),
        ("Reasoning.\n\nThe correct option is C.", ["C"]),
        ("Reasoning.\n\nSo the correct answer is **C** because it is bulk-safe.", ["C"]),
        ("Reasoning.\n\nThe correct options are A and C.", ["A", "C"]),
        ("Reasoning.\n\nBoth A and C", ["A", "C"]),
        ("Reasoning.\n\nBoth A and C are correct.", ["A", "C"]),
        ("Reasoning.\n\nOptions A and C are correct.", ["A", "C"]),
        ("Reasoning.\n\nOption C.", ["C"]),
        ("Answer: Both A and C", ["A", "C"]),
        ("Answer: The correct option is C", ["C"]),
        # the strict `Answer:` line wins over every fallback
        ("\\boxed{A}\n\nAnswer: C", ["C"]),
        ("Answer: B\n\nThe correct option is C.", ["B"]),
        # an `Answer:` line with no value anywhere does not hide an earlier one
        ("Answer: B\n\nAnswer:", ["B"]),
    ],
)
def test_choice_fallbacks(five, reply, expected):
    a = extract(five, reply)
    assert a.error is None, a.error
    assert a.choices == expected


@pytest.mark.parametrize(
    "reply",
    [
        # articles and pronouns are never options, in the value or in the fallbacks
        "Answer: I think a production org needs it",
        "Answer: A production org needs it",
        "I think the answer depends on the org.",
        "A production org would need this.",
        "The correct answer is a matter of taste.",
        "The correct answer is I think C.",
        # the fallbacks read explicit answer phrases only, and only on the last line
        "Option A is wrong because it is not bulk-safe.",
        "Both A and C are wrong.",
        "The correct option is C.\n\nThat said, a lot depends on the data volume.",
        # a letter that is not an option
        "The correct option is Z.",
        "\\boxed{Z}",
    ],
)
def test_choice_fallbacks_never_read_prose(nine, reply):
    a = extract(nine, f"Some reasoning.\n\n{reply}")
    assert a.choices == [] and a.error, a.choices


def test_pronoun_after_a_real_option_is_dropped(nine):
    assert extract(nine, "Answer: A, I think").choices == ["A"]
    assert extract(nine, "Answer: A and I are correct").choices == ["A", "I"]
    assert extract(nine, "Answer: **I**").choices == ["I"]


def test_text_answer_on_the_next_line_and_final_answer(make_task):
    t = make_task({"format": "text", "cite": True})
    a = extract(t, "Reasoning.\n\n**Final Answer:** 25\nSource: https://example.com/a.htm")
    assert (a.value, a.source) == ("25", "https://example.com/a.htm")
    a = extract(t, "Reasoning.\n\nAnswer:\n25 requests\n\nSource: https://example.com/a.htm")
    assert a.value == "25 requests"
    # the Source line is never read as the answer
    assert extract(t, "Answer:\nSource: https://example.com/a.htm").error


# --------------------------------------------------------------------------- http comments


def test_http_comment_lines_are_ignored(make_task):
    t = make_task({"format": "http"})
    reply = (
        "```http\n"
        "# Step 1: create the ingest job\n"
        "POST /services/data/v67.0/jobs/ingest HTTP/1.1\n"
        "# JSON, not CSV, for the job definition\n"
        "Content-Type: application/json\n"
        "\n"
        '{"object": "Contact",\n'
        "# upsert on the legacy id\n"
        ' "operation": "upsert"}\n'
        "# the response holds the job id\n"
        "### Step 2\n"
        "# upload the rows\n"
        "PUT /services/data/v67.0/jobs/ingest/750x/batches HTTP/1.1\n"
        "Content-Type: text/csv\n"
        "\n"
        "Legacy_Id__c,LastName\n"
        "#42,Smith\n"
        "```"
    )
    a = extract(t, reply)
    assert a.error is None, a.error
    assert [r.method for r in a.requests] == ["POST", "PUT"]
    first, second = a.requests
    assert first.headers == {"content-type": "application/json"}
    assert first.body == {"object": "Contact", "operation": "upsert"}
    # a CSV row may start with `#`: it is data, not a comment
    assert second.raw_body == "Legacy_Id__c,LastName\n#42,Smith"


def test_http_request_with_only_a_comment_after_it_has_no_body(make_task):
    t = make_task({"format": "http"})
    a = extract(t, "```http\nGET /services/data/v67.0/limits HTTP/1.1\n\n# expect 200\n```")
    assert a.requests[0].raw_body == "" and a.requests[0].body is None


# --------------------------------------------------------------------------- line endings


def _extraction(task: Task, reply: str) -> dict:
    return extract(task, reply).model_dump()


_TASKS = all_tasks(load_suites())


@pytest.mark.parametrize("fmt", list(AnswerFormat))
@pytest.mark.parametrize("ending", ["\r\n", "\r"])
def test_windows_line_endings_extract_like_unix_ones(fmt, ending):
    """Every task's reference output, with CRLF (or CR) line endings, extracts exactly as it
    does with LF ones."""
    tasks = [t for t in _TASKS if t.answer.format is fmt]
    assert tasks, f"no {fmt} task"
    for t in tasks:
        for reply in [t.reference_output, *t.alternative_outputs]:
            unix = _extraction(t, reply)
            assert unix["error"] is None, (t.id, unix["error"])
            assert _extraction(t, reply.replace("\n", ending)) == unix, t.id
