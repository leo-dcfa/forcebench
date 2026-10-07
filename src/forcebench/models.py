"""Model registry.

``models/providers.yaml`` says how to reach each provider (endpoint and key come from
environment variables, never from the repo). ``models/*.yaml`` list model configurations: one
entry per served model *and quantisation*, with the effort levels it supports.

A benchmark configuration is ``<model id>@<effort>``: the same weights at a different
reasoning effort are a different entry on the leaderboard.
"""

import os
import re
import sys
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

    @model_validator(mode="after")
    def _local_is_a_server(self) -> Provider:
        if self.local and self.kind != "openai_compatible":
            raise ValueError("only an OpenAI-compatible server the operator runs can be local")
        return self

    def resolved_base_url(self) -> str | None:
        if self.base_url_env and os.environ.get(self.base_url_env):
            return os.environ[self.base_url_env]
        return self.base_url

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


_EFFORT_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]*$")
    provider: str
    endpoint_model: str  # the model name the endpoint expects
    # The name a proxy serves it under (`forcebench run --via <provider>`), e.g. LiteLLM's.
    proxy_model: str | None = None
    display: str  # e.g. "Qwen3.8 27B"
    family: str  # e.g. "Qwen3.8"
    base_model: str  # the unquantised model, groups quantisations together
    quant: str  # e.g. "BF16", "FP8", "AWQ-INT4", "MLX 4-bit"
    engine: str  # inference engine, e.g. "vLLM", "SGLang", "MLX"
    open_weights: bool = True
    local: bool = True
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
        return self.model_dump(exclude={"provider", "endpoint_model", "proxy_model"})


class Registry(BaseModel):
    providers: dict[str, Provider]
    models: dict[str, ModelConfig]

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
    models: dict[str, ModelConfig] = {}
    for path in sorted(models_dir.glob("*.yaml")):
        if path.name == "providers.yaml":
            continue
        for entry in yaml.safe_load(path.read_text()).get("models", []):
            m = ModelConfig.model_validate(entry)
            if m.id in models:
                raise ValueError(f"duplicate model id {m.id} in {path}")
            if m.provider not in providers:
                raise ValueError(f"{m.id}: unknown provider {m.provider!r}")
            models[m.id] = m
    return Registry(providers=providers, models=models)
