"""The model registry's effort tiers: a plain thinking switch is not an effort level."""

from typing import Any

import pytest
import yaml

from forcebench import MODELS_DIR
from forcebench.models import THINKING_SWITCH, ModelConfig, Provider, load_registry


def _config(efforts: list[str], tiers: dict[str, str]) -> dict[str, Any]:
    return {
        "id": "m", "provider": "p", "endpoint_model": "m", "display": "M", "model_id": "m",
        "developer": "acme", "family": "F", "base_model": "B", "quant": "Q", "engine": "E",
        "default_effort": efforts[0],
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


def test_every_configuration_names_its_developer_a_lab_in_developers_yaml(tmp_path):
    reg = load_registry()
    for m in reg.models.values():
        assert m.developer in reg.developers, f"{m.id}: {m.developer}"
    families = {(m.family, m.developer) for m in reg.models.values()}
    assert ("Gemma-4", "google") in families
    assert ("Claude", "anthropic") in families
    with pytest.raises(ValueError, match="developer"):
        ModelConfig.model_validate(
            {k: v for k, v in _config(["on"], {"on": "on"}).items() if k != "developer"}
        )
    # A lab models/developers.yaml doesn't name is refused when the registry loads.
    for f in ("providers.yaml", "developers.yaml"):
        (tmp_path / f).write_text((MODELS_DIR / f).read_text())
    (tmp_path / "x.yaml").write_text(
        yaml.safe_dump(
            {
                "models": [
                    {**_config(["on"], {"on": "on"}), "provider": "local", "developer": "nobody"}
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="unknown developer 'nobody'"):
        load_registry(tmp_path)


def test_every_configuration_names_its_model_and_one_model_has_one_name(tmp_path):
    reg = load_registry()
    by_model: dict[str, set[tuple[str, str]]] = {}
    for m in reg.models.values():
        by_model.setdefault(m.model_id, set()).add((m.display, m.developer))
    assert all(len(v) == 1 for v in by_model.values()), by_model
    # Grouped by the config, never by the name: the same model served locally and through an API,
    # and two models whose names share a prefix.
    assert reg.get("deepseek-v4.1-flash-native").model_id == "deepseek-v4-1-flash"
    assert reg.get("deepseek-v4.1-flash-deepinfra-fp8").model_id == "deepseek-v4-1-flash"
    assert reg.get("claude-sonnet-5").model_id != reg.get("claude-sonnet-5.5").model_id
    with pytest.raises(ValueError, match="model_id"):
        ModelConfig.model_validate(
            {k: v for k, v in _config(["on"], {"on": "on"}).items() if k != "model_id"}
        )
    with pytest.raises(ValueError, match="model_id"):
        ModelConfig.model_validate({**_config(["on"], {"on": "on"}), "model_id": "Qwen3.8 27B"})
    # Two configurations with one model_id but different names are refused when the registry loads.
    for f in ("providers.yaml", "developers.yaml"):
        (tmp_path / f).write_text((MODELS_DIR / f).read_text())
    base = {**_config(["on"], {"on": "on"}), "provider": "local", "developer": "qwen"}
    (tmp_path / "x.yaml").write_text(
        yaml.safe_dump({"models": [base, {**base, "id": "m2", "display": "Other"}]})
    )
    with pytest.raises(ValueError, match="model_id 'm' is m's, a different model"):
        load_registry(tmp_path)


def test_a_list_price_is_for_hosted_configurations_with_its_source_and_date():
    hosted = {**_config(["on"], {"on": "on"}), "local": False}
    price = {
        "input": 2,
        "output": 10,
        "source": "https://example.com/pricing",
        "as_of": "2026-10-09",
    }
    assert ModelConfig.model_validate({**hosted, "price": price}).price is not None
    with pytest.raises(ValueError, match="a local configuration has no list price"):
        ModelConfig.model_validate({**hosted, "local": True, "price": price})
    with pytest.raises(ValueError, match="as_of"):
        ModelConfig.model_validate(
            {**hosted, "price": {k: v for k, v in price.items() if k != "as_of"}}
        )
    reg = load_registry()
    priced = [m for m in reg.models.values() if m.price]
    assert priced, "the registry has list prices"
    assert all(not m.local for m in priced)
    # Never published with a run: a price is today's, not the run's.
    assert "price" not in priced[0].public_dict()
