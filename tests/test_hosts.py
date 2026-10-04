"""Which model to test without --model: the coding agent's own settings (``hosts.py``)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from since_cutoff import cli
from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine, Settings
from since_cutoff.errors import ProviderError
from since_cutoff.hosts import (
    CLAUDE_CODE_NOT_FOUND_HINT,
    CLAUDE_CODE_NOT_FOUND_HINT_CUTOFF,
    DEFAULT_SOURCE,
    NOT_FOUND_HINT,
    NOT_FOUND_HINT_CUTOFF,
    Detected,
    detect_model,
    hosted_spec,
    not_found_hint,
)
from since_cutoff.models import ModelRegistry, bare_model_id, normalize_model_id
from since_cutoff.providers import check_spec
from tests.conftest import FakePyPI, ScriptedModel
from tests.test_engine import make_project


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A project that is its own repository root (the OpenCode search stops there)."""
    project = tmp_path / "my project"
    (project / ".git").mkdir(parents=True)
    return project


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home with spaces"


def detect(root: Path, home: Path, **env: str) -> tuple[str, str] | None:
    found = detect_model(root, env=env, home=home)
    return None if found is None else (found.spec, found.source)


# ----------------------------------------------------------------- the order
def test_the_most_specific_setting_wins(root: Path, home: Path) -> None:
    """SINCE_CUTOFF_MODEL; the agents' environment variables; the project's settings; the
    user's. Between equally specific ones: Claude Code, Codex, OpenCode, Aider."""
    env = {
        "SINCE_CUTOFF_MODEL": "openai:gpt-5.4",
        "ANTHROPIC_MODEL": "claude-opus-4-6",
        "AIDER_MODEL": "gpt-4.1",
    }
    write(home / ".claude" / "settings.json", '{"model": "haiku"}')
    codex_user = write(home / ".codex" / "config.toml", 'model = "gpt-5-codex"\n')
    claude = write(root / ".claude" / "settings.json", '{"model": "sonnet"}')
    codex = write(root / ".codex" / "config.toml", 'model = "o3"\n')
    opencode = write(root / "opencode.json", '{"model": "anthropic/claude-sonnet-4-5"}')
    aider = write(root / ".aider.conf.yml", "model: gemini/gemini-2.5-pro\n")

    assert detect(root, home, **env) == ("openai:gpt-5.4", "SINCE_CUTOFF_MODEL")
    del env["SINCE_CUTOFF_MODEL"]
    assert detect(root, home, **env) == ("claude-code:claude-opus-4-6", "ANTHROPIC_MODEL")
    del env["ANTHROPIC_MODEL"]
    assert detect(root, home, **env) == ("openai:gpt-4.1", "AIDER_MODEL")
    assert detect(root, home) == ("claude-code:sonnet", ".claude/settings.json")
    claude.unlink()
    assert detect(root, home) == ("openai:o3", ".codex/config.toml")
    codex.unlink()
    assert detect(root, home) == ("anthropic:claude-sonnet-4-5", "opencode.json")
    opencode.unlink()
    assert detect(root, home) == ("google:gemini-2.5-pro", ".aider.conf.yml")
    aider.unlink()
    assert detect(root, home) == ("claude-code:haiku", "~/.claude/settings.json")
    (home / ".claude" / "settings.json").unlink()
    assert detect(root, home) == ("openai:gpt-5-codex", "~/.codex/config.toml")
    codex_user.unlink()
    assert detect(root, home) is None


def test_a_project_setting_beats_any_user_setting(root: Path, home: Path) -> None:
    # Another agent's user-level model says nothing about the agent set up for this project.
    claude_user = write(home / ".claude" / "settings.json", '{"model": "opus"}')
    write(root / ".codex" / "config.toml", 'model = "gpt-5.4"\n')
    assert detect(root, home) == ("openai:gpt-5.4", ".codex/config.toml")

    claude_user.unlink()
    (root / ".codex" / "config.toml").unlink()
    write(home / ".codex" / "config.toml", 'model = "gpt-5.4"\n')
    write(root / "opencode.json", '{"model": "anthropic/claude-sonnet-4-5"}')
    assert detect(root, home) == ("anthropic:claude-sonnet-4-5", "opencode.json")
    (root / "opencode.json").unlink()
    write(root / ".aider.conf.yml", "model: gemini/gemini-2.5-pro\n")
    assert detect(root, home) == ("google:gemini-2.5-pro", ".aider.conf.yml")


def test_project_settings_are_found_up_to_the_repository_root(root: Path, home: Path) -> None:
    """``since-cutoff scan backend/`` in a monorepo: every agent's project settings are looked
    for from the folder up to the repository root, the nearest first."""
    backend = root / "backend"
    backend.mkdir()
    write(home / ".codex" / "config.toml", 'model = "gpt-5.4"\n')
    aider = write(root / ".aider.conf.yml", "model: gemini/gemini-2.5-pro\n")
    assert detect(backend, home) == ("google:gemini-2.5-pro", "../.aider.conf.yml")
    aider.unlink()
    codex = write(root / ".codex" / "config.toml", 'model = "gpt-5.5"\n')
    assert detect(backend, home) == ("openai:gpt-5.5", "../.codex/config.toml")
    claude = write(root / ".claude" / "settings.json", '{"model": "claude-opus-4-6"}')
    assert detect(backend, home) == ("claude-code:claude-opus-4-6", "../.claude/settings.json")
    # A setting in the folder itself is nearer than any in the repository root.
    write(backend / ".codex" / "config.toml", 'model = "gpt-5.4-mini"\n')
    assert detect(backend, home) == ("openai:gpt-5.4-mini", ".codex/config.toml")
    claude.unlink()
    codex.unlink()
    write(backend / ".claude" / "settings.local.json", '{"model": "haiku"}')
    assert detect(backend, home) == ("claude-code:haiku", ".claude/settings.local.json")


def test_inside_claude_code_only_its_own_settings_count(root: Path, home: Path) -> None:
    """Claude Code sets CLAUDECODE=1 for the commands it runs: then the model under test is
    Claude Code's, whatever other agents' files say."""
    write(home / ".codex" / "config.toml", 'model = "gpt-5.5"\n')
    write(root / "opencode.json", '{"model": "openai/gpt-5.4"}')
    write(root / ".aider.conf.yml", "model: gpt-4.1\n")
    assert detect(root, home) == ("openai:gpt-5.4", "opencode.json")  # outside Claude Code
    inside = {"CLAUDECODE": "1"}
    assert detect(root, home, **inside) is None  # Claude Code's default, not GPT
    assert detect(root, home, **inside, AIDER_MODEL="4o") is None
    assert detect(root, home, **inside, GEMINI_MODEL="gemini-2.5-pro") is None
    assert not_found_hint(inside) == CLAUDE_CODE_NOT_FOUND_HINT
    assert not_found_hint({}) == NOT_FOUND_HINT
    # scan and sync test no model: they use its cutoff.
    assert not_found_hint(inside, probes=False) == CLAUDE_CODE_NOT_FOUND_HINT_CUTOFF
    assert not_found_hint({}, probes=False) == NOT_FOUND_HINT_CUTOFF
    assert "tests" not in NOT_FOUND_HINT_CUTOFF + CLAUDE_CODE_NOT_FOUND_HINT_CUTOFF

    write(home / ".claude" / "settings.json", '{"model": "opus"}')
    assert detect(root, home, **inside) == ("claude-code:opus", "~/.claude/settings.json")
    write(root / ".claude" / "settings.json", '{"model": "claude-sonnet-4-6"}')
    assert detect(root, home, **inside) == (
        "claude-code:claude-sonnet-4-6",
        ".claude/settings.json",
    )
    assert detect(root, home, **inside, ANTHROPIC_MODEL="claude-opus-4-6") == (
        "claude-code:claude-opus-4-6",
        "ANTHROPIC_MODEL",
    )
    assert detect(root, home, **inside, SINCE_CUTOFF_MODEL="openai:gpt-5.4") == (
        "openai:gpt-5.4",
        "SINCE_CUTOFF_MODEL",
    )
    assert detect(root, home, CLAUDECODE="0") == (
        "claude-code:claude-sonnet-4-6",
        ".claude/settings.json",
    )


def test_nothing_is_detected_without_a_model_setting(root: Path, home: Path) -> None:
    assert detect(root, home) is None
    write(root / ".claude" / "settings.json", '{"permissions": {"allow": []}}')
    write(home / ".codex" / "config.toml", 'approval_policy = "never"\n')
    write(root / "opencode.json", '{"theme": "dark"}')
    write(home / ".aider.conf.yml", "dark-mode: true\n  model: nested-is-not-top-level\n")
    assert detect(root, home, SINCE_CUTOFF_MODEL="  ", ANTHROPIC_MODEL="") is None


def test_broken_files_are_skipped(root: Path, home: Path) -> None:
    write(root / ".claude" / "settings.json", '{"model": ')
    write(home / ".claude" / "settings.json", "[1, 2]")
    write(home / ".codex" / "config.toml", "model = \n")
    write(root / "opencode.json", "not json")
    (root / ".aider.conf.yml").write_bytes(b"model: \xff\xfe\n")  # not UTF-8
    write(home / ".aider.conf.yml", "model: 'gpt-4.1'\n")
    assert detect(root, home) == ("openai:gpt-4.1", "~/.aider.conf.yml")


# -------------------------------------------------------------- Gemini CLI
def test_gemini_cli_environment_project_and_user_precedence(root: Path, home: Path) -> None:
    write(home / ".gemini" / "settings.json", '{"model": "gemini-2.5-flash"}')
    write(root / ".gemini" / "settings.json", '{"model": {"name": "gemini-2.5-pro"}}')

    assert detect(root, home) == ("google:gemini-2.5-pro", ".gemini/settings.json")
    assert detect(root, home, GEMINI_MODEL="gemini-2.5-flash-lite") == (
        "google:gemini-2.5-flash-lite",
        "GEMINI_MODEL",
    )

    (root / ".gemini" / "settings.json").unlink()
    assert detect(root, home) == ("google:gemini-2.5-flash", "~/.gemini/settings.json")


def test_gemini_cli_supports_string_and_nested_model_and_skips_broken_json(
    root: Path, home: Path
) -> None:
    write(root / ".gemini" / "settings.json", '{"model": "gemini-2.5-flash"}')
    assert detect(root, home) == ("google:gemini-2.5-flash", ".gemini/settings.json")

    write(root / ".gemini" / "settings.json", '{"model": ')
    write(home / ".gemini" / "settings.json", '{"model": {"name": "gemini-2.5-pro"}}')
    assert detect(root, home) == ("google:gemini-2.5-pro", "~/.gemini/settings.json")


def test_gemini_cli_unknown_alias_is_reported_not_guessed(root: Path, home: Path) -> None:
    found = detect_model(root, env={"GEMINI_MODEL": "auto"}, home=home)
    assert found is not None and found.spec == "" and found.problem is not None
    assert found.problem.startswith(
        "Gemini CLI's model 'auto' (in GEMINI_MODEL) is not a model or alias"
    )


# --------------------------------------------------------------- Claude Code
def test_claude_code_settings_in_their_own_order(root: Path, home: Path) -> None:
    write(home / ".claude" / "settings.json", '{"model": "opus[1m]"}')
    assert detect(root, home) == ("claude-code:opus", "~/.claude/settings.json")
    write(root / ".claude" / "settings.json", '{"model": "claude-sonnet-4-6"}')
    assert detect(root, home) == ("claude-code:claude-sonnet-4-6", ".claude/settings.json")
    write(root / ".claude" / "settings.local.json", '{"model": "haiku"}')
    assert detect(root, home) == ("claude-code:haiku", ".claude/settings.local.json")
    assert detect(root, home, ANTHROPIC_MODEL="claude-opus-4-6[1m]") == (
        "claude-code:claude-opus-4-6",
        "ANTHROPIC_MODEL",
    )


def test_claude_config_dir_replaces_the_user_settings(root: Path, home: Path, tmp_path) -> None:
    write(home / ".claude" / "settings.json", '{"model": "opus"}')
    config = tmp_path / "claude config"
    path = write(config / "settings.json", '{"model": "sonnet", "env": {"X": "secret"}}')
    assert detect(root, home, CLAUDE_CONFIG_DIR=str(config)) == ("claude-code:sonnet", str(path))


# --------------------------------------------------------------------- Codex
def test_codex_profiles_projects_and_codex_home(root: Path, home: Path, tmp_path) -> None:
    write(
        home / ".codex" / "config.toml",
        'model = "gpt-5-codex"\nprofile = "deep"\n\n[profiles.deep]\nmodel = "o3"\n'
        '[model_providers.azure]\nenv_key = "AZURE_KEY"\n',
    )
    assert detect(root, home) == ("openai:o3", "~/.codex/config.toml, profile deep")

    codex_home = tmp_path / "codex home"
    path = write(codex_home / "config.toml", 'model = "gpt-5.4"\n')
    assert detect(root, home, CODEX_HOME=str(codex_home)) == ("openai:gpt-5.4", str(path))

    write(root / ".codex" / "config.toml", 'model = "qwen3-coder:30b"\nmodel_provider = "ollama"\n')
    found = detect(root, home, CODEX_HOME=str(codex_home))
    assert found == ("ollama:qwen3-coder:30b", ".codex/config.toml")


def test_a_codex_profile_that_does_not_set_the_model_keeps_the_top_level_one(
    root: Path, home: Path
) -> None:
    write(
        home / ".codex" / "config.toml",
        'model = "gpt-5.4"\nprofile = "quiet"\n[profiles.quiet]\napproval_policy = "never"\n',
    )
    assert detect(root, home) == ("openai:gpt-5.4", "~/.codex/config.toml")


# ------------------------------------------------------------------ OpenCode
def test_opencode_uses_the_nearest_config_up_to_the_repository(root: Path, home: Path) -> None:
    write(root.parent / "opencode.json", '{"model": "openai/gpt-5.4"}')  # outside the repository
    write(home / ".config" / "opencode" / "opencode.json", '{"model": "google/gemini-2.5-pro"}')
    assert detect(root, home) == ("google:gemini-2.5-pro", "~/.config/opencode/opencode.json")

    write(
        root / "opencode.jsonc",
        '{\n  // the model for this repository\n  "$schema": "https://opencode.ai/config.json",\n'
        '  "model": "github-copilot/claude-sonnet-4.5", /* via Copilot */\n  "provider": {},\n}\n',
    )
    service = root / "services" / "api"
    service.mkdir(parents=True)
    assert detect(service, home) == ("anthropic:claude-sonnet-4.5", "../../opencode.jsonc")
    write(service / "opencode.json", '{"model": "anthropic/claude-opus-4-6"}')
    assert detect(service, home) == ("anthropic:claude-opus-4-6", "opencode.json")


def test_opencode_global_config_honours_xdg_config_home(root: Path, home: Path, tmp_path) -> None:
    config = tmp_path / "xdg"
    path = write(config / "opencode" / "opencode.jsonc", '{"model": "openrouter/x-ai/grok-4"}')
    assert detect(root, home, XDG_CONFIG_HOME=str(config)) == (
        "openrouter:x-ai/grok-4",
        str(path),
    )


# --------------------------------------------------------------------- Aider
def test_aider_reads_only_the_model_line(root: Path, home: Path) -> None:
    write(
        home / ".aider.conf.yml",
        "# aider settings\nopenai-api-key: sk-do-not-read\nmodel: gemini/gemini-2.5-pro  # fast\n",
    )
    found = detect_model(root, env={}, home=home)
    assert found == Detected("google:gemini-2.5-pro", "~/.aider.conf.yml")
    assert "sk-" not in repr(found)
    write(root / ".aider.conf.yml", 'model: "anthropic/claude-sonnet-4-5"\nmodel: sonnet\n')
    assert detect(root, home) == ("anthropic:sonnet", ".aider.conf.yml")  # the last key wins
    assert detect(root, home, AIDER_MODEL="deepseek/deepseek-chat") == (
        "deepseek:deepseek-chat",
        "AIDER_MODEL",
    )


@pytest.mark.parametrize(
    ("alias", "spec"),
    [
        ("4o", "openai:gpt-4o"),
        ("flash", "google:gemini-2.5-flash"),
        ("gemini", "google:gemini-2.5-pro"),
        ("deepseek", "deepseek:deepseek-chat"),  # not the bare provider 'deepseek'
        ("r1", "deepseek:deepseek-reasoner"),
        ("grok3", "xai:grok-3"),
        ("sonnet", "anthropic:sonnet"),  # the newest Sonnet, reported as an assumption
        ("gpt-4.1", "openai:gpt-4.1"),
    ],
)
def test_aider_aliases_are_the_models_they_stand_for(
    root: Path, home: Path, alias: str, spec: str
) -> None:
    assert detect(root, home, AIDER_MODEL=alias) == (spec, "AIDER_MODEL")
    write(root / ".aider.conf.yml", f"model: {alias}\n")
    assert detect(root, home) == (spec, ".aider.conf.yml")


@pytest.mark.parametrize(
    ("model", "spec"),
    [
        ("dashscope/qwen3-coder-plus", "alibaba:qwen3-coder-plus"),
        ("moonshot/kimi-k2.7-code", "moonshotai:kimi-k2.7-code"),
        ("glm-4.6", "zai:glm-4.6"),
        ("llama-3.3-70b-instruct", "llama:llama-3.3-70b-instruct"),
    ],
)
def test_aider_recognises_additional_model_makers(
    root: Path, home: Path, model: str, spec: str
) -> None:
    write(root / ".aider.conf.yml", f"model: {model}\n")
    assert detect(root, home) == (spec, ".aider.conf.yml")


@pytest.mark.parametrize(
    ("model", "spec"),
    [
        ("qwen3-coder-plus", "alibaba:qwen3-coder-plus"),
        ("kimi-k2.7-code", "moonshotai:kimi-k2.7-code"),
        ("glm-4.6", "zai:glm-4.6"),
        ("llama-3.3-70b-instruct", "llama:llama-3.3-70b-instruct"),
    ],
)
def test_opencode_bare_ids_resolve_to_their_makers(
    root: Path, home: Path, model: str, spec: str
) -> None:
    write(root / "opencode.json", json.dumps({"model": model}))
    assert detect(root, home) == (spec, "opencode.json")


@pytest.mark.parametrize("model", ["my-local-model", "openai", "qwen", "deepseek/"])
def test_a_model_name_since_cutoff_cannot_place_is_reported_not_guessed(
    root: Path, home: Path, model: str
) -> None:
    found = detect_model(root, env={"AIDER_MODEL": model}, home=home)
    assert found is not None and found.spec == "" and found.problem is not None
    assert found.problem.startswith(f"Aider's model '{model}' (in AIDER_MODEL) is not a model")
    write(root / "opencode.json", '{"model": "my-local-model"}')  # not provider/model
    found = detect_model(root, env={}, home=home)
    assert found is not None and found.spec == "" and "OpenCode's model" in (found.problem or "")


def test_an_unknown_provider_suggests_an_openai_compatible_server(root: Path, home: Path) -> None:
    write(root / "opencode.json", '{"model": "lmstudio/my-local-model"}')
    assert detect(root, home) == ("lmstudio:my-local-model", "opencode.json")
    with pytest.raises(ProviderError) as info:
        check_spec("lmstudio:my-local-model")
    assert "'openai-compatible:my-local-model' with --base-url" in str(info.value)
    assert "'<provider>:lmstudio:my-local-model', or," in str(info.value)


# ------------------------------------------------------ provider/model names
@pytest.mark.parametrize(
    ("provider", "model", "spec"),
    [
        ("anthropic", "claude-sonnet-4-5", "anthropic:claude-sonnet-4-5"),
        ("openai", "gpt-5.4", "openai:gpt-5.4"),
        ("openrouter", "anthropic/claude-sonnet-4.5", "openrouter:anthropic/claude-sonnet-4.5"),
        ("ollama_chat", "qwen2.5-coder:32b", "ollama:qwen2.5-coder:32b"),
        ("oss", "gpt-oss:20b", "ollama:gpt-oss:20b"),
        ("google", "gemini-2.5-pro", "google:gemini-2.5-pro"),
        ("vertex_ai", "gemini-2.5-pro", "google:gemini-2.5-pro"),
        ("github-copilot", "gpt-5.4", "openai:gpt-5.4"),
        ("github-copilot", "kimi-k2.7-code", "moonshotai:kimi-k2.7-code"),
        ("github-copilot", "o3", "openai:o3"),
        (
            "amazon-bedrock",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "anthropic:claude-sonnet-4-5-20250929",
        ),
        ("google-vertex-anthropic", "claude-opus-4-1@20250805", "anthropic:claude-opus-4-1"),
        ("opencode", "grok-code", "xai:grok-code"),
        ("mistral", "devstral-medium-2507", "mistral:devstral-medium-2507"),
        ("groq", "llama-3.3-70b-versatile", "llama:llama-3.3-70b-versatile"),
        (None, "sonnet", "anthropic:sonnet"),
        (None, "my-local-model", "my-local-model"),
    ],
)
def test_hosted_models_are_named_so_their_cutoff_can_be_found(
    provider: str | None, model: str, spec: str
) -> None:
    assert hosted_spec(provider, model) == spec


@pytest.mark.parametrize(
    ("model_id", "bare"),
    [
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929"),
        ("anthropic.claude-3-5-haiku-20241022-v1:0", "claude-3-5-haiku-20241022"),
        ("claude-opus-4-1@20250805", "claude-opus-4-1"),
        ("openrouter/anthropic/claude-sonnet-4.5", "claude-sonnet-4.5"),
        ("Claude-Opus-4-6[1m]", "claude-opus-4-6"),
        ("gpt-4.5", "gpt-4.5"),
    ],
)
def test_bare_model_ids(model_id: str, bare: str) -> None:
    assert bare_model_id(model_id) == bare


def test_bedrock_and_vertex_ids_find_the_makers_model(tmp_path: Path) -> None:
    registry = ModelRegistry(DiskCache(tmp_path), offline=True)
    assert "claude-sonnet-4-5" in normalize_model_id("eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
    for model_id in (
        "claude-sonnet-4-5@20250929",
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ):
        info = registry.lookup(model_id, "claude-code")
        assert info is not None and info.knowledge == date(2025, 7, 31)


def test_scan_accepts_vendors_it_cannot_call() -> None:
    check_spec("google:gemini-2.5-pro", vendors={"google"}, calls=False)
    with pytest.raises(ProviderError, match="cannot call google models itself"):
        check_spec("google:gemini-2.5-pro", vendors={"google"})
    with pytest.raises(ProviderError, match="no known provider"):
        check_spec("qwen3:32b", vendors={"google"}, calls=False)


# -------------------------------------------------------------- the engine
def test_family_names_and_opusplan_map_to_the_newest_model(tmp_path: Path) -> None:
    registry = ModelRegistry(DiskCache(tmp_path), offline=True)
    newest_sonnet = registry.latest_in_family("anthropic", "sonnet", date(2026, 9, 1))
    assert newest_sonnet is not None

    def target(spec: str, *, calls: bool) -> tuple[Engine, Any]:
        settings = Settings(model=spec, today=date(2026, 9, 1))
        store = DiskCache(tmp_path)
        engine = Engine(
            settings,
            store=store,
            llm_cache=store,
            registry=registry,
            provider_factory=lambda s: ScriptedModel(),
        )
        return engine, engine.resolve_target(allow_calls=calls)

    for spec in ("anthropic:sonnet", "claude-code:opusplan"):
        _, scanned = target(spec, calls=False)
        assert scanned.model_id == newest_sonnet.id
        assert scanned.cutoff_source.endswith(
            f"assuming '{spec.split(':')[1]}' = {newest_sonnet.id}"
        )
    engine, ran = target("anthropic:sonnet", calls=True)
    # The Anthropic API takes model ids only, so the run calls the model the alias stood for.
    assert engine.settings.model == ran.spec == f"anthropic:{newest_sonnet.id}"


# ------------------------------------------------------------------ the CLI
@pytest.fixture
def offline_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: FakePyPI) -> list[Any]:
    """The CLI with the fake PyPI and the bundled model registry. Append a model to answer
    the next run; without one the real providers are used (and fail without keys)."""
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))
    models: list[Any] = []

    def engine(settings: Settings, **kwargs: Any) -> Engine:
        extra: dict[str, Any] = {"registry": ModelRegistry(kwargs["store"], offline=True)}
        if models:
            model = models.pop(0)
            extra["provider_factory"] = lambda spec: model
        return Engine(settings, **{**kwargs, "pypi": fake_pypi, **extra})

    monkeypatch.setattr(cli, "Engine", engine)
    return models


@pytest.fixture
def app(tmp_path: Path) -> Path:
    root = make_project(tmp_path)
    (root / ".git").mkdir()
    return root


def test_scan_tests_the_model_in_the_project_settings(app, capsys, offline_cli) -> None:
    write(app / ".claude" / "settings.json", '{"model": "claude-sonnet-4-5"}')
    assert cli.main(["scan", str(app)]) == 0
    out = capsys.readouterr().out
    assert (
        "Model claude-sonnet-4-5, training cutoff 2025-07-31 "
        "(from models.dev, model from .claude/settings.json)" in out
    )
    assert "No model setting found" not in out
    report = (app / ".since-cutoff" / "report.md").read_text(encoding="utf-8")
    assert "(source: models.dev, model from .claude/settings.json)" in report


def test_scan_maps_a_detected_alias_and_says_so(app, capsys, offline_cli, agent_home) -> None:
    write(agent_home / ".claude" / "settings.json", '{"model": "opus[1m]"}')
    assert cli.main(["scan", str(app)]) == 0
    out = capsys.readouterr().out
    assert "model from ~/.claude/settings.json, assuming 'opus' = claude-opus-" in out


def test_without_any_setting_the_default_is_named_as_a_guess(app, capsys, offline_cli) -> None:
    assert cli.main(["scan", str(app)]) == 0
    out = " ".join(capsys.readouterr().out.split())
    # scan tests nothing: it says it uses the default model's cutoff ("so this tests Claude
    # Code's default model" was run's wording).
    assert NOT_FOUND_HINT_CUTOFF in out and NOT_FOUND_HINT not in out
    assert f"model from {DEFAULT_SOURCE}, assuming 'sonnet' = claude-sonnet-" in out


def test_inside_claude_code_a_leftover_codex_setting_is_not_tested(
    app, capsys, offline_cli, agent_home, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    write(agent_home / ".codex" / "config.toml", 'model = "gpt-5.5"\n')
    assert cli.main(["scan", str(app)]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "gpt-5.5" not in out
    assert CLAUDE_CODE_NOT_FOUND_HINT_CUTOFF in out
    assert f"model from {DEFAULT_SOURCE}, assuming 'sonnet' = claude-sonnet-" in out


def test_a_model_setting_that_names_no_model_stops_the_run(
    app, capsys, offline_cli, monkeypatch
) -> None:
    monkeypatch.setenv("AIDER_MODEL", "my-local-model")
    assert cli.main(["scan", str(app)]) == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "Aider's model 'my-local-model' (in AIDER_MODEL) is not a model or alias" in err
    assert "pass --model provider:model" in err


def test_an_explicit_claude_code_model_ignores_the_detection(
    app, capsys, offline_cli, agent_home
) -> None:
    write(app / ".claude" / "settings.json", '{"model": "claude-opus-4-6"}')
    write(agent_home / ".claude" / "settings.json", '{"model": "claude-haiku-4-5"}')
    assert cli.main(["scan", str(app), "--model", "claude-code"]) == 0
    out = capsys.readouterr().out
    # As before detection: the user's settings, never the project's, and no source named.
    assert "Model claude-haiku-4-5, training cutoff 2025-02-28 (from models.dev)" in out
    assert "No model setting found" not in out


def test_a_model_since_cutoff_cannot_call_is_scanned_but_not_run(app, capsys, offline_cli) -> None:
    write(app / "opencode.json", '{"model": "google/gemini-2.5-pro"}')
    assert cli.main(["scan", str(app)]) == 0
    assert (
        "Model gemini-2.5-pro, training cutoff 2025-01-31 (from models.dev, model from opencode.json)"
        in (capsys.readouterr().out)
    )
    assert cli.main(["run", str(app)]) == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "cannot call google models itself" in err
    assert "(the model 'google:gemini-2.5-pro' is set in opencode.json; pass --model" in err


def test_a_run_fails_before_the_scan_when_the_model_cannot_be_called(
    app, capsys, offline_cli, agent_home, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    write(agent_home / ".codex" / "config.toml", 'model = "gpt-5.4"\n')
    assert cli.main(["run", str(app)]) == 1
    captured = capsys.readouterr()
    err = " ".join(captured.err.split())
    assert (
        "OPENAI_API_KEY is not set (the model 'openai:gpt-5.4' is set in ~/.codex/config.toml"
        in err
    )
    assert "Checking" not in captured.out  # no PyPI lookups first


@pytest.mark.pyright
def test_a_run_records_where_its_model_came_from(app, capsys, offline_cli, monkeypatch) -> None:
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "anthropic:claude-sonnet-4-5")
    offline_cli.append(ScriptedModel())
    argv = ["run", str(app), "--cutoff", "2025-07-31", "--max-probes", "1", "--no-fix"]
    assert cli.main(argv) == 0
    results = json.loads((app / ".since-cutoff" / "results.json").read_text(encoding="utf-8"))
    assert results["model_spec"] == "anthropic:claude-sonnet-4-5"
    assert results["cutoff_source"] == "--cutoff, model from SINCE_CUTOFF_MODEL"
    assert results["settings"]["model_source"] == "SINCE_CUTOFF_MODEL"
    report = (app / ".since-cutoff" / "report.md").read_text(encoding="utf-8")
    assert "| model taken from | SINCE_CUTOFF_MODEL |" in report
