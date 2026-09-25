# How since-cutoff works

since-cutoff answers one question for one project: **which of the library APIs this project
depends on does my coding model get wrong, and does a short note fix it?**

It is built so that every number it prints can be checked: nothing is judged by an LLM, all
scoring is done by a type checker against the exact package versions, and all intermediate data
(tasks, answers, diagnostics) lands in `.since-cutoff/report.md` and `results.json`.

## 1. Scan (no model calls)

1. **Dependencies and exact versions** come from the first source found: `uv.lock`,
   `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`; then the project's `.venv`
   (read from `*.dist-info/METADATA`, nothing is executed); then pinned lines in
   `requirements*.txt`; unpinned dependencies use the latest release on PyPI.
   By default only **direct** dependencies are checked (`--all-deps` adds transitive ones).
2. **The model's training cutoff** comes from [models.dev](https://models.dev) (a snapshot is
   bundled for offline use), or from `--cutoff`. Partial dates mean the end of the period
   (`2025-07` means 31 July 2025).
3. **The version the model could have seen** is the newest final, non-yanked release published
   on or before the cutoff date (PyPI upload time).
4. **The API diff.** Both versions are downloaded once (wheels, falling back to sdists; only
   `.py`/`.pyi` files are extracted) and loaded *statically* with
   [griffe](https://mkdocstrings.github.io/griffe/). since-cutoff reports only changes that
   break code written for the old version:

   | kind | example |
   |---|---|
   | removed | `anthropic.HUMAN_PROMPT` no longer exists |
   | moved | `pkg.helpers.Session` is now imported from `pkg.sessions` |
   | parameter removed | `messages.create(temperature=...)` |
   | parameter now required | `fetch(url)` needs `timeout=` |
   | keyword-only | `f(a, b)` must now be `f(a, b=b)` |
   | deprecated (PEP 702) | `@deprecated` added since the cutoff |

   Default values, attribute values and return annotations are ignored. Objects in private
   modules are reported under the public path that re-exports them. Sync/async twins and
   raw-response wrappers are merged into one change.

## 2. Probe

1. **Selection.** Changes are ranked (symbols the project's own code already uses first, hard
   breaks before soft ones, top-level API before internals) and near-duplicates are dropped.
   The probe budget (`--max-probes`, default 30) is shared round-robin across packages.
2. **Tasks.** A task-writer model (the tested model unless `--task-model` is given) gets the old
   and new signatures and docstrings and writes three short tasks that a developer who knows
   only the old version would solve with the old API. Tasks that mention the changed
   identifier are discarded. One task is the probe; the other two are held out.
3. **Answers.** The model under test gets the probe task and a system prompt that says which
   version the project pins (a real agent sees the lockfile too). It has no tools, no web, no
   project files: Claude Code is run with `--tools ""`, no MCP servers, no slash commands and an
   empty working directory.
4. **Scoring.** The code is type-checked with
   [basedpyright](https://github.com/DetachHead/basedpyright) twice: against the locked version
   and against the cutoff version, each in an isolated environment that contains only the
   standard library and that one package. Only diagnostics that involve the package count
   (other imports are simply absent from the environment and are ignored):

   | outcome | meaning |
   |---|---|
   | **stale** | valid for the version the model knew, invalid for the version you use |
   | **wrong** | invalid for both versions (hallucinated or misused API) |
   | **deprecated** | valid, but uses an API marked `@deprecated` in your version |
   | **correct** | valid for your version |
   | off-task / invalid | did not use the package / not parseable; excluded from rates |

   Generated code is never executed.

## 3. Fix and verify

1. **Notes.** For every failure, a note-writer model gets the change, the failing code and the
   type-checker errors, and returns one bullet plus a complete example. The note is kept only if
   the example type-checks cleanly against your version *and* every API the bullet recommends
   appears in that verified example. Otherwise a plain statement of the change, derived only
   from the diff, is used instead.
2. **The block.** Notes are grouped by package into a block between
   `<!-- since-cutoff:start -->` and `<!-- since-cutoff:end -->`. `--apply` writes it into
   `AGENTS.md` (or `CLAUDE.md` if that is what the project uses); re-running replaces the block,
   and `since-cutoff unapply` removes it.
3. **Held-out verification.** For each failure, the held-out tasks are answered twice, without
   and with the block in the system prompt, and scored the same way. A sample of APIs the model
   got right is re-checked with the block too, to catch notes that make things worse.

## What the numbers mean (and do not mean)

- "**Out of date on X of Y dependencies**": X dependencies had at least one probe scored
  *stale*; Y is the number of dependencies scanned.
- Probes cover a **sample** of the breaking changes (ranked as above), not all of them.
- Held-out tasks are paraphrases of the same change, so the before/after numbers measure whether
  the note fixes *that* change, not general coding ability. Failures were selected on the probe
  task, so the "before" rate on held-out tasks is also low by construction; the comparison that
  matters is before vs after on the same tasks.
- The type checker catches wrong names, wrong parameters and PEP 702 deprecations. It cannot see
  behaviour changes (same signature, different semantics) or deprecations that only warn at run
  time.
- Everything is cached. Re-running gives the same result; `--fresh` asks the model again.

## Safety

- No package code is imported or executed (griffe runs with `allow_inspection=False`, only
  `.py`/`.pyi` files are extracted, archive paths are validated).
- No model-generated code is executed; it is only type-checked.
- Nothing is written into your project except `.since-cutoff/` (git-ignored by its own
  `.gitignore`) and, only with `--apply`, the marked block in `AGENTS.md`/`CLAUDE.md`.
