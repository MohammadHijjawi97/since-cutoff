---
title: What your coding model doesn't know about your dependencies
description: Measuring which pinned library APIs a coding model gets wrong because they changed after its training cutoff, and fixing them with small, type-checked AGENTS.md notes.
---

# What your coding model doesn't know about your dependencies

*Mohammad Hijjawi · September 2026 · [since-cutoff on GitHub](https://github.com/MohammadHijjawi97/since-cutoff)*

Every coding model has a training cutoff. Your lockfile doesn't. When a library changes its
public API after the cutoff, a model that learned the old version keeps writing the old calls,
and nothing in the prompt tells it otherwise.

I wanted a number for that instead of an anecdote: **for one real project, which of the exact
dependency versions it pins does the model get wrong, and does a short note fix it?**
[since-cutoff](https://github.com/MohammadHijjawi97/since-cutoff) is the tool I built to answer it.

## The setup

The sample project ([`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app))
is a small agent app with nine dependencies pinned to current releases: anthropic 1.8.0,
openai 3.19.2, huggingface-hub 2.0.0, langchain-core 1.6.5, langgraph 1.2.12, pydantic 2.13.5,
fastapi, requests and httpx.

The model under test is **Claude Haiku 4.5**, whose training cutoff is February 2025. Tasks and
notes were written by Claude Opus 4.6.

## How it measures

1. **What changed.** For each dependency, take the newest release published on or before the
   model's cutoff, and diff its public API against the pinned version, statically (griffe, no code
   imported). That finds removed and moved objects, removed or newly required parameters,
   keyword/positional-only changes and new `@deprecated` markers. For this project, 7 of the 9
   dependencies changed; the diff flags 712 changes, many of them internals.
2. **Tasks that need the change.** For the highest-ranked changes, a task writer produces short,
   realistic coding tasks that require the changed functionality but never name the changed
   identifier or its replacement. One task is the probe; two are held out.
3. **Answers with nothing to look things up in.** The model gets the task and the pinned version
   number, and no tools, no web, no project files.
4. **Scoring by a type checker, twice.** The answer is type-checked with basedpyright against the
   version the model could have seen and against the pinned version, each in an isolated
   environment with that version's own dependencies. Only API-knowledge errors count (unknown
   names, imports and parameters, missing arguments, arity), never type-strictness complaints.
   - **stale**: valid for the version it knew, invalid for the pinned one
   - **wrong**: invalid, and not explained by the version change
   - **deprecated**: valid, but uses an API marked `@deprecated` in the pinned version
5. **Fix and verify.** For each failure, a one-line note is written for AGENTS.md. A note is kept
   only if its example type-checks against the pinned version and every API it recommends appears
   in that example. Then the held-out tasks are answered again, without and with the notes, and
   compared pair by pair.

No generated code is ever executed.

## Results

| | |
|---|---|
| API changes probed | 20 (plus 2 not counted: one off-task, one that never used the API) |
| stale | **5** |
| wrong | 1 |
| deprecated | 2 |
| correct | 12 |
| libraries with stale use | **3 of the 5 probed** |

The stale code, verbatim from the model's answers, each valid in the version it learned and
broken in the pinned one:

- `client.messages.create(..., temperature=...)`: removed in anthropic 1.8
- `hf_hub_download(..., resume_download=True)`, `local_dir_use_symlinks=...` and `proxies=...`:
  removed in huggingface-hub 2.0
- `client.beta.vector_stores`: an openai 1.x API that 3.x moved to `client.vector_stores`

### Does a note fix it?

since-cutoff wrote **8 notes, about 391 tokens**, 7 of them verified by the type checker (one fell
back to a plain statement from the API diff). For example:

```markdown
**anthropic 1.8.0**
- `temperature=...` was removed from `messages.create()` in anthropic 1.8.0. Omit the `temperature` parameter entirely; there is no replacement.

**openai 3.19.2**
- `client.beta.vector_stores` is removed in openai 3.19.2. Use `client.vector_stores` instead.
```

On the held-out tasks for the failing changes, the model was correct on **14% without the notes
and 57% with them** (14 paired tasks; 4 of 7 changes fixed; 95% CI 25-84% over changes). The six
APIs it already got right were still right with the notes in its context.

## What these numbers do and don't say

- **Small sample.** One model, one project, 20 probes. It is a demonstration of a method, not a
  benchmark. The confidence interval is wide on purpose.
- **A sample of changes, not all of them.** Probes are ranked (APIs the project's own code uses
  first, hard breaks before soft ones); the task writer also skips internals.
- **What a type checker can see.** Wrong names, parameters, arity and PEP 702 deprecations.
  Behaviour changes behind an unchanged signature are invisible.
- **Held-out tasks are paraphrases of the same change,** so the before/after measures whether a
  note fixes *that* change, not general ability.
- **"The version the model saw" is a date rule** (newest release before the cutoff). Models know
  the months right before their cutoff poorly, so real staleness can begin earlier.

## Why I care about this

This grew out of a paper I co-authored for EMNLP 2026 on *temporal isolation*: using a model's
training cutoff as a natural experiment for what it knows. Library releases are the same
experiment with a very practical payoff: the answer is a list of lines to put in AGENTS.md.

## Try it

```bash
# what changed since your model's cutoff (no model calls)
uvx --from git+https://github.com/MohammadHijjawi97/since-cutoff since-cutoff scan

# measure, write verified notes, apply them to AGENTS.md
uvx --from git+https://github.com/MohammadHijjawi97/since-cutoff since-cutoff run --apply
```

It works with Claude Code (as a plugin), Anthropic, OpenAI, OpenRouter, DeepSeek and Ollama.
It is Python-only for now; TypeScript is next. Feedback on the method is very welcome in the
[issues](https://github.com/MohammadHijjawi97/since-cutoff/issues).
