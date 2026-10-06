"""Tests for the ``ci_workflow`` grader (GitHub Actions workflows and CI shell scripts)."""

import textwrap

import pytest

from forcebench.answers import extract
from forcebench.graders import GradeEnv, grade
from forcebench.graders.ci import (
    UNKNOWN,
    Event,
    ScriptParser,
    cron_fires,
    evaluate_condition,
    filter_matches,
    grade_file,
    load_yaml,
    parse_unit,
    run_states,
    triggered,
)


def wf(s: str) -> str:
    return textwrap.dedent(s).lstrip()


def failed(checks) -> dict[str, str]:
    return {c.name: c.detail for c in checks if not c.passed}


def passes(text: str, params: dict, kind: str = "workflow") -> bool:
    return not failed(grade_file(text, params, kind))


def script_cmds(script: str, variables: dict | None = None, **kw):
    sp = ScriptParser(script, variables or {}, kw.get("errexit", True), kw.get("pipefail", False))
    return sp.parse()


BASE = wf(
    """
    name: validate
    on:
      pull_request:
        branches: [main]
    jobs:
      validate:
        runs-on: ubuntu-latest
        env:
          SF_USERNAME: ${{ secrets.SF_USERNAME }}
        steps:
          - uses: actions/checkout@v4
          - run: npm install --global @salesforce/cli
          - name: Authenticate
            run: |
              echo "${{ secrets.SF_JWT_KEY }}" > server.key
              sf org login jwt --client-id "${{ secrets.SF_CONSUMER_KEY }}" \\
                --jwt-key-file server.key --username "$SF_USERNAME" --alias prod
          - name: Validate
            run: sf project deploy validate --source-dir force-app --target-org prod --wait 60
    """
)

LOGIN = {
    "id": "login",
    "command": "org login jwt",
    "flags": {
        "username": {"secret": "SF_USERNAME"},
        "client-id": {"secret": "SF_CONSUMER_KEY"},
        "alias": "prod",
    },
}
VALIDATE = {
    "id": "validate",
    "command": "project deploy validate",
    "flags": {"test-level": {"equals": "RunLocalTests", "optional": True}, "target-org": "prod"},
    "after": ["login"],
    "gating": True,
}


# --------------------------------------------------------------------------- YAML


def test_yaml_on_key_stays_a_string_and_duplicates_fail():
    assert "on" in load_yaml("on: push\njobs: {}\n")
    assert load_yaml("x: yes\n") == {"x": "yes"}
    assert load_yaml("x: true\n") == {"x": True}
    with pytest.raises(Exception, match="duplicate key"):
        load_yaml("on: push\non: pull_request\n")


def test_invalid_yaml_and_structure_fail():
    assert failed(grade_file("on: [push\n", {}, "workflow"))["valid workflow"].startswith(
        "invalid YAML"
    )
    bad = wf(
        """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            needs: missing
            steps:
              - uses: actions/checkout@v4
                run: echo hi
        """
    )
    detail = failed(grade_file(bad, {}, "workflow"))["valid workflow"]
    assert "exactly one of `uses` or `run`" in detail
    assert "unknown job 'missing'" in detail


# --------------------------------------------------------------------------- shell parsing


def test_script_parser_splits_and_resolves():
    cmds = script_cmds(
        textwrap.dedent(
            """
            set -euo pipefail
            # a comment
            OUT=changed-sources
            mkdir -p "$OUT" && echo ok  # trailing comment
            cat > note.txt <<'EOF'
            sf this is not a command
            EOF
            sf sgd source delta --from "origin/$GITHUB_BASE_REF" \\
              --output-dir "$OUT"
            if ! sf project deploy validate -x "$OUT/package/package.xml"; then exit 1; fi
            """
        ),
        {"GITHUB_BASE_REF": "${{ github.base_ref }}"},
    )
    texts = [" ".join(c.tokens) for c in cmds]
    assert texts == [
        "mkdir -p changed-sources",
        "echo ok",
        "cat",
        "sf sgd source delta --from origin/${{ github.base_ref }} --output-dir changed-sources",
        "sf project deploy validate -x changed-sources/package/package.xml",
        "exit 1",
    ]
    assert all(c.errexit and c.pipefail for c in cmds)
    assert cmds[4].conditional
    assert cmds[4].script_exits


def test_script_parser_substitutions():
    cmds = script_cmds(
        "JOB_ID=$(sf project deploy validate -d force-app --json | jq -r .result.id)\n"
        'echo "id=$(sf org display --json)" >> "$GITHUB_OUTPUT"\n'
        "export X=$(sf org list --json)\n"
        'sf project deploy quick --job-id "$JOB_ID"\n'
    )
    by_text = {" ".join(c.tokens): c for c in cmds}
    assert by_text["sf project deploy validate -d force-app --json"].subst == "assign"
    assert by_text["sf project deploy validate -d force-app --json"].op_after == "|"
    assert by_text["sf org display --json"].subst == "arg"
    assert by_text["sf org list --json"].subst == "arg"
    # a dynamic variable stays opaque
    assert "sf project deploy quick --job-id $JOB_ID" in by_text


def test_script_parser_multiline_quotes_and_set():
    cmds = script_cmds('echo "line one\nline two"\nset +e\nsf org list\n', errexit=True)
    assert [c.tokens[0] for c in cmds] == ["echo", "sf"]
    assert cmds[1].errexit is False


def test_expressions_become_canonical_values():
    cmds = script_cmds("sf org login jwt --username ${{secrets.SF_USER}} --alias x\n")
    assert cmds[0].tokens[5] == "${{ secrets.SF_USER }}"
    cmds = script_cmds("sf org list --target-org ${{ env.ALIAS }}\n", {"ALIAS": "prod"})
    assert cmds[0].tokens[-1] == "prod"


# --------------------------------------------------------------------------- sf commands


def test_sf_commands_validated_against_manifest_and_extras():
    assert passes(BASE, {"expect": [LOGIN, VALIDATE]})
    legacy = BASE.replace("sf project deploy validate", "sfdx force:source:deploy --checkonly")
    assert any(k.startswith("sf command valid") for k in failed(grade_file(legacy, {}, "workflow")))
    sgd = "sf sgd source delta --from origin/main --to HEAD --output-dir out --generate-delta\n"
    assert passes(sgd, {}, "script")
    assert not passes("sf sgd source delta --from origin/main --output out\n", {}, "script")
    assert passes(
        "sf sgd source delta --from origin/main --output out\n",
        {"allow_deprecated": True},
        "script",
    )
    # --sfdx-url-stdin reads stdin when given without a value
    assert passes(
        'echo "$URL" | sf org login sfdx-url --sfdx-url-stdin --alias uat\n', {}, "script"
    )


def test_legacy_sfdx_cli_install_fails():
    bad = BASE.replace("@salesforce/cli", "sfdx-cli")
    assert "no legacy sfdx-cli" in failed(grade_file(bad, {}, "workflow"))


def test_secret_matcher_resolves_env_and_direct():
    assert passes(BASE, {"expect": [LOGIN]})
    literal = BASE.replace('--username "$SF_USERNAME"', "--username ci@example.com")
    detail = failed(grade_file(literal, {"expect": [LOGIN]}, "workflow"))["expect: login"]
    assert "--username" in detail


def test_ordering_needs_and_same_job():
    two_jobs = BASE + (
        "  deploy:\n"
        "    runs-on: ubuntu-latest\n"
        "    needs: validate\n"
        "    environment: production\n"
        "    steps:\n"
        '      - run: sf project deploy quick --job-id "$ID" --target-org prod\n'
    )
    unit = parse_unit(two_jobs, "workflow")
    assert not unit.errors, unit.errors
    quick = {"id": "quick", "command": "project deploy quick", "after": ["validate"]}
    assert passes(two_jobs, {"expect": [LOGIN, VALIDATE, quick]})
    # the deploy job runs on a fresh runner: it must authenticate itself
    quick_auth = {**quick, "same_job_after": ["login"]}
    detail = failed(grade_file(two_jobs, {"expect": [LOGIN, VALIDATE, quick_auth]}, "workflow"))
    assert "does not run `login` before it" in detail["expect: quick"]
    env_ok = {**quick, "environment": "Production"}
    assert passes(two_jobs, {"expect": [LOGIN, VALIDATE, env_ok]})
    env_bad = {**quick, "environment": "uat"}
    assert not passes(two_jobs, {"expect": [LOGIN, VALIDATE, env_bad]})
    no_needs = two_jobs.replace("needs: validate", "timeout-minutes: 30")
    assert not passes(no_needs, {"expect": [LOGIN, VALIDATE, quick]})


def test_after_must_reference_earlier_id():
    with pytest.raises(ValueError, match="earlier id"):
        grade_file(BASE, {"expect": [{**VALIDATE, "after": ["nope"]}]}, "workflow")


# --------------------------------------------------------------------------- constraints

CLEANUP = wf(
    """
    on: pull_request
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - run: sf org create scratch -f config/project-scratch-def.json -a ci -v hub
          - run: sf apex run test -o ci -w 30
          - name: Delete
            if: always()
            run: sf org delete scratch -o ci --no-prompt
    """
)
DELETE = {"command": "org delete scratch", "flags": {"no-prompt": True}, "always": True}


def test_always_step_and_job_level():
    assert passes(CLEANUP, {"expect": [DELETE]})
    assert not passes(CLEANUP.replace("if: always()", "if: success()"), {"expect": [DELETE]})
    assert not passes(CLEANUP.replace("        if: always()\n", ""), {"expect": [DELETE]})
    split = wf(
        """
        on: pull_request
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: sf apex run test -o ci -w 30
          cleanup:
            needs: test
            if: ${{ always() }}
            runs-on: ubuntu-latest
            steps:
              - run: sf org delete scratch -o ci --no-prompt
        """
    )
    assert passes(split, {"expect": [DELETE]})
    assert not passes(split.replace("if: ${{ always() }}", "if: success()"), {"expect": [DELETE]})


def test_full_history():
    sgd = {"command": "sgd source delta", "full_history": True}
    base = wf(
        """
        on: pull_request
        jobs:
          delta:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  fetch-depth: 0
              - run: sf sgd source delta --from origin/main --output-dir .
        """
    )
    assert passes(base, {"expect": [sgd]})
    assert passes(base.replace("fetch-depth: 0", "fetch-depth: '0'"), {"expect": [sgd]})
    assert not passes(base.replace("fetch-depth: 0", "fetch-depth: 1"), {"expect": [sgd]})
    unshallow = base.replace("fetch-depth: 0", "fetch-depth: 1").replace(
        "      - run: sf sgd", "      - run: git fetch --unshallow\n      - run: sf sgd"
    )
    assert passes(unshallow, {"expect": [sgd]})


@pytest.mark.parametrize(
    ("run", "shell", "ok"),
    [
        ("sf code-analyzer run -s 2", None, True),
        ("sf code-analyzer run -s 2 | tee out.txt", None, False),
        ("sf code-analyzer run -s 2 | tee out.txt", "bash", True),
        ("set -o pipefail\n  sf code-analyzer run -s 2 | tee out.txt", None, True),
        ("sf code-analyzer run -s 2 || true", None, False),
        ("sf code-analyzer run -s 2 || exit 1", None, True),
        ("sf code-analyzer run -s 2 && echo passed\n  echo done", None, False),
        ("sf code-analyzer run -s 2 && echo passed", None, True),
        ('echo "$(sf code-analyzer run -s 2)"', None, False),
        ("set +e\n  sf code-analyzer run -s 2\n  echo done", None, False),
    ],
)
def test_gating(run: str, shell: str | None, ok: bool):
    shell_line = f"\n        shell: {shell}" if shell else ""
    text = (
        "on: pull_request\njobs:\n  scan:\n    runs-on: ubuntu-latest\n    steps:\n"
        f"      - run: |\n          {run.replace(chr(10) + '  ', chr(10) + '          ')}"
        f"{shell_line}\n"
    )
    exp = {"command": "code-analyzer run", "gating": True}
    assert passes(text, {"expect": [exp]}) is ok


def test_continue_on_error_is_not_gating():
    text = BASE.replace(
        "      - name: Validate\n", "      - name: Validate\n        continue-on-error: true\n"
    )
    assert not passes(text, {"expect": [LOGIN, VALIDATE]})


# --------------------------------------------------------------------------- security


@pytest.mark.parametrize(
    "literal",
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "force://PlatformCLI::5Aep861abc.def@acme.my.salesforce.com",
        "3MVG9" + "A" * 60,
        "00D5g000000abcd!ARoAQJx9z8y7w6v5u4t3s2r1q0pONMLKJIH",
        "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIzTVZHOSJ9.c2lnbmF0dXJlLXZhbHVl",
    ],
)
def test_inline_credentials_fail(literal: str):
    text = BASE.replace('"${{ secrets.SF_CONSUMER_KEY }}"', f"'{literal}'")
    assert "no credentials in the file" in failed(grade_file(text, {}, "workflow"))
    assert passes(text, {"inline_credentials": False})


def test_password_literal_and_secret_reference():
    assert not passes("SF_PASSWORD=Sup3rS3cret!\nsf org list\n", {}, "script")
    assert passes('SF_PASSWORD="$PASSWORD_FROM_VAULT"\nsf org list\n', {}, "script")
    assert passes(BASE, {"secrets": ["SF_JWT_KEY", "SF_USERNAME"]})
    assert not passes(BASE, {"secrets": ["SF_MISSING"]})


def test_script_injection():
    bad = BASE.replace(
        "run: npm install --global @salesforce/cli",
        'run: echo "branch ${{ github.head_ref }}"',
    )
    assert "no script injection" in failed(grade_file(bad, {}, "workflow"))
    ok = BASE.replace(
        "run: npm install --global @salesforce/cli", 'run: echo "base ${{ github.base_ref }}"'
    )
    assert passes(ok, {})


def test_forbid_and_forbid_triggers():
    assert not passes(BASE, {"forbid": [{"command": "project deploy validate"}]})
    assert passes(
        BASE, {"forbid": [{"command": "project deploy start", "flags": {"dry-run": False}}]}
    )
    target = BASE.replace("pull_request:", "pull_request_target:")
    assert not passes(target, {"forbid_triggers": ["pull_request_target"]})


# --------------------------------------------------------------------------- expressions & events


def gh(**kw):
    return {"github": Event(**kw).github()}


def test_expression_evaluator():
    push_main = gh(name="push", branch="main")
    assert evaluate_condition("github.ref == 'refs/heads/main'", push_main) is True
    assert evaluate_condition("${{ github.event_name == 'PUSH' }}", push_main) is True
    assert evaluate_condition("github.event_name == 'pull_request'", push_main) is False
    assert evaluate_condition("startsWith(github.ref, 'refs/tags/')", push_main) is False
    assert evaluate_condition("needs.build.outputs.ok == 'true'", push_main) is UNKNOWN
    cond = "github.event_name == 'pull_request' && needs.build.outputs.ok == 'true'"
    assert evaluate_condition(cond, push_main) is False
    assert evaluate_condition("github.event.pull_request.merged == true", push_main) is False
    pr = gh(name="pull_request", base="develop")
    assert evaluate_condition("github.base_ref == 'develop'", pr) is True
    assert evaluate_condition("contains(github.head_ref, 'feature')", pr) is True
    # implicit success(): a failed dependency skips the job unless a status function is used
    assert evaluate_condition("github.base_ref == 'develop'", pr, success=False) is False
    assert evaluate_condition("always() && github.base_ref == 'develop'", pr, False) is True
    assert evaluate_condition(None, pr, success=False) is False
    assert evaluate_condition("!cancelled()", pr, success=False) is True
    assert evaluate_condition("${{ a }} && ${{ b }}", pr) is True  # string template


def test_filter_patterns():
    assert filter_matches(["main"], "main")
    assert not filter_matches(["main"], "main2")
    assert filter_matches(["release/**"], "release/2026/q3")
    assert not filter_matches(["release/*"], "release/2026/q3")
    assert filter_matches(["releases/**", "!releases/**-alpha"], "releases/1.0")
    assert not filter_matches(["releases/**", "!releases/**-alpha"], "releases/1.0-alpha")
    assert filter_matches(["v[12].*"], "v1.0")


def test_triggers():
    unit = parse_unit(
        wf(
            """
            on:
              push:
                branches: [main, 'release/**']
              pull_request:
                types: [opened, synchronize]
                branches-ignore: [experimental]
              schedule:
                - cron: '0 2 * * *'
              workflow_dispatch:
            jobs:
              a:
                runs-on: ubuntu-latest
                steps: [{run: echo}]
            """
        ),
        "workflow",
    )
    assert triggered(unit, Event("push", branch="main"))
    assert triggered(unit, Event("push", branch="release/1"))
    assert not triggered(unit, Event("push", branch="develop"))
    assert not triggered(unit, Event("push", tag="v1.0"))  # only branch filters
    assert triggered(unit, Event("pull_request", base="main"))
    assert not triggered(unit, Event("pull_request", base="experimental"))
    assert not triggered(unit, Event("pull_request", base="main", action="closed"))
    assert triggered(unit, Event("schedule", cron="00 02 * * 0-6"))
    assert not triggered(unit, Event("schedule", cron="0 3 * * *"))
    assert triggered(unit, Event("workflow_dispatch"))


def test_cron_equivalence():
    assert cron_fires("0 2 * * *") == cron_fires("0 2 * * SUN-SAT")
    assert cron_fires("0 2 * * 1-5") == cron_fires("0 2 * * MON-FRI")
    assert cron_fires("0 2 * * 1-5") != cron_fires("0 2 * * *")
    assert cron_fires("*/15 * * * *") == cron_fires("0,15,30,45 * * * *")
    assert cron_fires("0 2 * *") is None
    assert cron_fires("61 2 * * *") is None


def test_scenarios():
    text = wf(
        """
        on:
          push:
            branches: [main, develop]
          pull_request:
            branches: [main]
        jobs:
          validate:
            runs-on: ubuntu-latest
            steps:
              - run: sf project deploy validate -d force-app -o prod
          deploy:
            needs: validate
            if: github.event_name == 'push' && github.ref == 'refs/heads/main'
            environment: production
            runs-on: ubuntu-latest
            steps:
              - run: sf project deploy quick --job-id "$ID" -o prod
        """
    )
    expect = [
        {"id": "validate", "command": "project deploy validate"},
        {"id": "quick", "command": "project deploy quick", "after": ["validate"]},
    ]
    scenarios = [
        {
            "event": {"name": "pull_request", "base": "main"},
            "runs": ["validate"],
            "skips": ["quick"],
        },
        {"event": {"name": "push", "branch": "main"}, "runs": ["validate", "quick"]},
        {"event": {"name": "push", "branch": "develop"}, "skips": ["quick"]},
        {"event": {"name": "pull_request", "base": "develop"}, "triggered": False},
    ]
    assert passes(text, {"expect": expect, "scenarios": scenarios})
    unguarded = text.replace(
        "    if: github.event_name == 'push' && github.ref == 'refs/heads/main'\n", ""
    )
    detail = failed(grade_file(unguarded, {"expect": expect, "scenarios": scenarios}, "workflow"))
    assert set(detail) == {
        "on pull_request into main: quick does not run",
        "on push develop: quick does not run",
    }
    unit = parse_unit(text, "workflow")
    states = run_states(unit, Event("push", branch="develop"))
    assert list(states.values()) == [True, False]


# --------------------------------------------------------------------------- scripts & entry point


def test_script_strict_mode():
    script = "#!/usr/bin/env bash\nset -euo pipefail\nsf org list\n"
    assert passes(script, {"strict_mode": True}, "script")
    assert not passes("#!/bin/bash\nsf org list\n", {"strict_mode": True}, "script")
    assert passes("#!/bin/bash -e\nsf org list\n", {"strict_mode": True}, "script")


async def test_grader_entry_point_finds_dot_github_file(make_task):
    task = make_task(
        {"format": "files", "files": [".github/workflows/validate.yml"]},
        {"type": "ci_workflow", "expect": [LOGIN, VALIDATE], "secrets": ["SF_JWT_KEY"]},
        reference_output=f"File: .github/workflows/validate.yml\n```yaml\n{BASE}```\n",
    )
    g = await grade(task, extract(task, task.reference_output), GradeEnv())
    assert g.passed, g.summary()
    empty = await grade(task, extract(task, ""), GradeEnv())
    assert not empty.passed


# --------------------------------------------------------------------------- later additions


def test_on_failure_accepts_not_cancelled_and_success_or_failure():
    upload = {"uses": "^actions/upload-artifact@", "on_failure": True}
    base = CLEANUP.replace(
        "run: sf org delete scratch -o ci --no-prompt", "uses: actions/upload-artifact@v4"
    )
    for cond in ("always()", "${{ !cancelled() }}", "success() || failure()"):
        assert passes(base.replace("if: always()", f"if: {cond}"), {"expect": [upload]}), cond
    assert not passes(base.replace("if: always()", "if: failure()"), {"expect": [upload]})


def test_permissions_constraint():
    exp = [{"uses": "upload-sarif@", "permissions": {"security-events": "write"}}]
    body = (
        "on: pull_request\n{perms}jobs:\n  scan:\n    runs-on: ubuntu-latest\n{job_perms}"
        "    steps:\n      - uses: github/codeql-action/upload-sarif@v3\n"
    )
    ok_flow = body.format(
        perms="permissions: { contents: read, security-events: write }\n", job_perms=""
    )
    assert passes(ok_flow, {"expect": exp})
    ok_all = body.format(perms="permissions: write-all\n", job_perms="")
    assert passes(ok_all, {"expect": exp})
    unset = body.format(perms="", job_perms="")
    assert not passes(unset, {"expect": exp})
    # job-level permissions replace the workflow-level ones
    overridden = body.format(
        perms="permissions: write-all\n", job_perms="    permissions:\n      contents: read\n"
    )
    assert not passes(overridden, {"expect": exp})


def test_concurrency_constraint():
    exp = [{"command": "org list", "concurrency": True}]
    base = (
        "on: push\n{c}jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: sf org list\n"
    )
    assert passes(base.format(c="concurrency: prod-deploy\n"), {"expect": exp})
    assert passes(
        base.format(c="concurrency: {group: x, cancel-in-progress: false}\n"), {"expect": exp}
    )
    assert not passes(
        base.format(c="concurrency: {group: x, cancel-in-progress: true}\n"), {"expect": exp}
    )
    assert not passes(base.format(c=""), {"expect": exp})


def test_process_substitution_and_npx():
    cmds = script_cmds('sf org login jwt -i x -o y -f <(echo "$KEY") -a prod\nnpx sf org list\n')
    assert [" ".join(c.tokens) for c in cmds] == [
        "echo $KEY",
        'sf org login jwt -i x -o y -f <(echo "$KEY") -a prod',
        "npx sf org list",
    ]
    assert passes(
        'sf org login jwt -i x -o y -f <(echo "$KEY") -a prod\nnpx sf org list\n', {}, "script"
    )
    unit = parse_unit("npx @salesforce/cli@latest org list\n", "script")
    assert unit.items[0].kind == "sf"
    assert unit.items[0].pc.valid


def test_from_json_in_conditions():
    cond = 'contains(fromJSON(\'["develop", "main"]\'), github.base_ref)'
    assert evaluate_condition(cond, gh(name="pull_request", base="main")) is True
    assert evaluate_condition(cond, gh(name="pull_request", base="release/1")) is False
