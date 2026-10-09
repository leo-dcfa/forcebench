"""Starting assumptions for the site's estimated local cost (models/hardware.yaml).

What the hardware a local configuration ran on costs to buy and to power, per `hardware` string in
models/local.yaml. They are assumptions, not measurements: the site shows each as an input the
reader can change and labels every result an estimate.
"""

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from forcebench import MODELS_DIR


HARDWARE_FILE = MODELS_DIR / "hardware.yaml"

_URL = r"^https://"


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lifetime_years: float = Field(gt=0)
    utilisation: float = Field(gt=0, le=1)
    electricity_per_kwh: float = Field(ge=0)
    electricity_source: str = Field(pattern=_URL)
    electricity_note: str
    electricity_as_of: date
    currency: str


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    price: float = Field(gt=0)
    price_source: str = Field(pattern=_URL)
    price_note: str
    price_as_of: date
    power_w: float = Field(gt=0)
    power_source: str = Field(pattern=_URL)
    power_note: str
    power_as_of: date


class HardwareCosts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: Defaults
    parts: dict[str, Part]
    hardware: dict[str, dict[str, int]]

    def published(self) -> dict[str, Any]:
        """`local_cost` as published: the defaults; per hardware, its price, power and parts."""
        hardware = {}
        for name, made_of in self.hardware.items():
            parts = [
                {"count": n, **self.parts[p].model_dump(mode="json")} for p, n in made_of.items()
            ]
            hardware[name] = {
                "price": sum(p["count"] * p["price"] for p in parts),
                "power_w": sum(p["count"] * p["power_w"] for p in parts),
                "parts": parts,
            }
        return {"defaults": self.defaults.model_dump(mode="json"), "hardware": hardware}


def load_hardware_costs(path: Path = HARDWARE_FILE) -> HardwareCosts:
    costs = HardwareCosts.model_validate(yaml.safe_load(path.read_text()))
    unknown = sorted({p for made_of in costs.hardware.values() for p in made_of} - set(costs.parts))
    if unknown:
        raise ValueError(f"{path.name}: hardware made of unknown parts {unknown}")
    return costs
