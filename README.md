# since-cutoff

English | [简体中文](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md)

**Your coding agent learned your libraries before they changed.**
since-cutoff finds exactly which APIs of *your* dependency versions it gets wrong, and fixes
them with a small AGENTS.md note that is checked by a type checker, not by another LLM.

[![CI](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/since-cutoff)](https://pypi.org/project/since-cutoff/)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Status: beta](https://img.shields.io/badge/status-beta-orange)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/LICENSE)

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run.svg" alt="since-cutoff run: Claude Haiku 4.5 on a real project" width="860"></p>

## The problem

Every model has a training cutoff. Your lockfile does not. A few real examples for a model
with a July 2025 cutoff (Claude Sonnet 4.5) and current releases, found by `since-cutoff scan`:

| library | version the model saw | your version | what breaks |
|---|---|---|---|
| anthropic | 0.60.0 | 1.8.0 | `messages.create(temperature=..., top_p=..., top_k=...)` no longer accepted |
| huggingface-hub | 0.34.3 | 2.0.0 | `hf_hub_download(resume_download=..., force_filename=..., local_dir_use_symlinks=...)` removed |
| langchain-core | 0.3.72 | 1.6.5 | `retriever.get_relevant_documents()`, `llm.predict()` removed |
| openai | 1.98.0 | 3.19.2 | 26 breaking changes, 6 new deprecations |

For that sample project, 7 of 9 dependencies had changed their public API after the cutoff
(the static diff flags 483 changes; many are internals, which the task writer skips). An agent that learned the old API writes code that fails at
import or call time, or, worse, still runs because the old path is only deprecated.

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/scan.svg" alt="since-cutoff scan output" width="820"></p>

Documentation tools paste whole docs into the context and hope. since-cutoff **measures**
which of those changes your model actually gets wrong, writes **only** the notes that are
needed, and **proves** on held-out tasks that the notes fix them.

## Features

- **`scan`**: for every dependency, the version your model saw at its training cutoff vs. the one
  you pin, and a static diff of what broke in between (no model calls, no API key).
- **`run`**: probes the model with short tasks that need the changed APIs and scores its code with
  a type checker against *both* versions: stale, wrong, deprecated or correct.
- **Verified fixes**: one-line AGENTS.md / CLAUDE.md notes, kept only if their example
  type-checks against your exact version, and re-tested on held-out tasks.
- **Works where you are**: Claude Code plugin and skill, or any of Anthropic, OpenAI, OpenRouter,
  DeepSeek, Ollama and OpenAI-compatible servers.
- **Every lockfile**: uv, Poetry, PDM, pylock, Pipenv, requirements files, or a `.venv`.
- **Safe and reproducible**: never runs package or model-written code; everything is cached;
  full JSON and Markdown reports.

## Quick start

```bash
# list API changes since your model's cutoff (fast, no model calls)
uvx since-cutoff scan

# probe the model, write verified notes, and apply them to AGENTS.md
uvx since-cutoff run --apply
```

Or install it with `pipx install since-cutoff` (or `pip install since-cutoff`) and run
`since-cutoff`. Run it from your project root (anything with `uv.lock`, `poetry.lock`,
`pdm.lock`, `pylock.toml`, `Pipfile.lock`, `requirements*.txt`, `environment.yml`,
`pyproject.toml` or a `.venv`).

### In Claude Code

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

Then ask Claude to "check which of our dependencies you are out of date on", or run
`/since-cutoff:since-cutoff`. The skill runs the CLI; the measuring itself is done by a fresh,
tool-less copy of the model, so the agent cannot grade itself.

## A real run

Two Claude models on the 9-dependency sample project in
[`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
with Claude Opus 4.6 writing the tasks and notes:

| | Claude Haiku 4.5 | Claude Opus 4.6 |
|---|---|---|
| training cutoff | Feb 2025 | May 2025 |
| API changes probed | 20 | 16 |
| **stale** / wrong / deprecated / correct | **5** / 1 / 2 / 12 | **7** / 0 / 3 / 6 |
| libraries with stale use | 3 of 5 probed | 2 of 4 probed |
| notes written (type-checker verified) | 8 (7), about 391 tokens | 10 (7), about 437 tokens |
| **held-out correct, without -> with notes** | **14% -> 57%** (14 pairs) | **5% -> 65%** (20 pairs) |
| previously-correct APIs after notes | 6/6 still correct | 6/6 still correct |

The stronger model is not safer: Opus 4.6 confidently wrote APIs that were removed after its
cutoff, including `anthropic.HUMAN_PROMPT` with `client.completions`. Stale code from both runs,
each valid for the version the model learned and broken for the pinned one:
`messages.create(temperature=...)` (anthropic 1.8), `hf_hub_download(resume_download=...)`,
`local_dir_use_symlinks=...`, `force_filename=...` and `proxies=...` (huggingface-hub 2.0), and
`client.beta.vector_stores` (openai 3.x).

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run-opus.svg" alt="since-cutoff run: Claude Opus 4.6" width="860"></p>

The notes it wrote (excerpt, verbatim):

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

Small samples, two models, one project: treat it as a demonstration, not a benchmark. The full
report (every task, answer and type-checker error) is what `since-cutoff run` writes to
`.since-cutoff/report.md`. To reproduce: `cd examples/agent-app && since-cutoff run --model
claude-code:claude-haiku-4-5 --task-model claude-code:claude-opus-4-6`.

## Models

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

Training cutoffs come from [models.dev](https://models.dev) (a snapshot is bundled for
offline use). `since-cutoff models sonnet` lists them; `--cutoff 2025-07` overrides.

## How it works

```mermaid
flowchart LR
  L[lockfile] --> V[version at the model's cutoff<br/>vs your version]
  V --> D[static API diff<br/>griffe]
  D --> T[short tasks that need<br/>the changed API]
  T --> M[model answers<br/>no tools, no docs]
  M --> C[basedpyright against<br/>BOTH versions]
  C --> N[notes, verified<br/>by the type checker]
  N --> H[held-out tasks<br/>with vs without notes]
```

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

## What it runs, sends and fetches

- **Fetches** package metadata and wheels from PyPI and model cutoffs from models.dev (a snapshot
  is bundled for offline use).
- **Sends** prompts only to the model provider you choose (`run` only; `scan` sends nothing).
  Prompts contain package names, versions, public signatures and docstrings of the changed APIs,
  the generated tasks and, for notes, the model's own answer. Never your source code.
- **Runs** basedpyright locally on the model's answers. It never executes them.
- **Writes** `.since-cutoff/` in your project, its cache (`since-cutoff cache path`) and, with
  `--apply`, one marked block in `AGENTS.md`/`CLAUDE.md`. No telemetry.

## Safe by design

- **Never executes code.** Package code is read statically (griffe with inspection off; only
  `.py`/`.pyi` files are extracted, with path and size checks). Model-written code is only
  type-checked.
- **Writes almost nothing.** Only `.since-cutoff/` (which ignores itself in git) and, with
  `--apply`, one marked block in `AGENTS.md`/`CLAUDE.md`. Everything else in that file is
  left byte-for-byte unchanged.
- **Stays on PyPI.** Git, path, workspace and private-index dependencies are never looked up on
  public PyPI by name.
- **Local and cached.** No telemetry. PyPI data, diffs, tasks and answers are cached, so
  re-runs are free and reproducible (`--fresh` asks the model again).

## Use in CI

```bash
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

Exit codes: `0` ok, `1` error (including "no model answer could be scored"), `2` usage error,
`3` stale API use found with `--fail-on-stale`.

## Limitations

- Python only for now. TypeScript (`.d.ts` diffs, `tsc`) is next.
- A type checker sees wrong names, wrong parameters and PEP 702 deprecations. It cannot see
  behaviour changes behind an unchanged signature, or deprecations that only warn at run time.
- Probes cover a ranked **sample** of the breaking changes (symbols your code already uses
  first), not all of them.
- "The version the model saw" is the newest release on or before the cutoff date. Models know
  recent releases less well, so real staleness can start earlier.
- Held-out tasks are paraphrases of the same change: they show that a note fixes *that* change,
  not that the model got better in general.

## Related work

- [Context7](https://github.com/upstash/context7) and similar tools retrieve current docs at
  answer time. since-cutoff is complementary: it measures what is actually wrong and keeps a
  small, verified note in the repo.
- [cutoff](https://github.com/sandeepsirodia/cutoff) probes a library you maintain;
  [postcut](https://github.com/justi/postcut) pastes changelogs since the cutoff.
- Built on [griffe](https://mkdocstrings.github.io/griffe/),
  [basedpyright](https://github.com/DetachHead/basedpyright), [models.dev](https://models.dev)
  and [rich](https://github.com/Textualize/rich).

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CONTRIBUTING.md). The offline test
suite runs the whole pipeline with a toy library and a scripted model, so no API key is needed.

## Citation

If you use since-cutoff in research, please cite it (see [`CITATION.cff`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CITATION.cff)).

## License

MIT © [Mohammad Hijjawi](https://github.com/MohammadHijjawi97)
