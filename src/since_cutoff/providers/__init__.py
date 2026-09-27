"""Model providers.

A provider turns (system prompt, user prompt) into text. Probes must measure what the model
*knows*, so every provider calls the model without tools, web access or project context.

Model specs on the command line look like ``provider:model``:

    claude-code            the Claude Code CLI with its default model (your subscription)
    claude-code:sonnet     the Claude Code CLI with a model alias or full model id
    anthropic:claude-sonnet-4-5     Anthropic API (ANTHROPIC_API_KEY)
    openai:gpt-5.4                  OpenAI API (OPENAI_API_KEY)
    openrouter:qwen/qwen3-coder     OpenRouter (OPENROUTER_API_KEY)
    deepseek:deepseek-chat          DeepSeek API (DEEPSEEK_API_KEY)
    ollama:qwen3-coder:30b          local Ollama server
"""

from __future__ import annotations

from collections.abc import Collection

from since_cutoff.errors import ProviderError
from since_cutoff.providers.base import Completion, Provider
from since_cutoff.providers.claude_code import ClaudeCodeProvider
from since_cutoff.providers.http_api import AnthropicProvider, OpenAICompatibleProvider

OPENAI_COMPATIBLE = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
}

__all__ = ["Completion", "Provider", "ProviderError", "check_spec", "make_provider", "split_spec"]

KNOWN_PROVIDERS = ("claude-code", "claude", "anthropic", *OPENAI_COMPATIBLE, "openai-compatible")
_CLAUDE_ALIASES = ("sonnet", "opus", "haiku", "fable")


def check_spec(spec: str, *, vendors: Collection[str] = (), calls: bool = True) -> None:
    """Fail early, with a helpful hint, on model specs such as ``sonnet`` or ``gpt-5.4``.

    ``vendors`` are the model makers and resellers the model registry knows (``google``,
    ``github-copilot``, ...). since-cutoff cannot call them, but without ``calls`` (``scan``
    only looks up the training cutoff) ``vendor:model`` is accepted.
    """
    provider, model = split_spec(spec)
    if provider in KNOWN_PROVIDERS:
        return
    if model and provider in vendors:
        if not calls:
            return
        raise ProviderError(
            f"since-cutoff cannot call {provider} models itself (providers: "
            f"{', '.join(p for p in KNOWN_PROVIDERS if p != 'claude')}). `scan` works with "
            f"'{spec}', as it only looks up the training cutoff; to run, reach the model "
            "through openrouter:<maker>/<model>, or openai-compatible:<model> with --base-url"
        )
    if provider in _CLAUDE_ALIASES:
        hint = f"'claude-code:{spec}'"
    elif provider.startswith("claude-"):
        hint = f"'claude-code:{spec}' (the Claude Code CLI) or 'anthropic:{spec}' (the API)"
    elif provider.startswith(("gpt-", "o1", "o3", "o4")):
        hint = f"'openai:{spec}'"
    elif model:
        # ``lmstudio:qwen3-coder`` (a local or custom provider) or ``qwen3:32b`` (a model tag).
        hint = (
            f"'<provider>:{spec}', or, if '{provider}' is a local or custom server (LM Studio, "
            f"vLLM, ...), 'openai-compatible:{model}' with --base-url"
        )
    else:
        hint = f"'<provider>:{spec}'"
    raise ProviderError(
        f"--model must look like provider:model; '{spec}' has no known provider. Did you mean "
        f"{hint}? Providers: {', '.join(p for p in KNOWN_PROVIDERS if p != 'claude')}"
    )


def split_spec(spec: str) -> tuple[str, str | None]:
    """``"ollama:qwen3:8b"`` -> ``("ollama", "qwen3:8b")``; ``"claude-code"`` -> ``("claude-code", None)``."""
    provider, _, model = spec.partition(":")
    return provider.strip().lower(), (model.strip() or None)


def make_provider(
    spec: str,
    *,
    base_url: str | None = None,
    effort: str | None = None,
    timeout: float = 600.0,
) -> Provider:
    provider, model = split_spec(spec)
    if provider in ("claude-code", "claude"):
        return ClaudeCodeProvider(model, effort=effort, timeout=timeout)
    if provider == "anthropic":
        if not model:
            raise ProviderError("anthropic needs a model, e.g. anthropic:claude-sonnet-4-5")
        return AnthropicProvider(model, base_url=base_url, timeout=timeout)
    if provider in OPENAI_COMPATIBLE or provider == "openai-compatible":
        if not model:
            raise ProviderError(f"{provider} needs a model, e.g. {provider}:<model-id>")
        default_url, key_env = OPENAI_COMPATIBLE.get(provider, (None, "OPENAI_API_KEY"))
        url = base_url or default_url
        if not url:
            raise ProviderError("openai-compatible needs --base-url")
        return OpenAICompatibleProvider(
            provider, model, base_url=url, key_env=key_env, timeout=timeout
        )
    raise ProviderError(
        f"unknown provider '{provider}'. Use one of: claude-code, anthropic, openai, openrouter, "
        "deepseek, ollama, openai-compatible"
    )
