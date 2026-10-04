"""Tests for the sf CLI command parser and the ``sf_cli`` grader."""

import asyncio

import pytest

from forcebench.graders import GradeEnv
from forcebench.graders.sf_cli import (
    build_manifest,
    grade_commands,
    grade_params,
    load_manifest,
    match_values,
    norm_value,
    parse_line,
    split_line,
)
from forcebench.tasks import load_suites
from forcebench.validate import validate_tasks


@pytest.fixture(scope="module")
def m():
    return load_manifest()


def one(line: str, m, **kw):
    cmds = parse_line(line, m, **kw)
    assert len(cmds) == 1, cmds
    return cmds[0]


def flags(pc):
    return {n: (st.values if not st.spec.is_bool else st.truthy) for n, st in pc.flags.items()}


# --------------------------------------------------------------------------- manifest


def test_manifest_is_pinned(m):
    assert m.version.startswith("2.")
    assert len(m.commands) > 250
    # core and JIT plugin commands are both present
    assert "project:deploy:start" in m.commands
    assert m.commands["org:create:snapshot"].jit


def test_build_manifest_drops_alias_entries():
    raw = [
        {
            "id": "info:releasenotes:display",
            "aliases": ["whatsnew"],
            "pluginName": "@salesforce/plugin-info",
            "flags": {"version": {"type": "option", "char": "v", "multiple": False}},
            "args": {},
            "strict": True,
        },
        {
            "id": "whatsnew",
            "aliases": ["whatsnew"],
            "pluginName": "@salesforce/plugin-info",
            "flags": {},
            "args": {},
        },
    ]
    out = build_manifest(raw, "2.0.0", {})
    assert list(out["commands"]) == ["info:releasenotes:display"]
    assert out["commands"]["info:releasenotes:display"]["flags"]["version"] == {
        "type": "option",
        "char": "v",
    }


# --------------------------------------------------------------------------- command ids


@pytest.mark.parametrize(
    "line",
    [
        "sf project deploy start -d force-app",
        "sf project:deploy:start -d force-app",
        "sf deploy project start -d force-app",  # flexible taxonomy
    ],
)
def test_command_forms_resolve(m, line):
    pc = one(line, m)
    assert pc.valid, pc.errors
    assert pc.command.id == "project:deploy:start"


def test_longest_match_prefers_subcommand(m):
    assert one("sf apex run test -n Foo", m).command.id == "apex:run:test"
    assert one("sf apex run --file x.apex", m).command.id == "apex:run"


def test_sfdx_executable_fails(m):
    pc = one("sfdx force:source:deploy -p force-app -u uat", m)
    assert not pc.valid
    assert "legacy" in pc.errors[0]


def test_removed_force_command_fails(m):
    pc = one("sf force:source:push -u uat", m)
    assert not pc.valid
    assert "does not exist in sf v2" in pc.errors[0]


def test_deprecated_command_alias(m):
    pc = one("sf force:org:open --target-org uat", m)
    assert pc.command.id == "org:open"
    assert not pc.valid
    assert "deprecated legacy sfdx alias" in pc.errors[0]
    assert one("sf force:org:open --target-org uat", m, allow_deprecated=True).valid


def test_unknown_and_incomplete_commands(m):
    assert "unknown command" in one("sf project frobnicate", m).errors[0]
    assert "incomplete" in one("sf org", m).errors[0]
    assert "no command" in one("sf --json", m).errors[0]


def test_non_sf_command_is_shell(m):
    pc = one("cd my-project", m)
    assert pc.shell and not pc.valid


# --------------------------------------------------------------------------- flags


@pytest.mark.parametrize(
    "line",
    [
        "sf project deploy start --source-dir force-app --target-org uat",
        "sf project deploy start --source-dir=force-app --target-org=uat",
        "sf project deploy start -d force-app -o uat",
        "sf project deploy start -dforce-app -o=uat",
        "sf project deploy start -o uat -- ",
    ],
)
def test_flag_spellings(m, line):
    pc = one(line, m)
    assert pc.valid, pc.errors
    assert pc.flags["target-org"].values == ["uat"]


def test_greedy_multiple_and_repeated(m):
    pc = one("sf project deploy start -o uat -l RunSpecifiedTests --tests A B --tests C", m)
    assert pc.valid, pc.errors
    assert pc.flags["tests"].values == ["A", "B", "C"]


def test_comma_list_is_one_value_without_delimiter(m):
    # project deploy start --tests is not comma-delimited: the CLI warns and keeps one value
    pc = one("sf project deploy start -o uat --tests A,B", m)
    assert pc.flags["tests"].values == ["A,B"]


def test_delimiter_flag_splits(m):
    pc = one("sf apex run test -o uat --class-names A,B -n C", m)
    assert pc.flags["class-names"].values == ["A", "B", "C"]


def test_grouped_boolean_chars(m):
    pc = one("sf project deploy start -o uat -cg", m)
    assert pc.valid, pc.errors
    assert flags(pc)["ignore-conflicts"] is True
    assert flags(pc)["ignore-warnings"] is True


def test_negated_boolean(m):
    pc = one("sf org create scratch -f def.json -v hub --no-track-source", m)
    assert pc.valid, pc.errors
    assert pc.flags["track-source"].negated
    assert not pc.flags["track-source"].truthy


def test_negation_only_when_allowed(m):
    pc = one("sf project deploy start -o uat --no-dry-run", m)
    assert any("unknown flag" in e for e in pc.errors)


@pytest.mark.parametrize(
    "line",
    ["sf project deploy start --dry-run=true -o uat", "sf project deploy start --dry-run true"],
)
def test_boolean_with_value_fails(m, line):
    pc = one(line, m)
    assert any("does not take a value" in e for e in pc.errors), pc.errors


def test_enum_values_are_case_sensitive(m):
    pc = one("sf apex run test -o uat -r JUNIT", m)
    assert any("not one of" in e for e in pc.errors)
    assert one("sf apex run test -o uat -r junit", m).valid


def test_unknown_flag_and_missing_value(m):
    assert any("unknown flag" in e for e in one("sf org open --url /x", m).errors)
    assert any("expects a value" in e for e in one("sf org open --path", m).errors)
    # a value that is itself a flag is not a value
    assert any("expects a value" in e for e in one("sf org open --path -o uat", m).errors)


def test_single_flag_given_twice(m):
    pc = one("sf org open -o uat -o prod", m)
    assert any("only be specified once" in e for e in pc.errors)


def test_double_dash_ends_flags(m):
    pc = one("sf plugins install -- @salesforce/plugin-foo", m)
    assert pc.valid, pc.errors
    assert pc.args == ["@salesforce/plugin-foo"]


def test_negative_number_is_a_value(m):
    pc = one("sf project deploy start -o uat --tests -1", m)
    assert pc.flags["tests"].values == ["-1"]


def test_deprecated_flag_alias(m):
    pc = one("sf apex run test -u uat -n Foo", m)
    assert pc.flags["target-org"].values == ["uat"]
    assert any("deprecated alias" in e for e in pc.errors)
    assert one("sf apex run test -u uat -n Foo", m, allow_deprecated=True).valid
    pc = one("sf apex run test --targetusername uat", m)
    assert any("deprecated alias" in e for e in pc.errors)


def test_deprecated_flag(m):
    pc = one("sf apex run test -o uat --loglevel debug", m)
    assert any("deprecated" in e for e in pc.errors)


def test_required_flags(m):
    pc = one("sf org login jwt --username ci@example.com --jwt-key-file server.key", m)
    assert "missing required flag `--client-id`" in pc.errors
    # target-org / target-dev-hub default from config
    assert one("sf project deploy start -d force-app", m).valid
    assert one("sf package version create -p MyPkg -x -c", m).valid


def test_exclusive_and_depends_on(m):
    pc = one("sf project deploy start -o uat -x package.xml -d force-app", m)
    assert any("cannot be used together" in e for e in pc.errors)
    pc = one("sf project deploy start -o uat -d force-app --post-destructive-changes d.xml", m)
    assert any("requires `--manifest`" in e for e in pc.errors)
    pc = one("sf apex run test -o uat --detailed-coverage", m)
    assert any("requires `--code-coverage`" in e for e in pc.errors)


def test_unexpected_argument(m):
    pc = one("sf project deploy start force-app -o uat", m)
    assert any("unexpected argument" in e for e in pc.errors)


def test_varargs(m):
    assert one("sf config set target-org=uat", m).varargs()[0] == {"target-org": "uat"}
    assert one("sf config set target-org uat", m).varargs()[0] == {"target-org": "uat"}
    pc = one("sf config set --global target-org=uat org-api-version=62.0", m)
    assert pc.valid and pc.varargs()[0] == {"target-org": "uat", "org-api-version": "62.0"}
    assert not one("sf config set target-org uat extra", m).valid
    assert not one("sf alias set", m).valid


# --------------------------------------------------------------------------- shell handling


def test_split_line_operators_and_redirects():
    assert split_line("sf a && sf b; sf c | jq .") == [
        ["sf", "a"],
        ["sf", "b"],
        ["sf", "c"],
        ["jq", "."],
    ]
    assert split_line("sf data query -q 'SELECT Id FROM A' > out.csv 2>&1") == [
        ["sf", "data", "query", "-q", "SELECT Id FROM A"]
    ]
    assert split_line('sf x -q "a && b; c"') == [["sf", "x", "-q", "a && b; c"]]
    with pytest.raises(ValueError):
        split_line("sf x -q 'unbalanced")


def test_env_prefix_is_skipped(m):
    pc = one("SF_LOG_LEVEL=debug sf org list", m)
    assert pc.valid and pc.command.id == "org:list"


def test_unbalanced_quotes_fail(m):
    assert not one("sf data query -q 'SELECT Id", m).valid


# --------------------------------------------------------------------------- matchers


def test_norm_value():
    assert norm_value("./force-app/main/default/") == "force-app/main/default"
    assert norm_value("force-app\\main\\default") == "force-app/main/default"
    assert norm_value("${SF_KEY}") == "$SF_KEY"
    assert norm_value("https://login.salesforce.com/") == "https://login.salesforce.com"
    assert norm_value("/") == "/"


@pytest.mark.parametrize(
    ("spec", "present", "values", "ok"),
    [
        ("uat", True, ["uat"], True),
        ("uat", True, ["UAT"], False),
        ({"ci": "uat"}, True, ["UAT"], True),
        (30, True, ["30"], True),
        (30, True, ["30.0"], True),
        (30, True, ["33"], False),
        ({"one_of": ["a", "b"]}, True, ["b"], True),
        ({"regex": r"04t\w{15}"}, True, ["04t000000000000AAA"], True),
        ({"regex": r"04t"}, True, ["04t000000000000AAA"], False),  # fullmatch
        ({"contains": ["A"]}, True, ["A", "B"], True),
        ({"contains_ci": ["a"]}, True, ["A"], True),
        ({"set": ["A", "B"]}, True, ["B", "A"], True),
        (["A", "B"], True, ["A"], False),
        ({"min": 7, "max": 30}, True, ["14"], True),
        ({"min": 7, "max": 30}, True, ["31"], False),
        ({"count": 2}, True, ["a", "b"], True),
        ({"any": True}, True, ["x"], True),
        ({"any": True}, False, [], False),
        ({"equals": "x", "optional": True}, False, [], True),
        ({"equals": "x", "optional": True}, True, ["y"], False),
        ({"absent": True}, False, [], True),
        (True, True, [], True),
        (False, True, [], False),
        ("a", True, ["a", "a"], False),  # scalar needs exactly one value
    ],
)
def test_match_values(spec, present, values, ok):
    assert (match_values(spec, present, values) is None) is ok


def test_unknown_matcher_key_is_task_error():
    with pytest.raises(ValueError):
        match_values({"equal": "x"}, True, ["x"])


# --------------------------------------------------------------------------- grading


def _passed(checks):
    return all(c.passed for c in checks)


DEPLOY = {
    "expect": [
        {
            "command": "project deploy start",
            "flags": {
                "--source-dir": "force-app/main/default/classes",
                "--target-org": "uat",
                "--test-level": "RunSpecifiedTests",
                "--tests": {"set": ["FooTest", "BarTest"]},
            },
            "forbid": ["--ignore-conflicts"],
        }
    ]
}


@pytest.mark.parametrize(
    ("line", "ok"),
    [
        (
            "sf project deploy start --source-dir force-app/main/default/classes --target-org uat "
            "--test-level RunSpecifiedTests --tests FooTest BarTest",
            True,
        ),
        (
            "sf project deploy start -d ./force-app/main/default/classes/ -o uat -l RunSpecifiedTests -t BarTest -t FooTest",
            True,
        ),
        (
            "sf project:deploy:start --test-level=RunSpecifiedTests --tests=FooTest --tests=BarTest -o=uat -d force-app/main/default/classes",
            True,
        ),
        (
            "sf project deploy start -d force-app/main/default/classes -o uat -l RunSpecifiedTests -t FooTest,BarTest",
            False,
        ),
        (
            "sf project deploy start -d force-app/main/default/classes -o uat -l RunSpecifiedTests -t FooTest BarTest -c",
            False,
        ),
        (
            "sf project deploy start -d force-app/main/default/classes -o uat -l RunLocalTests",
            False,
        ),
        (
            "sfdx force:source:deploy -p force-app/main/default/classes -u uat -l RunSpecifiedTests -r FooTest,BarTest",
            False,
        ),
        (
            "sf project deploy start -d force-app/main/default/classes -u uat -l RunSpecifiedTests -t FooTest BarTest",
            False,
        ),
    ],
)
def test_grade_single_command(m, line, ok):
    assert _passed(grade_commands([line], DEPLOY, m)) is ok


def test_grade_counts_and_order(m):
    params = {
        "expect": [
            {"command": "org create scratch", "flags": {"--alias": "feat"}},
            {"command": "org open", "flags": {"--target-org": "feat"}},
        ]
    }
    create = "sf org create scratch -f config/project-scratch-def.json -a feat -v hub"
    open_ = "sf org open -o feat"
    assert _passed(grade_commands([create, open_], params, m))
    assert _passed(grade_commands([f"{create} && {open_}"], params, m))
    reversed_checks = grade_commands([open_, create], params, m)
    assert not _passed(reversed_checks)
    assert any("out of order" in c.detail for c in reversed_checks)
    assert _passed(grade_commands([open_, create], {**params, "ordered": False}, m))
    extra = "sf org list"
    assert not _passed(grade_commands([create, extra, open_], params, m))
    assert _passed(grade_commands([create, extra, open_], {**params, "allow_extra": True}, m))
    # extras must still be valid commands
    bad_extra = "sf org lst"
    assert not _passed(
        grade_commands([create, bad_extra, open_], {**params, "allow_extra": True}, m)
    )


def test_grade_shell_lines(m):
    params = {"expect": [{"command": "org list"}]}
    assert not _passed(grade_commands(["cd proj", "sf org list"], params, m))
    assert _passed(grade_commands(["cd proj", "sf org list"], {**params, "allow_shell": True}, m))


def test_grade_any_of(m):
    params = {
        "expect": [
            {
                "any_of": [
                    {"command": "project deploy validate", "flags": {"--manifest": "package.xml"}},
                    {
                        "command": "project deploy start",
                        "flags": {"--manifest": "package.xml", "--dry-run": True},
                    },
                ]
            }
        ]
    }
    assert _passed(grade_commands(["sf project deploy validate -x package.xml -o prod"], params, m))
    assert _passed(grade_commands(["sf project deploy start --dry-run -x package.xml"], params, m))
    assert not _passed(grade_commands(["sf project deploy start -x package.xml"], params, m))


def test_grade_vars(m):
    params = {"expect": [{"command": "config set", "vars": {"target-org": "uat"}}]}
    assert _passed(grade_commands(["sf config set target-org=uat"], params, m))
    assert _passed(grade_commands(["sf config set target-org uat"], params, m))
    assert not _passed(grade_commands(["sf config set defaultusername=uat"], params, m))


def test_task_errors_raise(m):
    with pytest.raises(ValueError):
        grade_commands(["sf org list"], {"expect": [{"command": "org lst"}]}, m)
    with pytest.raises(ValueError):
        grade_commands(
            ["sf org list"], {"expect": [{"command": "org list", "flags": {"--nope": 1}}]}, m
        )


def test_redirects_are_captured(m):
    pc = one("sf apex run -o uat < scripts/fix.apex", m)
    assert pc.valid and pc.stdin == "scripts/fix.apex"
    pc = one("cat scripts/fix.apex | sf apex run -o uat", m)
    assert pc.valid and pc.stdin == "scripts/fix.apex"
    pc = one("sf data query -q 'SELECT Id FROM Account' -r csv > out.csv 2> err.log", m)
    assert pc.valid and pc.stdout == "out.csv"
    # a numeric flag value right before a redirect is not a file descriptor
    pc = one("sf project deploy start -d force-app --wait 10 > deploy.log", m)
    assert pc.flags["wait"].values == ["10"] and pc.stdout == "deploy.log"


def test_grade_stdin_matcher(m):
    params = {
        "expect": [
            {
                "any_of": [
                    {"command": "apex run", "flags": {"--file": "x.apex"}},
                    {"command": "apex run", "flags": {"--file": False}, "stdin": "x.apex"},
                ]
            }
        ]
    }
    assert _passed(grade_commands(["sf apex run -o uat < x.apex"], params, m))
    assert _passed(grade_commands(["sf apex run -o uat -f ./x.apex"], params, m))
    assert not _passed(grade_commands(["sf apex run -o uat < y.apex"], params, m))


def test_grade_params_top_level_any_of(m):
    params = {
        "any_of": [
            {"expect": [{"command": "config set", "vars": {"a": "1", "b": "2"}}]},
            {
                "ordered": False,
                "expect": [
                    {"command": "config set", "vars": {"a": "1"}},
                    {"command": "config set", "vars": {"b": "2"}},
                ],
            },
        ]
    }
    assert _passed(grade_params(["sf config set a=1 b=2"], params, m))
    assert _passed(grade_params(["sf config set b=2", "sf config set a 1"], params, m))
    assert not _passed(grade_params(["sf config set a=1"], params, m))


def test_cli_suite_oracles():
    """Every cli task: reference and alternatives pass, empty and negatives fail."""
    (suite,) = load_suites(["cli"])
    results = asyncio.run(validate_tasks(suite.tasks, GradeEnv()))
    problems = {r.task.id: r.problems for r in results if r.problems or r.skipped}
    assert not problems
    assert len(suite.tasks) >= 18


def test_boolean_then_value_on_varargs_command(m):
    pc = one("sf config set org-metadata-rest-deploy --global true", m)
    assert pc.valid, pc.errors
    assert pc.varargs()[0] == {"org-metadata-rest-deploy": "true"}


# --------------------------------------------------------------------------- shell fidelity


def test_literal_dollar_is_not_the_variable(m):
    params = {"expect": [{"command": "package install", "flags": {"--installation-key": "$KEY"}}]}
    base = "sf package install -p 04t5f000000AbCdAAK -o uat --installation-key "
    assert _passed(grade_commands([base + '"$KEY"'], params, m))
    assert _passed(grade_commands([base + "${KEY}"], params, m))
    assert _passed(grade_commands([base + '"${KEY:?missing key}"'], params, m))
    checks = grade_commands([base + "'$KEY'"], params, m)
    assert not _passed(checks)
    assert any("\\$KEY" in c.detail for c in checks)
    assert not _passed(grade_commands([base + "\\$KEY"], params, m))


def test_adjacent_file_descriptors(m):
    pc = one("sf project deploy start -d force-app --wait 2 > deploy.log", m)
    assert pc.flags["wait"].values == ["2"] and pc.stdout == "deploy.log"
    pc = one("sf data query -q 'SELECT Id FROM Account' 1> out.txt 2>&1", m)
    assert pc.valid and pc.stdout == "out.txt"
    pc = one("sf apex run -o uat 0< fix.apex", m)
    assert pc.valid and pc.stdin == "fix.apex"


def test_comments_only_at_word_start(m):
    pc = one("sf org open -o qa --path /x#frag  # opens the page", m)
    assert pc.valid and pc.flags["path"].values == ["/x#frag"]


def test_launcher_flags_are_ignored(m):
    pc = one("sf --dev-debug --debug-filter sf:core project deploy start -d force-app", m)
    assert pc.valid, pc.errors
    assert pc.command.id == "project:deploy:start"


def test_deprecated_config_keys(m):
    pc = one("sf config set defaultusername=uat", m)
    assert any("config key `defaultusername` is deprecated" in e for e in pc.errors)
    params = {"expect": [{"command": "config set", "vars": {"target-org": "uat"}}]}
    assert not _passed(grade_commands(["sf config set defaultusername=uat"], params, m))
    lenient = {**params, "allow_deprecated": True}
    assert _passed(grade_commands(["sf config set defaultusername=uat"], lenient, m))


def test_dynamic_default_required_flags(m):
    # requiredOrg flags fall back to config; plain required flags do not
    assert m.commands["agent:generate:template"].flags["source-org"].dynamic_default
    assert not m.commands["org:create:snapshot"].flags["source-org"].dynamic_default
    assert not any("source-org" in e for e in one("sf agent generate template", m).errors)
    assert any(
        "missing required flag `--source-org`" in e
        for e in one("sf org create snapshot --name snap1", m).errors
    )
