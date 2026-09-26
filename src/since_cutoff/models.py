"""Knowledge cutoffs for models, from models.dev (with a bundled snapshot for offline use)."""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date
from importlib import resources
from typing import Any

from since_cutoff import net
from since_cutoff.cache import DiskCache
from since_cutoff.errors import ModelLookupError

MODELS_DEV_URL = "https://models.dev/api.json"
REGISTRY_TTL = 24 * 3600

# Provider names used on the command line -> provider ids used by models.dev.
PROVIDER_ALIASES = {
    "claude-code": "anthropic",
    "anthropic": "anthropic",
    "openai": "openai",
    "codex": "openai",
    "deepseek": "deepseek",
    "google": "google",
    "gemini": "google",
    "xai": "xai",
    "mistral": "mistral",
}


@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str
    name: str
    knowledge: date | None
    knowledge_raw: str | None
    release_date: date | None
    family: str | None = None

    @property
    def label(self) -> str:
        return self.id


def parse_cutoff(value: str) -> date:
    """Parse ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``. Partial dates mean *end* of the period."""
    value = value.strip()
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", value)
    if not m:
        raise ValueError(f"not a date: {value!r} (expected YYYY-MM or YYYY-MM-DD)")
    year = int(m.group(1))
    month = int(m.group(2) or 12)
    if m.group(3):
        return date(year, month, int(m.group(3)))
    return date(year, month, calendar.monthrange(year, month)[1])


def _maybe_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return parse_cutoff(value)
    except ValueError:
        return None


def normalize_model_id(model_id: str) -> list[str]:
    """Candidate spellings of a model id, most specific first."""
    mid = re.sub(r"\[[^\]]*\]$", "", model_id.strip().lower())  # "claude-opus-4-6[1m]"
    if "/" in mid:  # openrouter style "anthropic/claude-sonnet-4.5"
        mid = mid.split("/", 1)[1]
    candidates = [mid]
    no_tag = mid.split(":", 1)[0]  # ollama style "qwen3:32b"
    candidates.append(no_tag)
    for c in list(candidates):
        candidates.append(c.replace(".", "-"))
        candidates.append(re.sub(r"-\d{8}$", "", c))  # dated snapshot suffix
        candidates.append(re.sub(r"-latest$", "", c))
    seen: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    return seen


class ModelRegistry:
    def __init__(self, cache: DiskCache, *, offline: bool = False) -> None:
        self.cache = cache
        self.offline = offline
        self._data: dict[str, Any] | None = None
        self.source = "models.dev"

    def data(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        data: dict[str, Any] | None = None
        cached = self.cache.get("models", "models-dev", max_age=REGISTRY_TTL)
        if isinstance(cached, dict):
            data, self.source = cached, "models.dev (cached)"
        elif not self.offline:
            try:
                raw = net.get_json(MODELS_DEV_URL, timeout=30)
                data = slim_models_dev(raw)
                self.cache.set("models", "models-dev", data)
                self.source = "models.dev"
            except (net.HTTPError, ValueError, AttributeError):
                data = None
        if data is None:
            stale = self.cache.get("models", "models-dev")
            if isinstance(stale, dict):
                data, self.source = stale, "models.dev (older cached copy)"
            else:
                data, self.source = load_snapshot(), "bundled models.dev snapshot"
        self._data = data
        return data

    def all_models(self) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        for provider, models in self.data().items():
            for mid, m in models.items():
                out.append(
                    ModelInfo(
                        id=mid,
                        provider=provider,
                        name=m.get("name") or mid,
                        knowledge=_maybe_date(m.get("knowledge")),
                        knowledge_raw=m.get("knowledge"),
                        release_date=_maybe_date(m.get("release_date")),
                        family=m.get("family"),
                    )
                )
        return out

    def lookup(self, model_id: str, provider: str | None = None) -> ModelInfo | None:
        """Find a model by id, preferring the given provider, then any provider with a cutoff."""
        pref = PROVIDER_ALIASES.get(provider or "", provider)
        if "/" in model_id and not pref:
            pref = PROVIDER_ALIASES.get(model_id.split("/", 1)[0], model_id.split("/", 1)[0])
        models = self.all_models()
        for cand in normalize_model_id(model_id):
            hits = [m for m in models if m.id.lower() == cand]
            if not hits:
                continue
            hits.sort(key=lambda m: (m.provider != pref, m.knowledge is None, m.provider))
            return hits[0]
        return None

    def latest_in_family(self, provider: str, family_word: str, today: date) -> ModelInfo | None:
        """Resolve aliases such as ``sonnet`` to the newest released model of that family."""
        prov = PROVIDER_ALIASES.get(provider, provider)
        cands = [
            m
            for m in self.all_models()
            if m.provider == prov
            and family_word in m.id
            and m.release_date is not None
            and m.release_date <= today
            and m.knowledge is not None
            and not re.search(r"-\d{8}$", m.id)
            and "latest" not in m.id
        ]
        if not cands:
            return None
        cands.sort(key=lambda m: (m.release_date or date.min, m.id))
        return cands[-1]

    def require(self, model_id: str, provider: str | None = None) -> ModelInfo:
        info = self.lookup(model_id, provider)
        if info is None:
            raise ModelLookupError(
                f"unknown model '{model_id}'. Pass its training cutoff explicitly, e.g. --cutoff 2025-07"
            )
        if info.knowledge is None:
            raise ModelLookupError(
                f"models.dev has no knowledge cutoff for '{info.id}'. Pass it explicitly, e.g. --cutoff 2025-07"
            )
        return info


def slim_models_dev(raw: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Keep only the fields we use: {provider: {model_id: {name, knowledge, release_date, family}}}."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for provider, pdata in raw.items():
        models = pdata.get("models") if isinstance(pdata, dict) else None
        if not isinstance(models, dict):
            continue
        slim: dict[str, dict[str, Any]] = {}
        for mid, m in models.items():
            if not isinstance(m, dict):
                continue
            slim[mid] = {
                k: m.get(k) for k in ("name", "knowledge", "release_date", "family") if m.get(k)
            }
        if slim:
            out[provider] = slim
    return out


def load_snapshot() -> dict[str, Any]:
    text = resources.files("since_cutoff").joinpath("data/models_snapshot.json").read_text("utf-8")
    data: dict[str, Any] = json.loads(text)
    return data
