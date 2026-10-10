"""Knowledge cutoffs for models, from models.dev (with a bundled snapshot for offline use)."""

from __future__ import annotations

import calendar
import difflib
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
    "alibaba": "alibaba",
    "qwen": "alibaba",
    "dashscope": "alibaba",
    "moonshotai": "moonshotai",
    "moonshot": "moonshotai",
    "kimi": "moonshotai",
    "zai": "zai",
    "zhipu": "zai",
    "glm": "zai",
    "llama": "llama",
}
# The model makers themselves: preferred over resellers that list the same model id. (models.dev
# has a provider "meta" of its own, Meta's Muse Spark models, so "meta" is not an alias of
# "llama", Meta's Llama API.)
FIRST_PARTY = frozenset(PROVIDER_ALIASES.values())


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


# Amazon Bedrock ids: an optional region, the model maker, the maker's id and a version.
_BEDROCK_ID = re.compile(
    r"(?:[a-z-]+\.)?(?:anthropic|openai|meta|mistral|amazon|cohere|deepseek|qwen)\."
    r"(.+?)(?:-v\d+(?::\d+)?)?"
)


def bare_model_id(model_id: str) -> str:
    """A model id as its maker spells it, lowercased: without a router prefix
    (``openrouter/anthropic/``), Amazon Bedrock's region, maker and version
    (``us.anthropic.claude-sonnet-4-5-20250929-v1:0``), a Vertex AI date
    (``claude-sonnet-4-5@20250929``) or a context-size tag (``[1m]``)."""
    mid = re.sub(r"\[[^\]]*\]$", "", model_id.strip().lower())
    mid = mid.rsplit("/", 1)[-1]
    bedrock = _BEDROCK_ID.fullmatch(mid)
    if bedrock:
        mid = bedrock.group(1)
    return mid.split("@", 1)[0]


# A dated snapshot: Anthropic's "-20250929", OpenAI's "-2025-04-14".
_SNAPSHOT_DATE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def normalize_model_id(model_id: str) -> list[str]:
    """Candidate spellings of a model id, most specific first.

    Each spelling's own variants are among them too, so "claude-sonnet-4.6-20260101" leads
    to "claude-sonnet-4-6", with its dots as dashes and without its date.
    """
    mid = re.sub(r"\[[^\]]*\]$", "", model_id.strip().lower())  # "claude-opus-4-6[1m]"
    if "/" in mid:  # openrouter style "anthropic/claude-sonnet-4.5"
        mid = mid.split("/", 1)[1]
    no_tag = mid.split(":", 1)[0]  # ollama style "qwen3:32b"
    bare = bare_model_id(model_id)  # Bedrock, Vertex AI, nested router prefixes
    candidates = list(dict.fromkeys([mid, no_tag, bare]))
    for c in candidates:  # the list grows while it is read: variants of variants
        for variant in (
            c.replace(".", "-"),
            _SNAPSHOT_DATE.sub("", c),
            re.sub(r"-latest$", "", c),
        ):
            if variant not in candidates:
                candidates.append(variant)
    return [c for c in candidates if c]


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
        """Find a model by id: the first of its spellings (:func:`normalize_model_id`, most
        specific first) that a listing with a cutoff has; among those listings the given
        provider's, then the model maker's own (:data:`FIRST_PARTY`), then any. The maker's own
        listing of a new model often has no cutoff yet while a reseller's does, and a dated
        snapshot (``claude-sonnet-4-20250514``) may have none while the model (``claude-sonnet-4``)
        has: a listing without a cutoff is the answer only when none has one.

        Listings are matched by their id as written, then by their *bare* id
        (:func:`bare_model_id`): Amazon Bedrock's ``qwen.qwen3-coder-480b-a35b-v1:0``,
        Cloudflare's ``@cf/meta/llama-3.2-3b-instruct`` or OpenRouter's
        ``aion-labs/aion-rp-llama-3.1-8b`` for a model the maker does not list.
        """
        pref = PROVIDER_ALIASES.get(provider or "", provider)
        if "/" in model_id and not pref:
            pref = PROVIDER_ALIASES.get(model_id.split("/", 1)[0], model_id.split("/", 1)[0])
        models = self.all_models()
        candidates = normalize_model_id(model_id)
        first: ModelInfo | None = None
        for bare in (False, True):
            by_id: dict[str, list[ModelInfo]] = {}
            for m in models:
                by_id.setdefault(bare_model_id(m.id) if bare else m.id.lower(), []).append(m)
            for cand in candidates:
                hits = by_id.get(cand)
                if not hits:
                    continue
                best = min(
                    hits,
                    key=lambda m: (
                        m.knowledge is None,
                        m.provider != pref,
                        m.provider not in FIRST_PARTY,
                        m.provider,
                    ),
                )
                if best.knowledge is not None:
                    return best
                first = first or best
        return first

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

    def close_matches(self, query: str, n: int = 8) -> list[str]:
        """Known model ids (with a cutoff) that look like ``query``, for "did you mean"."""
        ids = sorted({m.id for m in self.all_models() if m.knowledge})
        wanted = normalize_model_id(query)
        close = difflib.get_close_matches(wanted[0], ids, n=n, cutoff=0.6)
        contained = [i for i in ids if any(w and w in i.lower() for w in wanted)]
        return list(dict.fromkeys([*close, *contained]))[:n]

    def require(self, model_id: str, provider: str | None = None) -> ModelInfo:
        info = self.lookup(model_id, provider)
        if info is None:
            close = self.close_matches(model_id) if model_id else []
            hint = f" Close matches: {', '.join(close)}." if close else ""
            raise ModelLookupError(
                f"unknown model '{model_id}'.{hint} Pass its training cutoff explicitly, "
                "e.g. --cutoff 2025-07"
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
