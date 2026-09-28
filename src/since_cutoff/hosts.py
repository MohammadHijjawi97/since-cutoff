"""Which model the user's coding agent runs, read from the agent's own settings.

Without ``--model``, since-cutoff tests the model the user's coding agent is set up with:

1. ``SINCE_CUTOFF_MODEL``, a full spec such as ``openai:gpt-5.4``, always wins.
2. Inside Claude Code (which sets ``CLAUDECODE=1`` for the commands it runs), only Claude Code's
   settings count, as it reads them: ``ANTHROPIC_MODEL``, then ``model`` in the project's
   ``.claude/settings.local.json`` and ``.claude/settings.json``, then in
   ``~/.claude/settings.json`` (or ``$CLAUDE_CONFIG_DIR/settings.json``) ->
   ``claude-code:<model>``; else Claude Code's default. Other agents' files say nothing about
   the model running here.
3. Elsewhere, by scope: first the agents' environment variables (Claude Code's
   ``ANTHROPIC_MODEL``, then Aider's ``AIDER_MODEL``); then the project's settings, the nearest
   folder first; then the user's. The project's settings are looked for from the project folder
   up to the repository root (the first folder with ``.git``), never in the home directory or
   above it; in each folder, and then among the user's settings, in this order:

   - Claude Code: ``.claude/settings.local.json``, ``.claude/settings.json``;
     ``~/.claude/settings.json`` or ``$CLAUDE_CONFIG_DIR/settings.json``.
   - Codex: ``.codex/config.toml``; ``$CODEX_HOME/config.toml`` or ``~/.codex/config.toml``.
     As Codex merges them, a ``profile`` selected in any of them picks ``[profiles.<name>]``
     -> ``openai:<model>``.
   - OpenCode: ``model`` (``provider/model``) in ``opencode.json`` or ``opencode.jsonc``;
     ``~/.config/opencode/`` (or ``$XDG_CONFIG_HOME/opencode/``).
   - Aider: ``model:`` in ``.aider.conf.yml``; ``~/.aider.conf.yml``. Aider's short aliases
     (``4o``, ``flash``, ``r1``, ...) are read as the models they stand for.

A model reached through another company's service (GitHub Copilot, Amazon Bedrock, Vertex AI,
OpenCode Zen, ...) is named after its maker, so that its training cutoff can be looked up; a
``run`` then calls the maker's API (``openai:`` needs ``OPENAI_API_KEY``).

Only the model fields are read. These files can hold API keys, which are never kept, used or
printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from since_cutoff.models import PROVIDER_ALIASES, bare_model_id

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

# The environment variables read here (tests clear them).
ENV_VARS = (
    "SINCE_CUTOFF_MODEL",
    "CLAUDECODE",
    "ANTHROPIC_MODEL",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "AIDER_MODEL",
)
# What the model is called when no setting names one (``run`` asks Claude Code which it is).
DEFAULT_SOURCE = "Claude Code's default (no model setting found)"
NOT_FOUND_HINT = (
    "No model setting found (SINCE_CUTOFF_MODEL, ANTHROPIC_MODEL, or the Claude Code, Codex, "
    "OpenCode and Aider settings), so this tests Claude Code's default model. To test another, "
    "pass --model provider:model or set SINCE_CUTOFF_MODEL, e.g. SINCE_CUTOFF_MODEL=openai:gpt-5.4"
)
CLAUDE_CODE_NOT_FOUND_HINT = (
    "Running inside Claude Code, whose settings name no model (ANTHROPIC_MODEL, "
    ".claude/settings.json), so this tests Claude Code's default model. If this session uses "
    "another (/model, claude --model), pass --model claude-code:<model> or set SINCE_CUTOFF_MODEL"
)
# The same for ``scan`` and ``sync``, which test nothing: they use the model's training cutoff.
NOT_FOUND_HINT_CUTOFF = (
    "No model setting found (SINCE_CUTOFF_MODEL, ANTHROPIC_MODEL, or the Claude Code, Codex, "
    "OpenCode and Aider settings), so this uses the training cutoff of Claude Code's default "
    "model. For another model's, pass --model provider:model or set SINCE_CUTOFF_MODEL, e.g. "
    "SINCE_CUTOFF_MODEL=openai:gpt-5.4"
)
CLAUDE_CODE_NOT_FOUND_HINT_CUTOFF = (
    "Running inside Claude Code, whose settings name no model (ANTHROPIC_MODEL, "
    ".claude/settings.json), so this uses the training cutoff of Claude Code's default model. "
    "If this session uses another (/model, claude --model), pass --model claude-code:<model> "
    "or set SINCE_CUTOFF_MODEL"
)

# Provider names in the agents' settings (OpenCode's models.dev ids, Aider's LiteLLM prefixes,
# Codex's model_provider) that since-cutoff can call itself.
_CALLABLE = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openrouter": "openrouter",
    "deepseek": "deepseek",
    "ollama": "ollama",
    "ollama_chat": "ollama",
    "oss": "ollama",  # Codex's --oss provider
}
# The model maker, from the start of the maker's model id.
_MAKERS = (
    (re.compile(r"claude-|(?:sonnet|opus|haiku)$"), "anthropic"),
    (re.compile(r"gpt-|o\d(?:-|$)|codex-|chatgpt-"), "openai"),
    (re.compile(r"gemini-|gemma-"), "google"),
    (re.compile(r"deepseek-"), "deepseek"),
    (re.compile(r"grok-"), "xai"),
    (re.compile(r"(?:mistral|codestral|devstral|magistral|ministral)-"), "mistral"),
    (re.compile(r"(?:qwen[\\d.]*|qwq|qvq)-"), "alibaba"),
    (re.compile(r"kimi-"), "moonshotai"),
    (re.compile(r"glm-"), "zai"),
    (re.compile(r"llama-"), "llama"),
)
# Aider's documented model aliases (``aider --model 4o``) as the models they stand for. Aider's
# sonnet, opus and haiku are left to hosted_spec, which reads them as the newest of the family.
_AIDER_ALIASES = {
    "3": "openai:gpt-3.5-turbo",
    "35turbo": "openai:gpt-3.5-turbo",
    "35-turbo": "openai:gpt-3.5-turbo",
    "4": "openai:gpt-4-0613",
    "4-turbo": "openai:gpt-4-1106-preview",
    "4o": "openai:gpt-4o",
    "deepseek": "deepseek:deepseek-chat",
    "r1": "deepseek:deepseek-reasoner",
    "flash": "google:gemini-2.5-flash",
    "flash-lite": "google:gemini-2.5-flash-lite",
    "gemini": "google:gemini-2.5-pro",
    "grok3": "xai:grok-3",
}
# Strings, comments and trailing commas in JSON with comments (opencode.jsonc).
_JSONC = re.compile(
    r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/|,(?=(?:\s|//[^\n]*|/\*.*?\*/)*[}\]])', re.S
)
# A top-level ``model:`` line in YAML, with its value quoted or bare and an optional comment.
_YAML_MODEL = re.compile(
    r"""^model[ \t]*:[ \t]*(?:"([^"\n]*)"|'([^'\n]*)'|([^#\s][^#\n]*?))[ \t]*(?:\#.*)?$""",
    re.M,
)

# How specific a setting is to this run, most specific first (see the module docstring).
_ENV, _PROJECT, _USER = 0, 1, 2
_Rank = tuple[int, int]  # (scope, folders above the project folder)


@dataclass(frozen=True)
class Detected:
    """A model spec (``provider:model``) and the setting it was read from.

    ``problem`` is set, and ``spec`` is empty, when the setting names a model since-cutoff
    cannot name as ``provider:model`` (an unknown Aider alias, a bare provider name): the CLI
    stops with that message rather than test another model.
    """

    spec: str
    source: str
    problem: str | None = None


def user_home() -> Path:
    """The user's home directory (a function, so that tests can point it elsewhere)."""
    return Path.home()


def inside_claude_code(env: Mapping[str, str] | None = None) -> bool:
    """Is since-cutoff running as a command of Claude Code (``CLAUDECODE=1``)?"""
    env = os.environ if env is None else env
    return env.get("CLAUDECODE", "").strip() not in ("", "0")


def not_found_hint(env: Mapping[str, str] | None = None, *, probes: bool = True) -> str:
    """What to tell the user when no setting names a model (see :data:`DEFAULT_SOURCE`):
    ``run`` tests that model (``probes``); ``scan`` and ``sync`` only use its cutoff."""
    if inside_claude_code(env):
        return CLAUDE_CODE_NOT_FOUND_HINT if probes else CLAUDE_CODE_NOT_FOUND_HINT_CUTOFF
    return NOT_FOUND_HINT if probes else NOT_FOUND_HINT_CUTOFF


def detect_model(
    root: Path, *, env: Mapping[str, str] | None = None, home: Path | None = None
) -> Detected | None:
    """The model the user's coding agent is set up with for the project at ``root``, if any.

    Each agent's own settings give its model (the agent's precedence among them); the agents'
    models are then ranked by how specific the setting is: environment variable, project
    setting (the nearest folder first), user setting. Inside Claude Code only Claude Code's
    settings are read.
    """
    env = os.environ if env is None else env
    places = _Places(root.resolve(), (user_home() if home is None else home).resolve(), env)
    spec = places.var("SINCE_CUTOFF_MODEL")
    if spec:
        return Detected(spec, "SINCE_CUTOFF_MODEL")
    agents: tuple[Callable[[_Places], tuple[_Rank, Detected] | None], ...] = (
        (_claude_code,) if inside_claude_code(env) else (_claude_code, _codex, _opencode, _aider)
    )
    found: list[tuple[_Rank, int, Detected]] = []
    for order, agent in enumerate(agents):
        hit = agent(places)
        if hit is not None:
            found.append((hit[0], order, hit[1]))
    # The most specific setting; between equally specific ones, the agents' order above.
    return min(found, key=lambda f: (f[0], f[1]))[2] if found else None


def hosted_spec(provider: str | None, model: str) -> str:
    """A since-cutoff spec for ``model`` as an agent reaches it through ``provider``.

    A provider since-cutoff can call keeps the model as written (``openrouter:anthropic/...``).
    Otherwise the model is named after its maker (``github-copilot/gpt-5.4`` -> ``openai:gpt-5.4``),
    else after the provider, which the model registry may still know (``groq:...``).
    """
    name = (provider or "").strip().lower()
    if name in _CALLABLE:
        return f"{_CALLABLE[name]}:{model}"

    maker_spec = _maker_spec(model)
    if maker_spec is not None:
        return maker_spec
    return f"{name}:{model}" if name else model


def claude_settings_model(path: Path) -> str | None:
    """``model`` in a Claude Code settings file, without a context tag such as ``[1m]``."""
    return _without_tag(_text(_json_object(path).get("model")))


# ------------------------------------------------------------------- finders
@dataclass(frozen=True)
class _Places:
    root: Path
    home: Path
    env: Mapping[str, str]

    def shown(self, path: Path) -> str:
        """A settings file as the user knows it: relative to the project (``../opencode.json``
        in a folder above it, below the home directory), else under ``~``, else in full."""
        folder = path.parent
        if folder == self.root or self.root in folder.parents:
            return path.relative_to(self.root).as_posix()
        # A project setting in a folder above: ``../opencode.json``, ``../.claude/settings.json``.
        for base in (folder, folder.parent):
            if base in self.root.parents and base != self.home and base not in self.home.parents:
                up = "../" * (len(self.root.parts) - len(base.parts))
                return up + path.relative_to(base).as_posix()
        if self.home in folder.parents or folder == self.home:
            return "~/" + path.relative_to(self.home).as_posix()
        return str(path)

    def var(self, name: str) -> str | None:
        value = self.env.get(name, "").strip()
        return value or None

    def project_folders(self) -> list[Path]:
        """The project folder and its parents up to the repository root (the first with
        ``.git``), never the home directory or above it: where project settings are."""
        return list(_upwards(self.root, self.home))


def _claude_code(places: _Places) -> tuple[_Rank, Detected] | None:
    model = _without_tag(places.var("ANTHROPIC_MODEL"))
    if model:
        return (_ENV, 0), Detected(f"claude-code:{model}", "ANTHROPIC_MODEL")
    files = [
        ((_PROJECT, distance), folder / ".claude" / name)
        for distance, folder in enumerate(places.project_folders())
        for name in ("settings.local.json", "settings.json")
    ]
    config = places.var("CLAUDE_CONFIG_DIR")
    files.append(
        ((_USER, 0), (Path(config) if config else places.home / ".claude") / "settings.json")
    )
    for rank, path in files:
        model = claude_settings_model(path)
        if model:
            return rank, Detected(f"claude-code:{model}", places.shown(path))
    return None


def _codex(places: _Places) -> tuple[_Rank, Detected] | None:
    codex_home = places.var("CODEX_HOME")
    user = Path(codex_home) if codex_home else places.home / ".codex"
    files = [
        ((_PROJECT, distance), folder / ".codex" / "config.toml")
        for distance, folder in enumerate(places.project_folders())
    ]
    files.append(((_USER, 0), user / "config.toml"))
    layers = [
        (rank, path, data) for rank, path in files if (data := _toml_object(path)) is not None
    ]
    profile = next((p for _, _, d in layers if (p := _text(d.get("profile")))), None)
    # As Codex merges them: the selected profile before the top level, and within each, the
    # nearest file first.
    tables = [
        (
            rank,
            _table(_table(data, "profiles"), profile),
            f"{places.shown(path)}, profile {profile}",
        )
        for rank, path, data in layers
        if profile
    ]
    tables += [(rank, data, places.shown(path)) for rank, path, data in layers]
    hit = next(((r, m, s) for r, t, s in tables if (m := _text(t.get("model")))), None)
    if hit is None:
        return None
    rank, model, source = hit
    provider = next((p for _, t, _ in tables if (p := _text(t.get("model_provider")))), None)
    return rank, Detected(hosted_spec(provider or "openai", model), source)


def _opencode(places: _Places) -> tuple[_Rank, Detected] | None:
    config_home = places.var("XDG_CONFIG_HOME")
    user = (Path(config_home) if config_home else places.home / ".config") / "opencode"
    folders = [((_PROJECT, d), folder) for d, folder in enumerate(places.project_folders())]
    for rank, folder in (*folders, ((_USER, 0), user)):
        for name in ("opencode.json", "opencode.jsonc"):
            path = folder / name
            value = _text(_json_object(path).get("model"))
            if value:
                source = places.shown(path)
                provider, _, model = value.partition("/")
                if model:
                    return rank, Detected(hosted_spec(provider, model), source)
                return rank, _named("OpenCode", value, _maker_spec(value), source)
    return None


def _aider(places: _Places) -> tuple[_Rank, Detected] | None:
    model = places.var("AIDER_MODEL")
    if model:
        return (_ENV, 0), _named("Aider", model, _litellm_spec(model), "AIDER_MODEL")
    files = [
        ((_PROJECT, distance), folder / ".aider.conf.yml")
        for distance, folder in enumerate(places.project_folders())
    ]
    files.append(((_USER, 0), places.home / ".aider.conf.yml"))
    for rank, path in files:
        model = _yaml_model(_read(path))
        if model:
            return rank, _named("Aider", model, _litellm_spec(model), places.shown(path))
    return None


# ------------------------------------------------------------------- helpers
def _named(agent: str, model: str, spec: str | None, source: str) -> Detected:
    """A detected model, or, when since-cutoff cannot name it, the problem to report."""
    if spec is not None:
        return Detected(spec, source)
    return Detected(
        "",
        source,
        problem=(
            f"{agent}'s model '{model}' (in {source}) is not a model or alias since-cutoff "
            "recognises, so it cannot tell which model to test; pass --model provider:model, "
            "e.g. --model openai:gpt-5.4"
        ),
    )


def _maker_spec(model: str) -> str | None:
    """``maker:model`` when the model id shows its maker (``gpt-5.4`` -> ``openai:gpt-5.4``)."""
    bare = bare_model_id(model)
    maker = next((m for pattern, m in _MAKERS if pattern.match(bare)), None)
    return f"{maker}:{bare}" if maker is not None else None


def _litellm_spec(model: str) -> str | None:
    """Aider's LiteLLM names: ``gpt-5.4``, ``anthropic/claude-...``, ``gemini/gemini-2.5-pro``,
    and its aliases (``4o``, ``flash``, ...). None for a name with no provider, maker or alias
    (never a spec that is only a provider, such as ``deepseek``)."""
    provider, _, rest = model.partition("/")
    if provider and rest:
        return hosted_spec(provider, rest)
    return _AIDER_ALIASES.get(model.strip().lower()) or _maker_spec(model)


def _upwards(root: Path, home: Path) -> Iterator[Path]:
    """``root`` and its parents, up to the repository root (the first with ``.git``); never the
    home directory or above it, whose settings are the user's."""
    for folder in (root, *root.parents):
        if folder == home or folder in home.parents:
            return
        yield folder
        if _exists(folder / ".git"):
            return


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # a folder the user may not read
        return False


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""


def _json_object(path: Path) -> dict[str, Any]:
    """A JSON (or JSON with comments) file's top-level object; ``{}`` when there is none."""
    text = _read(path)
    if not text.strip():
        return {}
    plain = _JSONC.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    try:
        data = json.loads(plain)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _toml_object(path: Path) -> dict[str, Any] | None:
    text = _read(path)
    if not text.strip():
        return None
    try:
        data: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return data


def _yaml_model(text: str) -> str | None:
    """The last top-level ``model:`` value in a YAML file (a later key overrides)."""
    values = [next(g for g in m.groups() if g is not None) for m in _YAML_MODEL.finditer(text)]
    return (values[-1].strip() or None) if values else None


def _table(data: dict[str, Any], key: str | None) -> dict[str, Any]:
    value = data.get(key) if key else None
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _without_tag(model: str | None) -> str | None:
    """``opus[1m]`` -> ``opus``: the context size does not change what the model knows."""
    return (re.sub(r"\[[^\]]*\]$", "", model).strip() or None) if model else None
