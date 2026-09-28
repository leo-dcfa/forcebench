"""The model registry's effort tiers: a plain thinking switch is not an effort level."""

from __future__ import annotations

from typing import Any

import pytest

from forcebench.models import THINKING_SWITCH, ModelConfig, load_registry


def _config(efforts: list[str], tiers: dict[str, str]) -> dict[str, Any]:
    return {
        "id": "m", "provider": "p", "endpoint_model": "m", "display": "M", "family": "F",
        "base_model": "B", "quant": "Q", "engine": "E", "default_effort": efforts[0],
        "efforts": {e: {} for e in efforts}, "effort_tiers": tiers,
    }  # fmt: skip


def test_every_thinking_switch_in_the_registry_is_tier_on():
    switches = [m for m in load_registry().models.values() if set(m.efforts) <= THINKING_SWITCH]
    assert switches, "the registry has thinking-switch models (Gemma, Qwen3.6, MiMo)"
    for m in switches:
        assert m.effort_tiers == {"off": "off", "on": "on"}, m.id


def test_no_model_claims_max_for_a_thinking_switch():
    for m in load_registry().models.values():
        if "on" in m.efforts:
            assert m.effort_tiers["on"] == "on", m.id


@pytest.mark.parametrize("tier", ["max", "high", "medium", "low", "off"])
def test_a_thinking_switch_mapped_to_a_graded_tier_is_refused(tier):
    with pytest.raises(ValueError, match="a thinking switch is not an effort level"):
        ModelConfig.model_validate(_config(["on", "off"], {"off": "off", "on": tier}))


def test_tier_on_is_only_for_a_thinking_switch():
    with pytest.raises(ValueError, match="tier 'on' is for a plain thinking switch"):
        ModelConfig.model_validate(_config(["low", "high"], {"low": "low", "high": "on"}))
    m = ModelConfig.model_validate(_config(["on"], {"on": "on"}))
    assert m.effort_tiers == {"on": "on"}
