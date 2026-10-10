from __future__ import annotations

import os
import time
from datetime import date

import pytest

from since_cutoff import models
from since_cutoff.cache import DiskCache
from since_cutoff.errors import ModelLookupError
from since_cutoff.models import ModelRegistry, normalize_model_id, parse_cutoff, slim_models_dev
from tests.conftest import Reply

DATA = {
    "anthropic": {
        "models": {
            "claude-sonnet-4-5": {
                "name": "Claude Sonnet 4.5",
                "knowledge": "2025-07-31",
                "release_date": "2025-09-29",
                "family": "claude-sonnet",
            },
            "claude-sonnet-4-5-20250929": {
                "name": "Claude Sonnet 4.5",
                "knowledge": "2025-07-31",
                "release_date": "2025-09-29",
            },
            "claude-haiku-4-5": {
                "name": "Claude Haiku 4.5",
                "knowledge": "2025-02",
                "release_date": "2025-10-15",
            },
            "claude-sonnet-9": {
                "name": "Future",
                "knowledge": "2027-01",
                "release_date": "2027-03-01",
            },
        }
    },
    "openrouter": {"models": {"claude-sonnet-4-5": {"knowledge": "2025-01"}}},
    "ollama-cloud": {"models": {"qwen3-coder": {"knowledge": "2025-04"}}},
    "broken": "not a dict",
}


@pytest.fixture
def registry(tmp_path):
    cache = DiskCache(tmp_path)
    cache.set("models", "models-dev", slim_models_dev(DATA))
    return ModelRegistry(cache, offline=True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025-07-31", date(2025, 7, 31)),
        ("2025-02", date(2025, 2, 28)),
        ("2024-02", date(2024, 2, 29)),
        ("2025", date(2025, 12, 31)),
    ],
)
def test_parse_cutoff_uses_the_end_of_partial_periods(text, expected):
    assert parse_cutoff(text) == expected


def test_parse_cutoff_rejects_garbage():
    with pytest.raises(ValueError):
        parse_cutoff("last summer")


def test_normalize_model_id():
    assert "claude-sonnet-4-5" in normalize_model_id("anthropic/claude-sonnet-4.5")
    assert "claude-sonnet-4-5" in normalize_model_id("claude-sonnet-4-5-20250929")
    assert "qwen3-coder" in normalize_model_id("qwen3-coder:30b")
    assert normalize_model_id("claude-opus-4-6[1m]")[0] == "claude-opus-4-6"


def test_lookup_prefers_the_requested_provider(registry):
    info = registry.lookup("claude-sonnet-4-5", "anthropic")
    assert info is not None and info.provider == "anthropic" and info.knowledge == date(2025, 7, 31)


def test_lookup_handles_dated_and_dotted_ids(registry):
    assert registry.require("claude-sonnet-4.5", "claude-code").knowledge == date(2025, 7, 31)
    assert registry.require("claude-haiku-4-5-20251001").knowledge == date(2025, 2, 28)
    assert registry.require("qwen3-coder:30b", "ollama").knowledge == date(2025, 4, 30)


def test_lookup_prefers_additional_model_makers_over_resellers(tmp_path):
    registry = ModelRegistry(DiskCache(tmp_path), offline=True)
    assert registry.require("kimi-k2.7-code").provider == "moonshotai"
    assert registry.require("qwen3-coder-plus", "qwen").provider == "alibaba"
    assert registry.require("glm-4.6", "glm").provider == "zai"
    assert registry.require("llama-3.3-70b-instruct", "llama").provider == "llama"


# Issue #87: what models.dev lists for the same model ids as the makers do.
RESELLERS = {
    "zai": {"models": {"glm-5.1": {"name": "GLM-5.1"}, "glm-4.6": {"knowledge": "2025-04"}}},
    "opencode": {"models": {"glm-5.1": {"knowledge": "2025-07"}, "glm-6": {"name": "GLM-6"}}},
    "alibaba": {"models": {"qwen3-coder-plus": {"knowledge": "2025-04"}}},
    "amazon-bedrock": {"models": {"qwen.qwen3-coder-480b-a35b-v1:0": {"knowledge": "2025-03"}}},
    "cloudflare-workers-ai": {
        "models": {"@cf/meta/llama-3.2-3b-instruct": {"knowledge": "2023-12"}}
    },
    "openrouter": {"models": {"aion-labs/aion-rp-llama-3.1-8b": {"knowledge": "2023-12"}}},
    "anthropic": {
        "models": {
            "claude-sonnet-4": {"knowledge": "2025-03"},
            "claude-sonnet-4-20250514": {"name": "Claude Sonnet 4"},
        }
    },
    "llama": {"models": {"x-model": {"knowledge": "2024-01"}}},
    "meta": {
        "models": {"x-model": {"knowledge": "2025-01"}, "muse-spark-1.3": {"knowledge": "2025-06"}}
    },
}


@pytest.fixture
def resellers(tmp_path):
    cache = DiskCache(tmp_path)
    cache.set("models", "models-dev", slim_models_dev(RESELLERS))
    return ModelRegistry(cache, offline=True)


def test_a_listing_with_a_cutoff_beats_the_makers_listing_without_one(resellers):
    """Issue #87: the maker's own entry for a new model often has no cutoff yet, while a
    reseller's has; ``opencode/glm-5.1`` stopped with "models.dev has no knowledge cutoff"."""
    for provider in ("zai", "glm", None):
        info = resellers.require("glm-5.1", provider)
        assert info.provider == "opencode" and info.knowledge == date(2025, 7, 31)
    # A dated snapshot without a cutoff takes the model's.
    assert resellers.require("claude-sonnet-4-20250514").knowledge == date(2025, 3, 31)
    # Listed without one everywhere: still an error, with the id as listed.
    with pytest.raises(ModelLookupError, match="no knowledge cutoff for 'glm-6'"):
        resellers.require("glm-6")


def test_a_model_the_maker_does_not_list_is_found_in_the_resellers_listing(resellers):
    """hosts.hosted_spec names Bedrock's ``qwen.qwen3-coder-480b-a35b-v1:0``
    ``alibaba:qwen3-coder-480b-a35b`` and Cloudflare's ``@cf/meta/llama-3.2-3b-instruct``
    ``llama:llama-3.2-3b-instruct``; the makers list neither, the resellers do under their
    own spelling. OpenRouter's own ids have a ``maker/`` prefix the lookup drops."""
    assert resellers.require("qwen3-coder-480b-a35b", "alibaba").provider == "amazon-bedrock"
    assert resellers.require("llama-3.2-3b-instruct", "llama").provider == "cloudflare-workers-ai"
    found = resellers.require("aion-labs/aion-rp-llama-3.1-8b", "openrouter")
    assert found.provider == "openrouter" and found.knowledge == date(2023, 12, 31)
    assert resellers.lookup("qwen3-coder-480b", "alibaba") is None


def test_meta_is_a_provider_of_its_own_not_an_alias_of_llama(resellers):
    """models.dev lists Meta's Muse Spark models under ``meta``; Llama is ``llama``."""
    assert "meta" not in models.PROVIDER_ALIASES
    assert resellers.require("x-model", "meta").knowledge == date(2025, 1, 31)
    assert resellers.require("x-model", "llama").knowledge == date(2024, 1, 31)
    assert resellers.require("meta/muse-spark-1.3").provider == "meta"


def test_unknown_model_asks_for_cutoff(registry):
    with pytest.raises(ModelLookupError, match="--cutoff"):
        registry.require("mystery-model")


def test_alias_resolves_to_newest_released_model(registry):
    assert (
        registry.latest_in_family("claude-code", "sonnet", date(2026, 9, 1)).id
        == "claude-sonnet-4-5"
    )
    assert (
        registry.latest_in_family("claude-code", "sonnet", date(2028, 1, 1)).id == "claude-sonnet-9"
    )


def test_bundled_snapshot_is_usable_offline(tmp_path):
    registry = ModelRegistry(DiskCache(tmp_path), offline=True)
    info = registry.require("claude-sonnet-4-5", "anthropic")
    assert info.knowledge is not None and info.knowledge.year == 2025


def test_models_dev_is_asked_once_a_day_with_fallbacks_when_it_is_down(
    tmp_path, http_server, monkeypatch, slept
):
    monkeypatch.setattr(models, "MODELS_DEV_URL", f"{http_server.url}/api.json")
    http_server.routes["/api.json"] = [Reply(body=DATA)]
    cache = DiskCache(tmp_path / "cache")

    def source(registry: ModelRegistry) -> str:
        assert registry.lookup("claude-haiku-4-5").knowledge == date(2025, 2, 28)
        return registry.source

    assert source(ModelRegistry(cache)) == "models.dev"
    assert source(ModelRegistry(cache)) == "models.dev (cached)"
    assert len(http_server.seen) == 1
    # A day later, with models.dev down (or answering something else): the older copy.
    day_old = time.time() - models.REGISTRY_TTL - 60
    os.utime(cache.path("models", "models-dev"), (day_old, day_old))
    for reply in (Reply(503, "down"), Reply(body=["not", "models.dev"]), Reply(body="<html>")):
        http_server.routes["/api.json"] = [reply]
        assert source(ModelRegistry(cache)) == "models.dev (older cached copy)"
    # With no copy at all: the snapshot that ships with since-cutoff.
    registry = ModelRegistry(DiskCache(tmp_path / "empty"))
    assert registry.require("claude-sonnet-4-5", "anthropic").knowledge is not None
    assert registry.source == "bundled models.dev snapshot"
