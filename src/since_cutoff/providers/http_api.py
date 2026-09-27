"""HTTP providers: the Anthropic Messages API and any OpenAI-compatible chat completions API."""

from __future__ import annotations

import os
from typing import Any

from since_cutoff import net
from since_cutoff.errors import ProviderError
from since_cutoff.providers.base import Completion

# OpenRouter attributes requests to an app by these headers (its "app attribution"). They name
# the tool, never the user or the project.
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/MohammadHijjawi97/since-cutoff",
    "X-OpenRouter-Title": "since-cutoff",
    "X-Title": "since-cutoff",  # the older name of X-OpenRouter-Title
}


class AnthropicProvider:
    provider_name = "anthropic"

    def __init__(self, model: str, *, base_url: str | None = None, timeout: float = 600.0) -> None:
        self.model = model
        self.timeout = timeout
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self.base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        ).rstrip("/")

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def key(self) -> str:
        return f"anthropic:{self.model}"

    def complete(self, system: str, user: str) -> Completion:
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01"}
        try:
            data = net.post_json(
                f"{self.base_url}/v1/messages", payload, headers=headers, timeout=self.timeout
            )
        except net.HTTPError as exc:
            raise ProviderError(f"Anthropic API error: {exc}") from exc
        except ValueError as exc:  # a web page: a wrong base URL, a proxy's error page
            raise ProviderError(
                f"Anthropic API error: {self.base_url} did not answer with JSON"
            ) from exc
        try:
            text = "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            )
            usage = data.get("usage") or {}
            return Completion(
                text=text,
                model=data.get("model", self.model),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        except (AttributeError, TypeError) as exc:  # JSON, but not a Messages API answer
            raise ProviderError(f"anthropic: unexpected response: {str(data)[:300]}") from exc


class OpenAICompatibleProvider:
    def __init__(
        self,
        provider: str,
        model: str,
        *,
        base_url: str,
        key_env: str | None,
        timeout: float = 600.0,
    ) -> None:
        self.provider_name = provider
        self.model = model
        self.timeout = timeout
        env_url = os.environ.get("OPENAI_BASE_URL") if provider == "openai" else None
        self.base_url = (env_url or base_url).rstrip("/")
        self.api_key = os.environ.get(key_env) if key_env else None
        if key_env and not self.api_key:
            raise ProviderError(f"{key_env} is not set")

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def key(self) -> str:
        return f"{self.provider_name}:{self.model}"

    def complete(self, system: str, user: str) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if self.provider_name == "openrouter":
            headers.update(OPENROUTER_HEADERS)
        try:
            data = net.post_json(
                f"{self.base_url}/chat/completions", payload, headers=headers, timeout=self.timeout
            )
        except net.HTTPError as exc:
            raise ProviderError(f"{self.provider_name} API error: {exc}") from exc
        except ValueError as exc:  # a web page: a wrong base URL, a proxy's error page
            raise ProviderError(
                f"{self.provider_name} API error: {self.base_url} did not answer with JSON"
            ) from exc
        try:
            text = data["choices"][0]["message"].get("content") or ""
            if not isinstance(text, str):  # a list of content parts
                raise TypeError("the content is not text")
            usage = data.get("usage") or {}
            return Completion(
                text=text,
                model=data.get("model", self.model),
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(
                f"{self.provider_name}: unexpected response: {str(data)[:300]}"
            ) from exc
