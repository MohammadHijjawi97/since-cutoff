# Changelog

## 0.2.0 - 2026-09-27

### New

- `since-cutoff mcp`: an MCP server (stdio) with three read-only tools, so any agent (Claude
  Code, Codex, Cursor, VS Code, Claude Desktop, Gemini CLI) can ask what changed since its
  training cutoff before writing code: `api_changes` for one PyPI package (optionally one
  symbol), `project_changes` for every dependency of a project, and `model_cutoff`. No model
  calls; package code is only read statically. Adds a dependency on the MCP Python SDK
  (`mcp>=2.0`).
- The MCP tools say what they return, when to use which, that they are read-only, and how long
  a first call can take; each parameter has a description with an example in the input schema,
  and `project_changes` and `api_changes` send progress notifications when the client asks for
  them. `symbol` in `api_changes` takes a call the way code writes it
  (`client.messages.create`, with or without arguments; several separated by commas) and
  suggests the bare name when nothing matches.
- A GitHub Action (`uses: MohammadHijjawi97/since-cutoff@v0`): scans the project for a given
  model, adds a Markdown summary to the job summary, exposes the changed packages and change
  counts as outputs, and fails only with `fail-on-changes: true`. Its cache is saved even when
  that makes the job fail. No model calls.
- `since-cutoff scan --markdown PATH` writes a short GitHub-flavoured Markdown summary (per
  dependency: the version at the cutoff, the pinned version and the top changes, with changes
  to names your code uses first); `--markdown -` prints it to stdout.
- `since-cutoff scan --fail-on-changes` exits with code 3 when a dependency changed its API
  after the cutoff.
- A pre-commit hook, `since-cutoff-scan`, that runs when a lockfile, requirements file or
  pyproject.toml changes.
- Requests to OpenRouter carry its app attribution headers (`HTTP-Referer`,
  `X-OpenRouter-Title`, `X-Title`), which name since-cutoff, not the user.

### Packaging and distribution

- The Claude Code plugin starts the MCP server from a root `.mcp.json` pinned to this release
  (`uvx since-cutoff==0.2.0 mcp`), and its manifest no longer has `displayName`, which Claude
  Code 2.1.101 rejects. The marketplace entry now has a version, author, links, license,
  keywords and a category.
- Agent Plugins manifests at the repository root (`plugin.json`, `mcp.json`), a Codex plugin
  manifest (`.codex-plugin/plugin.json`, with `assets/icon.svg`), a Gemini CLI extension
  (`gemini-extension.json`), and a `Dockerfile` and `glama.json` for MCP directories.
- `server.json` and a release job that publishes each version to the official MCP Registry as
  `io.github.MohammadHijjawi97/since-cutoff`, authenticated with GitHub OIDC.
- The skill asks before `run` (it sends prompts to the chosen provider and uses its credits),
  tells agents other than Claude Code to pass `--model provider:model`, and mentions the MCP
  tools. The README shows how to install it in other agents with `npx skills add`.
- `PRIVACY.md` and `SECURITY.md`, and README sections with example prompts and on privacy and
  support.
- The release workflow runs only for full version tags (`vX.Y.Z`), fails when any version
  field differs from the tag (`scripts/check_versions.py`, which the tests also run), and
  creates the GitHub release. The `v0` tag is moved by hand after a release.
- Social preview images for the repository and the GitHub Pages write-up, with the sample size
  next to the headline number.

### API diff

- Fix: a base class replaced by a module-level alias (`PreTrainedTokenizer = PythonBackend` in
  transformers 5) no longer makes every inherited member look removed, and when a class's bases
  cannot be followed at all its inherited members are no longer reported as removed. A member
  removed from a base class is reported once, on that class, with the subclasses that inherited
  it counted as similar. transformers 4.52.4 -> 5.17.0 goes from 42,270 reported changes to
  6,515.
- Fix: a removed class is reported as moved only when the class of the same name found
  elsewhere keeps at least half of its public members. Otherwise it is reported as removed, and
  the unrelated class is named (`openai.Completion` in 0.x is not `openai.types.Completion`).
- Fix: in namespace distributions (`google.genai`, `azure.identity`, ...) objects were looked up
  below the wrong module, so parameter, signature and deprecation changes were missed and some
  removals were false. google-genai 1.3.0 -> 2.25.0 loses five false `__init__` removals and
  gains a parameter that is now required (`errors.APIError(response_json=...)`).
- Kind changes say what changed ("changed from class to function").
- Test suites, benchmarks and examples shipped inside a package are no longer diffed
  (`pkg.testing` or `pkg.test` directly under the package still is). Type-checker plugins,
  compatibility shims and per-backend modules (`pydantic.mypy`, `sqlalchemy.dialects`,
  `polars.interchange`, ...) rank lower.
- Deprecations declared with a library's own decorator (any decorator whose name contains
  "deprecat", such as polars' `@deprecate_renamed_parameter`) are reported. A renamed parameter
  that is still accepted is reported as deprecated rather than removed. `run` does not probe
  these, because the type checker cannot see them.
- Faster cold scans: package sources are downloaded in parallel, and similar-name suggestions
  are skipped for a package with more than 2,000 removals.
- Warnings that Python prints while parsing old package sources ("invalid escape sequence") are
  silenced. Diffs cached by 0.1.0 are recomputed.

### Ranking and reports

- A changed method or attribute now counts as "used by your code" only when the code also names
  its class (so `create` no longer matches every class with a `create` method), and repeated
  changes are listed under their shortest public path. This affects `project_changes` too.
- A changed parameter counts as used only when the code also names its function:
  `hf_hub_download(..., resume_download=True)` no longer marks
  `snapshot_download(resume_download=...)`. Changes where the code names both the function and
  the parameter come first, then those where it names only the function. The marks say what
  the code uses (`your code uses hf_hub_download and resume_download`) and appear only for
  packages the code imports; a namespace distribution such as google-cloud-storage no longer
  counts as imported because the code imports `google.genai`.
- Every report counts the same way: a change reachable under several import paths is counted
  once. The terminal summary, `report.md`, `results.json` (`breaking_changes`,
  `deprecations`), the Markdown summary, the GitHub Action's outputs and the MCP tools now give
  the same numbers, and the full report lists each change once with an example of its other
  paths (`results.json` still has every path).
- `since-cutoff scan --cutoff DATE` without `--model` labels the report as a custom cutoff
  instead of guessing a Claude model, and looks up no model (`model` and `model_spec` are null
  in `results.json`).
- Fix: the summary panel no longer crops long lines in CI logs and pipes; it wraps them. The
  change counts are on a line of their own, and the panel is wide enough for its subtitle.
- Fix: when several models.dev providers list the same model id, the model maker's own entry
  now wins (for example `openai` for `gpt-5.4`, not a reseller that sorts first).

## 0.1.0 - 2026-09-26

First release.

- `since-cutoff scan`: find the dependency versions a model could have seen at its training
  cutoff and statically diff their public API against the versions your project uses.
- `since-cutoff run`: probe the model on the most relevant breaking changes, score its code with
  basedpyright against both versions (stale / wrong / deprecated / correct), write verified
  AGENTS.md notes and measure their effect on held-out tasks.
- Lockfiles: uv.lock, poetry.lock, pdm.lock, pylock.toml, Pipfile.lock; requirements files;
  `.venv` metadata; pyproject.toml.
- Providers: Claude Code CLI, Anthropic API, OpenAI, OpenRouter, DeepSeek, Ollama and any
  OpenAI-compatible endpoint.
- Claude Code plugin with a `since-cutoff` skill.
- `--effort` (default `low`) for Claude Code calls, and only API-knowledge errors (unknown names,
  imports and parameters, missing arguments, arity) count as stale or wrong; type-strictness
  complaints do not.
- A reproducible example project in `examples/agent-app` and a first real run in the README.
