from forcebench.graders._rules import check_rule, resolve

DOC = {
    "edition": "Enterprise",
    "features": ["Communities", "MultiCurrency", "ContributorsPlus:5"],
    "settings": {"lightningExperienceSettings": {"enableS1DesktopEnabled": True}},
    "records": [{"attributes": {"type": "Account"}, "Name": "A"}],
}


def ok(rule):
    return check_rule(DOC, rule).passed


def test_resolve():
    assert resolve(DOC, "records[0].attributes.type") == "Account"
    assert resolve(DOC, "Edition") == "Enterprise"  # case-tolerant key


def test_ops():
    assert ok({"path": "edition", "equals_ci": "enterprise"})
    assert not ok({"path": "edition", "equals": "enterprise"})
    assert ok({"path": "features", "contains_ci": ["communities", "contributorsplus"]})
    assert not ok({"path": "features", "contains_ci": ["PersonAccounts"]})
    assert ok({"path": "features", "not_contains_ci": ["PersonAccounts"]})
    assert ok(
        {"path": "settings.lightningExperienceSettings.enableS1DesktopEnabled", "equals": True}
    )
    assert not ok(
        {"path": "settings.lightningExperienceSettings.enableS1DesktopEnabled", "equals": 1}
    )
    assert ok({"path": "hasSampleData", "absent_or": False})
    assert ok({"path": "orgPreferences", "absent": True})
    assert ok({"path": "records[0]", "match": {"Name": "A"}})
    assert ok(
        {
            "any_of": [
                [{"path": "edition", "equals": "Developer"}],
                [{"path": "edition", "equals": "Enterprise"}],
            ]
        }
    )
