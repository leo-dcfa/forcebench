"""Model registry.

``models/providers.yaml`` says how to reach each provider (endpoint and key come from
environment variables, never from the repo). ``models/developers.yaml`` names the companies and
labs that develop the models. The other ``models/*.yaml`` list model configurations: one entry
per served model *and quantisation*, with the effort levels it supports.

A benchmark configuration is ``<model id>@<effort>``: the same weights at a different
reasoning effort are a different entry on the leaderboard.
"""

import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from forcebench import MODELS_DIR, REPO_ROOT


# The common scale effort labels map to, for comparing models: "off", then the graded levels
# low < medium < high < max. "on" is the tier of a plain thinking switch (efforts "off" and "on"
# only) switched on: thinking at the model's own default depth, which no request set, so it is
# not a level on the graded scale (docs/methodology.md, section 4).
EffortTier = Literal["off", "on", "low", "medium", "high", "max"]
THINKING_SWITCH = frozenset({"off", "on"})


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["openai_compatible", "openai", "anthropic", "google"]
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None
    # A server the operator runs, on this machine or a private network; never a vendor's API.
    # Off unless the provider says so. Private-pool tasks of tier `private` go only to such a
    # server, and only while its base URL resolves to a private address (pool.served_locally).
    local: bool = False
    # A service that chooses the reasoning effort itself (a gateway): runs through it record the
    # effort Forcebench inferred from their behaviour, and the leaderboard marks it inferred.
    sets_effort: bool = False
    # The hosted service's public name, shown on the leaderboard's API board (e.g. "Anthropic").
    # Never an address, and never the name of a service whose address is private.
    label: str | None = None
    # A router that names the upstream provider that served each answer (OpenRouter). Only such a
    # service's answers record `served_by`: any other server's `provider` field (a gateway, a
    # proxy, a local engine) is not recorded, so no server's name reaches the published cases.
    reports_upstream: bool = False

    @model_validator(mode="after")
    def _local_is_a_server(self) -> Provider:
        if self.local and self.kind != "openai_compatible":
            raise ValueError("only an OpenAI-compatible server the operator runs can be local")
        if self.local and self.label:
            raise ValueError("a local server has no public label: the leaderboard shows hardware")
        return self

    def resolved_base_url(self) -> str | None:
        if self.base_url_env and os.environ.get(self.base_url_env):
            return os.environ[self.base_url_env]
        return self.base_url

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


_EFFORT_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")


class Developer(BaseModel):
    """A company or lab that develops models (models/developers.yaml), whatever serves them."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)  # as it names itself, e.g. "Anthropic", "Z.ai"


class Price(BaseModel):
    """A hosted service's published list price for a model, in USD per million tokens."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)  # reasoning included: it is billed as output
    source: str = Field(pattern=r"^https://")  # where the service publishes it
    as_of: date  # the day it was taken from there


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]*$")
    provider: str
    endpoint_model: str  # the model name the endpoint expects
    # The name a proxy serves it under (`forcebench run --via <provider>`), e.g. LiteLLM's.
    proxy_model: str | None = None
    display: str  # e.g. "Qwen3.8 27B"
    # The model, whatever its effort, quantisation, engine or service: every configuration of the
    # same model has the same id (e.g. "qwen3-8-27b"), so the leaderboard can group them without
    # matching names. Configurations with one model_id have one display name and developer.
    model_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    # Who develops the model, an id in models/developers.yaml: the lab, never the service that
    # serves it (a gateway, a cloud). The leaderboard publishes it; the site shows its logo.
    developer: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    family: str  # e.g. "Qwen3.8"
    base_model: str  # the unquantised model, groups quantisations together
    quant: str  # e.g. "BF16", "FP8", "AWQ-INT4", "MLX 4-bit"
    engine: str  # inference engine, e.g. "vLLM", "SGLang", "MLX"
    open_weights: bool = True
    local: bool = True
    # What a local configuration is served on: the accelerator's model and count, or the
    # computer's model and memory, e.g. "Apple M5 Max (40-core GPU, 128 GB)". Published, so a
    # hardware model only, never a machine's name. None for a hosted model, or while unknown.
    hardware: str | None = None
    # A hosted configuration's list price on the service it is reached through, for the site's
    # cost estimates. None for a local configuration, or when the service publishes none.
    price: Price | None = None
    context: int | None = None
    max_tokens: int = 32768
    # Vendor-recommended sampling. Keys other than temperature/top_p go in the request body.
    sampling: dict[str, Any] = Field(default_factory=dict)
    default_effort: str
    # effort label -> extra request body fields that select it
    efforts: dict[str, dict[str, Any]]
    # effort label -> normalised tier, for comparing across models
    effort_tiers: dict[str, EffortTier]
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> ModelConfig:
        if self.default_effort not in self.efforts:
            raise ValueError(f"{self.id}: default_effort {self.default_effort!r} not in efforts")
        if self.hardware and not self.local:
            raise ValueError(f"{self.id}: a hosted model's hardware is its vendor's, not recorded")
        if self.price and self.local:
            raise ValueError(f"{self.id}: a local configuration has no list price")
        missing = set(self.efforts) - set(self.effort_tiers)
        if missing:
            raise ValueError(f"{self.id}: effort_tiers missing {sorted(missing)}")
        switch = set(self.efforts) <= THINKING_SWITCH
        for label, tier in self.effort_tiers.items():
            if tier == "on" and not (switch and label == "on"):
                raise ValueError(
                    f"{self.id}: tier 'on' is for the 'on' of a plain thinking switch (efforts "
                    "'off' and 'on' only); map graded effort levels to low, medium, high or max"
                )
            if switch and label == "on" and tier != "on":
                raise ValueError(
                    f"{self.id}: a thinking switch is not an effort level: map 'on' to tier 'on', "
                    f"not {tier!r}"
                )
        # The effort is part of the run id and so of a directory name (runner.RUN_ID_RE).
        bad = sorted(e for e in self.efforts if not _EFFORT_RE.fullmatch(e))
        if bad:
            raise ValueError(
                f"{self.id}: effort names must be lower-case letters, digits, '_', '.' or '-': "
                f"{bad}"
            )
        return self

    def public_dict(self) -> dict[str, Any]:
        """Everything worth publishing about this configuration (no endpoints or keys)."""
        return self.model_dump(exclude={"provider", "endpoint_model", "proxy_model", "price"})


class Registry(BaseModel):
    providers: dict[str, Provider]
    models: dict[str, ModelConfig]
    developers: dict[str, Developer] = Field(default_factory=dict)

    def get(self, model_id: str) -> ModelConfig:
        try:
            return self.models[model_id]
        except KeyError:
            raise KeyError(f"unknown model {model_id!r}; have {sorted(self.models)}") from None

    def provider_for(self, m: ModelConfig) -> Provider:
        return self.providers[m.provider]

    def via(self, m: ModelConfig, provider: str) -> ModelConfig:
        """``m`` called through another provider (a proxy), under its ``proxy_model`` name."""
        if provider not in self.providers:
            raise KeyError(f"unknown provider {provider!r}; have {sorted(self.providers)}")
        if not m.proxy_model:
            raise ValueError(f"{m.id} has no proxy_model: the name {provider!r} serves it under")
        return m.model_copy(update={"provider": provider, "endpoint_model": m.proxy_model})


# Safety switches come only from the environment the sandbox and the Makefile set, never from
# .env (which `run` loads inside the networked sandbox, see docs/sandbox.md).
DOTENV_IGNORED = frozenset(
    {
        "HOME",
        "FORCEBENCH_SANDBOX",
        "FORCEBENCH_PROVISION",
        "FORCEBENCH_DEVHUB_USERNAME",
        "FORCEBENCH_CACHE_DIR",
        "FORCEBENCH_LWC_OFFLINE",
        "FORCEBENCH_LWC_SANDBOX",
        "FORCEBENCH_LWC_WORKSPACE",
    }
)


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); existing environment variables win.

    Keys in DOTENV_IGNORED are skipped with a warning.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in DOTENV_IGNORED:
            print(  # noqa: T201 (a warning for the operator, on stderr)
                f"WARNING: ignoring {key} in {path.name}: set only by the sandbox", file=sys.stderr
            )
            continue
        os.environ.setdefault(key, val.strip().strip("'\""))


def load_registry(models_dir: Path = MODELS_DIR) -> Registry:
    load_dotenv()
    providers = {
        k: Provider.model_validate(v)
        for k, v in yaml.safe_load((models_dir / "providers.yaml").read_text()).items()
    }
    developers = {
        k: Developer.model_validate(v)
        for k, v in (yaml.safe_load((models_dir / "developers.yaml").read_text()) or {}).items()
    }
    models: dict[str, ModelConfig] = {}
    for path in sorted(models_dir.glob("*.yaml")):
        if path.name in ("providers.yaml", "developers.yaml"):
            continue
        for entry in yaml.safe_load(path.read_text()).get("models", []):
            m = ModelConfig.model_validate(entry)
            if m.id in models:
                raise ValueError(f"duplicate model id {m.id} in {path}")
            if m.provider not in providers:
                raise ValueError(f"{m.id}: unknown provider {m.provider!r}")
            if m.developer not in developers:
                raise ValueError(
                    f"{m.id}: unknown developer {m.developer!r} (models/developers.yaml)"
                )
            same = next((x for x in models.values() if x.model_id == m.model_id), None)
            if same and (same.display, same.developer) != (m.display, m.developer):
                raise ValueError(
                    f"{m.id}: model_id {m.model_id!r} is {same.id}'s, a different model "
                    f"({same.display!r} by {same.developer}, not {m.display!r} by {m.developer})"
                )
            models[m.id] = m
    return Registry(providers=providers, models=models, developers=developers)
