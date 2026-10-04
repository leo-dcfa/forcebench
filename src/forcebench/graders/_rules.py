"""A small, declarative rule language for checking JSON values.

Used by the json, http and scratch-def graders. A rule is a mapping with a ``path`` and one
operator, e.g. ``{path: features, contains_ci: [Communities]}``.

Paths are dotted, with ``[n]`` for list indexes: ``settings.communitiesSettings.enableNetworksEnabled``
or ``records[0].attributes.type``. ``$`` (or an empty path) is the root.

Operators:
    equals, equals_ci, not_equals, in, in_ci, regex, exists (bool), absent (bool),
    type ("string" | "number" | "integer" | "boolean" | "array" | "object" | "null"),
    contains (list of items the array must contain), contains_ci, not_contains_ci,
    length, min_length, max_length, min, max,
    absent_or (value: passes if the path is missing OR equals the value),
    any_of (list of rule lists: passes if every rule of at least one list passes),
    subset_of_ci (array items must all be in the given list, case-insensitive),
    match (a JSON value; objects match if they contain the given keys with matching values)
"""

import re
from typing import Any

from forcebench.graders import Check, TaskError

_MISSING = object()
_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve(value: Any, path: str) -> Any:
    if path in ("", "$"):
        return value
    cur = value
    for key, idx in _TOKEN_RE.findall(path.removeprefix("$.")):
        if idx:
            if not isinstance(cur, list) or int(idx) >= len(cur):
                return _MISSING
            cur = cur[int(idx)]
        else:
            if not isinstance(cur, dict):
                return _MISSING
            # tolerate case differences in keys only if there is exactly one candidate
            if key in cur:
                cur = cur[key]
            else:
                cands = [k for k in cur if isinstance(k, str) and k.lower() == key.lower()]
                if len(cands) != 1:
                    return _MISSING
                cur = cur[cands[0]]
    return cur


def _ci(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def _feature_name(v: Any) -> Any:
    # scratch org features may carry a quantity: "MultiCurrency", "ContributorsPlus:5"
    return _ci(v.split(":", 1)[0]) if isinstance(v, str) else v


_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def matches(actual: Any, expected: Any) -> bool:
    """Partial structural match: dicts must contain expected keys; lists match element-wise."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and matches(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(matches(a, e) for a, e in zip(actual, expected, strict=True))
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    return actual == expected


def check_rule(value: Any, rule: dict[str, Any]) -> Check:
    rule = dict(rule)
    path = rule.pop("path", "$")
    name = rule.pop("name", None)
    if len(rule) != 1:
        raise TaskError(f"rule must have exactly one operator: {rule}")
    op, arg = next(iter(rule.items()))
    got = resolve(value, path)
    label = name or f"{path} {op}"
    present = got is not _MISSING
    shown = "<missing>" if not present else repr(got)[:200]

    def ok(passed: bool, why: str = "") -> Check:
        return Check(name=label, passed=passed, detail="" if passed else (why or f"got {shown}"))

    match op:
        case "exists":
            return ok(present == bool(arg))
        case "absent":
            return ok((not present) == bool(arg))
        case "absent_or":
            return ok(not present or matches(got, arg))
        case "any_of":
            for alt in arg:
                if all(check_rule(value, r).passed for r in alt):
                    return ok(True)
            return ok(False, "no alternative matched")
    if not present:
        return ok(False)
    # Membership tests use lists, not sets: answer values may be unhashable (a dict in
    # `features`), and such a value must fail the rule, not crash it.
    match op:
        case "equals":
            return ok(matches(got, arg) and matches(arg, got))
        case "match":
            return ok(matches(got, arg))
        case "equals_ci":
            return ok(_ci(got) == _ci(arg))
        case "not_equals":
            return ok(got != arg)
        case "in":
            return ok(got in arg)
        case "in_ci":
            return ok(_ci(got) in [_ci(a) for a in arg])
        case "regex":
            return ok(isinstance(got, str) and re.search(arg, got) is not None)
        case "type":
            t = _TYPES[arg]
            passed = isinstance(got, t) and not (
                arg in ("number", "integer") and isinstance(got, bool)
            )
            return ok(passed)
        case "contains":
            items = arg if isinstance(arg, list) else [arg]
            return ok(isinstance(got, list) and all(any(matches(g, i) for g in got) for i in items))
        case "contains_ci":
            items = arg if isinstance(arg, list) else [arg]
            have = [_feature_name(g) for g in got] if isinstance(got, list) else []
            missing = [i for i in items if _feature_name(i) not in have]
            return ok(not missing, f"missing {missing}")
        case "not_contains_ci":
            items = arg if isinstance(arg, list) else [arg]
            have = [_feature_name(g) for g in got] if isinstance(got, list) else []
            extra = [i for i in items if _feature_name(i) in have]
            return ok(not extra, f"must not contain {extra}")
        case "subset_of_ci":
            allowed = [_feature_name(a) for a in arg]
            bad = (
                [g for g in got if _feature_name(g) not in allowed]
                if isinstance(got, list)
                else [got]
            )
            return ok(not bad, f"unexpected {bad}")
        case "length":
            return ok(hasattr(got, "__len__") and len(got) == arg)
        case "min_length":
            return ok(hasattr(got, "__len__") and len(got) >= arg)
        case "max_length":
            return ok(hasattr(got, "__len__") and len(got) <= arg)
        case "min":
            return ok(isinstance(got, (int, float)) and got >= arg)
        case "max":
            return ok(isinstance(got, (int, float)) and got <= arg)
    raise TaskError(f"unknown rule operator {op!r}")


def check_rules(value: Any, rules: list[dict[str, Any]]) -> list[Check]:
    return [check_rule(value, r) for r in rules]
