# since-cutoff

<!-- mcp-name: io.github.MohammadHijjawi97/since-cutoff -->

**For Python projects written with a coding agent: since-cutoff finds the dependency APIs that
changed after the model's training cutoff, measures which of them the model gets wrong, and
fixes those with short AGENTS.md notes, each checked by a type checker or taken directly from the
API diff.**

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero.svg" width="640" alt="Your coding model learned your libraries before they changed. Claude Opus 4.6 on one sample project, measured with since-cutoff 0.1.0: on 7 of 16 probed API changes it used a name or parameter that has since been removed; with the notes, 5% to 65% of 20 held-out tasks were correct. Try it: uvx since-cutoff scan (no model calls, no API key).">
</picture></p>

[![PyPI](https://img.shields.io/pypi/v/since-cutoff)](https://pypi.org/project/since-cutoff/)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/LICENSE)
![Status: beta](https://img.shields.io/badge/status-beta-orange)

**English** | [简体中文](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md) | [Español](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md) | [Français](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md)

## The problem

Every model has a training cutoff; your lockfile keeps moving. When a library changes its public
API after the cutoff, a model that learned the old version keeps writing the old calls. Some of
that code fails at import or call time. Some still runs, because the old path is only deprecated.

A few of the changes `since-cutoff scan` finds for Claude Sonnet 4.5 (training cutoff July
2025) in the [sample project](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
which pins six of its nine dependencies to current releases (for the other three, which are
unpinned, the tool uses the latest release):

| library | release at the cutoff | pinned | what breaks |
|---|---|---|---|
| anthropic | 0.60.0 | 1.8.0 | `messages.create(temperature=..., top_p=..., top_k=...)` is no longer accepted |
| huggingface-hub | 0.34.3 | 2.0.0 | `hf_hub_download(resume_download=..., force_filename=..., local_dir_use_symlinks=...)` removed |
| langchain-core | 0.3.72 | 1.6.5 | `retriever.get_relevant_documents()` and `llm.predict()` removed |
| openai | 1.98.0 | 3.19.2 | 21 breaking changes, 6 new deprecations |

In that project, 7 of 9 dependencies changed their public API after the cutoff. The static diff
flags 317 breaking changes and 23 new deprecations; some are internals, which the probes skip.

It is not one model or one vendor. Across 36 widely used Python AI libraries and 21 models from
OpenAI, Anthropic, Google, xAI, DeepSeek, Qwen, Moonshot and Mistral, even the newest model
tested (Claude Opus 5.5, June 2026 cutoff) predates a public API break in 20 of the 36
([full results](https://mohammadhijjawi97.github.io/since-cutoff/ai-stack.html)):

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/ai-stack-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/ai-stack.svg" width="860" alt="Bar chart: for each of 21 models from 8 vendors, how many of 36 Python AI libraries broke their public API and how many are on a new major version since the model's training cutoff. From 21 of 36 for GPT-4o (13 of the libraries did not exist yet) to 33 of 36 for models with early-2025 cutoffs, and 20 of 36 for Claude Opus 5.5 (June 2026).">
</picture></p>

since-cutoff does three things about it:

1. **`scan`** finds, for each dependency, the newest release on or before the model's cutoff and
   diffs its public API against the version you pin. No model calls, no API key.
2. **`run`** asks the model short coding tasks that need the changed APIs, with no tools and no
   docs, and scores each answer with a type checker against *both* versions: stale, wrong,
   deprecated or correct. No LLM judges anything.
3. **Notes**: for each failure it writes a one-line AGENTS.md / CLAUDE.md note. It keeps a
   model-written note only if its example type-checks against your version; otherwise it uses a
   plain statement of the change from the API diff. Then it re-tests the model on held-out tasks
   with and without the notes.

The same diff is available to agents through an [MCP server](https://github.com/MohammadHijjawi97/since-cutoff#use-it-from-any-agent-mcp)
and to CI through a [GitHub Action and a pre-commit hook](https://github.com/MohammadHijjawi97/since-cutoff#use-in-ci).

## Quick start

```bash
# list API changes since your model's cutoff (no model calls, no API key)
uvx since-cutoff scan

# probe the model, write verified notes, and add them to AGENTS.md
uvx since-cutoff run --apply
```

Or install it with `pipx install since-cutoff` (or `pip install since-cutoff`) and run
`since-cutoff`. Run it from your project root: it reads `uv.lock`, `poetry.lock`, `pdm.lock`,
`pylock.toml`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml` or a `.venv`. Without
`--model` it tests the model your Claude Code uses; for any other model, pass `--model` (see
[Choosing the model](https://github.com/MohammadHijjawi97/since-cutoff#choosing-the-model)).
`scan` is free; `run` sends prompts to the model provider and uses your API credits or Claude
Code usage.

What `scan` prints for the sample project:

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/scan.svg" width="100%" alt="since-cutoff scan --model anthropic:claude-sonnet-4-5 on the sample project: 7 of 9 dependencies changed their API after the cutoff; static diff: 317 breaking changes, 23 new deprecations; a table of each package's pinned version, version at the cutoff and number of changes, and one example change per package"></p>

### In Claude Code

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

Then ask Claude to "check which of our dependencies you are out of date on", or run
`/since-cutoff:since-cutoff`. The skill runs the CLI; the measuring itself is done by a fresh,
tool-less copy of the model, so the agent cannot grade itself. The plugin also starts the
[MCP server](https://github.com/MohammadHijjawi97/since-cutoff#use-it-from-any-agent-mcp), so
Claude can look up a library's changes before it writes code.

### In other coding agents

```bash
npx skills add MohammadHijjawi97/since-cutoff
```

This installs the same skill through the open [skills](https://github.com/vercel-labs/skills)
CLI for Codex, Cursor, Gemini CLI, GitHub Copilot, OpenCode and other agents that read
`SKILL.md`. Outside Claude Code, tell the tool which model to test, for example
`since-cutoff scan --model openai:gpt-5.4`, and add the MCP server as shown below.

Prompts that work well:

- "Which of our dependencies changed their public API after your training cutoff?" The agent
  runs `since-cutoff scan` or calls the MCP tool `project_changes`.
- "Measure which of those changes you actually get wrong, and add the verified notes to
  AGENTS.md." The agent asks you first, then runs `since-cutoff run --quick --apply`.
- "Before you write the httpx code, check what changed in httpx since your cutoff." The agent
  calls the MCP tool `api_changes`.

### Choosing the model

| `--model` | uses | needs |
|---|---|---|
| `claude-code` (default) | your Claude Code login (subscription or key), current model | the `claude` CLI |
| `claude-code:sonnet`, `claude-code:claude-haiku-4-5` | a specific Claude model | the `claude` CLI |
| `anthropic:<model>` | Anthropic API | `ANTHROPIC_API_KEY` |
| `openai:<model>` | OpenAI API | `OPENAI_API_KEY` |
| `openrouter:<vendor/model>` | OpenRouter | `OPENROUTER_API_KEY` |
| `deepseek:<model>` | DeepSeek API | `DEEPSEEK_API_KEY` |
| `ollama:<model>` | local Ollama | Ollama running |
| `openai-compatible:<model>` | any OpenAI-compatible server | `--base-url`, optional `OPENAI_API_KEY` |

Training cutoffs come from [models.dev](https://models.dev) (a snapshot is bundled for offline
use). `since-cutoff models sonnet` lists them; `--cutoff 2025-07` overrides the date, and
`since-cutoff scan --cutoff 2025-07` without `--model` scans against that date alone.

## Results

Two Claude models on the 9-dependency sample project in
[`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
**measured with since-cutoff 0.1.0**, with Claude Opus 4.6 writing the tasks and notes:

| | Claude Haiku 4.5 | Claude Opus 4.6 |
|---|---|---|
| training cutoff | Feb 2025 | May 2025 |
| API changes probed | 20 | 16 |
| **stale** / wrong / deprecated / correct | **5** / 1 / 2 / 12 | **7** / 0 / 3 / 6 |
| libraries with stale use | 3 of 5 probed | 2 of 4 probed |
| notes written (type-checker verified) | 8 (7), about 391 tokens | 10 (7), about 437 tokens |
| **held-out correct, without -> with notes** | **14% -> 57%** (14 pairs) | **5% -> 65%** (20 pairs) |
| previously-correct APIs after notes | 6/6 still correct | 6/6 still correct |

*Held-out* tasks are paraphrases of the task each failing change was probed with; each one is
answered twice, without and with the notes, and scored the same way. The last row re-checks APIs
the model already got right, to catch notes that make things worse.

In this sample the stronger model was not safer: Opus 4.6 wrote APIs that were removed after its
cutoff, including `anthropic.HUMAN_PROMPT` with `client.completions`. Stale code from both runs,
each valid for the version the model learned and broken for the pinned one:
`messages.create(temperature=...)` (anthropic 1.8), `hf_hub_download(resume_download=...)`,
`local_dir_use_symlinks=...`, `force_filename=...` and `proxies=...` (huggingface-hub 2.0), and
`client.beta.vector_stores` (openai 3.x).

The notes written in the Claude Haiku 4.5 run (excerpt, verbatim):

```markdown
<!-- since-cutoff:start -->
## Library changes after the model's training cutoff

**anthropic 1.8.0**
- `temperature=...` was removed from `messages.create()` in anthropic 1.8.0. Omit the `temperature` parameter entirely; there is no replacement.

**huggingface-hub 2.0.0**
- `hf_hub_download(..., resume_download=True)`: The `resume_download` parameter was removed in huggingface-hub 2.0.0. Omit it; downloads resume automatically.

**openai 3.19.2**
- `client.beta.vector_stores` is removed in openai 3.19.2. Use `client.vector_stores` instead.
<!-- since-cutoff:end -->
```

The terminal summary of the Claude Opus 4.6 run, recorded with 0.1.0. The probe results are the
ones in the table above. The diff counts on the card are 0.1.0's ("725 changes flagged"); after
fixes to the diff, `scan` in 0.2.0 reports 513 breaking changes and 48 new deprecations for the
same cutoff. The card's "changes fixed" count and its 95% CI also follow 0.1.0: the interval
belongs to that count, not to the 5% -> 65% rates, and versions up to 0.2.0 counted a change as
fixed even when a held-out answer was already correct without the notes.

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run-opus.svg" width="100%" alt="since-cutoff 0.1.0 run on Claude Opus 4.6: stale API use in 2 of 4 probed dependencies; 16 API changes probed: 7 stale, 0 wrong, 3 deprecated, 6 correct; 10 notes; held-out tasks correct without -> with notes: 5% -> 65% (20 paired tasks); a list of the stale calls"></p>

<details>
<summary>The same summary for the Claude Haiku 4.5 run (also 0.1.0; scan in 0.2.0 reports 491 breaking changes and 50 new deprecations for its cutoff)</summary>
<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run.svg" width="100%" alt="since-cutoff 0.1.0 run on Claude Haiku 4.5: stale API use in 3 of 5 probed dependencies; 20 API changes probed: 5 stale, 1 wrong, 2 deprecated, 12 correct; 8 notes; held-out tasks correct without -> with notes: 14% -> 57% (14 paired tasks)"></p>
</details>

Small samples, two models, one project: treat this as a demonstration of the method, not a
benchmark. Every run writes its full report (each task, answer and type-checker error) to
`.since-cutoff/report.md`. To repeat the experiment with the current version (its diff and
ranking changed, so the probes will not be identical):
`cd examples/agent-app && since-cutoff run --model claude-code:claude-haiku-4-5 --task-model claude-code:claude-opus-4-6`.
Versions after 0.2.0 can also save the tasks a run used: add `--tasks-out tasks.json`, and anyone
can repeat the run on exactly the same tasks with `--tasks-from tasks.json`, for another model or
another set of notes. Results from your own projects are very welcome in
[Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6).

How the measurement works and what these numbers do and do not show, in more detail:
[the write-up](https://mohammadhijjawi97.github.io/since-cutoff/).

## Use it from any agent (MCP)

`since-cutoff mcp` is an MCP server that lets a coding agent ask "what changed in this library
since my training cutoff?" before it writes code. It has three read-only tools:

| tool | answers |
|---|---|
| `api_changes(package, model, symbol=...)` | what changed in one library between the release at the model's cutoff and the latest (or a given) version, hard breaks first |
| `project_changes(project_dir, model)` | the same for every dependency of a project at its pinned version, starting with APIs your code already uses |
| `model_cutoff(model)` | a model's training cutoff, from [models.dev](https://models.dev) |

The agent passes its own model id, so the answer covers what that model could not have seen.
The tools read PyPI and package sources statically: no model calls, no API key, no package code
executed.

**Claude Code**

```bash
claude mcp add --scope user since-cutoff -- uvx since-cutoff@latest mcp
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.since-cutoff]
command = "uvx"
args = ["since-cutoff@latest", "mcp"]
startup_timeout_sec = 60
tool_timeout_sec = 900
```

**Cursor** (`~/.cursor/mcp.json`) and **Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "since-cutoff": { "command": "uvx", "args": ["since-cutoff@latest", "mcp"] }
  }
}
```

**VS Code** (`.vscode/mcp.json`)

```json
{
  "servers": {
    "since-cutoff": { "type": "stdio", "command": "uvx", "args": ["since-cutoff@latest", "mcp"] }
  }
}
```

**Gemini CLI**

```bash
gemini mcp add --scope user since-cutoff uvx since-cutoff@latest mcp
# or as an extension, which starts the same server:
gemini extensions install https://github.com/MohammadHijjawi97/since-cutoff
```

`@latest` makes `uvx` pick up new releases instead of reusing the first version it cached (the
plugin's own `.mcp.json` pins the exact release). If the client cannot find `uvx`, install
[uv](https://docs.astral.sh/uv/) or give the full path (`which uvx`). The server is listed in the
[MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.MohammadHijjawi97/since-cutoff`.

The first `project_changes` call on a larger project downloads the wheels of every dependency
that changed and can take several minutes (very large packages such as transformers take the
longest). Results are cached, so later calls take seconds. To warm the cache, run
`since-cutoff scan` in the project once; it shares the cache with the server. Clients with a
short default tool timeout may need a longer one, as in the Codex example above.

What `api_changes("huggingface-hub", model="claude-haiku-4-5")` returns (real output, trimmed):

```markdown
# huggingface-hub 0.29.1 -> 2.0.0

- From 0.29.1 (2025-02-20): the newest release on or before 2025-02-28 (training cutoff of claude-haiku-4-5, from models.dev)
- To 2.0.0 (2026-09-24): the latest release on PyPI
- 116 breaking changes, 0 new deprecations (removed or moved 62, parameters removed 43, parameters now required 9, changed kind 1, now keyword-only or positional-only 1)

## Removed or moved

- `huggingface_hub.InferenceApi` was removed; similar names now: `inference`, `InferenceEndpoint`, `InferenceClient`
...

## Parameters removed

- `huggingface_hub.snapshot_download(resume_download=...)`: parameter `resume_download` was removed; similar parameters now: `force_download`
- `huggingface_hub.file_download.hf_hub_download(force_filename=...)`: parameter `force_filename` was removed; similar parameters now: `filename`
...

Not listed: 76 breaking changes, 0 new deprecations (removed or moved 47, parameters removed 29). Narrow with symbol="..." or raise limit.
```

With `symbol="hf_hub_download"` it lists only the 8 changes to that function (`resume_download=`,
`force_filename=`, `local_dir_use_symlinks=` and `proxies=`, on the function and on `HfApi`).
`symbol` also takes a call the way code writes it: `client.messages.create` finds the changes
to `Messages.create`.

## Use in CI

### GitHub Action

Scans the project on each pull request and adds a summary to the job page: per dependency, the
version at the model's cutoff, the version you pin and the top changes, with changes to names
your code uses first. Like `scan`, it only reads PyPI and models.dev: no model calls, no API key.

```yaml
# .github/workflows/since-cutoff.yml
name: since-cutoff
on:
  pull_request:
    paths: ["**/*.lock", "**/pylock*.toml", "**/requirements*.txt", "**/pyproject.toml"]
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: MohammadHijjawi97/since-cutoff@v0
        with:
          model: anthropic:claude-sonnet-4-5  # the model your team codes with
```

| input | default | |
|---|---|---|
| `model` | required | `provider:model` as for `--model`; only its training cutoff is used |
| `working-directory` | `.` | the project directory |
| `only`, `exclude` | | comma-separated PyPI names |
| `cutoff` | | override the training cutoff (`YYYY-MM` or `YYYY-MM-DD`) |
| `fail-on-changes` | `false` | fail the step when a dependency changed its API after the cutoff |
| `step-summary` | `true` | add the Markdown summary to the job summary |
| `cache` | `true` | keep PyPI metadata, package sources and API diffs between runs (also when `fail-on-changes` fails the job) |
| `args` | | more `since-cutoff scan` arguments, e.g. `--all-deps --limit 20` |
| `since-cutoff-version` | `0.2.0` | the since-cutoff release to run, or `latest` |

Outputs: `changed-packages` (comma-separated), `changes` (breaking changes), `deprecations`,
`markdown` (the summary's path, for example to post it as a pull request comment) and `report`
(the full report's path). A change reachable under several import paths is counted once.

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/MohammadHijjawi97/since-cutoff
    rev: v0.2.0
    hooks:
      - id: since-cutoff-scan
        args: [--model=anthropic:claude-sonnet-4-5]  # add --fail-on-changes to block the commit
```

The hook runs when a lockfile, a requirements file or `pyproject.toml` changes, and prints the
scan. Give it `--model` in `args`. It needs PyPI, so skip it on pre-commit.ci
(`ci: {skip: [since-cutoff-scan]}`).

### Other CI

```bash
# Markdown summary for any CI; exit code 3 if a dependency changed its API after the cutoff
since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown summary.md --fail-on-changes

# measure the model as well (needs its API key, or the claude CLI)
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

`--markdown -` prints the summary to stdout (the usual output then goes to stderr). Exit codes:
`0` ok, `1` error (for example, no model answer could be scored), `2` usage error, `3` stale API
use found with `run --fail-on-stale`, or API changes found with `scan --fail-on-changes`.

## How it works

Three stages. The first needs no model; in the other two, a type checker scores every answer
and checks every note the model writes:

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works.svg" width="640" alt="Three stages. Scan, with no model calls: the lockfile gives your exact versions, then the release at the model's cutoff, then a static API diff with griffe. Probe: short tasks that need the change, the model answers from memory, basedpyright checks the answer against both versions. Fix and verify: a model-written note is kept only if its example type-checks, otherwise the change is stated from the API diff; held-out tasks are answered without and with the notes, and --apply writes a block into AGENTS.md.">
</picture></p>

| outcome | meaning |
|---|---|
| **stale** | the code is valid for the version the model knew and invalid for yours, and the error involves an API that changed |
| **wrong** | invalid for your version, but not explained by a change (hallucinated or misused API) |
| **deprecated** | valid, but uses an API marked `@deprecated` in your version |
| **correct** | valid for your version and actually uses the changed API |
| untouched / off-task / invalid / error | not counted in any rate, and always reported |

Everything is scored by a type checker against the exact package versions, each in an isolated
environment with that package's own runtime dependencies. No LLM judges anything, and every
number traces back to `results.json`. Details: [docs/how-it-works.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md).

## What it runs, sends and stores

- **Runs no package code and no model-written code.** Packages are read statically (griffe with
  inspection off; only `.py`/`.pyi` files are extracted, with path and size checks). The model's
  answers are only type-checked, locally, with basedpyright.
- **Fetches** public package metadata and wheels from PyPI, and model cutoffs from models.dev (a
  snapshot is bundled for offline use). Git, path, workspace and private-index dependencies are
  never looked up on public PyPI by name.
- **Sends** prompts only in `run`, and only to the model provider you choose: package names,
  versions, public signatures and docstrings of the changed APIs, the generated tasks and, for
  notes, the model's own answers. Never your source code. `scan`, the MCP server, the GitHub
  Action and the pre-commit hook send nothing to any model.
- **Stores** results in `.since-cutoff/` in your project (it ignores itself in git) and a local
  cache (`since-cutoff cache path` shows it, `since-cutoff cache clear` removes it). With
  `--apply` it writes one marked block into `AGENTS.md`/`CLAUDE.md` and leaves the rest of the
  file byte-for-byte unchanged; `since-cutoff unapply` removes the block.
- **No telemetry**, no account, no personal data. Re-runs come from the cache, so they are free
  and reproducible (`--fresh` asks the model again). See [PRIVACY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/PRIVACY.md).

## Limitations

- Python only for now. TypeScript (`.d.ts` diffs, `tsc`) is next
  ([#1](https://github.com/MohammadHijjawi97/since-cutoff/issues/1)).
- A type checker sees wrong names, wrong parameters and PEP 702 deprecations. It cannot see
  behaviour changes behind an unchanged signature, or deprecations that only warn at run time.
  `scan` also lists deprecations declared with a library's own decorator (name containing
  "deprecat"), but `run` does not probe them.
- The diff covers the public API: `_private` names, and test suites, benchmarks and examples
  shipped inside a package, are skipped.
- Probes cover a ranked **sample** of the breaking changes (symbols your code already uses
  first), not all of them.
- "The version the model saw" is the newest release on or before the cutoff date. Models know
  recent releases less well, so real staleness can start earlier.
- Held-out tasks are paraphrases of the same change: they show that a note fixes *that* change,
  not that the model got better in general.

## How it compares

| kind of tool | what it does | how since-cutoff relates |
|---|---|---|
| Docs retrieval MCP servers: [Context7](https://github.com/upstash/context7), [Ref](https://github.com/ref-tools/ref-tools-mcp), [docs-mcp-server](https://github.com/arabold/docs-mcp-server) | give the agent current documentation when it looks a library up, at answer time | complementary: since-cutoff finds which changes this model gets wrong, so you know where a lookup or a note is needed, and keeps a small verified note in the repo |
| Library-shipped skills: [library-skills](https://github.com/tiangolo/library-skills), [pydantic/skills](https://github.com/pydantic/skills) | the library's maintainers ship agent guidance with the package, in step with each release | works for any PyPI package, including those that ship no guidance, and measures whether the model needs it |
| Dependency bots: [Renovate](https://github.com/renovatebot/renovate), [Dependabot](https://github.com/dependabot/dependabot-core) | open pull requests that update your pinned versions | the GitHub Action can run on those pull requests and list the API changes the model has not seen |
| Benchmarks: [GitChameleon 2.0](https://arxiv.org/abs/2507.12367), [VersiCode](https://arxiv.org/abs/2406.07411), [CodeUpdateArena](https://arxiv.org/abs/2407.06249), [LibEvolutionEval](https://arxiv.org/abs/2412.04478) | measure how models handle library versions on fixed, historical task sets | measures this model on your pinned versions, and verifies the fix with a type checker |

Two smaller tools work on the same problem: [cutoff](https://github.com/sandeepsirodia/cutoff)
probes a library you maintain by running model-written programs against its current version, and
[postcut](https://github.com/justi/postcut) turns a Ruby `Gemfile.lock` into a brief of changes
since the cutoff. since-cutoff is built on [griffe](https://mkdocstrings.github.io/griffe/),
[basedpyright](https://github.com/DetachHead/basedpyright), [models.dev](https://models.dev) and [rich](https://github.com/Textualize/rich).

## Contributing

since-cutoff is young. The most useful help right now:

- **Run it on your project** and post what it found, including false positives, in
  [Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6).
- **Pick up a [good first issue](https://github.com/MohammadHijjawi97/since-cutoff/labels/good%20first%20issue)**:
  small, self-contained tasks such as another lockfile format or a provider preset.
- **Bigger pieces** are labelled [help wanted](https://github.com/MohammadHijjawi97/since-cutoff/labels/help%20wanted),
  for example [TypeScript support](https://github.com/MohammadHijjawi97/since-cutoff/issues/1).
- **Report a bug or an odd result** in the [issues](https://github.com/MohammadHijjawi97/since-cutoff/issues).

[CONTRIBUTING.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CONTRIBUTING.md)
explains the code layout and the checks; the offline test suite runs the whole pipeline with a
toy library and a scripted model, so no API key is needed. Security issues: [SECURITY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/SECURITY.md).

## Citation

If you use since-cutoff in research, please cite it (see [`CITATION.cff`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CITATION.cff)).

## License

MIT © [Mohammad Hijjawi](https://github.com/MohammadHijjawi97)
