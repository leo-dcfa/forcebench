"""The harness study's aggregates: intervals, and arms grouped from runs on disk."""

import json

import pytest

from forcebench.agent.harness_study import collect, newcombe, study, wilson

TASK = "lwc-x"


def test_wilson_matches_known_values():
    lo, hi = wilson(5, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.237, 0.763)
    assert wilson(0, 10)[0] == 0.0 and round(wilson(0, 10)[1], 3) == 0.278
    assert wilson(10, 10)[1] == 1.0


def test_newcombe_matches_a_worked_example():
    # Newcombe (1998), method 10: 56/70 against 48/80 gives -0.2 [-0.3339, -0.0524] (p2 - p1 reversed).
    d, low, high = newcombe(56, 70, 48, 80)
    assert round(d, 4) == -0.2
    assert (round(low, 4), round(high, 4)) == (-0.3339, -0.0524)


def _run(root, run_id, harness, skills, outcomes, concurrency=2, tasks=(TASK,), preload=False):
    run = root / run_id
    (run / "raw" / "agent").mkdir(parents=True)
    agent = {"name": harness, "version": "1"}
    if skills:
        agent["skills"] = {"name": skills, "version": "1.0", "skills": ["lwc-guide", "apex-guide"]}
        if preload:
            agent["skills"]["preload"] = {"lwc": ["lwc-guide"]}
    meta = {
        "run_id": run_id, "config_id": "m@low", "effort": "low", "effort_tier": "low",
        "model": {"display": "Model M"}, "agent": agent, "concurrency": concurrency,
        "task_ids": list(tasks),
    }  # fmt: skip
    (run / "run.json").write_text(json.dumps(meta))
    lines = []
    for i, (passed, tools, *loaded) in enumerate(outcomes):
        lines.append(json.dumps({"task_id": TASK, "sample": i, "passed": passed, "output_tokens": 100 * (i + 1),
                                 "input_tokens": 1000, "latency_s": 10.0, "finish_reason": "stop"}))  # fmt: skip
        s = run / "raw" / "agent" / f"{TASK}#{i}"
        s.mkdir()
        s.joinpath("summary.json").write_text(
            json.dumps(
                {
                    "steps": 3,
                    "tools": tools,
                    "skills": loaded[0] if loaded else [],
                    "usage": {"requests": 4, "first_prompt_tokens": 5000 + i},
                }
            )
        )
    (run / "cases.jsonl").write_text("\n".join(lines) + "\n")


@pytest.fixture
def runs(tmp_path):
    _run(tmp_path, "r1", "opencode", None, [(True, {"bash": 2}), (False, {"read": 1})])
    _run(
        tmp_path,
        "r2",
        "claude-code",
        "sf-skills",
        # The second session loaded only Claude Code's own skill: not the pack's.
        [(True, {"Skill": 1, "Bash": 1}, ["lwc-guide"]), (True, {"Skill": 1}, ["verify"])],
    )
    # Not the study's: a leaderboard run of many tasks, a run with preloaded skills, a pilot.
    _run(tmp_path, "r3", "opencode", None, [(True, {})], tasks=(TASK, "apex-y"))
    _run(tmp_path, "r4", "opencode", "sf-skills", [(True, {})], preload=True)
    _run(tmp_path, "r0", "pi", None, [(True, {})])
    return tmp_path


def test_sessions_are_grouped_by_arm_with_their_counts(runs):
    assert ("m@low", "pi", "") in collect(runs, TASK)
    arms = collect(runs, TASK, since="r1")
    assert set(arms) == {("m@low", "opencode", ""), ("m@low", "claude-code", "sf-skills")}

    class T:
        id, suite, difficulty = TASK, "lwc", "medium"

    out = study(runs, T, since="r1")  # type: ignore[arg-type]
    by = {(a["harness"], a["skills"]): a for a in out["arms"]}
    oc, cc = by[("opencode", None)], by[("claude-code", "sf-skills 1.0")]
    assert (oc["sessions"], oc["passed"], oc["skill_sessions"]) == (2, 1, None)
    assert (cc["passed"], cc["skill_sessions"], cc["tool_calls_median"]) == (2, 1, 1.5)
    assert oc["output_tokens"]["median"] == 150.0 and oc["concurrency"] == [2]
    assert oc["first_prompt_tokens_median"] == 5000.5
    [cmp] = out["comparisons"]
    assert cmp["config_id"] == "m@low" and -1 <= cmp["ci_low"] <= cmp["d"] <= cmp["ci_high"] <= 1
