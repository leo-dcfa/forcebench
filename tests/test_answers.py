from forcebench.answers import extract, render_prompt, strip_reasoning


def test_strip_reasoning():
    assert strip_reasoning("<think>hmm</think>\nAnswer: B") == "Answer: B"
    assert strip_reasoning("leaked thoughts</think>final") == "final"


def test_command_block_last_wins(make_task):
    t = make_task({"format": "command"})
    reply = "Try:\n```bash\nsf org list\n```\nActually:\n```bash\n# deploy\n$ sf project deploy start \\\n  --source-dir force-app\n```"
    a = extract(t, reply)
    assert a.error is None
    assert a.commands == ["sf project deploy start --source-dir force-app"]


def test_command_without_block(make_task):
    t = make_task({"format": "command"})
    assert extract(t, "Run sf org list --all").commands == []
    assert extract(t, "Run:\nsf org list --all").commands == ["sf org list --all"]


def test_files(make_task):
    t = make_task(
        {
            "format": "files",
            "files": [
                "force-app/main/default/classes/A.cls",
                "force-app/main/default/classes/B.cls",
            ],
        }
    )
    reply = (
        "File: force-app/main/default/classes/A.cls\n```apex\nclass A {}\n```\n"
        "**File:** `B.cls`\n```apex\nclass B {}\n```\n"
    )
    a = extract(t, reply)
    assert a.files == {
        "force-app/main/default/classes/A.cls": "class A {}\n",
        "force-app/main/default/classes/B.cls": "class B {}\n",
    }


def test_single_file_fallback(make_task):
    t = make_task({"format": "files", "files": ["force-app/main/default/classes/A.cls"]})
    a = extract(t, "Here you go\n```apex\nclass A {}\n```")
    assert a.files == {"force-app/main/default/classes/A.cls": "class A {}\n"}


def test_choice(make_task):
    t = make_task({"format": "choice", "choices": {"A": "a", "B": "b", "C": "c"}, "multiple": True})
    assert extract(t, "reasoning...\n**Answer:** A, C").choices == ["A", "C"]
    assert extract(t, "no answer line").error


def test_text_with_source(make_task):
    t = make_task({"format": "text", "cite": True})
    a = extract(t, "Answer: 100\nSource: <https://developer.salesforce.com/docs/x.htm>.")
    assert a.value == "100"
    assert a.source == "https://developer.salesforce.com/docs/x.htm"


def test_json_and_soql(make_task):
    t = make_task({"format": "json"})
    assert extract(t, '```json\n{"edition": "Developer"}\n```').json_value == {
        "edition": "Developer"
    }
    assert extract(t, '```json\n{"edition": }\n```').error
    t = make_task({"format": "soql"})
    assert extract(t, "```soql\nSELECT Id FROM Account;\n```").value == "SELECT Id FROM Account"


def test_http(make_task):
    t = make_task({"format": "http"})
    reply = (
        "```http\nPATCH /services/data/v67.0/sobjects/Account/Ext__c/42?x=1 HTTP/1.1\n"
        'Content-Type: application/json\n\n{"Name": "A"}\n###\n'
        "GET https://example.my.salesforce.com/services/data/v67.0/limits\n```"
    )
    a = extract(t, reply)
    assert [r.method for r in a.requests] == ["PATCH", "GET"]
    assert a.requests[0].body == {"Name": "A"}
    assert a.requests[0].query == {"x": ["1"]}
    assert a.requests[1].path == "/services/data/v67.0/limits"


def test_render_prompt_has_format_and_files(make_task):
    t = make_task(
        {"format": "files", "files": ["force-app/main/default/classes/A.cls"]},
        context_files={"force-app/main/default/classes/Old.cls": "class Old {}"},
    )
    p = render_prompt(t)
    assert "File: force-app/main/default/classes/Old.cls" in p
    assert "Files to return:\n- force-app/main/default/classes/A.cls" in p


def test_answer_line_in_backticks_or_bold(make_task):
    t = make_task({"format": "text", "cite": True})
    a = extract(t, "`Answer: trigger`\n`Source: https://developer.salesforce.com/docs/x.htm`")
    assert a.value == "trigger"
    assert a.source == "https://developer.salesforce.com/docs/x.htm"
    assert extract(t, "**Answer**: 42").value == "42"


def test_inline_comment_with_apostrophe(make_task):
    t = make_task({"format": "command"})
    a = extract(t, "```bash\nsf org list  # don't forget\n```")
    assert a.error is None
    assert a.commands == ["sf org list  # don't forget"]


def test_dot_directories_survive(make_task):
    t = make_task({"format": "files", "files": [".github/workflows/ci.yml"]})
    a = extract(t, "File: .github/workflows/ci.yml\n```yaml\non: push\n```\n")
    assert list(a.files) == [".github/workflows/ci.yml"]
    a = extract(t, "File: ./.github/workflows/ci.yml\n```yaml\non: push\n```\n")
    assert list(a.files) == [".github/workflows/ci.yml"]


def test_artifacts_confined_to_case_dir(make_task, tmp_path):
    from forcebench.graders import Check, Grade
    from forcebench.llm import Generation
    from forcebench.runner import write_artifacts

    t = make_task({"format": "files", "files": ["force-app/main/default/classes/A.cls"]})
    reply = (
        "File: force-app/main/default/classes/A.cls\n```apex\nclass A {}\n```\n"
        "File: ../../escape.txt\n```\npwned\n```\n"
        "File: /etc/evil\n```\npwned\n```\n"
    )
    g = Grade.from_checks([Check(name="x", passed=True)])
    g.artifacts["deploy"] = {"success": True}
    case = tmp_path / "run" / "artifacts" / "t" / "0"
    write_artifacts(case, Generation(text=reply), extract(t, reply), g)
    assert (case / "files/force-app/main/default/classes/A.cls").read_text() == "class A {}\n"
    assert (case / "deploy.json").exists() and (case / "grade.json").exists()
    written = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    assert all(str(p).startswith("run/artifacts/t/0/") for p in written)
    assert not (tmp_path / "escape.txt").exists()


def test_invalidate_last_record_wins(tmp_path):
    import json

    from forcebench.llm import Generation
    from forcebench.runner import GenerationStore, invalidate

    run = tmp_path / "20260928T000000Z_m@low"
    (run / "raw").mkdir(parents=True)
    path = run / "raw" / "generations.jsonl"
    path.write_text(
        json.dumps({"key": "t#0", "generation": Generation(text="a", latency_s=900).model_dump()})
        + "\n"
        + json.dumps({"key": "u#0", "generation": Generation(text="b", latency_s=10).model_dump()})
        + "\n"
    )
    assert invalidate(run, ["t#0"], "proxy retried") == 1
    store = GenerationStore(path)
    assert set(store.done) == {"u#0"}


def test_leaked_control_tokens_are_stripped(make_task):
    t = make_task({"format": "files", "files": ["force-app/main/default/lwc/x/x.html"]})
    reply = "File: force-app/main/default/lwc/x/x.html\n```html\n<template></template>\n```<|channel><channel|>"
    a = extract(t, reply)
    assert a.files == {"force-app/main/default/lwc/x/x.html": "<template></template>\n"}
    assert extract(make_task({"format": "text"}), "Answer: 42<|im_end|>").value == "42"
