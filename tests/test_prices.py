"""The price lists (prices/*.yaml): every price has a source and a date, and fits its config."""

from datetime import date

import pytest

from forcebench.models import load_registry
from forcebench.prices import PRICES_DIR, PriceList, check_prices, load_prices


def _list(**models) -> dict:
    return {
        "version": "2026-10-10",
        "currency": "USD",
        "providers": {
            "openrouter": {
                "reasoning": "output",
                "reasoning_source": "https://example.com/reasoning",
                "as_of": "2026-10-10",
                "models": models,
            }
        },
    }


def test_every_price_in_every_list_has_a_source_url_and_a_date():
    files = sorted(PRICES_DIR.glob("*.yaml"))
    assert files, "there is a price list"
    for path in files:
        prices = load_prices(path)
        assert prices.version == path.stem
        date.fromisoformat(prices.version)  # named for the day the prices were taken
        for pid, p in prices.providers.items():
            for cid in p.models:
                price = prices.price(pid, cid)
                assert price is not None
                assert price.source.startswith("https://"), f"{path.name}: {pid}/{cid}"
                assert price.reasoning_source.startswith("https://")
                assert isinstance(price.as_of, date)
                assert price.currency == "USD"


def test_the_newest_list_fits_the_model_configs():
    assert check_prices(load_prices(), load_registry()) == []


def test_a_price_without_a_source_is_refused():
    with pytest.raises(ValueError, match="no source"):
        PriceList.model_validate(
            _list(**{"gpt-6-astra": {"input": 1, "cached_input": 0, "output": 2}})
        )


def test_a_router_price_must_be_for_the_route_its_config_is_pinned_to():
    raw = _list(
        **{
            "gpt-6-astra": {
                "route": ["openai"],  # the config is pinned to openai/flex, which is cheaper
                "input": 10,
                "cached_input": 1,
                "output": 50,
                "source": "https://example.com/astra",
            }
        }
    )
    (problem,) = check_prices(PriceList.model_validate(raw), load_registry())
    assert "pinned to ['openai/flex']" in problem


def test_a_configuration_without_a_list_price_has_none():
    prices = load_prices()
    assert prices.price("openrouter", "nemotron-3-ultra-nvidia") is None  # a free endpoint
    p = prices.price("anthropic", "claude-haiku-5.5")
    assert p is not None
    assert p.prompt_tokens_max == 100_000
