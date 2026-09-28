from pathlib import Path

ROOT = Path(".")

def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected exactly one match for replacement, found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")

# hosts.py: document Gemini CLI and include it in detection precedence.
replace_once(
    "src/since_cutoff/hosts.py",
    """3. Elsewhere, by scope: first the agents' environment variables (Claude Code's
   ``ANTHROPIC_MODEL``, then Aider's ``AIDER_MODEL``); then the project's settings, the nearest
""",
    """3. Elsewhere, by scope: first the agents' environment variables (Claude Code's
   ``ANTHROPIC_MODEL``, Gemini CLI's ``GEMINI_MODEL``, then Aider's ``AIDER_MODEL``); then the
   project's settings, the nearest
""",
)
replace_once(
    "src/since_cutoff/hosts.py",
    """   - Codex: ``.codex/config.toml``; ``$CODEX_HOME/config.toml`` or ``~/.codex/config.toml``.
     As Codex merges them, a ``profile`` selected in any of them picks ``[profiles.<name>]``
     -> ``openai:<model>``.
   - OpenCode: ``model`` (``provider/model``) in ``opencode.json`` or ``opencode.jsonc``;
""",
    """   - Codex: ``.codex/config.toml``; ``$CODEX_HOME/config.toml`` or ``~/.codex/config.toml``.
     As Codex merges them, a ``profile`` selected in any of them picks ``[profiles.<name>]``
     -> ``openai:<model>``.
   - Gemini CLI: ``model`` in ``.gemini/settings.json``; ``~/.gemini/settings.json``.
     Recent versions use ``{"model": {"name": "gemini-..."} }``; older ones may use a string.
   - OpenCode: ``model`` (``provider/model``) in ``opencode.json`` or ``opencode.jsonc``;
""",
)
replace_once(
    "src/since_cutoff/hosts.py",
    '    "XDG_CONFIG_HOME",\n    "AIDER_MODEL",\n',
    '    "XDG_CONFIG_HOME",\n    "GEMINI_MODEL",\n    "AIDER_MODEL",\n',
)
replace_once(
    "src/since_cutoff/hosts.py",
    '"OpenCode and Aider settings), so this tests Claude Code\'s default model. To test another, "',
    '"Gemini CLI, OpenCode and Aider settings), so this tests Claude Code\'s default model. To test another, "',
)
replace_once(
    "src/since_cutoff/hosts.py",
    '"OpenCode and Aider settings), so this uses the training cutoff of Claude Code\'s default "',
    '"Gemini CLI, OpenCode and Aider settings), so this uses the training cutoff of Claude Code\'s default "',
)
replace_once(
    "src/since_cutoff/hosts.py",
    """        (_claude_code,) if inside_claude_code(env) else (_claude_code, _codex, _opencode, _aider)
""",
    """        (_claude_code,)
        if inside_claude_code(env)
        else (_claude_code, _codex, _gemini, _opencode, _aider)
""",
)
replace_once(
    "src/since_cutoff/hosts.py",
    """def _opencode(places: _Places) -> tuple[_Rank, Detected] | None:
""",
    """def _gemini(places: _Places) -> tuple[_Rank, Detected] | None:
    model = places.var("GEMINI_MODEL")
    if model:
        return (_ENV, 0), _named("Gemini CLI", model, _maker_spec(model), "GEMINI_MODEL")

    files = [
        ((_PROJECT, distance), folder / ".gemini" / "settings.json")
        for distance, folder in enumerate(places.project_folders())
    ]
    files.append(((_USER, 0), places.home / ".gemini" / "settings.json"))
    for rank, path in files:
        value = _json_object(path).get("model")
        model = _text(value.get("name")) if isinstance(value, dict) else _text(value)
        if model:
            return rank, _named("Gemini CLI", model, _maker_spec(model), places.shown(path))
    return None


def _opencode(places: _Places) -> tuple[_Rank, Detected] | None:
""",
)

# tests/test_hosts.py: requested acceptance cases and Claude Code isolation.
replace_once(
    "tests/test_hosts.py",
    """# --------------------------------------------------------------- Claude Code
""",
    """# -------------------------------------------------------------- Gemini CLI
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
""",
)
replace_once(
    "tests/test_hosts.py",
    '    assert detect(root, home, **inside, AIDER_MODEL="4o") is None\n',
    '    assert detect(root, home, **inside, AIDER_MODEL="4o") is None\n    assert detect(root, home, **inside, GEMINI_MODEL="gemini-2.5-pro") is None\n',
)

# README and contributor documentation.
replace_once(
    "README.md",
    """3. Elsewhere the most specific setting wins: first `ANTHROPIC_MODEL` or `AIDER_MODEL`, then the
   project settings, nearest folder first, from the scanned folder up to the repository root
""",
    """3. Elsewhere the most specific setting wins: first `ANTHROPIC_MODEL`, `GEMINI_MODEL` or
   `AIDER_MODEL`, then the project settings, nearest folder first, from the scanned folder up to
   the repository root
""",
)
replace_once(
    "README.md",
    """| Codex | `.codex/config.toml`, with its selected profile | `$CODEX_HOME/config.toml` or `~/.codex/config.toml` |
| OpenCode | `opencode.json`, `opencode.jsonc` | `~/.config/opencode/` |
""",
    """| Codex | `.codex/config.toml`, with its selected profile | `$CODEX_HOME/config.toml` or `~/.codex/config.toml` |
| Gemini CLI | `.gemini/settings.json` | `~/.gemini/settings.json` |
| OpenCode | `opencode.json`, `opencode.jsonc` | `~/.config/opencode/` |
""",
)
replace_once(
    "CONTRIBUTING.md",
    "| `hosts.py` | Which model the user's coding agent runs, read from Claude Code, Codex, OpenCode and Aider settings (the default for `--model`) |",
    "| `hosts.py` | Which model the user's coding agent runs, read from Claude Code, Codex, Gemini CLI, OpenCode and Aider settings (the default for `--model`) |",
)
replace_once(
    "CHANGELOG.md",
    "## 0.5.0 - 2026-09-28\n\n",
    """## 0.5.0 - 2026-09-28

- Without `--model`, Gemini CLI's model is detected from `GEMINI_MODEL`, then the project's
  `.gemini/settings.json`, then the user's settings; both string and nested `model.name` forms
  are supported, and unknown aliases are reported instead of guessed.

""",
)
