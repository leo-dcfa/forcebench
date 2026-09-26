"""Offline tests for the ``scratch_def`` and ``sfdx_project`` graders (no org needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forcebench.answers import extract
from forcebench.graders import GradeEnv, grade
from forcebench.graders.scratch_def import (
    build_shape,
    classify_failure,
    feature_problems,
    project_problems,
    settings_field_problems,
    settings_file,
    settings_naming_problems,
    structure_problems,
)

KNOWN = frozenset({"AccountSettings", "LightningExperienceSettings"})


def reply(value: dict) -> str:
    return "```json\n" + json.dumps(value) + "\n```"


@pytest.fixture
def scratch_task(make_task):
    def _make(rules=None, type_="scratch_def"):
        return make_task({"format": "json"}, {"type": type_, "rules": rules or []})

    return _make


# --------------------------------------------------------------------------- structure


def test_structure_accepts_documented_options_and_custom_fields():
    defn = {
        "$schema": "x",
        "orgName": "Acme",
        "edition": "Enterprise",
        "country": "AU",
        "language": "en_US",
        "adminEmail": "a@example.com",
        "hasSampleData": True,
        "release": "preview",
        "workitem__c": "W-1",
        "features": "Communities;ServiceCloud",
        "objectSettings": {"opportunity": {"sharingModel": "private"}},
    }
    assert structure_problems(defn) == []


@pytest.mark.parametrize(
    ("defn", "fragment"),
    [
        ({"edition": "Developer", "orgPreferences": {"enabled": ["X"]}}, "orgPreferences"),
        ({"edition": "Developer", "durationDays": 7}, "--duration-days"),
        ({"edition": "Developer", "Country": "AU"}, "InvalidJsonCasing"),
        ({"edition": "Developer", "timeZone": "Australia/Sydney"}, "unknown option"),
        ({"edition": "enterprise"}, "is not one of"),
        ({"orgName": "no edition"}, "'edition' is a required property"),
        ({"edition": "Developer", "language": "de_DE"}, "did you mean 'de'"),
        ({"edition": "Developer", "country": "au"}, "two-letter"),
        ({"edition": "Developer", "hasSampleData": "true"}, "not of type 'boolean'"),
        ({"edition": "Developer", "settings": {"accountSettings": True}}, "must be an object"),
        ({"snapshot": "snap1", "edition": "Developer"}, "snapshot"),
    ],
)
def test_structure_rejects(defn, fragment):
    assert any(fragment in p for p in structure_problems(defn)), structure_problems(defn)


# --------------------------------------------------------------------------- features


def test_features_valid_forms():
    assert feature_problems(["communities", "FieldService:5", "FieldServiceMobileUser: 3"]) == []


@pytest.mark.parametrize(
    ("feature", "fragment"),
    [
        ("PersonAccount", "did you mean 'PersonAccounts'"),
        ("MultiCurrency", "000395964"),
        ("EditInSubtab", "deprecated"),
        ("FieldService:26", "outside the supported quantity 1-25"),
        ("Communities:x", "Name:<quantity>"),
    ],
)
def test_features_invalid(feature, fragment):
    problems = feature_problems([feature])
    assert any(fragment in p for p in problems), problems


# --------------------------------------------------------------------------- settings


def test_settings_naming():
    assert settings_naming_problems("lightningExperienceSettings") == []
    assert "lower camel case" in settings_naming_problems("LightningExperienceSettings")[0]
    assert "type name" in settings_naming_problems("lightningExperience")[0]


def test_settings_catalog_fields_and_values():
    ok = {
        "sessionSettings": {"sessionTimeout": "TwoHours"},
        "passwordPolicies": {"minimumPasswordLength": 12, "expiration": "NinetyDays"},
    }
    assert settings_field_problems("securitySettings", ok) == []
    assert settings_field_problems("chatterSettings", {"enableChatter": "true"}) == []
    bad = settings_field_problems(
        "securitySettings",
        {"sessionTimeout": "TwoHours", "sessionSettings": {"sessionTimeout": "2h"}},
    )
    assert any("has no field 'sessionTimeout'" in p for p in bad)
    assert any("'2h' is not a valid SessionTimeout" in p for p in bad)
    assert (
        "not a valid boolean"
        in settings_field_problems("chatterSettings", {"enableChatter": "yes"})[0]
    )
    assert "not a Metadata API Settings type" in settings_field_problems("fooSettings", {})[0]
    assert "removed" in settings_field_problems("orgPreferenceSettings", {})[0]


def test_settings_repeated_fields():
    one = {"forecastingTypeSettings": [{"name": "OpportunityRevenue", "active": True}]}
    assert settings_field_problems("forecastingSettings", {"enableForecasts": True, **one}) == []
    dup = settings_field_problems("nameSettings", {"enableMiddleName": [True, False]})
    assert "not repeatable" in dup[0]


def test_settings_file_matches_salesforce_cli():
    # js2xmlparser output captured from Salesforce CLI's scratchOrgSettingsGenerator
    expected = (
        "<?xml version='1.0'?>\n<CaseSettings>\n"
        "    <systemUserEmail>a&lt;b>&amp;c@example.com</systemUserEmail>\n"
        "    <defaultCaseOwner/>\n    <emailToCase/>\n    <x>null</x>\n    <y>1.5</y>\n"
        "</CaseSettings>\n"
    )
    value = {
        "systemUserEmail": "a<b>&c@example.com",
        "defaultCaseOwner": "",
        "caseFeedItemSettings": [],
        "emailToCase": {},
        "x": None,
        "y": 1.5,
    }
    assert settings_file("CaseSettings", value) == expected


def test_object_shape_matches_salesforce_cli(tmp_path: Path):
    objs = {"opportunity": {"sharingModel": "private", "defaultRecordType": "standard"}}
    build_shape(tmp_path, {"accountSettings": {"enableAccountTeams": True}}, objs, "67.0")
    assert (tmp_path / "objects" / "Opportunity.object").read_text() == (
        "<?xml version='1.0'?>\n"
        "<CustomObject xmlns='http://soap.sforce.com/2006/04/metadata'>\n"
        "    <sharingModel>Private</sharingModel>\n"
        "    <recordTypes>\n"
        "        <fullName>Standard</fullName>\n"
        "        <label>Standard</label>\n"
        "        <active>true</active>\n"
        "        <businessProcess>StandardProcess</businessProcess>\n"
        "    </recordTypes>\n"
        "    <businessProcesses>\n"
        "        <fullName>StandardProcess</fullName>\n"
        "        <isActive>true</isActive>\n"
        "        <values>\n"
        "            <fullName>Prospecting</fullName>\n"
        "        </values>\n"
        "    </businessProcesses>\n"
        "</CustomObject>\n"
    )
    package = (tmp_path / "package.xml").read_text()
    assert "<members>Account</members>" in package
    assert "<members>Opportunity.StandardProcess</members>" in package
    assert "<version>67.0</version>" in package


@pytest.mark.parametrize(
    ("problem", "definite"),
    [
        ("Error parsing file: Element {}enableFoo invalid at this location in type X", True),
        ("Not available for deploy for this API version", True),
        ("ControlledByParent is not a valid sharing model for Case", True),
        ("The object 'Foo' of type Settings metadata does not exist.", True),
        ("The object 'Account' of type Settings metadata does not exist.", False),
        ("Not available for deploy for this organization", False),
        ("enableDeterministicEncryption", False),
        ('Cannot invoke "java.lang.Boolean.booleanValue()"', False),
    ],
)
def test_classify_failure(problem, definite):
    assert classify_failure(problem, KNOWN) is definite


# --------------------------------------------------------------------------- grader


async def test_grader_offline_reports_skipped_org_check(scratch_task):
    task = scratch_task([{"path": "features", "contains_ci": ["PersonAccounts"]}])
    defn = {
        "edition": "Enterprise",
        "features": ["PersonAccounts"],
        "settings": {"lightningExperienceSettings": {"enableS1DesktopEnabled": "true"}},
    }
    g = await grade(task, extract(task, reply(defn)), GradeEnv())
    assert g.passed, g.summary()
    settings = next(c for c in g.checks if c.name == "settings")
    assert "org check skipped (no grader org registered for profile 'scratchdef')" in (
        settings.detail
    )


async def test_grader_never_uses_the_base_org(scratch_task, monkeypatch):
    """The base grader org holds seeded data for other suites: never deploy there."""
    from forcebench.graders import scratch_def

    async def boom(*args, **kwargs):
        raise AssertionError("deployed to an org")

    monkeypatch.setattr(scratch_def, "org_settings_check", boom)
    task = scratch_task()
    defn = {"edition": "Developer", "settings": {"chatterSettings": {"enableChatter": True}}}
    g = await grade(task, extract(task, reply(defn)), GradeEnv(orgs={"base": ["fb-base"]}))
    assert g.passed, g.summary()


async def test_side_effect_settings_are_never_deployed(scratch_task, monkeypatch):
    from forcebench.graders import scratch_def

    deployed: list[dict] = []

    async def fake(env, alias, settings, objects, tag):
        deployed.append({"settings": settings, "objects": objects})
        return {"settings": {}, "objects": {}, "other": []}

    monkeypatch.setattr(scratch_def, "org_settings_check", fake)
    task = scratch_task()
    defn = {
        "edition": "Enterprise",
        "settings": {
            "companySettings": {"fiscalYear": {"startMonth": "July"}},
            "currencySettings": {"enableMultiCurrency": True},
            "industriesLoyaltySettings": {"enableConfigureClubs": True},
            "accountSettings": {"enableReportsToOnPersonAccount": True},
            "chatterSettings": {"enableChatter": True},
        },
        "objectSettings": {"opportunity": {"sharingModel": "private"}},
    }
    env = GradeEnv(orgs={"scratchdef": ["fb-scratchdef"]})
    g = await grade(task, extract(task, reply(defn)), env)
    assert g.passed, g.summary()
    assert deployed == [{"settings": {"chatterSettings": {"enableChatter": True}}, "objects": {}}]
    detail = next(c for c in g.checks if c.name == "settings").detail
    assert "settings.companySettings" in detail and "objectSettings.opportunity" in detail

    only_risky = {
        "edition": "Enterprise",
        "settings": {"currencySettings": {"enableMultiCurrency": True}},
    }
    deployed.clear()
    g = await grade(task, extract(task, reply(only_risky)), env)
    assert g.passed and deployed == []
    assert "nothing safe to deploy" in next(c for c in g.checks if c.name == "settings").detail


def test_documented_values_offline():
    assert (
        settings_field_problems(
            "companySettings",
            {"fiscalYear": {"startMonth": "July", "fiscalYearNameBasedOn": "endingMonth"}},
        )
        == []
    )
    bad = settings_field_problems("companySettings", {"fiscalYear": {"startMonth": "7"}})
    assert "is not one of" in bad[0]


async def test_grader_rules_see_normalised_booleans(scratch_task):
    rule = {"path": "settings.communitiesSettings.enableNetworksEnabled", "equals": True}
    task = scratch_task([rule])
    defn = {
        "edition": "Enterprise",
        "settings": {"communitiesSettings": {"enableNetworksEnabled": "true"}},
    }
    assert (await grade(task, extract(task, reply(defn)), GradeEnv())).passed
    defn["settings"]["communitiesSettings"]["enableNetworksEnabled"] = "TRUE"
    g = await grade(task, extract(task, reply(defn)), GradeEnv())
    assert not g.passed  # xsd:boolean is case-sensitive
    assert "not a valid boolean" in g.summary()


# --------------------------------------------------------------------------- sfdx-project.json


def test_project_problems():
    good = {
        "packageDirectories": [
            {"path": "core", "package": "Core", "versionNumber": "1.0.0.NEXT"},
            {
                "path": "app",
                "default": True,
                "package": "App",
                "versionNumber": "2.0.0.NEXT",
                "ancestorVersion": "HIGHEST",
                "dependencies": [{"package": "Core", "versionNumber": "1.0.0.LATEST"}],
            },
        ],
        "namespace": "acme",
        "sourceApiVersion": "67.0",
        "packageAliases": {"Core": "0Ho5e000000XbCdCAK", "App": "0Ho5e000000XbCiCAK"},
    }
    assert project_problems(good) == []
    bad = json.loads(json.dumps(good))
    bad["packageDirectories"][0]["default"] = True
    bad["packageDirectories"][1]["dependencies"][0] = {"package": "Kore", "versionNumber": "1.0"}
    bad["packageAliases"]["Core"] = "Core"
    bad["namespace"] = "acme__"
    problems = " | ".join(project_problems(bad))
    for fragment in (
        "exactly one package directory must be the default",
        "neither an ID nor a packageAliases entry",
        "LATEST|RELEASED",
        "is not a package (0Ho) or version (04t) ID",
        "namespace",
    ):
        assert fragment in problems, problems


async def test_sfdx_project_rules_use_normalised_paths(scratch_task):
    task = scratch_task(
        [{"path": "packageDirectories", "contains": [{"path": "force-app"}]}], "sfdx_project"
    )
    proj = {"packageDirectories": [{"path": "./force-app/", "default": True}]}
    assert (await grade(task, extract(task, reply(proj)), GradeEnv())).passed
