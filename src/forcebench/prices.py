"""List prices of the hosted services API configurations are reached through (prices/*.yaml).

One file per price list, named for the day the prices were taken (``prices/2026-10-10.yaml``),
never edited once published: the leaderboard uses the newest one and records its version, so a
published cost can always be traced to the prices it came from.
"""

from datetime import date
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from forcebench import REPO_ROOT
from forcebench.models import Registry


PRICES_DIR = REPO_ROOT / "prices"

_URL = r"^https://"


class ModelPrice(BaseModel):
    """One configuration's line in a price list, before its provider's defaults are applied."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    cached_input: float = Field(ge=0)
    output: float = Field(ge=0)
    route: list[str] | None = None
    prompt_tokens_max: int | None = Field(default=None, gt=0)
    source: str | None = Field(default=None, pattern=_URL)


class ProviderPrices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, pattern=_URL)
    reasoning: Literal["output"]
    reasoning_source: str = Field(pattern=_URL)
    as_of: date
    models: dict[str, ModelPrice]

    @model_validator(mode="after")
    def _every_price_has_a_source(self) -> Self:
        unsourced = sorted(k for k, m in self.models.items() if not (m.source or self.source))
        if unsourced:
            raise ValueError(f"no source for {unsourced}: give the provider or the model one")
        return self


class Price(BaseModel):
    """A configuration's list price, in ``currency`` per million tokens, with where it is from."""

    model_config = ConfigDict(extra="forbid")

    input: float
    cached_input: float
    output: float
    reasoning: Literal["output"]  # reasoning tokens are billed as output tokens
    currency: str
    source: str
    reasoning_source: str
    as_of: date
    route: list[str] | None = None
    prompt_tokens_max: int | None = None


class PriceList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str  # the file's name: the day the prices were taken
    currency: Literal["USD"]
    providers: dict[str, ProviderPrices]

    def price(self, provider: str, config_id: str) -> Price | None:
        """The list price of configuration ``config_id`` on ``provider``, or None without one."""
        p = self.providers.get(provider)
        m = p.models.get(config_id) if p else None
        if p is None or m is None:
            return None
        return Price(
            input=m.input,
            cached_input=m.cached_input,
            output=m.output,
            reasoning=p.reasoning,
            currency=self.currency,
            source=m.source or p.source or "",
            reasoning_source=p.reasoning_source,
            as_of=p.as_of,
            route=m.route,
            prompt_tokens_max=m.prompt_tokens_max,
        )

    def sources(self) -> list[dict[str, str]]:
        """Every page a price comes from, once each, by provider: what the site links to."""
        seen: dict[str, dict[str, str]] = {}
        for pid, p in self.providers.items():
            for m in p.models.values():
                url = m.source or p.source or ""
                seen.setdefault(url, {"provider": pid, "url": url, "as_of": p.as_of.isoformat()})
            seen.setdefault(
                p.reasoning_source,
                {"provider": pid, "url": p.reasoning_source, "as_of": p.as_of.isoformat()},
            )
        return list(seen.values())


def load_prices(path: Path | None = None) -> PriceList:
    """The price list at ``path``, by default the newest in prices/."""
    if path is None:
        files = sorted(PRICES_DIR.glob("????-??-??.yaml"))
        if not files:
            raise FileNotFoundError(f"no price list in {PRICES_DIR}")
        path = files[-1]
    raw = yaml.safe_load(path.read_text()) or {}
    return PriceList.model_validate({**raw, "version": path.stem})


def check_prices(prices: PriceList, registry: Registry) -> list[str]:
    """What is wrong with a price list against the model configs: each problem, as a sentence.

    Every priced configuration exists, is hosted, is reached through the provider it is listed
    under, and, on a router, is pinned to exactly the route its price is for.
    """
    problems = []
    for pid, p in prices.providers.items():
        for cid, m in p.models.items():
            config = registry.models.get(cid)
            if config is None:
                problems.append(f"{pid}/{cid}: no such configuration in models/")
                continue
            if config.local:
                problems.append(f"{pid}/{cid}: a local configuration has no list price")
            if config.provider != pid:
                problems.append(f"{pid}/{cid}: reached through {config.provider!r}, not {pid!r}")
            pinned = (config.sampling.get("provider") or {}).get("only")
            if (m.route or pinned) and sorted(m.route or []) != sorted(pinned or []):
                problems.append(f"{pid}/{cid}: priced for route {m.route}, pinned to {pinned}")
    return problems
