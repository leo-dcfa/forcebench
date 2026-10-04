"""CI shell lexing regressions from an external review: ``ci_workflow`` lexes shell like
``sf_cli`` (``graders/_shell.py``), so ``#`` in ``${VAR#v}`` is not a comment, ``'$VAR'`` is
not substituted, and comments never satisfy or violate text checks."""

import textwrap

from forcebench.graders import _shell, sf_cli
from forcebench.graders.ci import ScriptParser, grade_file


def failed(checks) -> dict[str, str]:
    return {c.name: c.detail for c in checks if not c.passed}


def script_cmds(script: str, variables: dict | None = None) -> list[str]:
    return [" ".join(c.tokens) for c in ScriptParser(script, variables or {}, True, False).parse()]


def test_hash_inside_parameter_expansion_is_not_a_comment():
    assert script_cmds("echo ${TAG#v} done  # a real comment\n") == ["echo ${TAG#v} done"]
    assert script_cmds("sf package version promote --package Acme@${VERSION#v} --no-prompt\n") == [
        "sf package version promote --package Acme@${VERSION#v} --no-prompt"
    ]


def test_single_quoted_variable_stays_literal():
    params = {
        "expect": [{"command": "org login jwt", "flags": {"username": {"secret": "SF_USER"}}}]
    }
    workflow = textwrap.dedent(
        """
        on: push
        jobs:
          deploy:
            runs-on: ubuntu-latest
            env:
              SF_USER: ${{ secrets.SF_USER }}
            steps:
              - run: >-
                  sf org login jwt --client-id "${{ secrets.KEY }}" --jwt-key-file server.key
                  --username QUOTED --alias prod
        """
    )
    assert not failed(grade_file(workflow.replace("QUOTED", '"$SF_USER"'), params, "workflow"))
    bad = failed(grade_file(workflow.replace("QUOTED", "'$SF_USER'"), params, "workflow"))
    assert "--username: got '\\$SF_USER'" in bad["expect: sf org login jwt"]
    # single-quoted and escaped `$` are kept (marked literal); a double-quoted one resolves
    literal = _shell.LITERAL_DOLLAR
    assert script_cmds("echo '$X' \\$X \"$X\"\n", {"X": "v"}) == [f"echo {literal}X {literal}X v"]


def test_text_and_secret_checks_ignore_comments():
    params = {
        "text": [{"name": "allowlist", "any": [r"unsignedPluginAllowList\.json"]}],
        "secrets": ["SF_AUTH_URL"],
    }
    commented = textwrap.dedent(
        """
        on: push  # uses ${{ secrets.SF_AUTH_URL }} later
        jobs:
          deploy:
            runs-on: ubuntu-latest
            steps:
              # TODO: write ~/.config/sf/unsignedPluginAllowList.json
              - run: echo hi  # unsignedPluginAllowList.json
        """
    )
    wanted = {"allowlist", "uses secret SF_AUTH_URL"}
    assert wanted <= set(failed(grade_file(commented, params, "workflow")))
    real = commented.replace(
        "echo hi",
        "echo '[\"sfdx-git-delta\"]' > ~/.config/sf/unsignedPluginAllowList.json && "
        'echo "${{ secrets.SF_AUTH_URL }}" > auth.txt',
    )
    assert not wanted & set(failed(grade_file(real, params, "workflow")))
    # a forbidden pattern mentioned only in a comment is not a violation
    none = {"text": [{"name": "no --no-verify", "none": [r"--no-verify"]}]}
    assert not failed(grade_file("git push  # never use --no-verify\n", none, "script"))


def test_comment_contents_are_not_commands():
    assert script_cmds("sf org list  # old CLIs: `sfdx force:org:list`\n") == ["sf org list"]


def test_ci_and_sf_cli_lex_the_same():
    m = sf_cli.load_manifest()
    for line in [
        "sf org open -o qa --path /x#frag  # opens the page",
        "sf package install -p 04t5f000000AbCdAAK -o uat --installation-key '$KEY'",
        "sf project deploy start -d force-app --wait 2 > deploy.log",
        'sf data query -q "SELECT Id FROM A # x" -o qa 2>&1',
        "sf project deploy start -d force-app &> deploy.log",
    ]:
        direct = sf_cli.parse_line(line, m)[0]
        via_ci = [c.tokens for c in ScriptParser(line + "\n", {}, True, False).parse()]
        assert via_ci == [direct.tokens], line


def test_redirect_of_both_streams_is_not_backgrounding():
    cmds = ScriptParser("sf project deploy start -d force-app &> log.txt\n", {}, True, False)
    assert [c.op_after for c in cmds.parse()] == [None]


def test_quotes_nest_inside_command_substitution():
    # the `#` is inside the inner double quotes: not a comment
    line = 'echo "$(sf data query -q "SELECT Id FROM A # x" --json)"'
    assert _shell.strip_comment(line) == line
    assert _shell.strip_comment("echo $(date)#tag  # comment") == "echo $(date)#tag  "
