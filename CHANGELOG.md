# Changelog

## Unreleased

- `run` now exits with code 1 when no API change could be probed (for example because every
  task-writer call hit a rate limit) or when the task writer failed for at least half of the
  changes. Before, such a run exited 0, so `--fail-on-stale` passed a run that measured nothing.
- The held-out result keeps every interval next to the number it belongs to. Before, the 95% CI
  of "changes fixed" was printed next to the task-level before -> after rates, where it read as
  an interval for those rates. Now the summary shows the task-level rates with the number of
  paired tasks and API changes behind them; their difference with a 95% cluster-bootstrap
  interval (resampling API changes with all their tasks, 4000 resamples, fixed seed); "changes
  fixed: X of Y" with its own Wilson 95% CI, the number of changes broken and an exact two-sided
  sign test (changes fixed vs broken); and the held-out pairs that were not counted, by reason
  (untouched, off-task, invalid, error). report.md adds a statistics table (held-out tasks and
  the regression check side by side) and a table per API change; results.json adds
  `difference`, `ci95_difference`, `sign_test_p`, `changes_broken`, `excluded_reasons` and
  `per_change`. The printed difference is the difference of the two rates printed next to it
  (`12%` -> `75%` is `+63`), while results.json keeps the exact value.
- "Changes fixed" now counts what it says: an API change is fixed when more than half of its
  counted held-out tasks are wrong without the notes and correct with them (with one or two
  held-out tasks per change, as in `--quick` and the default, that means all of them); "broken"
  is the mirror case. Before, a held-out answer that was already correct without the notes also
  counted towards a fix.
- The regression check (previously-correct APIs) reports the API changes still correct with the
  notes (more than half of their pairs correct) with a Wilson 95% CI, and the changes broken;
  report.md leaves out "changes fixed" and the sign test for it, which say nothing about changes
  the model already got right. In results.json, `heldout.regression.changes_fixed` and
  `ci95_changes_fixed` now count wrong -> right, as for the held-out tasks (so they are about 0);
  what they counted before, the changes still correct, is now `changes_still_correct` and
  `ci95_changes_still_correct`.
- `run --tasks-out FILE` saves the tasks a run used (per API change, the probe task first), and
  `run --tasks-from FILE` runs on those tasks instead of calling the task writer; API changes
  the file does not cover are skipped with that reason. Together they repeat a measurement on
  exactly the same tasks, for another model or another set of notes. A tasks file may be UTF-8
  with or without a byte order mark. Within a change, a task that repeats an earlier one, the
  probe included (ignoring case, punctuation and spacing), is dropped, as the task writer's
  repeats are. When failing changes have fewer held-out tasks than `--heldout` asks for (a file
  written with a smaller `--heldout`, or a task writer that repeated itself), the run says so
  and records the numbers it used (`heldout_used` in the settings; report.md shows "2 asked, 1
  used"). The tasks file keeps who wrote the tasks: written again from reused tasks, it names
  the original task writer, prompt version and tool, and a run on reused tasks records them
  (`tasks_written_by`; report.md shows "tasks written by", and the run's own model as the note
  writer). `--tasks-out` is checked before any model call (not a folder, not the `--tasks-from`
  file, which it would shrink to the changes this run probed) and written last, so a failed
  write keeps the result card, the JSON output and `--apply`.
- results.json (`settings`) and report.md ("Run settings") record what a run's numbers depend
  on besides the answers: the since-cutoff version, the model under test, the task and note
  model, the Claude Code effort, the prompt version, the API diff schema, the probe, held-out
  and regression budgets, the Python version, the tasks file (if any) and the date.
- `run --compare template,signatures` measures simpler notes next to the verified ones, so a run
  shows how much the verified notes add instead of assuming they are best. `template` states
  each failing change in one sentence from the API diff; `signatures` gives the new signature
  and first docstring paragraph of each changed API, or of the replacement its library names.
  Neither calls a model. Every held-out task and regression check is answered once without
  notes and once per block, so all blocks are paired against the same answers without notes,
  and they are compared on the same pairs: a pair counts for every block only when its answer
  without notes is scorable and no block's answer with notes is an error (so a rate-limit error
  on the one task a baseline did not fix cannot hand it a "fix"). The verified notes' own result
  keeps every pair it can count, as without `--compare`; when errors leave out some of those,
  the result card adds the verified notes' rates on the shared pairs. The result card adds a
  line per baseline; report.md adds a table with each block's size in tokens, held-out correct
  without -> with, the pairs it did not count and why, changes fixed with its 95% CI, changes
  broken, the regression check, and the changes only the verified notes or only the baseline
  fixed with an exact sign test; results.json adds `arms` and, on each held-out attempt, the
  `arm` it saw. Without `--compare` a run does and reports exactly what it did before.
- Without `--model`, `run` and `scan` test the model your coding agent is set up with.
  `SINCE_CUTOFF_MODEL` (a full spec such as `openai:gpt-5.4`) always wins. Inside Claude Code
  (which sets `CLAUDECODE=1` for the commands it runs) only Claude Code's settings count:
  `ANTHROPIC_MODEL`, then `model` in the project's `.claude/settings.local.json` and
  `.claude/settings.json`, then in `~/.claude/settings.json`, else Claude Code's default; other
  agents' files are not read. Elsewhere the most specific setting wins: first the agents'
  environment variables (`ANTHROPIC_MODEL`, `AIDER_MODEL`), then every project setting, the
  nearest folder first, then the user settings. Project settings are looked for from the
  scanned folder up to the repository root (the first folder with `.git`), never in the home
  folder or above: Claude Code's `.claude/settings.local.json` and `.claude/settings.json`,
  Codex's `.codex/config.toml` (with the selected profile, as `openai:<model>`), OpenCode's
  `opencode.json` or `opencode.jsonc`, Aider's `.aider.conf.yml`. User settings, in the same
  order: `~/.claude/settings.json`, `$CODEX_HOME/config.toml` or `~/.codex/config.toml`,
  `~/.config/opencode/`, `~/.aider.conf.yml`. Aider's short aliases (`4o`, `flash`, `gemini`,
  `deepseek`, `r1`, `grok3`, ...) are read as the models they stand for; a model name
  since-cutoff cannot place stops the run with a message naming the setting, instead of a
  malformed spec. The model line, report.md and results.json say where the model came from
  ("model from .claude/settings.json"), and an error about a detected model names the setting.
  Only the model fields are read. Before, the default was Claude Code's user-level model, and
  `scan` guessed Sonnet whenever `~/.claude/settings.json` named none, even when the project,
  `ANTHROPIC_MODEL` or another agent named a model. When no setting names a model, the default
  is still Claude Code's, and the tool now says so and how to choose another.
  `--model claude-code` behaves as before.
- A `--model` whose provider since-cutoff does not know, such as `lmstudio:qwen3-coder`, gets a
  hint to use `openai-compatible:<model>` with `--base-url` for a local or custom server.
- `scan` looks up the training cutoff of models from any maker or reseller the model registry
  knows, such as `--model google:gemini-2.5-pro`; `run` says which providers it can call
  instead. Amazon Bedrock (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) and Vertex AI
  (`claude-sonnet-4-5@20250929`) model ids are recognised, `claude-code:opusplan` counts as
  Sonnet (the model that writes the code), and `anthropic:sonnet` (Aider's alias) means the
  newest Sonnet, reported as an assumption. The MCP tools take `provider:model` ids for any
  provider the registry knows, such as `github-copilot:gpt-5.4`.
- `run` checks that it can call the model (its API key, or the `claude` CLI) before it scans
  the dependencies, instead of failing minutes later on the first call.

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
