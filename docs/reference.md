# Reference: environment, files and exit codes

Use `since-cutoff status --json` for an offline check, or `since-cutoff sync --check`
to compare the notes with current API diffs. This page describes the settings read by
the code; it does not require setting provider keys for commands that do not call a model.

## Environment variables

| Group | Variable | Read by | Effect when set |
|---|---|---|---|
| since-cutoff | `SINCE_CUTOFF_MODEL` | Model detection in `run`, `scan`, `sync`, `status` and MCP tools | Explicit `provider:model` default; `--model` takes precedence. `sync` otherwise keeps the model of an existing block. |
| since-cutoff | `SINCE_CUTOFF_CACHE` | Commands/tools using the disk cache, including `cache path` and `cache clear` | Cache root, with `~` expanded; overrides platform defaults below. |
| since-cutoff | `SINCE_CUTOFF_NO_PROCESSES` | Engine diffing in `run`, `scan`, `sync` and MCP tools | Any nonempty value disables process workers, including the string `0`; diffs run in-process. |
| Coding agents | `CLAUDECODE` | Model detection; `run`'s Claude Code subprocess | Nonempty other than `0` restricts detection to Claude Code settings (after `SINCE_CUTOFF_MODEL`). Removed from the child environment to avoid nested-session detection. |
| Coding agents | `CLAUDE_CODE_ENTRYPOINT` | `run`'s Claude Code subprocess | Removed from the child environment; not a model selector. |
| Coding agents | `ANTHROPIC_MODEL` | Model detection | Claude Code model name; takes precedence over its settings files. |
| Coding agents | `CLAUDE_CONFIG_DIR` | Model detection | Replaces `~/.claude` for the user `settings.json`. |
| Coding agents | `CODEX_HOME` | Model detection | Replaces `~/.codex` for the user `config.toml`; project config still participates. |
| Coding agents | `XDG_CONFIG_HOME` | Model detection | Replaces `~/.config` for user OpenCode settings under `opencode/`. |
| Coding agents | `GEMINI_MODEL` | Model detection | Gemini CLI model; takes precedence over Gemini settings files. |
| Coding agents | `AIDER_MODEL` | Model detection | Aider model; takes precedence over `.aider.conf.yml`. |
| Coding agents | `CLAUDE_PROJECT_DIR` | MCP tools resolving a project path | Base for relative `project_dir`; otherwise the server's working directory. Absolute paths are unchanged. |
| Providers | `ANTHROPIC_API_KEY` | `run` with the Anthropic API | Required API key; not used by the Claude Code CLI provider. |
| Providers | `ANTHROPIC_BASE_URL` | `run` with the Anthropic API | API base URL after `--base-url`, before the provider default. |
| Providers | `OPENAI_API_KEY` | `run` with OpenAI or `openai-compatible` | Required for OpenAI; optional for a custom compatible server. |
| Providers | `OPENAI_BASE_URL` | `run` with OpenAI | OpenAI base URL; currently takes precedence even over `--base-url`. Does not affect `openai-compatible`. |
| Providers | `OPENROUTER_API_KEY` | `run` with OpenRouter | Required OpenRouter key. |
| Providers | `DEEPSEEK_API_KEY` | `run` with DeepSeek | Required DeepSeek key. Ollama needs no key. |
| Output | `COLUMNS` | CLI console output | Controls width; noninteractive output otherwise uses 160 columns. |
| Cache defaults | `XDG_CACHE_HOME` | Cache root on non-Windows/non-macOS systems | Base for `since-cutoff/`; defaults to `~/.cache`. |
| Cache defaults | `LOCALAPPDATA` | Cache root on Windows | Base for `since-cutoff/Cache`; defaults to `~/AppData/Local`. macOS instead uses `~/Library/Caches/since-cutoff`. |
| GitHub Actions | `GITHUB_WORKSPACE` | Scan Markdown summaries and annotations | Relativizes file paths to the workspace. |
| GitHub Actions | `GITHUB_REPOSITORY`, `GITHUB_SHA` | Scan Markdown summaries | Repository and commit used to link source files; both are needed for GitHub source links. |
| GitHub Actions | `GITHUB_SERVER_URL` | Scan Markdown summaries | Source-link server, default `https://github.com`. |

The model-detection variables are defined in `hosts.ENV_VARS`. Detection ranks environment,
project, then user settings; between equally specific settings the agent order is Claude
Code, Codex, Gemini CLI, OpenCode, Aider. `scan`, `sync`, `status` and MCP tools do not send
model prompts. Provider credentials apply to `run`, not to reading a model's training cutoff.

## Files read and written

Paths are relative to the selected project unless marked as user settings or cache paths.

| Files/directories | Read by | Writes and purpose |
|---|---|---|
| `pyproject.toml`, `requirements*.txt` and referenced requirement files; `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`; `.python-version` | Project loading in `run`, `scan`, `sync`, `status`, MCP project tools | Read only: dependency declarations, versions and Python target. The first supported lockfile supplies locked versions. |
| `.venv`, `venv`, `env`, `.env` distribution metadata | Project loading when an environment is available | Read only: installed versions; not an instruction to execute the project's package code. |
| Project `.py` / `.pyi` sources | `run`, `scan`, `sync`, MCP project-change tools | Read statically for imports/API use. `status` does not rescan source usage. |
| Project/parent `.claude/settings.local.json`, `.claude/settings.json`; user `~/.claude/settings.json` | Model detection | Read only, subject to `CLAUDE_CONFIG_DIR`. |
| Project/parent `.codex/config.toml`; user `~/.codex/config.toml` | Model detection | Read only, including selected profiles; user root follows `CODEX_HOME`. |
| Project/parent and user `.gemini/settings.json`, `.aider.conf.yml` | Model detection | Read only. Gemini accepts a model string or nested `model.name`. |
| Project/parent `opencode.json`, `opencode.jsonc`; user `~/.config/opencode/` equivalents | Model detection | Read only; user base follows `XDG_CONFIG_HOME`. |
| `AGENTS.md`, `CLAUDE.md`, or `--target` | `sync`, `status`, `unapply`, `run --apply` | `sync` writes only after confirmation or `--yes`; `--check`/`--dry-run` do not write. `run --apply` adds notes; `unapply` removes the marked block. Other file contents are retained. |
| `.since-cutoff/report.md`, `.since-cutoff/results.json`, `.since-cutoff/.gitignore` | Reports from `run` and `scan` | Written under the project by default (`--out` changes the report directory). The ignore file keeps reports out of git. |
| `--markdown FILE`, `--tasks-out FILE`, `--tasks-from FILE` | CLI reports; `run` task reuse | Markdown/task output paths are explicit writes; `--tasks-from` is read only. `--markdown -` writes stdout. |
| Cache `pypi/`, `sources/`, `sources-meta/`, `diffs/` | Package lookup, source extraction and static diffing | Cached metadata, extracted source trees, extraction metadata and API diffs. |
| Cache `tasks/`, `answers/`, `notes/`, `envs/` | `run` | Generated tasks, model answers, notes and isolated type-checker environments. |
| Cache `models/` | Model registry/cutoff lookup | Cached registry data; a bundled snapshot supports offline lookup. |
| Temporary `since-cutoff-check-*` directories | `run` type checking | Temporary snippets/configuration, removed after checking. No model-written code is executed. |

`since-cutoff cache path` prints the effective cache root. `since-cutoff cache clear`
clears the namespaces in `cli.CACHE_NAMESPACES`, listed above. It does not remove project
reports or notes blocks. See [the data policy](../PRIVACY.md) for network and storage details.

## Exit codes

These meanings follow the CLI's `EXAMPLES` and `sync.EXIT_*` constants. Help is exit 0;
invalid arguments are exit 2. An interrupted command returns 130. A closed output pipe
returns 141, including on commands that otherwise succeed.

| Command | Codes and meaning |
|---|---|
| `run` | 0: completed; 1: operational error, including no scoreable answer; 2: usage error; 3: stale API use with `--fail-on-stale`. |
| `scan` | 0: completed; 1: operational error; 2: usage error; 3: the selected `--fail-on` condition (`changes`, `used`, `old-form`; `--fail-on-changes` aliases `changes`). |
| `sync` | 0: current or written, or `--dry-run` without `--check`; 1: operational error; 2: usage error; 3: out of date and not written (`--check`, declined confirmation or no terminal); 4: hand-edited changed block refused without `--force` (except dry-run without check). |
| `status` | 0: current or no block; 1: operational error; 2: usage error; 3: out of date. `--hook` deliberately returns 0 for its status check, including unreadable projects. |
| `unapply`, `models`, `cache`, `mcp` | 0: normal completion; 1: handled operational error; 2: usage error. MCP tool errors are protocol responses, not separate CLI exit codes. |

An exit 0 from `status` with no block does not say whether notes are needed:
`since-cutoff sync --check` checks that. Test results and downstream acceptance are separate.
