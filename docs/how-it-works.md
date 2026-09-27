# How since-cutoff works

since-cutoff answers one question for one project: **which of the library APIs this project
depends on does my coding model get wrong, and does a short note fix it?**

It is built so that every number it prints can be checked: nothing is judged by an LLM, all
scoring is done by a type checker against the exact package versions, and all intermediate data
(tasks, answers, diagnostics) lands in `.since-cutoff/report.md` and `results.json`.

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works.svg" width="640" alt="Three stages. Scan, with no model calls: the lockfile gives your exact versions, then the release at the model's cutoff, then a static API diff with griffe. Probe: short tasks that need the change, the model answers from memory, basedpyright checks the answer against both versions. Fix and verify: a model-written note is kept only if its example type-checks, otherwise the change is stated from the API diff; held-out tasks are answered without and with the notes, and --apply writes a block into AGENTS.md.">
</picture></p>

## 1. Scan (no model calls)

1. **Dependencies and exact versions** come from the first source found: `uv.lock`,
   `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`; then the project's `.venv`
   (read from `*.dist-info/METADATA`, nothing is executed); then `==` pins in `pyproject.toml`
   and `requirements*.txt`; unpinned dependencies use the latest release on PyPI.
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
   | positional-only | `f(a=a)` must now be `f(a)` |
   | changed kind | `pkg.Grammar` was a class and is now an attribute |
   | deprecated | `@deprecated` (PEP 702) added since the cutoff, or a library's own decorator whose name contains "deprecat" (`scan` lists both; `run` probes only the PEP 702 ones) |

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
   identifier are discarded. One task is the probe; the other two are held out. In versions
   after 0.2.0, `run --tasks-out FILE` saves the tasks a run used, and `run --tasks-from FILE`
   uses them instead of calling the task writer (changes the file does not cover are skipped),
   so a measurement can be repeated on exactly the same tasks.
3. **Answers.** The model under test gets the probe task and a system prompt that says which
   version the project pins (a real agent sees the lockfile too). It has no tools, no web, no
   project files: Claude Code is run with `--tools ""`, no MCP servers, no slash commands and an
   empty working directory.
4. **Scoring.** The code is type-checked with
   [basedpyright](https://github.com/DetachHead/basedpyright) twice: against the locked version
   and against the cutoff version, each in an isolated environment that contains that package
   version and its own runtime dependencies, nothing else. Only diagnostics that involve the
   package count (other imports are absent from the environment and are ignored):

   | outcome | meaning |
   |---|---|
   | **stale** | valid for the version the model knew, invalid for the version you use, and the error involves the changed API |
   | **wrong** | invalid for your version, and not explained by the version change (hallucinated or misused API) |
   | **deprecated** | valid, but uses an API marked `@deprecated` in your version |
   | **correct** | valid for your version, and actually uses the changed API |
   | untouched / off-task / invalid | valid but avoided the changed API / did not use the package / not parseable; excluded from rates |

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

### Held-out statistics

This is what versions after 0.2.0 print; 0.2.0 and earlier print one shorter line, and the
[changelog](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CHANGELOG.md) lists
the differences.

- **Pairs.** A held-out task counts as a pair when its answer without the notes is scorable and
  its answer with the notes is not an error. With the notes, only a passing answer is correct
  (an answer that avoids the package counts as wrong). Pairs that are not counted are listed by
  reason: untouched, off-task, invalid, error.
- **"Held-out tasks correct without -> with notes"** gives the two task-level rates, with the
  number of paired tasks and of API changes behind them.
- **"Difference"** is with minus without, in percentage points over all counted pairs. Its 95% CI
  is a percentile bootstrap that resamples API changes with all their pairs (4000 resamples,
  seed 0), because the tasks of one change are not independent of each other.
- **"Changes fixed by the notes: X of Y"**: an API change is fixed when more than half of its
  counted pairs are wrong without the notes and correct with them (with one or two held-out tasks
  per change, as in `--quick` and the default, that means all of them), and broken in the mirror
  case. Its 95% CI is a Wilson score interval for X of Y and belongs to that count only.
- **Sign test**: an exact two-sided test of changes fixed against changes broken; changes that
  are neither are left out.
- With few API changes (under about ten) the bootstrap interval tends to be too narrow; the sign
  test is exact at any size.

`report.md` has a statistics table (held-out tasks and the regression check side by side) and a
table per API change; `results.json` has the same numbers (`difference`, `ci95_difference`,
`ci95_changes_fixed`, `sign_test_p`, `changes_broken`, `excluded_reasons`, `per_change`).

## What the numbers mean (and do not mean)

- "**Stale API use in X of Y probed dependencies**": Y is the number of dependencies with at
  least one scored probe; X is how many of them had at least one probe scored *stale*.
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
