# Reference: environment, files and exit codes

What since-cutoff reads from the environment, the files it reads and writes, and what its exit
codes mean. `tests/test_docs.py` fails when the code reads an environment variable that has no
row here, or no longer reads one that has.

## Environment variables

| Group | Variable | Read by | Effect when set |
|---|---|---|---|
| since-cutoff | `SINCE_CUTOFF_MODEL` | Model detection in `run`, `scan`, `sync` and `status` | The model as `provider:model`, ahead of every coding-agent setting; `--model` takes precedence. `sync` keeps the model of an existing block unless `--model` or `--cutoff` is given, and `scan` or `sync` with `--cutoff` and no `--model` uses no model. |
| since-cutoff | `SINCE_CUTOFF_CUTOFF_MARGIN` | `run`, `scan`, `sync`, `status`; the MCP tools | How many days before the training cutoff the comparison release must have been published: a whole number, 0 or more (default 30; 0 compares from the latest release at the cutoff). `--cutoff-margin`, or a tool's `cutoff_margin`, takes precedence. Any other value is an error. |
| since-cutoff | `SINCE_CUTOFF_CACHE` | Every command and MCP tool that uses the cache; `cache path`, `cache info` and `cache clear` | The cache root, with `~` expanded, instead of the platform default (see `XDG_CACHE_HOME` and `LOCALAPPDATA`). |
| since-cutoff | `SINCE_CUTOFF_CACHE_MAX_MB` | Source extraction in `run`, `scan`, `sync` and the MCP tools; `cache info` | The cap on the extracted package sources, in MB (default 2048; 0 or less: no cap). Over it, after a new version is extracted, the least recently used trees are removed, never one the running scan uses or one used in the last hour (another process may be reading it); the first removal prints one line. A value that is not a number gives a warning and the default. |
| since-cutoff | `SINCE_CUTOFF_NO_PROCESSES` | API diffing in `run`, `scan`, `sync` and the MCP tools | Any nonempty value, `0` included, diffs in this process instead of in worker processes. |
| Coding agents | `CLAUDECODE` | Model detection; the `claude` subprocess of `run` | Anything but empty or `0` (Claude Code sets `1` for the commands it runs): only Claude Code's settings are read, after `SINCE_CUTOFF_MODEL`. Removed from the subprocess's environment, so that it does not take itself for a nested session. |
| Coding agents | `CLAUDE_CODE_ENTRYPOINT` | The `claude` subprocess of `run` | Removed from the subprocess's environment; since-cutoff does not read it. |
| Coding agents | `ANTHROPIC_MODEL` | Model detection | Claude Code's model, ahead of its settings files. |
| Coding agents | `CLAUDE_CONFIG_DIR` | Model detection | Replaces `~/.claude` for the user's `settings.json`. |
| Coding agents | `CODEX_HOME` | Model detection | Replaces `~/.codex` for the user's `config.toml`; the project's `.codex/config.toml` still counts. |
| Coding agents | `XDG_CONFIG_HOME` | Model detection | Replaces `~/.config` for the user's OpenCode settings in `opencode/`. |
| Coding agents | `GEMINI_MODEL` | Model detection | Gemini CLI's model, ahead of its settings files. |
| Coding agents | `AIDER_MODEL` | Model detection | Aider's model, ahead of `.aider.conf.yml`. |
| Coding agents | `CLAUDE_PROJECT_DIR` | The MCP tools | The folder a relative `project_dir` is resolved against (Claude Code sets it for the servers it starts); else the server's working directory. An absolute path is used as it is. |
| Providers | `ANTHROPIC_API_KEY` | `run` with an `anthropic:` model; the `claude` subprocess of `run` | Required for `anthropic:`. since-cutoff does not read it for `claude-code:`, but the `claude` subprocess inherits it, as it does `ANTHROPIC_BASE_URL` and the rest of the environment, and Claude Code then uses the key instead of a subscription. |
| Providers | `ANTHROPIC_BASE_URL` | `run` with an `anthropic:` model; the `claude` subprocess of `run` | The API's base URL when `--base-url` is not given; default `https://api.anthropic.com`. The `claude` subprocess sends its requests there too; `--base-url` does not change it. |
| Providers | `OPENAI_API_KEY` | `run` with an `openai:` or `openai-compatible:` model | Required for `openai:`. Sent to an `openai-compatible:` server when set; a local server needs none. |
| Providers | `OPENAI_BASE_URL` | `run` with an `openai:` model | The base URL when `--base-url` is not given; default `https://api.openai.com/v1`. Not read for `openai-compatible:` or the other providers. |
| Providers | `OPENROUTER_API_KEY` | `run` with an `openrouter:` model | Required. |
| Providers | `DEEPSEEK_API_KEY` | `run` with a `deepseek:` model | Required. `ollama:` needs no key (default URL `http://localhost:11434/v1`). |
| Output | `COLUMNS` | The CLI's console output | The width. Output to a file, a pipe or a CI log is otherwise 160 columns wide. |
| Cache defaults | `XDG_CACHE_HOME` | The cache root on Linux and other systems that are not Windows or macOS | The folder `since-cutoff/` goes in; default `~/.cache`. |
| Cache defaults | `LOCALAPPDATA` | The cache root on Windows | The folder `since-cutoff/Cache` goes in; default `~/AppData/Local`. macOS uses `~/Library/Caches/since-cutoff`. |
| GitHub Actions | `GITHUB_WORKSPACE` | `scan --annotate github` and `scan --markdown` | Annotations name files relative to the workspace, and the summary's links point at them there, when the project is inside it. |
| GitHub Actions | `GITHUB_REPOSITORY`, `GITHUB_SHA` | `scan --markdown` | The repository and commit the summary links source files at; both are needed for the links. |
| GitHub Actions | `GITHUB_SERVER_URL` | `scan --markdown` | The server of those links; default `https://github.com`. |
| git | `PATH` | `sync`, where it runs git (the files git ignores, for rule files; git's history, before a first block in AGENTS.md or CLAUDE.md) | Where git is looked for: the first `git` (`git.exe` on Windows) in an absolute folder of it that is neither the current folder nor the project's, nor one inside the project. Windows would otherwise run a `git.exe` in the current folder first. With none found, rule files also name files git ignores, and the history is not read. |
| Network | `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | Python's `urllib`, for every request (PyPI, models.dev, the model APIs) | The proxy, as Python reads it (the lowercase names too); without them, on Windows and macOS, the system's proxy settings. since-cutoff does not read them itself. |
| Network | `SSL_CERT_FILE`, `SSL_CERT_DIR` | Python's `ssl`, for the same requests | The CA certificates to trust (one with a company's certificate, say): `SSL_CERT_FILE` replaces OpenSSL's default bundle file and `SSL_CERT_DIR` its default folder, each on its own, so with only one of them set the other default still counts; on Windows, the system's certificate store is trusted too. since-cutoff does not read them itself. |

The model-detection variables are `hosts.ENV_VARS`. Detection ranks environment variables, then
the project's settings, then the user's; between equally specific settings the agent order is
Claude Code, Codex, Gemini CLI, OpenCode, Aider. `scan`, `sync`, `status` and the MCP tools send
no prompt to any model: the provider variables are for the models `run` calls, the tested one
and the `--task-model`.

## Files read and written

Paths are relative to the project folder unless they are the user's settings, the cache or
temporary folders. Project settings of the coding agents are looked for in the project folder
and its parents up to the repository root (the first folder with `.git`), never in the home
folder or above it.

| Files and folders | Read by | What is read or written |
|---|---|---|
| `pyproject.toml`, `Pipfile`, `requirements*.txt`, `requirements/*.txt`, the files these include with `-r`, and the `.in` file a pip-compile output was made from | Loading the project: `run`, `scan`, `sync`, `status`, the MCP project tools | Read only: the declared dependencies, their pins and `requires-python`. `setup.py` and `setup.cfg` are never read or run. |
| `environment.yml`, else `environment.yaml` (conda), and the files its `pip:` block includes with `-r`, relative to it | Loading the project | Read only: the `pip:` block under `dependencies:`, read as a requirements file (up to 10,000 lines): the packages pip installs from PyPI, their pins and ranges. The conda packages around it (`python=3.12`) are not read, as they do not come from PyPI, nor is the rest of the file. |
| `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`, else the first `pylock.*.toml` | Loading the project | Read only: the locked versions, from the first of these that exists. A `poetry.lock`, `pdm.lock` or `Pipfile.lock` the project is not set up for is ignored, with a warning, when its versions disagree with the pins. |
| `.python-version` | Loading the project | Read only: the project's Python version (else `requires-python`). |
| `.venv`, `venv`, `env` or `.env` with a `pyvenv.cfg`: its `*.dist-info/METADATA` | Loading the project | Read only: the installed versions, for what no lockfile lists. Nothing in the environment is run. |
| The project's `.py` files (up to 5,000, none over 1 MB; not in hidden folders, virtual environments, `build`, `dist`, `node_modules` or `.since-cutoff`) | Loading the project | Parsed, never run: the imports, and in `run`, `scan`, `sync` and the MCP tools, where the code uses a changed API. `status` uses only the imports, to know which dependencies count. |
| `.claude/settings.local.json`, `.claude/settings.json`; the user's `~/.claude/settings.json` | Model detection | Read only: `model`. The user's folder follows `CLAUDE_CONFIG_DIR`. |
| `.codex/config.toml`; the user's `~/.codex/config.toml` | Model detection | Read only: `model`, `model_provider` and the selected `profile`. The user's folder follows `CODEX_HOME`. |
| `.gemini/settings.json`; the user's `~/.gemini/settings.json` | Model detection | Read only: `model`, a string or `model.name`. |
| `opencode.json`, `opencode.jsonc`; the same in the user's `~/.config/opencode/` | Model detection | Read only: `model`. The user's folder follows `XDG_CONFIG_HOME`. |
| `.aider.conf.yml`; the user's `~/.aider.conf.yml` | Model detection | Read only: `model:`. |
| `AGENTS.md` and `CLAUDE.md`, or the `--target` file | `sync`, `status`, `run --apply`, `unapply` | Without `--target`, the block goes into the files that already have one; else into `AGENTS.md`, or `CLAUDE.md` when only that one exists, and into both when both exist and `CLAUDE.md` does not import `AGENTS.md` with `@AGENTS.md` (#13). `sync` writes after you confirm, or with `--yes`; `--check` and `--dry-run` write nothing, and `sync --json` prints to stdout what it proposed and wrote. `run --apply` writes the block; `unapply` removes it, from both files unless `--target` names one. Text outside the block stays byte for byte. When the notes are in rule files (next row) and neither file has a block, a plain `sync` writes neither. `explain` and the MCP tool `explain_api` read the blocks. |
| `.claude/rules/since-cutoff-<package>.md`, `.cursor/rules/since-cutoff-<package>.mdc`, `.github/instructions/since-cutoff-<package>.instructions.md` | `sync --target claude-rules` (`cursor-rules`, `copilot-instructions`), then a plain `sync`; `status`, `explain`, `unapply` | One rule file per changed package: a front matter that names the files it applies to, then the same block as AGENTS.md's, for that package (#16). Only files of these names with since-cutoff's markers and that package's block, inside the project, are written or removed; one whose package has no notes left goes, unless it holds text of your own. Other files in these folders are never read. A folder or file reached through a link outside the project, a hard link and a file that is not UTF-8 are left alone, with a line from `status`, `sync` and `unapply` when they have the markers. |
| git: `.gitignore`, `.git/info/exclude`, the global excludes; the history of the three rule folders | `sync` with rule files; a plain `sync` about to write a first block into AGENTS.md or CLAUDE.md | Read through git, run locally (`git check-ignore`; `git log` and `git show` of the last since-cutoff rule file a commit removed, for its meta line, whose model and cutoff the command it gives names unless `sync` was given `--model` or `--cutoff`): a rule file names no file git ignores, and sync says when the notes were in rule files until the last of them went. git is found on `PATH` (above). |
| `.since-cutoff/report.md`, `.since-cutoff/results.json`, `.since-cutoff/.gitignore` | `run` and `scan` | Written on every run and scan; `--out` names another folder, relative to the project. The `.gitignore` (`*`) keeps the reports out of git, and is written only into a folder since-cutoff created or one named `.since-cutoff`. `sync`, `status` and the MCP tools write no reports. |
| `--markdown FILE` (`scan`), `--tasks-out FILE` and `--tasks-from FILE` (`run`) | `scan`, `run` | Relative to the current folder. `--markdown` and `--tasks-out` write the file (`--markdown -` writes to stdout); `--tasks-from` is read only. |
| Cache `pypi/` | Package lookup | PyPI's metadata: release lists (used for 12 hours; an older copy when PyPI cannot be reached), project URLs and summaries. |
| Cache `sources/` | Source extraction | The `.py` and `.pyi` files of each package version, extracted after the wheel or sdist matches the sha256 PyPI lists, each tree with a `.since-cutoff.json` marker. Capped by `SINCE_CUTOFF_CACHE_MAX_MB`. |
| Cache `sources-meta/` | `cache clear` | Nothing writes it now; `cache clear` removes it with `sources/`. |
| Cache `diffs/` | API diffing | The API diffs between two versions of a package. |
| Cache `receivers/` | Finding the uses of changed APIs | What the annotations of each pinned version say a call returns, to tell which class a method call in the code is on. |
| Cache `tasks/`, `answers/`, `notes/` | `run` | The tasks, answers and notes the models wrote, which cost money to make again. `--fresh` asks again. |
| Cache `envs/` | `run` | An empty virtual environment for the type checker, so that it never sees your site-packages. |
| Cache `models/` | Training-cutoff lookup | The models.dev registry, used for 24 hours. A bundled snapshot serves offline lookups (`status`, `models --offline`). |
| Temporary `since-cutoff-check-*` | `run`'s type checking | The answers to check and the checker's configuration, removed after each check. Model-written code is type-checked, never run. |
| Temporary `since-cutoff-claude-*` | `run` with a `claude-code:` model | The working folder of the `claude` calls and their system prompts, removed when the run ends. |

`since-cutoff cache path` prints the cache root, and `since-cutoff cache info` the size of each
cache folder above and the cap on the sources. `since-cutoff cache clear` removes all of them
(`cache.NAMESPACES`), or with `--sources` (with `sources-meta/`), `--diffs` or `--pypi` only
those (`cache.CLEAR_FLAGS`), which keeps what the models wrote. It removes no reports and no
notes blocks. See [PRIVACY.md](../PRIVACY.md) for what is sent where.

## Exit codes

These are the CLI's `EXAMPLES` and the `sync.EXIT_*` constants. `--help`, on every command, and
`since-cutoff --version` exit with 0, a usage error (an unknown option or value, options that do
not go together) with 2, an interrupted command (Ctrl+C) with 130, and one whose output was
closed early (piped into `head`) with 141, also when it would otherwise succeed. `since-cutoff`
without a command is `since-cutoff run`.

| Command | Codes and meaning |
|---|---|
| `run` | 0: done; 1: an error, including no model answer that could be scored (every model call failed, no API change could be probed, or the task writer failed for half of them) and no type checker (basedpyright: `pip install "since-cutoff[run]"`); 2: usage error; 3: stale API use found, with `--fail-on-stale`. |
| `scan` | 0: done; 1: an error, including no dependency that could be checked; 2: usage error; 3: the `--fail-on` condition: `changes` (a dependency changed its API after the cutoff; `--fail-on-changes` is the same), `used` (your code uses a changed API) or `old-form` (it uses one in the old form). |
| `sync` | 0: the notes are current or were written, or `--dry-run` without `--check`; 1: an error; 2: usage error, including `--json` without `--yes`, `--check` or `--dry-run`; 3: out of date and not written (`--check`, the answer was no, or there was no terminal to ask in); 4: a block edited by hand would change, and nothing was written without `--force` (`--dry-run` without `--check` exits with 0). With `--json`, the JSON's `exit_code` is the exit code; an error (1) prints no JSON. |
| `status` | 0: the notes are current, or there is no block (`since-cutoff sync --check` says whether the code needs one); 1: an error; 2: usage error; 3: a block is out of date. `--hook` exits with 0 unless the options are wrong, also when the project cannot be read. |
| `explain` | 0: the name explains one to 3 changed APIs; 1: an error, a name that matches no changed API, or more than 3 (their names are listed); 2: usage error, including `--from` or `--to` without `--package`. |
| `unapply` | 0: removed, or no block to remove (a rule file it leaves alone gets a line, not an error); 1: an error (the folder does not exist, a file cannot be read or written, its since-cutoff markers cannot be read, or `--target` names a rule file reached through a link outside the project or hard-linked); 2: usage error. |
| `models`, `cache` | 0: done; 1: an error; 2: usage error, including `cache path` or `cache info` with `--sources`, `--diffs` or `--pypi`, which go with `clear`. |
| `mcp` | 0 when the client closes the connection. A tool that fails answers with an error result; the server keeps running. |

[Troubleshooting](troubleshooting.md) has what to do about the messages behind an exit code 1.
