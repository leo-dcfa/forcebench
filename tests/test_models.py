"""The model registry's effort tiers: a plain thinking switch is not an effort level."""

from typing import Any

import pytest

from forcebench.models import THINKING_SWITCH, ModelConfig, Provider, load_registry


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
        # Both sides of the switch, or one of them (a served model whose switch is fixed, say).
        assert m.effort_tiers == {e: e for e in m.efforts}, m.id


def test_no_model_claims_max_for_a_thinking_switch():
    for m in load_registry().models.values():
        if "on" in m.efforts:
            assert m.effort_tiers["on"] == "on", m.id


@pytest.mark.parametrize("tier", ["max", "high", "medium", "low", "off"])
def test_a_thinking_switch_mapped_to_a_graded_tier_is_refused(tier):
    with pytest.raises(ValueError, match="a thinking switch is not an effort level"):
        ModelConfig.model_validate(_config(["on", "off"], {"off": "off", "on": tier}))


def test_tier_on_is_only_for_a_thinking_switch():
    with pytest.raises(ValueError, match="tier 'on' is for the 'on' of a plain thinking switch"):
        ModelConfig.model_validate(_config(["low", "high"], {"low": "low", "high": "on"}))
    with pytest.raises(ValueError, match="tier 'on' is for the 'on' of a plain thinking switch"):
        ModelConfig.model_validate(_config(["on", "off"], {"off": "on", "on": "on"}))
    m = ModelConfig.model_validate(_config(["on"], {"on": "on"}))
    assert m.effort_tiers == {"on": "on"}


def test_via_calls_the_proxy_name_and_refuses_what_it_cannot():
    reg = load_registry()
    m = reg.via(reg.get("qwen3.8-27b-awq-int4"), "local")
    assert (m.provider, m.endpoint_model) == ("local", "qwen3.8-27b")
    assert "proxy_model" not in reg.get("qwen3.8-27b-awq-int4").public_dict()
    with pytest.raises(ValueError, match="no proxy_model"):
        reg.via(reg.get("gemma-4-31b-qat-w4a16"), "local")
    with pytest.raises(KeyError, match="unknown provider"):
        reg.via(reg.get("qwen3.8-27b-awq-int4"), "nowhere")


def test_hardware_is_for_local_configurations_and_labels_for_hosted_services():
    with pytest.raises(ValueError, match="a hosted model's hardware is its vendor's"):
        ModelConfig.model_validate(
            {**_config(["on"], {"on": "on"}), "local": False, "hardware": "X"}
        )
    with pytest.raises(ValueError, match="a local server has no public label"):
        Provider.model_validate({"kind": "openai_compatible", "local": True, "label": "Home"})
    reg = load_registry()
    hosted = [m for m in reg.models.values() if not m.local]
    assert hosted, "the registry has hosted models"
    for m in hosted:
        assert reg.provider_for(m).label, f"{m.provider} needs a label: its public name"
