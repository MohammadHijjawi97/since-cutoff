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
