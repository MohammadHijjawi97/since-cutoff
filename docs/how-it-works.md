# How since-cutoff works

since-cutoff answers two questions for one project. Without a model (`scan`, `sync`):
**which APIs that this project's code uses changed after the model's training cutoff, and what
should the coding assistant be told about them?** With `since-cutoff run`, which calls the
model: **which of those changes does the model get wrong, and does a short note fix it?**

It is built so that everything it prints can be checked: each note says what it rests on,
nothing is judged by an LLM, all scoring is done by a type checker against the exact package
versions, and all intermediate data (changes, uses, notes, tasks, answers, diagnostics) lands in
`.since-cutoff/report.md` and `results.json`.

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works.svg" width="640" alt="Three stages. Scan, with no model calls: the lockfile gives your exact versions, then the release at the model's cutoff, then a static API diff with griffe. Probe: short tasks that need the change, the model answers from memory, basedpyright checks the answer against both versions. Write and test notes: a model-written note is kept only if its example type-checks, otherwise the change is stated from the API diff; held-out tasks are answered without and with the notes, and --apply writes a block into AGENTS.md.">
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
3. **The comparison release** is the newest final, non-yanked release published on or before the
   cutoff date (PyPI upload time). A package that had only pre-releases by
   then is compared with the newest of them (development releases do not count). A package
   first released after the cutoff, or whose release at the cutoff only reserved the name (no
   modules, or only empty ones), is reported as first released after the cutoff.
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
   | dependency switched | openai 3.19.2 requires `httpx2` instead of `httpx`, and `OpenAI(http_client=...)` takes an `httpx2.Client` ([below](#a-dependency-the-pinned-release-switched)) |
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

### What the training cutoff is used for

The cutoff date picks a comparison point. It is not a claim about what a model memorised.
since-cutoff takes the date from models.dev (or `--cutoff`); a month means its last day
(`2025-07` is 31 July 2025). For each dependency it takes the newest final, non-yanked release
uploaded on or before that date (a pre-release only if the package had no final release by then,
never a development release), and diffs that release's public API against your locked version.
That diff is a list of candidates: API changes that the model's training data probably does not
include.

The date decides three things:

- which packages are diffed at all: a package whose locked version is no newer than that release
  has nothing to diff, and a package first released after the date is listed as new;
- in `run`, which versions of that release's own dependencies the old side is type-checked with
  (the newest each requirement allowed on that date);
- the "old" side of every probe in `run`, so an answer that is valid there and invalid for your
  version, with the error on a changed API, is "stale" rather than "wrong".

A model can know a release after its stated cutoff or not know releases shortly before it, so the
scan can list changes the model already handles and miss some it does not. Whether the model
actually writes the old API is shown only by `run`, which asks it: with no tools, told which
version the project pins.

## Notes without a model: scan and sync

Since 0.4.0, `scan` starts with the changed APIs the project's code uses, and writes a note for
each from the API diff; `sync` puts those notes into AGENTS.md or CLAUDE.md and keeps them in
step with the lockfile. Neither calls a model or needs an API key.

### Where the code uses a changed API

An **API** is one callable, class, attribute or module: `Messages.create` losing `temperature`,
`top_k` and `top_p` is one API with three changes. "Your code uses" is judged file by file, in
the files that import the package, and statically (nothing is run):

- a module-level name under any public path that leads to it: the module that defines it, and
  every module that re-exports it (`from huggingface_hub import hf_hub_download` counts for
  `huggingface_hub.file_download.hf_hub_download`; an import under `if TYPE_CHECKING:` counts
  when the module lists the name in `__all__`);
- a method or attribute where the file reads it on the class, or on what the file shows is an
  instance of the class (`client = anthropic.Anthropic(); client.messages.create(...)`), or on
  `self` or `super()` in a subclass of it;
- a constructor where the class is called, under any name of the class;
- a parameter only where it is passed by keyword to that very callable: `temperature=` passed to
  another library's `create` in the same file does not count. An SDK's beta mirror
  (`client.beta.messages.create`) is another API than the one outside `beta`.

A change from a diff that recorded no import paths (an older cache, or a change made by hand
through the Python API) is matched by package and name, and the report says "matched by name".

Each use is one of two **forms**:

| form | meaning |
|---|---|
| **old form** | the code uses the API as the release at the cutoff allowed and the pinned release no longer does: it reads or imports what was removed, moved (its old path) or changed kind, uses what is deprecated, or passes a removed or deprecated parameter by keyword |
| **uses this API** | the code is fine today, but an assistant editing it may write the old form; a parameter that is now required, keyword-only or positional-only is always this, since a name match cannot tell whether a call passes it the new way |

The terminal and the MCP tool `project_changes` show at most 3 files per API (`scan -v` shows
every one). Locations are reported at the matching source location (for example `app/main.py:2`). The JSON includes `line`, `column` and `code` in `used_apis[].locations`.

### The note for each API

One bullet per API covers all of its changes, whichever of them the code uses: parameters in the
order of the old signature, packages in alphabetical order, and a package's bullets in the
order of their APIs, so the text does not change when the code starts or stops passing one of
them. A replacement is named only with evidence, and the tag says which:

| evidence | what the note says | tag |
|---|---|---|
| none | "`Client.send()` no longer accepts `temperature`; do not pass it. since-cutoff found no replacement in toylib's deprecation text." When the pinned method takes `extra_body` or `extra_query` (an SDK method that sends a request: anthropic, openai and other Stainless-generated clients), the removed parameter left the signature but perhaps not the API, so the note says so instead of "do not pass them": "`Messages.create()` no longer accepts `temperature`, `top_k` or `top_p` as keyword arguments. If the API still needs them, pass them through its `extra_body` or `extra_query` argument. since-cutoff found no replacement in anthropic's deprecation text." Where the library's own text says so: "huggingface-hub's deprecation text says there is no replacement for `resume_download`." Advice in that text that is not stated as a replacement is quoted: "On `x`, pkg 1.0 said: "…"" | `[diff]` |
| the library's deprecation text (a docstring, a parameter's docstring entry, an `@deprecated` message or a `warnings.warn` text) states the replacement ("Use `stop` instead", "replaced by", "renamed to", "in favour of"), and the name exists in the pinned version | "Use `stop` instead of `stop_sequences`." | `[diff + library]` |
| a moved object that looks like the same object: a class or module keeps at least half of its public names, a function its parameters, a value the same value | "`pkg.helpers.Session` moved to `pkg.sessions.Session`: import it with `from pkg.sessions import Session`." | `[diff + move checked]` |
| the older release's Requires-Dist lists a library the pinned one does not, and the public API that named its types names another required library's types, or a copy it ships, instead ([below](#a-dependency-the-pinned-release-switched)) | "openai 3.19.2 requires `httpx2` instead of `httpx`: `OpenAI(http_client=...)` takes `httpx2.Client` (`httpx2.AsyncClient` for `AsyncOpenAI`), `OpenAI(timeout=...)` takes `httpx2.Timeout` and `OpenAI(base_url=...)` takes `httpx2.URL`. Use `httpx2` there, not `httpx`." | `[diff + metadata]` |
| a new parameter in the same position with the same annotation | "`begin` was probably renamed to `start` (same position and type)." | `[diff; probable rename]` |
| names that merely look similar | Nothing, by default. The terminal, report.md and the MCP tools say "similar parameters in 2.0.0, not confirmed as replacements: `x`", and nothing where the library says there is no replacement. `sync --suggestions` adds "Similar names in 2.0.0, not confirmed as replacements: `x`." | `[diff; not confirmed]`, with `--suggestions` |

Other changes are stated as they are: "`x` is deprecated; avoid it in new code.", "… now
requires `timeout`.", "Pass `x` to `f()` by keyword.", "`pkg.Grammar` changed from class to
attribute; check its new signature before use." A stubs package's note (`pandas-stubs`,
`types-*`) says what its declarations no longer have and that type checkers reject it. Text
from a package (deprecation messages) is quoted as whole sentences and cannot close the block or
open code.

When the pinned release's source still reads a removed parameter by name (in a decorator the
callable uses, or a function of the same module that the decorator calls), the terminal,
report.md, the MCP tools and the JSON add a **Runtime** line: huggingface-hub 2.0.0 still handles
`force_filename`, `local_dir_use_symlinks`, `proxies` and `resume_download`
(`huggingface_hub/utils/_validators.py:178-203`), so calls passing them may run with a warning,
while type checkers reject them. The block leaves it out: the advice is the same either way.
[What "verified" means](#what-verified-means) says what each tag checks and what it does not.

### A dependency the pinned release switched

A diff of a package's own names does not see this one: openai 3, anthropic 1.8, huggingface-hub
2.0 and mcp 2.2 require `httpx2` and no longer `httpx`, and their signatures take `httpx2`
objects. Code written for the older release passes an `httpx.Client` or an `httpx.Timeout`, or
catches `httpx.HTTPError` from huggingface-hub. So the diff also compares the two releases'
Requires-Dist (their wheels' METADATA, stored with the sources), and when the pinned one dropped
a requirement, it reads where each public API names that library's types. A
`dependency_switched` change (diff schema 19) needs all of:

- a library X the older release requires and the pinned one does not: a base requirement, or
  one of an extra both releases define (fastmcp-slim 4.0 lists `httpx2` where 3.4 listed `httpx`
  for its `client`, `mcp` and `server` extras; the `fastmcp` distribution installs the `client`
  and `server` ones); not a typing helper (`typing-extensions`, `typing-inspection`,
  `mypy-extensions`, `eval-type-backport`, `typed-ast`), one of the package's own modules, or a
  module its wheel ships itself (pytest 7.2 dropped `py` and has a `py.py`);
- places in the public API (a parameter's, return's or attribute's annotation, a base class or
  a base's type argument, a re-export; not in `cli`, `commands`, `testing` or `experimental`
  modules) that named X types and, at the same place, name the types of Y, with the same type
  name (`httpx.Client` -> `httpx2.Client`) at half of them or more. Y is a library the pinned
  release requires (for an extra's X, with that extra), or a copy of X it ships: a private module
  named after it that does not import X itself (typer 0.27 no longer requires `click` and ships
  `typer._click`; langsmith 0.14's `_openapi_client._httpx` imports `httpx2` or else `httpx`, so
  it is a shim, not a copy);
- no X left anywhere in the pinned release's public API, defaults and command-line modules
  included: langsmith 0.14 dropped `httpx` from its requirements and still names it, and hishel
  1 moved it to an extra.

A library is looked for under its normalised name only (`httpx2`, `huggingface_hub`), so the
result does not depend on what else is in the cache; one whose module is named otherwise
(Pillow's `PIL`) is missed, never matched wrongly. `Annotated[...]` counts only its type and
`Literal[...]` nothing. A base class counts through the package's own classes (mcp 2.2's
`OAuthClientProvider` derives from its `RedirectAwareAuth`, which derives from `httpx2.Auth`),
but a subclass of a class whose own base switched is not counted again. A parameter is counted
by the type it names now: openai 3's `http_client` takes an `httpx2.Client` in 8 signatures and
an `httpx2.AsyncClient` in 6. A constructor is recorded under its class's path
(`openai.OpenAI`), the way code calls it, a classmethod or staticmethod under its class's
(`fastmcp.FastMCP.from_openapi`), and each switched parameter with its position in the call
(after `self` or `cls`), or none for a keyword-only one.

The note, tagged `[diff + metadata]`, is one or two sentences of at most 300 characters: what
the pinned release requires instead ("mcp 2.2.0 requires `httpx2` instead of `httpx`";
"fastmcp-slim 4.0.10's `client`, `mcp` and `server` extras require `httpx2` instead of
`httpx`"; "typer 0.27.2 no longer requires `click` and ships `typer._click` instead"; and where
Y was already required, "gradio 4.28.3 no longer requires `requests`; its API names `httpx`
types instead"), up to three places (the signatures code most likely calls first, a sync one
with its async twin's type, a base class, a re-export), and what to use there: Y, or for a
copy, the package's own names for its classes (`typer.BadParameter`, `typer.Context`). What the
note leaves out is in a **Places** line of `scan --all` and report.md, and in the MCP tools:
the Requires-Dist entries ("its Requires-Dist lists `httpx2<3,>=2.12.0` and no `httpx`",
or which extra still lists X), how many places switched, and more of them, each with how many
other signatures name the same type there.

A file that imports the package uses it in the old form when it hands a value that reads X, by
keyword or by position, to a switched parameter of a switched constructor, function, or
classmethod or staticmethod called on its class (`OpenAI(http_client=httpx.Client())`, or
`OpenAI(http_client=c)` after `c = httpx.Client()` or with `c: httpx.Client`;
`hf_raise_for_status(httpx.get(url))`; `FastMCP.from_openapi(spec, client=httpx.AsyncClient())`),
or catches a switched X type around a call into the package (`except httpx.HTTPError` around
`hf_hub_download(...)`). Only what the file shows counts: reading X's names for itself
(`session = httpx.Client()`) or importing both is "uses this API", `OpenAI(timeout=30.0)` next
to an unrelated `httpx.get` hands nothing over, an argument after `*args` has no known
position, and an instance method's argument is not counted (the file may have the instance from
anywhere). `OpenAI(timeout=30)` without X is no use at all. `sync --scope imported` puts the
switch first among the package's notes. What the pinned release does with X objects at run
time differs (according to their sources, openai 3.19.2 converts some when `httpx` is
imported, and anthropic 1.8.0 raises `TypeError`), so the note does not say. The terminal,
report.md, the MCP tools and the JSON (`used_apis[].installed`) add an **Installed** line,
whether the project still has X: its lockfile or pins, or else its virtual environment,
transitive distributions included (another package may need X: 12 of the 36 pins in
[the AI stack example](ai-stack.md) require `httpx`). A **Runtime** line points to where the
pinned source still imports X or has its name as a string; a name a module only exports (in
`__all__`, compared in a module-level `__getattr__`, returned by `__dir__`: huggingface-hub
2.0's `utils` answers `utils.httpx` with `httpx2`) is not one, nor is a package name in a list
of requirements with versions or in a documentation keyword (`examples=`, `description=`:
fastmcp 4's `Field(examples=[["fastmcp>=2.0,<3", "httpx", "pandas>=2.0"]])`). `run` does not
probe the switch: its task templates need a callable and a parameter.

Measured on 283 release pairs from PyPI with the first version of this rule (diff schema 18:
base requirements only, Y a requirement of the pinned release; 271 of the pairs dropped 530 base
requirements between them): 16 switches, each with the same type names on both sides at half of
its places or more: `httpx` -> `httpx2` in openai (2 pairs), anthropic (4), huggingface-hub (1)
and mcp (2), and `requests` -> `httpx2` or `httpx` in huggingface-hub (4), litellm (1) and
gradio (2). Three pairs that a looser rule, without the same names and the typing helpers, also
counted are not switches: crewai 1.15's `TokenCalcHandler` derives from pydantic's `BaseModel`
where it derived from langchain's `BaseCallbackHandler`, and flask-limiter 3.12 and
pytest-asyncio 0.25 dropped `typing-extensions`. In that measurement the pass, which runs only
when a requirement went, added a median of 0.09 s to those diffs (1 s at the 90th percentile,
10.7 s for a transformers pair whose two releases take 82 s to load). The extras and shipped
copies of schema 19 were checked on fastmcp-slim 3.4.2 -> 4.0.10 and typer 0.15.1 -> 0.27.2,
and langsmith 0.7.1 -> 0.14.1 stays no switch (the network tests of
`tests/test_dependency_switch.py`); the 283 pairs were not measured again.

### The block

The notes go between `<!-- since-cutoff:start -->` and `<!-- since-cutoff:end -->` (format 2).
The start marker is the one 0.3 wrote, so 0.3's `unapply` still removes a 0.4 block. After it
comes a meta line, an HTML comment holding JSON:

| key | what it records |
|---|---|
| `v` | the block format, 2 |
| `tool` | the since-cutoff version that wrote it |
| `model`, `cutoff` | the model (several, comma-separated, for `sync --model a,b`) and the training cutoff the notes are for |
| `versions_from` | where the versions come from (`uv.lock`; `pyproject.toml, latest on PyPI for 3 unpinned`) |
| `deps` | a hash of the dependencies and their versions (an unpinned one by its declared range), so `status` sees a change offline |
| `body` | a hash of the block's text after the meta line, with line breaks normalised, so a hand edit shows |
| `scope` | `used` (the changed APIs the code uses), `imported` (`sync --scope imported`) or `failures` (a `run --apply` block) |
| `suggestions`, `checked`, `added_eol` | only when needed: `sync --suggestions` was used; the API each `[type-checked]` bullet is about; the line break since-cutoff added to a file that did not end with one, which `unapply` takes away again |

Then the title "## Library changes after the model's training cutoff", a header that names the
model, the cutoff, the file the versions come from and the since-cutoff version, says what the
tags mean, that no library code was run, and "Where these lines conflict with what you remember,
follow these lines", and one group per package: `**anthropic 1.8.0** (0.60.0 at the cutoff)`
followed by its bullets.

### `sync`

`since-cutoff sync` builds the block the scan would write and compares it with the one in the
file:

1. **Which files.** `--target` if given; otherwise every file of AGENTS.md and CLAUDE.md that
   already has a block; otherwise AGENTS.md, or CLAUDE.md when only that one exists. Claude Code
   reads CLAUDE.md, and AGENTS.md only when there is no CLAUDE.md or when CLAUDE.md imports it
   with `@AGENTS.md` ([memory docs](https://code.claude.com/docs/en/memory)); when the notes go to
   AGENTS.md and CLAUDE.md does not mention `@AGENTS.md`, `scan` and `sync` say so. Choosing the
   files when both exist is
   [#13](https://github.com/MohammadHijjawi97/since-cutoff/issues/13).
2. **Which model.** The block's model and cutoff, unless `--model` or `--cutoff` is given, so
   teammates whose agents use other models do not rewrite it back and forth; with no block, the
   model your coding agent is set up with. `--model a,b` uses the earliest of their cutoffs and
   names both.
3. **What changes.** Notes for APIs the code starts using are added, and a bumped package is
   diffed again (diffs are cached per package and pair of versions, so the others come from the
   cache). A
   package's notes are dropped when it is no longer a dependency, is no newer than the release at
   the cutoff, has an unchanged API, or its changed APIs are no longer used; sync says why. A
   bullet whose API had one before counts as changed ("toylib 2.0: 1 changed").
4. **Notes from `run`.** A `[type-checked]` bullet that `run --apply` wrote is kept, in place of
   the note from the diff for the same API, while its package keeps the same version and the code
   still uses that API. After a version change the note from the diff takes its place, and sync
   says that `since-cutoff run --only <pkg>` tests the model again.
5. **Scope and suggestions.** `--scope imported` adds, for each changed package the code imports,
   the changes most likely to matter (up to 5 APIs per package, `--per-package N` for another
   number; fields of params classes that mirror a lost parameter, and hooks with a parameter
   whose type is private to the library, are left out), in this order: a library the pinned
   release requires instead of one it required, then import paths that no longer work (a
   module or package that moved, a top-level name removed), then changes with a
   known replacement (`[diff + library]`, a rename or move the diff checked), then removed
   classes and functions (and, after them, names only no longer re-exported: the object is
   still there, in a module below), then parameters and members, then deprecations; within
   each, classes, functions and modules before constants and type aliases. A subpackage whose
   modules all moved to one new parent, leaving nothing public behind, is one change; a name
   bound to a method (`duplicate_space = api.duplicate_space`) and the method are one API.
   `--suggestions` adds similar names, tagged `[not confirmed]`. The block records all three,
   and later syncs keep them.
6. **Hand edits.** When the text after the meta line no longer matches its `body` hash, sync
   shows the diff, writes nothing and exits with code 4, unless `--force`. A block that 0.3 wrote
   is upgraded, keeping its model. When the notes are unchanged, a block another since-cutoff
   version wrote is left exactly as it is.
7. **Writing.** sync prints a unified diff (from `/dev/null` for a new file) and asks
   `Write this to AGENTS.md? [y/N]`; `--yes` writes without asking, `--dry-run` only shows the
   diff, and with no terminal to ask in nothing is written. Text outside the markers keeps its
   bytes: CRLF line breaks, a byte order mark before the start marker, and a missing final line
   break. A new block is appended after a blank line. When nothing changed, the file is not
   touched (its bytes and modification time stay the same).

| exit code | `sync` | `sync --check` | `status` |
|---|---|---|---|
| 0 | written, or up to date (`--dry-run`: also when out of date or edited by hand) | up to date | current, or no block (`--hook`: always) |
| 1 | error: a package the notes are about could not be checked (PyPI unreachable), a file cannot be written, broken markers | same | broken markers |
| 3 | out of date and not written (you said no, or there is no terminal to ask in) | out of date; nothing written | out of date |
| 4 | the block was edited by hand; nothing written without `--force` | same | – |

What it prints, from the sample project:

- up to date: "AGENTS.md is up to date: 2 notes for claude-sonnet-4-5 (cutoff 2025-07-31),
  versions from pyproject.toml, latest on PyPI for 3 unpinned. Nothing written."
- `--check`, out of date: "AGENTS.md is out of date: anthropic 0.60.0 in pyproject.toml is the
  latest release at the cutoff. Run `since-cutoff sync`."
- a hand edit: "The since-cutoff block in AGENTS.md was edited by hand; sync would replace it
  (diff above). Move your text below `<!-- since-cutoff:end -->`, or run
  `since-cutoff sync --force`."

### `status`

`since-cutoff status` compares the block with the project's dependencies as they are, with no
network and without reading the code (`sync --check` does that): per package, the version the
notes are for and the version the lockfile has; whether other dependencies changed (the `deps`
hash); where the versions come from; and whether the model your coding agent is set up with has
an earlier training cutoff than the notes, which may then miss changes (it prints the
`sync --model` command for it; this is not a failure, since sync keeps the block's model). A
block that 0.3 or `run --apply` wrote is out of date, as `sync --check` would find it. No block
is not a failure: a project whose code uses no changed API never gets one. `--json` prints the
same for scripts. `--hook` prints one line only when the notes are out of date and always exits
with 0; the Claude Code plugin runs `uvx since-cutoff==<version> status --hook` when a session
starts (0.4.0 and later).

## 2. Probe

1. **Selection.** Changes are ranked (symbols the project's own code already uses first, hard
   breaks before soft ones, top-level API before internals) and near-duplicates are dropped.
   "Your code uses" is judged as in
   [Where the code uses a changed API](#where-the-code-uses-a-changed-api).
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
   | **stale** | valid for the comparison release, invalid for the version you use, and the error involves the changed API |
   | **wrong** | invalid for your version, and not explained by the version change (hallucinated or misused API) |
   | **deprecated** | valid, but uses an API marked `@deprecated` in your version |
   | **correct** | valid for your version, and actually uses the changed API |
   | untouched / off-task / invalid | valid but avoided the changed API / did not use the package / not parseable; excluded from rates |

   Generated code is never executed.

## 3. Write and test notes

1. **Notes.** For every failure, a note-writer model gets the change, the failing code and the
   type-checker errors, and returns one bullet plus a complete example. The note is kept, tagged
   `[type-checked]`, only if basedpyright reports no error attributed to the package for the
   example, checked against your version, *and* every library name the bullet puts in backticks
   appears in that example or in the API-diff entry (see
   [What "verified" means](#what-verified-means)). Otherwise the note from the API diff, with its
   tag, is used instead.
2. **The block.** The notes go into a block in format 2 (see [The block](#the-block)), whose
   header says they are the changes the model got wrong when tested. `--apply` writes it into
   AGENTS.md or CLAUDE.md, the files `sync` would choose, in place of the block that is there;
   a later `since-cutoff sync` adds the notes from the diff for the other changed APIs the code
   uses and keeps the run's `[type-checked]` notes while their package keeps its version.
   `since-cutoff unapply` removes the block.
3. **Held-out test of the notes.** For each failure, the held-out tasks are answered twice,
   without and with the block in the system prompt, and scored the same way. A sample of APIs
   the model got right is re-checked with the block too, to catch notes that make things worse.
   With `--compare`, the same tasks are also answered with baseline blocks (see below).

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
notes next to since-cutoff's own (the arm with the id `verified` in results.json), so a run
shows how much its notes add instead of assuming they are best. Neither baseline calls a model,
and both keep 0.3's block and wording, so that their numbers stay comparable with 0.3's; the
run's own block has the header that says what its tags mean:

- **template** states each failing change in one sentence from the API diff: the text 0.3 fell
  back to when a model-written note's example did not type-check (0.4 falls back to the note
  from the API diff, with its tag).
- **signatures** gives the new signature and first docstring paragraph of each changed API, or
  of the replacement its library names (for a removed object, also a line saying it is gone).
  It shows what the new version offers, not what changed.

Every held-out task and regression check is answered once without notes and once per block, so
all blocks are paired against the same answers without notes, and they are compared on the
same pairs: a pair counts for every block only when its answer without notes is scorable and no
block's answer with notes is an error (for the other blocks it is listed as "error with another
block"). So a rate-limit error on the one task a baseline did not fix cannot hand it a win. The
result for since-cutoff's own notes keeps every pair it can count, as without `--compare`; when
errors leave some of those out, the result card adds their rates on the shared pairs.

The result card adds a line per baseline: its size in tokens (about four characters per
token), held-out correct without -> with, changes fixed with its 95% CI, and changes broken.
report.md adds a table with the same for every block, the pairs it did not count and why, the
regression check, and the changes that only since-cutoff's notes or only the baseline fixed,
with an exact sign test; results.json adds `arms`, and each held-out answer records the `arm`
it saw. `--compare` needs held-out tasks, so it cannot be combined with `--no-fix` or
`--heldout 0`.

## What "verified" means

since-cutoff does not use the word "verified" on its own. Each note carries a tag that says what
was checked, and nothing else is claimed:

| tag | what was checked | what was not |
|---|---|---|
| `[diff]` | The change is in a static comparison (griffe) of the public APIs of two releases: the latest release on or before the model's training cutoff, and the version your project pins. The sources are read, not imported. With `[diff]` alone, no replacement is named: the note says what the library's own deprecation text says ("there is no replacement for `resume_download`"), or that since-cutoff found no replacement in it. | Behaviour, and whether a call still runs: the pinned release may still accept a removed parameter with a warning, as huggingface-hub 2.0.0 does for `resume_download` ([_validators.py](https://github.com/huggingface/huggingface_hub/blob/v2.0.0/src/huggingface_hub/utils/_validators.py#L171-L191)). The terminal, report.md, the MCP tools and the JSON add a "Runtime:" line when the pinned source still handles one; the block does not, since the advice is the same. Whether your model gets it wrong. |
| `[diff + library]` | As `[diff]`, and the library's own deprecation text (a docstring, a parameter's docstring entry, an `@deprecated` message or a `warnings.warn` text, in the older release or, for a deprecation, in the pinned one) states the replacement ("Use `stop` instead"), and that name exists in your pinned version. Text that only mentions a name as advice is quoted under `[diff]`, not taken as a replacement. | That the replacement behaves the same. |
| `[diff + move checked]` | As `[diff]`, and the object at the new path is the same object as far as can be counted: a class or module keeps at least half of the old one's public names, a function keeps its parameters, a value is the same. | Behaviour. |
| `[diff + metadata]` | The older release's Requires-Dist (its wheel's METADATA) lists a library the pinned one does not, and places in the public API that named that library's types (parameters, return types, attributes, base classes, re-exports) name the types of another library the pinned release requires, or of a copy of the old one it ships, with none of the old library left: openai 3.x, anthropic 1.8, huggingface-hub 2.0 and mcp 2.2 take `httpx2` objects where they took `httpx` ones. | Behaviour: whether the pinned release still accepts the old library's objects (openai 3 converts some, anthropic 1.8 raises `TypeError`, according to their sources). The terminal, report.md, the MCP tools and the JSON add an "Installed:" line (whether your project, its virtual environment included, still has the old library) and a "Runtime:" line that points to where the pinned source still names it. |
| `[diff; probable rename]` | A parameter in the same position, with the same annotation, has a new name. A guess, labelled as one. | That it is the same parameter. |
| `[type-checked]` | Written by a model during `since-cutoff run` and kept because its example passed the type check below. | Behaviour; that the bullet's explanation is true beyond the names it shows. |
| `[not confirmed]` | Only with `sync --suggestions`: names in the pinned version that look similar to what was removed (the bullet's tag then reads `[diff; not confirmed]`). | That any of them replaces it. |

Tags combine: evidence is joined with `+` (`[diff + library]`), and a guess comes after `;`
(`[diff; probable rename]`, `[diff; not confirmed]`).

Names that merely look similar are never written into the notes by default. The terminal,
report.md and the MCP tools show them as "not confirmed as replacements", and not at all where
the library says there is no replacement; `sync --suggestions` adds them, tagged
`[not confirmed]`.

**The type check behind `[type-checked]`.** A model-written note is kept only when all of these
hold (the model gets two tries):

- its complete example parses and imports the package;
- basedpyright (standard mode, deprecations reported as errors, `# type: ignore` and `# pyright:`
  comments removed) reports no error attributed to that package, and no deprecation of it, when
  the example is checked against the sources of the exact version in your lockfile plus that
  version's runtime dependencies, in an otherwise empty environment;
- every library name the bullet puts in backticks appears in that example or in the API-diff entry
  the note is about;
- the bullet has at most 60 words.

So `[type-checked]` means the imports, names, parameters and argument counts the example uses
exist in your version and are not marked `@deprecated` (PEP 702). Errors outside the package (the
standard library, other libraries) do not block a note. It does not mean that the code behaves
correctly at run time, that the replacement is the one the maintainers recommend, or that the
note helps the model. When a note fails the check, since-cutoff writes the note from the API diff
instead, with its tag.

**Measured** is separate: `since-cutoff run` answers held-out tasks without and with the notes
and reports the counts. Nothing else in since-cutoff says whether a note helps.

**In `run`, an answer counts as correct** when it imports the package, uses the changed API, and
has no "knowledge" error attributed to the package on your version (unknown name, import or
parameter; missing required argument; wrong number of arguments). Pure type-strictness complaints
are ignored. No answer is executed.

The block in AGENTS.md holds the bullets with their tags, and for each package the version its
notes apply to and the release at the cutoff. The rest is in `scan --json` and `results.json`:
`used_apis[]` (each changed API the code uses, where, its changes, its replacements with their
source, and its note with `tags`, `applies_to` and `checks`) and, after `run`, `notes_detail[]`
(each note with the model's example and what the held-out test measured; `verified` is kept,
meaning the same as `checks.example_type_checks`).

## What the numbers mean (and do not mean)

- In `scan`, "**Your code uses N APIs**" counts APIs, not changes: `Messages.create` losing three
  parameters is one API. "**Old form**" is a static name match in the project's files (see
  [Where the code uses a changed API](#where-the-code-uses-a-changed-api)); nothing is run, and a
  file that only calls the API is "uses this API".
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
  `.gitignore`) and, only with `sync` (after it shows the diff and you agree, or with `--yes`)
  or `run --apply`, the marked block in `AGENTS.md`/`CLAUDE.md`. Text outside the markers keeps
  its bytes.
