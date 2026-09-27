# Privacy

since-cutoff is a command-line tool and MCP server that runs on your machine. It has no
account, no server of its own and no telemetry, and it collects no personal data.

## What it reads on your machine

- Your project's dependency files (lockfiles, `requirements*.txt`, `pyproject.toml`, `.venv`
  metadata) and its Python files, to see which dependencies you pin and which of their names
  your code uses. This stays on your machine.
- The notes block in `AGENTS.md` or `CLAUDE.md`, for `sync`, `status` and `unapply`.
- Without `--model`, the model fields of your coding agent's settings (Claude Code, Codex,
  OpenCode, Aider) and the `SINCE_CUTOFF_MODEL`, `ANTHROPIC_MODEL` and `AIDER_MODEL` variables,
  to know which model to use. Only the model fields are read.

## What it sends, and where

- **PyPI** (`pypi.org`, `files.pythonhosted.org`): requests for public package metadata and
  wheels, by package name and version.
- **models.dev**: a request for the public list of models and their training cutoffs (a
  snapshot is bundled for offline use).
- **The model provider you choose, in `since-cutoff run` only**: prompts with package names,
  versions, public API signatures and docstrings of the changed APIs, generated coding tasks
  and, for notes, the model's own answers. Never your source code. With `--model claude-code`
  the prompts go through your local `claude` CLI. Requests to OpenRouter carry headers that
  name since-cutoff (`HTTP-Referer`, `X-OpenRouter-Title`), not you or your project.
- `scan`, `sync`, `status`, the MCP server, the GitHub Action and the pre-commit hooks call no
  model and send no prompts. `status` sends nothing at all.

Locations and code snippets shown by `scan` stay on your machine; `run` prompts never contain
your project's code. (For now `scan` shows file names, not lines or code.) They leave it only
where you send them: `scan --markdown` and `--annotate github` write them for a CI job's summary
and annotations, and the MCP tool `project_changes` returns them to the coding agent that called
it, which passes tool results on to its model like any others.

Requests carry a `since-cutoff/<version>` user agent. Each service's own privacy policy applies
to what it receives.

## What it stores

- `.since-cutoff/` in your project: reports and `results.json` (it adds a `.gitignore` that
  ignores itself).
- A local cache of PyPI data, package sources, API diffs and model answers. `since-cutoff cache
  path` shows where it is; `since-cutoff cache clear` removes it.
- With `sync` (after it shows you the diff and you agree, or with `--yes`) or `run --apply`
  only: one marked block in `AGENTS.md` or `CLAUDE.md`. Text outside it is left as it is, and
  `since-cutoff unapply` removes the block.

## Contact

Questions and bug reports: https://github.com/MohammadHijjawi97/since-cutoff/issues. For
security issues, see [SECURITY.md](SECURITY.md).
