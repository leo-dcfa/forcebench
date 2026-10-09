"""Starting assumptions for the estimated local cost (models/hardware.yaml)."""

from forcebench.local_cost import load_hardware_costs
from forcebench.models import load_registry


def test_every_local_hardware_has_assumptions_with_sources_and_dates():
    costs = load_hardware_costs()
    used = {m.hardware for m in load_registry().models.values() if m.local and m.hardware}
    assert used <= set(costs.hardware), f"no assumptions for {sorted(used - set(costs.hardware))}"
    for part in costs.parts.values():
        assert part.price_source.startswith("https://")
        assert part.power_source.startswith("https://")
        assert part.price_note
        assert part.power_note
    assert costs.defaults.electricity_source.startswith("https://")


def test_a_machine_of_several_parts_adds_them_up():
    hw = load_hardware_costs().published()["hardware"]
    (three,) = (v for k, v in hw.items() if k.startswith("3"))  # 2 DGX Spark, 1 AI TOP ATOM
    assert three["price"] == 2 * 4699 + 4700
    assert three["power_w"] == 3 * 240
    assert sorted(p["count"] for p in three["parts"]) == [1, 2]


def test_the_file_is_not_a_model_config():
    assert "hardware" not in {m.id for m in load_registry().models.values()}
