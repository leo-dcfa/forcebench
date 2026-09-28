"""Model registry.

``models/providers.yaml`` says how to reach each provider (endpoint and key come from
environment variables, never from the repo). ``models/*.yaml`` list model configurations: one
entry per served model *and quantisation*, with the effort levels it supports.

A benchmark configuration is ``<model id>@<effort>``: the same weights at a different
reasoning effort are a different entry on the leaderboard.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from forcebench import MODELS_DIR, REPO_ROOT

EffortTier = Literal["off", "low", "medium", "high", "max"]


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["openai_compatible", "openai", "anthropic", "google"]
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None

    def resolved_base_url(self) -> str | None:
        if self.base_url_env and os.environ.get(self.base_url_env):
            return os.environ[self.base_url_env]
        return self.base_url

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]*$")
    provider: str
    endpoint_model: str  # the model name the endpoint expects
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
        return self

    def public_dict(self) -> dict[str, Any]:
        """Everything worth publishing about this configuration (no endpoints or keys)."""
        return self.model_dump(exclude={"provider", "endpoint_model"})


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

    Keys in DOTENV_IGNORED are skipped with a warning."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in DOTENV_IGNORED:
            print(
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
