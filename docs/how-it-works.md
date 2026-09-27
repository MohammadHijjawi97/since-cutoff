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
   (read from `*.dist-info/METADATA`, nothing is executed); then pins in `pyproject.toml`,
   `requirements*.txt` and `Pipfile`. `setup.py` and `setup.cfg` are not read (that would mean
   running the project's code); a project with nothing else gets an error that says so.
   The version source in the reports names every place the versions come from.
   - A lockfile the project is not set up for (a `poetry.lock` or `pdm.lock` next to a
     `pyproject.toml` without that tool's table or build backend, a `Pipfile.lock` without a
     `Pipfile`) is ignored, with a warning, when its versions disagree with the pinned
     requirements. A lockfile the project uses still wins, with a warning about the pins it
     overrides.
   - A requirements file written by pip-compile or `uv pip compile` counts as a lockfile: a pin
     whose `# via` annotation names only other packages (`# via kombu`) is a transitive
     dependency.
   - A dependency declared several times with environment markers gets the declaration for the
     project's Python (`--python` or `.python-version`), or else for the newest Python; of
     several pins that apply, the newest wins.
   - An unpinned dependency gets the newest release in the range the project declares
     (`numpy<2.3`), and with `--python` or `.python-version` only a release that supports that
     Python. A package with only pre-releases gets the newest of them, as pip and uv install.

   By default only **direct** dependencies are checked, and transitive ones that the project's
   code imports itself; `--all-deps` adds the rest (without a lockfile or environment the
   transitive dependencies are unknown, and it warns).
2. **The model's training cutoff** comes from [models.dev](https://models.dev) (a snapshot is
   bundled for offline use), or from `--cutoff`. Partial dates mean the end of the period
   (`2025-07` means 31 July 2025). Without `--model`, the model is the one your coding agent is
   set up with (see [Choosing the model](https://github.com/MohammadHijjawi97/since-cutoff#choosing-the-model)).
3. **The version the model could have seen** is the newest final, non-yanked release published
   on or before the cutoff date (PyPI upload time). A package that had only pre-releases by
   then is compared with the newest of them (development releases do not count). A package
   first released after the cutoff, or whose release at the cutoff only reserved the name (no
   modules, or only empty ones), is reported as newer than the model.
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
   | deprecated | `@deprecated` (PEP 702) added since the cutoff, a library's own decorator whose name contains "deprecat", or a removed name the module still serves with a warning through `__getattr__`, a table of deprecated aliases or a metaclass property (`scan` lists all of them, with the library's message; `run` probes only the PEP 702 ones). A deprecation on some overloads only is a deprecated call form (`call_form` in results.json) |

   Default values, attribute values and return annotations are ignored. Objects are reported
   under their shortest public path (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`, not
   a chain of module imports). Sync/async twins and raw-response wrappers are merged into one
   change. A move needs the same object at the new path (a class or module that kept its public
   names, a function with the old parameters, the same value), and "similar names now" offers
   only names that are new in the same owner. A positional parameter renamed in place is one
   change, with the new name as a suggestion. A removed `*args` or `**kwargs` is reported as such
   (its parameter in results.json is `*args` or `**kwargs`), unless the new signature names the
   parameters it took.

   Not reported, because code written for the old version still works or never used them:
   - a name still listed in `__all__` of a module that binds names the source does not spell
     out (typing_extensions' `globals().update(...)`), or still served without a warning
     through `__getattr__` or a table of aliases;
   - a member a class may still inherit: from a standard-library base, from a base the package
     vendors, or from a base in another package when the removed override called the base's
     method; and a module function the old version attached to a class that is now a method of
     it;
   - a function the new version declares only with `@overload`, as most stubs do;
   - names bound only under `if __name__ == "__main__":` or on some `if`/`try` paths, loggers,
     type variables, `TYPE_CHECKING`, imports that `from x import *` copies, generated
     placeholders (transformers' `utils.dummy_*_objects`) and the parameters of pytest
     fixtures, which pytest passes;
   - kind changes that do not break a call: a callable alias (`namedtuple(...)`,
     `X = ValueError`) becoming a function or class or the reverse, an attribute becoming a type
     alias, and definitions that depend on the Python version or `TYPE_CHECKING`.

## 2. Probe

1. **Selection.** Changes are ranked (symbols the project's own code already uses first, hard
   breaks before soft ones, top-level API before internals) and near-duplicates are dropped.
   "Your code uses" is judged file by file, in the files that import the package: a
   module-level name under the change's own import path, a method or attribute where the file
   reads it on the class or on what it shows is an instance of it, a constructor where the
   class is called, a parameter where it is passed by keyword to its own callable.
   The probe budget (`--max-probes`, default 30) is shared round-robin across packages.
2. **Tasks.** A task-writer model (the tested model unless `--task-model` is given) gets the old
   and new signatures and docstrings and writes three short tasks that a developer who knows
   only the old version would solve with the old API. Tasks that mention the changed
   identifier are discarded, and so are repeats. One task is the probe; the other two are held
   out.

   Since 0.3.0, `run --tasks-out FILE` saves the tasks a run used (per API change,
   the probe first), and `run --tasks-from FILE` uses them instead of calling the task writer,
   so a measurement can be repeated on exactly the same tasks, for another model or another
   set of notes:
   - Changes are keyed by a fingerprint that does not depend on the versions, so a file still
     applies after an upgrade to the changes that are still there. A change the file does not
     cover is skipped ("not in the tasks file"), and so is one without at least a probe and,
     when held-out tasks are asked for, one held-out task.
   - Within a change, a task that repeats an earlier one, the probe included (ignoring case,
     punctuation and spacing), is dropped, as the task writer's repeats are. When failing
     changes have fewer held-out tasks than `--heldout` asks for, the run says so and records
     the number used (`heldout_used`; report.md shows "2 asked, 1 used").
   - The file names who wrote the tasks: the task model, the prompt version and the
     since-cutoff version. A file written again from reused tasks keeps them, and a run on
     reused tasks reports them ("tasks written by" in report.md, `tasks_written_by` in
     results.json), with this run's task model as the note writer.
   - The file may be UTF-8 with or without a byte order mark (Notepad and Windows PowerShell
     write one). A malformed file stops the run before any model call; so does a `--tasks-out`
     path that is a folder or the `--tasks-from` file itself, which the run would shrink to the
     changes it probed. The tasks are written last, so a failed write keeps the result card,
     the JSON output and `--apply`.
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
   got right is re-checked with the block too, to catch notes that make things worse. With
   `--compare`, the same tasks are also answered with baseline blocks (see below).

### Held-out statistics

This is what 0.3.0 and later print; 0.2.0 and earlier print one shorter line, and the
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
- **Regression check.** For changes the model already got right, fixing is not the question.
  The result card gives the regression pairs still correct with the notes ("Previously-correct
  APIs with notes: X/Y still correct", and how many broke); report.md counts the API changes
  still correct (more than half of their pairs correct with the notes) with a Wilson 95% CI,
  and those broken, and leaves out "changes fixed" and the sign test.

`report.md` has a statistics table (held-out tasks and the regression check side by side) and a
table per API change; `results.json` has the same numbers (`difference`, `ci95_difference`,
`ci95_changes_fixed`, `sign_test_p`, `changes_broken`, `excluded_reasons`, `per_change`, and
for the regression check `changes_still_correct` and `ci95_changes_still_correct`).

`results.json` (`settings`) and report.md ("Run settings") also record what the numbers depend
on besides the answers: the since-cutoff version, the model under test and where it came from,
the task and note writer, the Claude Code effort, the prompt version, the API diff schema, the
probe, held-out and regression budgets, the Python version, the tasks file and who wrote its
tasks, the baselines compared, and the date.

### Baselines (`--compare`)

`run --compare template,signatures` (or either one; 0.3.0 and later) measures simpler
notes next to the verified ones, so a run shows how much the verified notes add instead of
assuming they are best. Neither baseline calls a model, and both use the verified block's
header:

- **template** states each failing change in one sentence from the API diff: the text a
  verified note falls back to when its example does not type-check.
- **signatures** gives the new signature and first docstring paragraph of each changed API, or
  of the replacement its library names (for a removed object, also a line saying it is gone).
  It shows what the new version offers, not what changed.

Every held-out task and regression check is answered once without notes and once per block, so
all blocks are paired against the same answers without notes, and they are compared on the
same pairs: a pair counts for every block only when its answer without notes is scorable and no
block's answer with notes is an error (for the other blocks it is listed as "error with another
block"). So a rate-limit error on the one task a baseline did not fix cannot hand it a win. The
verified notes' own result keeps every pair it can count, as without `--compare`; when errors
leave some of those out, the result card adds the verified notes' rates on the shared pairs.

The result card adds a line per baseline: its size in tokens (about four characters per
token), held-out correct without -> with, changes fixed with its 95% CI, and changes broken.
report.md adds a table with the same for every block, the pairs it did not count and why, the
regression check, and the changes that only the verified notes or only the baseline fixed,
with an exact sign test; results.json adds `arms`, and each held-out answer records the `arm`
it saw. `--compare` needs held-out tasks, so it cannot be combined with `--no-fix` or
`--heldout 0`.

## What the numbers mean (and do not mean)

- In `scan`, "**breaking**" and "**deprecated**" count each change once, however many import
  paths reach it. "**Imported by your code**" means a file of the project imports the package
  (by its import names); dependencies whose API changed come first, the imported ones first
  among them.
- "**Stale API use in X of Y probed dependencies**": Y is the number of dependencies with at
  least one scored probe; X is how many of them had at least one probe scored *stale*.
- Probes cover a **sample** of the breaking changes (ranked as above), not all of them.
- Held-out tasks are paraphrases of the same change, so the before/after numbers measure whether
  the note fixes *that* change, not general coding ability. Failures were selected on the probe
  task, so the "before" rate on held-out tasks is also low by construction; the comparison that
  matters is before vs after on the same tasks.
- Each interval belongs to the number next to it. The bootstrap interval is for the
  **difference** of the two task-level rates, and resamples API changes, the unit that was
  sampled; the Wilson interval is for "**changes fixed**" only. Neither is an interval for the
  rates themselves. With few API changes, trust the sign test more than the bootstrap interval.
- "**Changes fixed**" needs a change to go from wrong to correct on most of its pairs; a
  held-out answer that was already correct without the notes does not count (versions up to
  0.2.0 counted it, so their "changes fixed" can be higher than this rule gives).
- Pairs not counted are listed by reason, so each rate can be read against what was left out.
  With the notes, an answer that avoids the package counts as wrong, so notes cannot win by
  making the model avoid it.
- The regression check ("**still correct**") looks for harm: it says whether the notes broke APIs
  the model already knew, not whether they improved anything.
- The **baseline** lines (`--compare`) answer "better than what?": every block is scored on the
  same pairs against the same answers without notes, and its size in tokens is what it costs on
  every agent turn.
- Runs on the same **tasks file** keep the tasks fixed, so a difference between them comes from
  what you changed (the model or the notes) and from the answers' own variation, not from the
  task writer. Runs that each write their own tasks also differ in the tasks.
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
