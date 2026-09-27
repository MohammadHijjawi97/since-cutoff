# Changelog

## 0.3.1 - 2026-09-27

- The CLI no longer crashes on its own output. On Windows, stdout or stderr sent to NUL (`> NUL`,
  or Git Bash's `> /dev/null`) crashed every scan with a UnicodeEncodeError on the progress
  spinner ("lost sys.stderr", exit code 1, which also broke `--fail-on-changes` in CI), because
  NUL claims to be a terminal. A file, a pipe or NUL now gets UTF-8 and no live progress bar,
  and a terminal shows a character it cannot encode as `?`. When the reader of the output goes
  away (`since-cutoff scan | head`), the run stops quietly with exit code 141, the code a shell
  gives a writer stopped by SIGPIPE, instead of printing a traceback and exiting with 120.
- Output to a file, a pipe or a CI log is laid out 160 columns wide (`COLUMNS` still sets the
  width) instead of rich's default of 80, which cut the panel's subtitle and wrapped every
  table row. In a narrow terminal, long package names and versions fold instead of squeezing
  the status and changes columns to nothing, and a subtitle too long for the terminal moves
  into the panel. A skip reason too long for the table is cut at a word and ends with "..."
  (it was cut at 80 characters, in the middle of `--max-download-mb`).
- In a terminal narrower than 100 columns the table gives the status its short form
  ("changed, imported", "new"), the version columns the width their versions need, and the
  package names the rest, broken at a "-" (`opentelemetry-` / `sdk`). At 80 columns every
  changed row took three lines and names broke as `opentelemetr` / `y-sdk`.
- In a file, a pipe or a CI log, where there is no live progress bar, the diff says how far it
  got about ten times ("diffed 40/71 (transformers)") instead of nothing for minutes. Wheels
  are extracted about 40% faster on Windows, where resolving the path of every file took half
  of the time (each directory is resolved once now).
- SyntaxWarnings about the scanned project's own code ("invalid escape sequence '\W'") no
  longer reach stderr, in the CLI or the MCP tools.
- A change reachable under several import paths says "also removed under 12 other paths, e.g.
  `setuptools.command.alias.alias.ensure_string_list`" (MCP) or "(also removed under 12 other
  paths)" (Markdown) instead of "12 similar, e.g. ...", which read like a replacement next to
  "similar names now" but named another path that lost the name too.
- MCP `api_changes` takes `symbol="Redis"` as redis's `Redis` class: the symbol names the
  package itself only when spelled like its PyPI or import name (`redis`), and a class name as
  written (`Redis`, not `legacy`) matches whole path segments, so `Redis` finds `Redis`'s
  changes and not `RedisCluster`'s. Before, `Redis` and `Celery` applied no filter at all.
- `scan --markdown -` prints only the progress and the report's path on stderr, as `--json`
  does, instead of the whole console report, which CI logs then showed twice.
- Counts of one are singular ("0 of 1 dependency changed its API", "1 dependency was not
  flagged"); "N more dependencies were not flagged" says "more" only when the table lists
  others; a single unchecked dependency's reason is no longer "for example"; and the progress
  line says "Diffing the API of N packages released after their cutoff version" instead of
  calling them "changed" before the diff has found out. "Checking N dependencies on PyPI"
  counts only those it looks up, not those installed from git, a path or a private index.
- The version source says where the versions come from. Without a lockfile or environment, it
  names the files that pin them (`pyproject.toml`, `requirements.txt`, ...), with "latest on
  PyPI for N unpinned", or says "latest on PyPI, as nothing is pinned". It said "requirements"
  even for a pyproject.toml or Pipfile that pins nothing, whose versions were the latest
  releases on PyPI. The `version_source` field of results.json changes the same way.
- `--all-deps` without a lockfile or virtual environment warns that the transitive dependencies
  are unknown, so only the declared ones are checked (in the terminal and the reports), instead
  of silently doing nothing. A requirements file written by pip-compile or `uv pip compile`
  counts as a lockfile: it pins the whole tree.
- The output of pip-compile and `uv pip compile` is read as such: a pin whose `# via` annotation
  names only other packages (`# via kombu`) is a transitive dependency, checked with
  `--all-deps`, and only those `# via -r requirements.in` (or via the project's
  `pyproject.toml`) are direct; without annotations, the `.in` file next to it says which. Every
  pin counted as direct before, so a scan checked the whole tree (84 packages for pypistats.org,
  whose requirements.in names 17).
- A transitive dependency that the project's code imports itself (alembic and sqlalchemy,
  pinned "via flask-migrate"; a package only a lockfile lists) is checked without `--all-deps`,
  like a direct one.
- A lockfile the project is not set up for no longer overrides the versions its requirements
  pin: a poetry.lock or pdm.lock next to a pyproject.toml without that tool's table or build
  backend (or a Pipfile.lock without a Pipfile) is ignored when its versions disagree with the
  pinned requirements, with a warning that names the first disagreements ("ignored poetry.lock:
  the project is not set up for Poetry, and 51 of its versions disagree with the pinned
  requirements (alembic 1.4.2 vs ==1.16.4, ...)"), and its lock-only packages are not added
  with `--all-deps`. A 2020 poetry.lock hid 13 changed packages of pypistats.org, whose
  requirements.txt pins flask 3.1.1, not 1.1.2. A lockfile the project uses still wins, with a
  warning about the disagreeing pins. The version source names every place the versions come
  from ("poetry.lock, requirements.txt for 32 not in it") instead of the lockfile alone. MCP
  `project_changes` shows these warnings under its heading.
- A project whose dependencies are only in setup.py or setup.cfg gets an error that says those
  files are not read and what to do instead (list the dependencies in requirements.txt or
  pyproject.toml, or install the project into a .venv), and names every file that is read,
  Pipfile included.
- A dependency installed from git, a URL or a local path is skipped as "installed from
  git+https://github.com/org/pkg.git@ref, not from PyPI" (with any credentials removed) instead
  of "not installed from PyPI (direct URL)". A git or URL source in uv.lock, poetry.lock or
  pdm.lock is named the same way ("installed from
  git+https://github.com/pydantic/strict-no-cover@7fc59da2c4df"), not as "installed from git";
  a Poetry private index (`type = "legacy"`) reads "a private index", not "a URL".
- A metapackage without code of its own (docling over docling-slim, griffe 2, fastmcp 4, bs4) is
  skipped with a reason that names what it installs and says to check that instead of "could
  not find importable modules in <wheel>".
- The download-limit message gives the limit as set and the file's size in the same unit: it
  said "84 MB" for the default `--max-download-mb 80`.
- A package that had only pre-releases by the cutoff (sqlgpt-parser 0.0.1a5, betas such as
  0.51b0) is compared with the newest of them, instead of being reported as first released
  after the cutoff; unpinned, a package with only pre-releases is checked at its newest one,
  which pip and uv install, instead of being skipped. A development release (`0.0.1.dev5`) is
  not such a baseline: it is how nvidia-cuda-runtime and its siblings reserved their names in
  2021, so they are "newer than the model" again instead of "could not find importable modules".
- A release at the cutoff that only reserved the name, with no modules or only empty ones in
  small pure-Python files (zensical 0.0.0's empty `__init__.py`), makes the package "newer
  than the model" ("0.0.0 at the cutoff was an empty placeholder"), instead of "no breaking
  changes" or "not checked". MCP `api_changes` says the API is newer than the model's training
  data.
- `scan --model claude-haiku-4-5` works: `scan` only needs the training cutoff, so a model id
  without a provider (or an alias such as `sonnet`) is enough. An unknown model id gets close
  matches, and `run`'s hint for a Claude id without a provider names `anthropic:` next to
  `claude-code:`.
- MCP `project_changes` stays under about 24,000 characters: past that, the remaining changed
  dependencies get one line each under "N more with API changes", with a hint to pass them in
  `only`, and long lists of names end with "and N more" (a project with 155 dependencies got
  56 KB). Its heading counts the dependencies checked ("9 of 14 dependencies checked") instead
  of all of them. The one-line list counts towards the budget too (private-gpt got 25,135
  characters).
- `Tools().project_changes` called from a script diffs the packages in-process unless given
  `Tools(processes=True)`: its worker processes imported the script again, which raised a
  RuntimeError on Windows and macOS without an `if __name__ == "__main__":` guard and ran the
  script once more per worker. `since-cutoff mcp` still diffs several packages in parallel.
- `imported` in results.json (`scan[]`, and now `packages[]` too) says whether the project's
  code imports the package, from the package's import names (a namespace package such as
  `google.genai` only when the code imports that part): true or false for the packages whose
  files were read, null for the others (released before the cutoff, first released after it,
  or not checked). It was always false. The import names behind it are the ones code imports:
  a stub-only distribution's `pandas-stubs` directory is `pandas` (types-requests, types-tqdm,
  ...); pywin32's `win32\lib\win32con` is `win32con` and `pythonwin\pywin` is `pywin`, the
  directories its `.pth` file puts on `sys.path` (its changes were listed as
  `pythonwin.pywin.*`, which cannot be imported); and a distribution with modules in a shared
  namespace (google-cloud-core's `google/cloud/client.py`) is those modules, not all of
  `google.cloud`, which `from google.cloud import bigquery` imports.
- Every report lists the dependencies in the same order: those whose API changed first, of
  those the ones the code imports, then by breaking changes and then deprecations. The terminal
  table, its list of changes and report.md used to rank by the number of changes alone, so the
  internals of a dev tool the code never imports (mypy, sphinx, coverage) came first, while
  `--markdown` and MCP `project_changes` put imported packages first. The terminal table and
  report.md now also say "imported by your code" in the status, as `--markdown` did.
- The terminal table and report.md show "breaking" and "deprecated" columns instead of one
  "changes" column that counted breaking changes only, was blank for a package with only
  deprecations and looked out of order (a package with 2 breaking changes and 7 deprecations
  sat above one with 7 breaking changes). The `--markdown` summary lists "you use" before "at
  cutoff", as the others do, and report.md written by `scan` leaves out the probed, stale and
  wrong columns, which are always empty without probes.
- "Your code uses X" marks are judged file by file, in the files that import the package, and
  under the change's own import path. A module-level name counts only when a file imports it or
  reads it on an imported module: `copier.asdict` is not the `asdict` of `from dataclasses
  import asdict`, `typing_extensions.Callable` not `typing.Callable`, `torchaudio.io` not the
  standard `io`. A method or attribute counts only when the same file reads it on the class, on
  what the file shows is an instance of it (`app = Starlette(...)` then `app.middleware`, a
  parameter annotated with the class, `self.middleware` in a subclass), or on an attribute
  named after the class (`client.messages.create`), not on any value in a file that imports
  the class (`self.middleware` of mcp's own `Server` marked starlette's removed
  `Starlette.middleware`); a constructor only when
  the class is called; and a changed parameter only when it is passed by keyword to its own
  callable (`subprocess.run(text=True)` says nothing about `Version(text=...)`). Before, any
  attribute, keyword argument or imported name anywhere in the project matched, which marked
  (and ranked first) changes the code never touches, in the terminal, in both Markdown reports,
  under MCP's "Touching names your code uses" and in the choice of changes `run` probes.
- A dependency that nothing pins gets the newest release in the range the project declares
  (`numpy<2.3`, `transformers>=4.54,<5`) instead of the newest on PyPI, which could be a version
  the project excludes; with `--python` or a `.python-version`, only a release that supports
  that Python. `version_source` in results.json says so ("latest on PyPI matching <2.3").
  Pipfile versions (`requests = "==2.28.2"`, `"<3"`) and Poetry constraints written as PEP 440
  specifiers (`">=1,<2"`) count too; before, every Pipfile entry was unpinned.
- A dependency declared several times with environment markers (`typer==0.27.1 ;
  python_version >= '3.10'` and `typer==0.23.2 ; python_version < '3.10'`) gets the declaration
  for the project's Python (`--python` or `.python-version`), or else for the newest Python, as
  uv.lock forks do; before, the last pin in the files won. Of several pins that apply, the
  newest wins. A dependency declared from PyPI but also as a direct URL in some extra (torch in
  a CUDA extra) is checked; only one declared by URL alone is skipped as not from PyPI.
- Names that still work are no longer reported as removed. A name the new version still lists
  in `__all__` of a module that binds names the source does not spell out (typing_extensions
  binds `Callable`, `Dict`, ... with `globals().update(...)`, and adds some with
  `__all__.append(...)` per Python version) is not removed, which takes typing-extensions
  4.12 -> 4.15 from 41 breaking changes to none; a stale `__all__` entry that nothing binds
  still is (black 26.5.1's `blib2to3.pgen2.tokenize.generate_tokens`). A name a module still hands
  out through `__getattr__` or a table of deprecated aliases (click's `BaseCommand`,
  `mypy_extensions.NoReturn`, `anyio.abc.CapacityLimiter`, Django's
  `django.core.mail.BadHeaderError`), or a class through a metaclass property (urllib3's
  `Retry.BACKOFF_MAX`), is reported as deprecated, with the library's own message or
  replacement ("use `anyio.CapacityLimiter` instead"), or not at all when it is served without
  a warning. So is a name that the class of a module object swapped into `sys.modules` serves:
  transformers 5's `_LazyModule` hands out every `*TokenizerFast` as the tokenizer without
  "Fast", every name that a module below it lists in `__all__` (`BartTokenizerFast`), and a
  tokenizer its converter table maps to another one (`ElectraTokenizerFast`). Of the 100
  `*TokenizerFast` names private-gpt got as removed (and `RobertaTokenizerFast` as "moved to"
  `tokenization_roberta_old`), 5 remain (Bloom, MT5, Realm, RetriBert), for which neither the
  import structure nor the converter table has an entry. A module that re-exports another
  distribution
  with `from other import *` (mcp 2's `mcp.types`, from mcp-types) no longer has every name
  reported removed.
- Stubs are read as type checkers read them. A function declared only with `@overload` (the
  usual case in a `.pyi` file: numpy's `ndarray.partition`, `cachetools.cached`, django-stubs'
  `QuerySet.defer`, pytest-django's `assertNumQueries`) is no longer reported removed, and names
  that only a stub next to a module declares reach `from x import *` (qdrant-client's `grpc`
  package: 555 false removals). Imports that `from x import *` copies without x meaning to
  re-export them (`av.container.Any`, `yaml.Composer`, `nltk.corpus.reader.ieer.os`,
  `copier.asdict`), and names a plain module star-imports for its own use, are not API.
- Other names that were never API are left out: names bound only under
  `if __name__ == "__main__":` (`rich.diagnose.console`, virtualenv's `py_info.argv`), values
  bound only on some paths through `if`/`try` blocks (`torchvision.extension.lib_path`,
  `certifi.core.Package`), per-module loggers and type variables, `TYPE_CHECKING` and
  `VERSION_TUPLE` in generated version files, transformers' `utils.dummy_*_objects`
  placeholders, and griffe's `dde/*` placeholder for an unresolved star import.
- A member that a class may still inherit is no longer reported removed: from a base in the
  standard library (`BatchFeature.keys` through `UserDict`, `FrozenError.args` through
  `AttributeError`), or an override that called its base's method when the base comes from
  another package (starlette's `TestClient.get` through `httpx.Client`), or from a base whose
  copy the package ships itself (setuptools 80's `Command.ensure_string_list`, from
  `distutils.core.Command`, which setuptools serves from `setuptools._distutils` on Python
  3.12 and later; `pkg._vendor.*` likewise). A module function that
  the old version attached to a class (`Document.new_page = utils.new_page`) and that is now a
  method of the class is not removed either (58 such changes for pymupdf 1.25 -> 1.27).
- Parameter changes: a positional parameter renamed in place is one change, the old name's
  removal with the new name as a suggestion, instead of "removed" plus "now required"; a
  positional-only one (`/`, or the `__x` convention: pydantic's `model_post_init(__context)`
  becoming `(context, /)`) is not a change. `*args` and `**kwargs` are shown as such
  ("`**kwargs` was removed, so extra keyword arguments are no longer accepted") instead of
  `close(kwargs=...)`, and not reported when the new signature names the parameters they took.
  A function wrapped by a class (typing_extensions' `@_TypedDictSpecialForm`) keeps its call
  signature, and the parameters of a pytest fixture, which pytest passes (time-machine's
  `time_machine_fixture(request)`), are not reported. Signatures show `/` and no longer show
  `*args = ()`. The console, `--markdown` and report.md show the new name of a renamed
  parameter, as MCP did: "parameter `pos` was removed (now `start`?)".
- A deprecation on some overloads of a function is a deprecated call form, not a deprecated
  function: "`pydantic.config.with_config` called as `with_config(*, config: ConfigDict)` is
  deprecated: Passing `config` as a keyword argument is deprecated. ..." (and
  `cachetools.cached` with a positional `info`). results.json has the call form in `call_form`.
- Kind changes: a name bound to something callable (`write_pack_index = write_pack_index_v2`,
  `Scanned = namedtuple(...)`, a callable instance, `BadHeaderError = ValueError`, a stub's
  `ones: Final[_Constructor]`) that becomes a function or class, or the reverse, is not a
  break, nor is an attribute becoming a type alias or a definition that depends on the Python
  version or `TYPE_CHECKING`. A value that cannot be called (a literal, `Union[...]`) still is.
- "Moved to" and "similar names now" hints are checked. A move needs the same object, not just
  the same name: a class or module that kept its public names (of several such modules, the one
  that kept the most, and none on a tie), a function with the old parameters, a value with the
  same literal or type expression; never a logger or type variable from another module. The
  target is where the object is defined or re-exported, not a module that merely imports it
  (`dulwich.protocol.PEELED_TAG_SUFFIX`, not `dulwich.server`). A protobuf message that
  generated code built with `GeneratedProtocolMessageType` and a newer stub declares as a class
  of another module moved there (21 of qdrant-client's `grpc.points_pb2` messages, now in
  `qdrant_common_pb2`). Similar names are only names new
  in the same owner and of the same sort (no `collections` for `Collection`, no existing
  `Dimension.zero` for `Dimension.is_zero`). Objects are reported under their shortest public
  path (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`, not a chain of module imports).
  Diffs cached by earlier versions are recomputed.

## 0.3.0 - 2026-09-27

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
