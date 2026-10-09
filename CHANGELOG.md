# Changelog

## Unreleased

- Check each downloaded wheel or sdist against the sha256 that PyPI lists, skipping the package with a clear error on a mismatch without caching.
- Without `--model`, Gemini CLI's model is detected from `GEMINI_MODEL`, then the project's
  `.gemini/settings.json`, then the user's settings; both string and nested `model.name` forms
  are supported, and unknown aliases are reported instead of guessed.
- Bare and routed Qwen, Kimi, GLM and Llama model ids are recognised as their model makers
  (Alibaba, Moonshot AI, Z.ai and Llama), so cutoff lookup prefers first-party entries over
  reseller listings.
- The MCP server has two prompts, `check_project` and `before_upgrade`, that ask the agent to call
  the existing read-only tools; listing them needs no network or API key.
- `--help` and `--version` piped into a reader that goes away (`since-cutoff --help | head -1`)
  end with exit code 141 and no traceback also when stdout is unbuffered (`PYTHONUNBUFFERED`,
  set in many containers). argparse wrote them itself, past the check for a closed pipe:
  Python 3.10 printed a `BrokenPipeError` traceback and exited with 1, and 3.11 and later
  exited with 0.
- A module that the pinned release ships compiled, with no `.py` source and no `.pyi` stub
  (`fast.py` -> `fast.cpython-312-x86_64-linux-gnu.so`, or a compiled module that lost its
  stub), is no longer reported as removed, nor is what it defines or a name a readable
  module still imports from it (#52). Since only sources are extracted, the diff could not
  tell it from a removal. A name a readable module stopped importing from it is still
  removed, and a package whose `__init__` is compiled hides only its own names, not its
  readable submodules. The source tree now records the compiled modules (extension modules
  in wheels, `.pth` directories included, and Cython `.pyx` files without a `.py` in sdists),
  and the scan, the report and the MCP tools warn that changes to such a module, private
  ones included, and to the names taken from it are not reported. After upgrading, cached
  sources are downloaded and extracted again once (source schema 3) and cached diffs are
  recomputed (diff schema 20).

## 0.5.0 - 2026-09-28

- The skill's `allowed-tools` include `since-cutoff unapply`, which its steps already tell the
  agent to run to remove the notes (found in a catalogue's review of the skill).
- The skill's frontmatter keeps to the six fields of the Agent Skills spec, which claude.ai
  uploads and the Skills API enforce ("Unexpected key(s) in SKILL.md frontmatter:
  argument-hint"): `argument-hint` is gone (Claude Code still hands the skill the arguments of
  `/since-cutoff:since-cutoff scan`, now without the `$ARGUMENTS` placeholder, a Claude Code
  feature the spec does not have), the author and the version are strings under `metadata`
  (`scripts/check_versions.py` checks the version with the other release fields), and
  `compatibility` has no backticks, which skill catalogues read as shell substitution. The body
  has the sections those catalogues look for: Overview, Prerequisites, Instructions (the steps
  as before, plus `sync --scope imported` when `scan` suggests it), Output (what is written
  where, and what each tag says was checked), Error Handling (with the exit codes of `sync` and
  `status`), three Examples and Resources. `skills-ref validate` passes; the tons-of-skills
  marketplace grader gives it 92/100 (A, from 73, C), its remaining errors being the top-level
  `version`, `author` and `tags` that the spec does not allow.
- A dependency the pinned version switched is now a change of its own (DIFF_SCHEMA 19,
  `dependency_switched`, tagged `[diff + metadata]`): openai 3.x, anthropic 1.8, huggingface-hub
  2.0 and mcp 2.2 require `httpx2` and no longer `httpx`, and their public signatures take
  `httpx2` objects; code that hands them `httpx` clients or timeouts, or catches
  `httpx.HTTPError` around huggingface-hub's calls, is flagged with where it does so. A diff of
  the package's own API showed `httpx` only inside other changes' signatures. The diff reads both
  releases' Requires-Dist (the wheels' METADATA, already stored with the sources) and, only when
  the pinned release dropped a requirement, where the public API names that library's types: a
  switch needs places (parameters, returns, attributes, base classes, re-exports) that named the
  dropped library's types and name the types of another library the release requires, or of a
  copy of the dropped one it ships, the same type names at half of them or more, and no trace of
  the dropped library left in the pinned public API. Measured with the first version of the
  rule (base requirements only) on 283 real release pairs: 16 switches (`httpx` -> `httpx2` in
  openai, anthropic, huggingface-hub and mcp; `requests` -> `httpx2` or `httpx` in
  huggingface-hub, litellm and gradio) and none of the rewrites a looser rule counts (crewai's
  langchain -> pydantic).
- The rule also covers a requirement dropped from an extra both releases define ("fastmcp-slim
  4.0.10's `client`, `mcp` and `server` extras require `httpx2` instead of `httpx`"), a copy of
  the dropped library the package ships ("typer 0.27.2 no longer requires `click` and ships
  `typer._click` instead", with typer's own names to use: `typer.BadParameter`, `typer.Context`),
  and a library the older release already required ("gradio 4.28.3 no longer requires
  `requests`; its API names `httpx` types instead"). A private module named after the dropped
  library that imports it is a shim, not a copy: langsmith 0.7.1 -> 0.14.1 stays no switch. A
  switched parameter is counted by the type it names now (openai 3's `http_client`: 8
  signatures take `httpx2.Client`, 6 `httpx2.AsyncClient`), a constructor is recorded under its
  class's path and a classmethod or staticmethod under its class's
  (`fastmcp.FastMCP.from_openapi`), each switched parameter with its position in the call.
- The note is one or two sentences of at most 300 characters: "openai 3.19.2 requires `httpx2`
  instead of `httpx`: `OpenAI(http_client=...)` takes `httpx2.Client` (`httpx2.AsyncClient` for
  `AsyncOpenAI`), `OpenAI(timeout=...)` takes `httpx2.Timeout` and `OpenAI(base_url=...)` takes
  `httpx2.URL`. Use `httpx2` there, not `httpx`." The Requires-Dist entries, how many places
  switched and more of them (with how many other signatures name the same type) are in a
  "Places:" line of `scan --all` and report.md, and in the MCP tools.
- Code uses a switched dependency in the old form only where a file hands the package a value
  that reads the old library: by keyword or by position, at a switched parameter of a switched
  constructor, function, or classmethod or staticmethod called on its class
  (`OpenAI(http_client=httpx.Client())`, or `OpenAI(http_client=c)` after `c = httpx.Client()`
  or with `c: httpx.Client`; `hf_raise_for_status(httpx.get(url))`, the way huggingface-hub's
  own code calls it; `FastMCP.from_openapi(spec, client=httpx.AsyncClient())`), or by catching
  a switched type of it around a call into the package. Reading its names or importing it next
  to the package is "uses this API": `OpenAI(timeout=30.0)` next to an unrelated `httpx.get` or
  `httpx.Client()` hands nothing over, and an old object passed to an instance method is not
  counted. The switch comes first in `sync --scope imported`, `scan --all` and the MCP tools (a
  new first section, "Dependencies switched", in `api_changes`, whose `symbol` matches the
  libraries, the switched type names, parameters and callables), and `run` does not probe it
  yet. The terminal, report.md, the MCP
  tools and the JSON add an "Installed:" line (whether the project's lockfile or pins, or else
  its virtual environment, transitive distributions included, still have the old library,
  `used_apis[].installed`; 12 of the 36 pins of the AI stack example require `httpx`) and a
  "Runtime:" line pointing to where the pinned source still names it (`openai/_httpx2.py:27`;
  a name a module only exports, in `__all__`, a module-level `__getattr__` or `__dir__`, is not
  one, nor is a package name in a list of requirements or a documentation example, such as
  fastmcp 4's `Field(examples=[["fastmcp>=2.0,<3", "httpx", "pandas>=2.0"]])`): what a library
  does with the old objects differs (openai 3 converts some, anthropic 1.8 raises `TypeError`,
  according to their sources), so the note does not say.
- The header of the notes block explains `[metadata]` when a note has it: "the two releases'
  declared requirements (Requires-Dist in their wheels' METADATA)". A notes block puts a
  package's switched dependency first, and the "old form" legend says what the pinned release
  did: "switched to another library for the types it uses".
- README: the Results list links the benchmark (360 Claude Code sessions, since-cutoff 0.4.1's
  notes against Claude Code alone on the 17 post-cutoff tasks: cost ratio 0.80, 95% CI
  0.70-0.90) to its write-up and repository, with the Context7 arms' quota caveat.

## 0.4.1 - 2026-09-28

- A parameter removed from an SDK method that still takes `extra_body` or `extra_query`
  (anthropic, openai and other Stainless-generated clients) no longer gets "do not pass them":
  "`Messages.create()` no longer accepts `temperature`, `top_k` or `top_p` as keyword
  arguments. If the API still needs them, pass them through its `extra_body` or `extra_query`
  argument." The parameter left the signature, which a static diff can see; whether the API
  still takes the field, it cannot. In the benchmark pilot, every agent given the old note
  followed it and dropped the `temperature=0` its task asked for. The diff records which of the
  two a removed parameter's method takes (DIFF_SCHEMA 17, `request_extras`), because the
  recorded signature is cut at 400 characters and anthropic 1.8's `create()` is longer.
- `sync --scope imported` puts first what an assistant writing new code against a package is
  most likely to run into: import paths that no longer work (a module or package that moved, a
  name the package exported at its top level that was removed), then changes with a known
  replacement (one the library's own text names, `[diff + library]`, or a rename or move the
  diff checked, `[diff + move checked]`), then removals of public classes and functions, then
  parameters removed or now required and members removed, then deprecations. The changes
  `scan --all`, the Markdown summary and the MCP tool `project_changes` list per package follow
  the same order. Measured on mcp 1.28.1 -> 2.2.0, the five notes were parameter removals on
  `ClientSession`, `add_response_router`, `McpError` and `mcp.client.experimental`; they are now
  `McpError` -> `MCPError`, the `mcp.server.fastmcp` package -> `mcp.server.mcpserver`,
  `FastMCP` -> `MCPServer`, `streamablehttp_client` (the library names `streamable_http_client`)
  and `ClientSession.list_prompts(cursor=)` (the library names `params`). The `used` scope keeps
  its ranking.
- A subpackage whose modules all moved to one new parent (three of them, or every module that
  left it), leaving nothing public behind, is one change in the notes and in the scan: "The
  package `mcp.server.fastmcp` moved to `mcp.server.mcpserver`; import from there. [diff + move
  checked]", in place of one note per module. The individual moves stay in results.json. Code
  that imports anything from the old package uses the change. The diff records, on each
  module's move, how many public names the old package still has in the new version
  (`move_evidence.left_behind`; 0 for mcp 2's `mcp/server/fastmcp.py`, a shim with no API): a
  module that stayed makes no change of its own, so the moves alone could not tell, and a
  package with a module left behind is not moved whole (`from pkg.a import w` still works;
  the modules that left are listed each on its own, and that code uses no change).
- The diff finds a class or function that came back under another name (DIFF_SCHEMA 16): a new
  object in the same module, or in the module the old one moved to, that keeps the old one's
  public members (at least 80% and at least 5, or all of fewer than 5; a name that differs in
  case alone with every member kept) and that alone does. The note says both names: "`FastMCP`
  is now `MCPServer`: `mcp.server.FastMCP` moved to `mcp.server.MCPServer`; import it with `from
  mcp.server import MCPServer`. [diff + move checked]". Not for a tiny class, for a type of
  fields alone unless the names are related, when two candidates qualify, when the candidate
  existed before, or when the library's own text names the replacement (that stays
  `[diff + library]`). A class keeps a member only when it defines it itself or inherits it
  from a base the old class did not have: a new sibling subclass of the same base, which
  inherits what the old one overrode, is not the rename (a module of handlers or resources
  dropping one subclass and adding another). Names unrelated to the old one need five such
  members (`Task` -> `Worker` on `run`, `close` and `start` is none). On anthropic 0.115.0 ->
  1.8.0, huggingface-hub 1.21.0 -> 2.0.0, openai 2.44.0 -> 3.19.2, pandas 2.3.2 -> 3.0.6 and
  langchain-core 0.3.72 -> 1.6.5 it finds none; on mcp 1.28.1 -> 2.2.0 `FastMCP` ->
  `MCPServer` (29 of 30) and `McpError` -> `MCPError`. Cached diffs are computed again.
- A module-level name bound to a method of an instance the module makes (huggingface_hub 1's
  `api = HfApi()` and `duplicate_space = api.duplicate_space` in `hf_api.py`) says which method
  (`alias_of`, DIFF_SCHEMA 16) and takes the method's deprecation text. When both went, the
  notes and the scan list them as one API, under the name: "`huggingface_hub.duplicate_space`
  was removed; do not use it. Use `HfApi.duplicate_repo` instead. [diff + library]", where
  `--scope imported` wrote "found no replacement" for the name and cut the method's note, which
  named it, at the budget; code using either form uses it. On huggingface-hub 1.21.0 -> 2.0.0
  three of the five default notes now name the library's replacement.
- Within a tier of the `--scope imported` order, classes, functions and modules come before a
  name that is only no longer re-exported (the object is still there, in a module below the one
  it was imported from: langgraph 1's `langgraph.pregel.merge_configs` is
  `langgraph.pregel.main.merge_configs`; such a move ranks with removals, after them, not as a
  known replacement) and before constants and type aliases (anthropic 1.8's `AI_PROMPT`,
  `ProxiesTypes`; `from anthropic import HUMAN_PROMPT` breaks too, but the modules that moved
  out of `anthropic.types.beta` come first).
- `sync --per-package 0` is rejected ("must be 1 or more"): it silently meant the default.
- New `sync --per-package N`: how many APIs `--scope imported` notes per package (default 5);
  a package moved whole counts as one. The block records a choice other than the default, and
  later syncs keep it, as with `--suggestions`.
- When `sync` has no notes to write, it says how many changes `--scope imported` would write,
  and for how many packages.

## 0.4.0 - 2026-09-28

- New `since-cutoff sync` writes the notes from the API diff for the changed APIs your code
  uses into a since-cutoff block in AGENTS.md (or CLAUDE.md), and keeps them in step: run
  again after a lockfile or code change, it adds notes for newly used APIs, checks a bumped
  package again, and drops the notes of a package that is no longer a dependency, is no newer
  than the release at the cutoff, or whose changed APIs your code no longer uses. It prints a
  unified diff of the file first and asks before writing (`--yes` writes without asking;
  without a terminal to ask in, it writes nothing and exits with code 3). Text outside the
  block keeps its bytes, CRLF line breaks included. No model is called.
- `sync` keeps the model and cutoff the block was written for (so teammates whose agents use
  other models do not rewrite it back and forth), unless `--model` or `--cutoff` is given;
  `--model a,b` uses the earliest of their cutoffs and names both. It keeps a
  `[type-checked]` note that `since-cutoff run --apply` wrote, in place of the note from the
  diff for the same API, while its package keeps the same version and the code still uses that
  API (the block's meta line records which API each such note is about); after a version
  change the note from the diff takes its place, and sync says that `since-cutoff run --only
  <pkg>` tests the model again (it does not suggest `run --apply`, which writes a block of that
  run's notes alone). When the notes are unchanged, the block is left exactly as it is, even if
  another since-cutoff version wrote it. A block 0.3 wrote is upgraded, keeping its model.
- `sync --check` writes nothing and exits with code 3 when the notes are out of date, and says
  why ("AGENTS.md is out of date: anthropic 1.8.0 in the notes, 1.9.2 in uv.lock; ...");
  `--dry-run` shows the diff and exits with 0. A block edited by hand is not replaced: `sync`
  shows the diff and exits with the new code 4 unless `--force` is given. When a package the
  notes are about cannot be checked (PyPI unreachable), `sync` writes nothing (exit code 1).
  A bullet whose API had one before is counted as changed ("toylib 2.0: 1 changed"), and after
  `sync --force` replaces a hand edit, sync says only that.
- The block does not depend on which of an API's changes the code uses: a note lists its
  parameters in the order of the old signature, and a package's notes are in the order of
  their APIs. Code that starts or stops passing a removed parameter no longer rewords the note
  (so `sync --check`, the pre-commit hook and the Action's `check-notes` no longer fail on
  unchanged facts).
- `sync --scope imported` also notes the changes most likely to matter in each changed package
  your code imports (up to 5 APIs per package), for code that uses none of the changed APIs
  yet; `sync --suggestions` adds the similar names, tagged `[not confirmed]`. The block records
  both, and the next `sync` keeps them.
- New `since-cutoff status` says, without the network, whether the notes are current for the
  lockfile: per package, the version the notes are for and the one the lockfile has; whether
  other dependencies changed (the block's hash of them); and where the versions come from,
  naming every file ("the versions in requirements-dev.txt, requirements.txt"). Exit code 3 when
  a block is out of date as `sync --check` would find it offline, including a block that
  since-cutoff 0.3 or `run --apply` wrote, which sync rewrites; not when there is no block, which
  a project whose code uses no changed API never gets (`sync --check` says whether notes are
  needed). When the model your coding agent is set up with has an earlier training cutoff than
  the notes (they may then miss changes), `status` says so with the `sync --model` command for
  it; that is not a failure, since sync keeps the block's model. `--json` for scripts. `--hook`
  prints one line only when the notes are out of date, and always exits with 0.
- The Claude Code plugin runs `since-cutoff status --hook` when a session starts, so the
  session is told when the notes are out of date (the first time, uvx downloads since-cutoff).
  The plugin installs from the repository, so this hook ships with the release that has
  `status`: until then it waits in `hooks/hooks.json.in`, and `scripts/check_versions.py` fails
  a release before 0.4.0 that has `hooks/hooks.json`, and one from 0.4.0 on that does not.
- New pre-commit hooks: `since-cutoff-sync` updates the notes when a lockfile, requirements
  file, pyproject.toml, AGENTS.md or CLAUDE.md changes (it fails by changing the file; with
  `args: [--check]` it only checks), and `since-cutoff-status`, the offline check.
- The GitHub Action's new `check-notes` input runs `sync --check` and fails the job when the
  notes are out of date or were edited by hand; the new `notes` output says which. For a
  project with no block yet, it checks against the `cutoff` input too, as the scan does.
- The notes go where a block already is: `run --apply` and `sync` update the block in
  AGENTS.md and in CLAUDE.md, whichever has one, instead of adding a second block to AGENTS.md
  when the first was written to CLAUDE.md. A first block goes where it did in 0.3 (AGENTS.md,
  or CLAUDE.md when only that one exists); issue #13 covers projects with both. Until then,
  when the notes go to AGENTS.md and there is a CLAUDE.md that does not import it, `scan` and
  `sync` say "CLAUDE.md does not import AGENTS.md, so Claude Code does not read the notes in
  AGENTS.md: add a line `@AGENTS.md` to CLAUDE.md".
- The block is appended after a blank line, also to a file whose last line has no line break
  (the meta line records the line break since-cutoff added, and `unapply` takes it away again,
  byte for byte). A file that ends right after the end marker, without a line break, stays so
  and is up to date (`sync --check` failed on it for ever). A byte order mark before the start
  marker (Windows editors) is read and kept: `sync`, `status` and `unapply` failed with
  "unbalanced or repeated since-cutoff markers". `--target` into a folder that does not exist
  creates it, and a file that cannot be written is an "error: cannot write ..." (exit code 1),
  not a traceback. The header puts only file names in code spans ("the versions in
  `pyproject.toml`, latest on PyPI for 3 unpinned").
- `scan` says "2 notes ready: `since-cutoff sync` writes them to AGENTS.md and keeps them in
  step with uv.lock.", and, when your code uses none of the changed APIs of a package it
  imports, suggests `since-cutoff sync --scope imported`. That scope skips, among the APIs the
  code does not use, a params class's field that mirrors a parameter a callable lost
  (anthropic's `MessageCreateParamsBase.temperature`) and an internal hook, one of whose
  parameters has a type private to the library (sqlalchemy's
  `MappedColumn.declarative_scan_for_composite`).
- Without `--model` and with no model setting, `scan` and `sync` say they use the training
  cutoff of Claude Code's default model (they said "this tests", as `run` does). A warning of
  the scan (an ignored lockfile) is printed once, not two or three times.
- The block's hash of the dependencies (`deps`) counts a dependency nothing pins by its
  declared range, not by PyPI's latest release, so that `status` can compute it offline.
- `scan` leads with your project: "Your code uses 2 APIs that changed after
  claude-sonnet-4-5's training cutoff (2025-07-31)", then, per package ("anthropic 0.60.0 ->
  1.8.0 (0.60.0 was the latest release at the cutoff; uv.lock pins 1.8.0)"), each changed API
  your code uses: what changed, "old form" or "uses this API", the files that use it (at most
  3; `-v` lists all), its note with the tag, the "Runtime:" line and the names that merely look
  similar. Then how many notes are ready, what the labels and tags mean, and one line for the
  rest: "Also changed, not used by your code: 326 changes in 7 packages (huggingface-hub 144,
  ...). `--all` lists them." `scan --all` prints 0.3's summary panel, dependency table and
  top changes per package after that first section. A dependency first released after the
  cutoff that your code imports gets its own line, with the date of its first release. No
  model is called and no API key is needed, as before.
- "Old form" means the code uses the API as the release at the cutoff allowed and the pinned
  release no longer does: it reads or imports what was removed, moved (its old path) or changed
  kind, uses what is deprecated, or passes a removed or deprecated parameter by keyword. It is a
  static name match; nothing is run. A parameter that is now required, keyword-only or
  positional-only is always "uses this API" for now.
- Where the code uses an API is shown per file for now (`app/main.py`); line numbers are
  issue #8. `FileUse.file` (the file's path relative to the project, with "/"),
  `selection.uses()` and `selection.form()` are the interface they will extend.
- The Markdown summary (`scan --markdown`) starts with "**Your code uses N APIs that changed
  after the cutoff**" and a table (where, API, change, form, replacement), with each file linked
  on GitHub when `GITHUB_REPOSITORY` and `GITHUB_SHA` are set (under the project's folder in
  `GITHUB_WORKSPACE`), then the notes block, folded ("2 notes ready for AGENTS.md
  (`since-cutoff sync` writes them)"). Each package's list of changes is folded now; it was open
  for packages with changes your code uses.
- report.md starts with "## Used by your code" (each used API with the files, the note, the
  runtime caveat), and each change your code uses under "All changes found" says where:
  "· used in `app/main.py`".
- results.json and `scan --json`: `report_schema` 2; `used` (counts of APIs, changes, old-form
  uses, files, packages, and the new packages the code imports); each `used_apis` entry gains
  `form`, `used_in`, `locations` (file, kind, names, form, match; `line`, `column` and `code`
  stay null until lines are recorded), `locations_total` and `replacements` (every one the
  note names, each with what it replaces; `replacement` is the first), and each of its changes
  a `form`.
  `used_apis[].versions_from` names the file that pins the package (`pyproject.toml`) where it
  said "pinned". A package first released after the cutoff has `first_released` in `scan`.
  Nothing 0.3 wrote was removed.
- The MCP `project_changes` answer starts with "## Your code uses these changed APIs": each used
  API with the form, at most 3 files, its note, the runtime caveat and the similar names, not
  confirmed as replacements; within the same size limit as before.
- `scan --fail-on changes|used|old-form` exits with code 3 when a dependency changed its API
  after the cutoff, when your code uses a changed API, or when it uses one in the old form, and
  says why ("Exit code 3: --fail-on old-form: your code uses 1 changed API in the old form").
  `--fail-on-changes` stays, as `--fail-on changes`. `--fail-on` can be given more than once
  (`--fail-on used --fail-on old-form`: either fails).
- `scan --annotate github` prints GitHub Actions annotations: a warning on each file that uses a
  changed API in the old form, a notice on the others, escaped as the Actions toolkit does. They
  go to stdout, so `--annotate` cannot be combined with `--json` or `--markdown -` (exit code 2).
- In a log or a pipe, a diff served from the cache no longer prints "diffed 3/8 (openai)": a
  warm scan goes from "Diffing the API of ..." straight to the results.
- "Your code uses" finds the names a package re-exports. `from huggingface_hub import
  hf_hub_download` followed by `hf_hub_download(..., resume_download=True)` is marked, as are
  `import pkg; pkg.fetch(...)` and `from pkg import Client; Client().send(...)` for a `fetch`
  or `Client` defined in a submodule: only the path of the module that defines them counted.
  A change reached under more than six paths (a method removed from a base class that many
  classes inherit) is found under every one of them, not only the six the report lists.
- "Your code uses" is precise enough for "old form", `--fail-on old-form` and the warning
  annotations. A changed parameter counts only when the file passes it to that callable: to a
  call of an imported name, of a name read on an imported module or class, or of a method of
  what the file shows is an instance of the class, or, for a method matched by its attribute
  name (`client.messages.create`), at the end of that same chain; `temperature=` passed to
  another library's `create` in the same file no longer makes anthropic's `Messages.create`
  "old form". An SDK's beta mirror (`client.beta.messages.create`) is another API than the one
  outside `beta`: anthropic 1.8.0's `output_format`, removed from the beta `Messages.create`
  only, is no longer said of `client.messages.create`, and a beta note names its module. A
  removed name is matched under its own paths only: `DeprecatedIn37` and `DeprecatedIn45`, both
  `= CryptographyDeprecationWarning` in cryptography, are two changes, and code importing one
  is not reported as using the other.
- A change listed once for several (a sync method and its async twin, a beta mirror) is
  matched as each of them: `client.messages.create(temperature=...)` marks it even when the
  one listed is `AsyncMessages.create`. A constructor call counts under every name of its
  class: `AsyncClient(timeout=...)`, or the name a package re-exports the class under.
- The API diff records more about each change, in results.json and `scan --json`:
  `import_paths` (every public path that leads to the changed object, re-exports included;
  results.json and `scan --json` list the shortest 5 and `import_paths_total`, since a method of
  a base class can have thousands),
  `hint_source` (where the old version's deprecation text comes from), `move_evidence` (how
  many public names or parameters a moved object kept, which is how the move was checked)
  and, for a removed parameter that the new version still reads by name in a decorator,
  `still_handled_at` (`huggingface_hub/utils/_validators.py:187` for
  `hf_hub_download(resume_download=...)` in huggingface-hub 2.0.0, which drops the argument
  with a warning instead of raising `TypeError`) and `still_handled_text`, what that code says
  of it ("deprecated without replacement").
- A removed parameter's deprecation text is also read from its own docstring entry
  (huggingface-hub 1.21.0's `text_generation(stop_sequences=...)`: "Deprecated argument. Use
  `stop` instead.") and from the old version's `warnings.warn` text
  (`hf_hub_download(resume_download=...)`: "... If you want to force a new download, use
  `force_download=True`."). The `--compare template` notes leave these out, so they stay
  what 0.3 wrote and results stay comparable. The MCP tools say "the old version warned"
  before a `warnings.warn` text.
- Diffs cached by earlier versions are recomputed once (diff schema 14). A change without
  import paths (from an older diff, or made by hand through the Python API) is matched by
  package and name: a path of the same package ending in the same name counts, and the mark
  says "matched by name".
- The MCP `api_changes` tool finds a change under any of its paths
  (`symbol="M7.legacy"` for a method removed from the base class of `M7`).
- `scan` writes notes from the API diff, with no model and no API key, for every changed API
  your code uses: one bullet per API, with all its changes (`Messages.create()` no longer
  accepts `temperature`, `top_k` or `top_p`; the parameters in the order of the old
  signature). The terminal shows each under its API;
  report.md has them under "Notes for AGENTS.md (with their sources)"; `scan --json` and
  results.json have them under `used_apis` (package, versions, the changes, the replacement
  and the note with its tags, the versions it applies to and what was checked) and
  `notes_preview` (the block, its size and the files `sync` would write it to).
- Each note carries a tag that says what was checked, and a replacement is named only with
  evidence: `[diff]` (the static comparison of the two releases' public APIs; "do not pass
  it", and "since-cutoff found no replacement in anthropic's deprecation text", which is what
  it read, right after what it is about; or, where the library's own text says so,
  "huggingface-hub's deprecation text says there is no replacement for `resume_download`", read
  also in what the pinned release's code that still handles the parameter says of it);
  `[diff + library]` (the replacement
  is named in the library's own deprecation text, a docstring, `@deprecated` message or
  `warnings.warn`, and exists in your pinned version: huggingface-hub 1.21.0 ->
  2.0.0's `text_generation(stop_sequences=...)` says "Use `stop` instead of `stop_sequences`",
  sourced "huggingface-hub 1.21.0 huggingface_hub/inference/_client.py"; text that only
  mentions a name as advice, as hub 0.34.3's "If you want to force a new download, use
  `force_download=True`" does, is quoted under `[diff]` and is no replacement, in the note,
  results.json or the Markdown table); `[diff + move checked]`
  (a moved object that keeps the old one's public names or parameters); `[diff; probable
  rename]` (a parameter in the same position, with the same type, under a new name: a guess,
  labelled as one). A note written by the model in `run` is `[type-checked]`. A replacement
  that is a method is named as the note names its API ("Use `BaseChatModel.invoke` instead.");
  a quote is whole sentences, without those that only say "deprecated" or ask to open an issue,
  or none; the same sentence is not said twice. A stubs package's note (`pandas-stubs`,
  `types-*`) says what its declarations no longer have and that type checkers reject it, not
  that the runtime no longer accepts it.
- Names that merely look similar are never written into the notes. The terminal, report.md
  and the MCP tools show them as "similar names in 2.0.0, not confirmed as replacements"
  (0.3 said "similar parameters now" in MCP and "(now `x`?)" in the reports), and not at all
  where the library says there is no replacement ("deprecated without replacement"). A
  parameter the diff found renamed in place reads "probably renamed to `start` (same position
  and type)".
- When your pinned version's source still reads a removed parameter by name, the terminal,
  report.md and the MCP tools add a "Runtime:" line: huggingface-hub 2.0.0 still handles
  `force_filename`, `local_dir_use_symlinks`, `proxies` and `resume_download`
  (`huggingface_hub/utils/_validators.py:178-203`), so calls passing them may run with a warning
  while type checkers reject them. It is not written into the notes, whose
  advice ("do not pass it") is the same either way.
- The notes block has a new format (2). Its first line is unchanged, so 0.3's `unapply` still
  removes it. A meta line follows with the model, cutoff, the file the versions come from, a
  hash of the dependencies and a hash of the block's own text (so a hand edit shows), and the
  header says what the tags mean, names the real file the versions come from (`uv.lock`, not
  "requirements"), says "No library code was run", and each package line gives the version the
  notes apply to and the release at the cutoff: `**anthropic 1.8.0** (0.60.0 at the cutoff)`.
  `notes.parse_block` reads both formats back.
- `run`: a note whose example does not type-check falls back to the note from the API diff
  (with its tag) instead of 0.3's one-line template, and the block `run --apply` writes, and
  measures, is format 2. The `--compare` baselines keep 0.3's block and wording exactly, so
  their numbers stay comparable with 0.3's; the run's own headline numbers are not directly
  comparable with 0.3's. results.json's `notes_detail` gains `tags`, `applies_to`, `checks`
  (including what the held-out test measured for the note) and `replacement`; `verified` stays,
  meaning the same as `checks.example_type_checks`. A fallback note's `source` is now "diff"
  (it was "template").
- The word "verified" is gone from what since-cutoff prints: the CLI description, `--help`,
  stages ("Writing notes for 4 failures and type-checking their examples", "Testing the notes
  on 18 held-out tasks"), the report ("type-checked notes", "Notes for AGENTS.md (with their
  sources)", "Held-out test of the notes"), the package description and the descriptions of the
  Claude Code, Codex and Agent Plugins manifests and of the skill say what was checked.
  A dependency first released after the cutoff is "first released after the cutoff" (it was
  "newer than the model"). The MCP instructions say that packages released after your
  reported training cutoff may be missing from your training data, and that the tools do not
  see behaviour changes or runtime shims (they said "trust ... over your memory").
- The API diff records, per change, the names the library's deprecation text gives that exist
  in the new version (`library_names`), the files the old and new texts were read from
  (`hint_file`, `deprecation_file`) and whether a parameter's suggested new name is a rename in
  place (`renamed`); diff schema 13. Diff schema 14 adds `still_handled_text`.
- Docs: the README starts with what `scan` prints for the sample project (the changed APIs its
  code uses, and the note for each) and one command that needs no API key; the 0.1.0 result card
  moved to a Results section that says its scope (one project, one model, since-cutoff 0.1.0).
  New README sections: "Keep the notes current: sync and status", "Measure your model" and
  "What 'verified' means" (each evidence tag: what was checked and what was not). The CI examples
  use `check-notes`, `sync --check`, the `since-cutoff-sync` hook and `--fail-on old-form`.
  docs/how-it-works.md has a section "Notes without a model: scan and sync" (where the code uses a
  changed API, the note for each API and its evidence, the block and its meta line, sync's rules
  and exit codes, status), and "Fix and verify" is now "Write and test notes", in the diagram
  too. PRIVACY.md says that the locations `scan` shows stay on your machine and that `run`
  prompts never contain your code. The Chinese, Spanish and French READMEs say the same.
  `scripts/demo_svg.py scan` renders the new scan output (docs/img/scan.svg).

## 0.3.2 - 2026-09-27

- A wheel whose METADATA, top_level.txt or .pth file expands to gigabytes no longer fills the
  memory: as with the sources, only the start of each file is read (they were decompressed
  whole, and deflate shrinks a gigabyte of padding to about a megabyte).
- A TLS connection that drops in the middle of a download (`ssl.SSLEOFError: EOF occurred in
  violation of protocol`) is retried like any other network error, instead of ending the scan
  with a traceback.
- When the extracted sources cannot be moved into the cache (a virus scanner holding a file on
  Windows, or another since-cutoff process replacing the same broken copy at the same moment),
  the dependency is skipped with "could not publish the sources", or the other process's copy
  is used, instead of the scan ending with a traceback.
- A download that turns out larger than `--max-download-mb` says "is above the 80 MB download
  limit (--max-download-mb)", as when PyPI lists the size, instead of "network error (response
  larger than 83886081 bytes)".
- `openai-compatible:<model>` works without `OPENAI_API_KEY`, as the README says: a local
  server (vLLM, LM Studio, llama.cpp) needs no key. It failed with "OPENAI_API_KEY is not set".
- A model API that answers with something other than JSON (a web page at a wrong `--base-url`,
  a proxy's error page), or with JSON of another shape (`"message": null` or a list of
  content parts from an OpenAI-compatible server; a list or `"content": null` from a gateway
  in front of the Anthropic API), fails that model call with a clear error instead of ending
  the whole run with a JSONDecodeError, AttributeError or TypeError traceback.
- Claude Code calls that stay overloaded or rate-limited fail right after the third attempt,
  instead of waiting 8 more seconds first.
- A run with a `claude-code` model no longer leaves an empty `since-cutoff-claude-*` folder
  behind in the temporary directory.
- `--model gpt-4.1-2025-04-14`, `o3-2025-04-16` and OpenAI's other dated snapshots find
  their model's training cutoff instead of failing with "unknown model" when models.dev does
  not list that snapshot, and so does an id with both dots and a date
  (`claude-sonnet-4.6-20260101` is `claude-sonnet-4-6`).
- `since-cutoff unapply` gives back the file as it was before `--apply`, byte for byte: it
  added a line break after a last line without one, and dropped trailing spaces (a Markdown
  line break) and blank lines at the end. For that, `--apply` now puts the block right on the
  line after a last line without a line break. A block moved into the middle of the file by
  hand is removed without taking the indentation of the line after it or leaving blank lines
  at the top of the file.
- A hand-edited or damaged uv.lock, poetry.lock, pdm.lock, pylock.toml, Pipfile.lock,
  pyproject.toml or Pipfile with a string, list or number where a table or list belongs is
  read as far as it makes sense, or refused with "could not parse ...", instead of ending the
  scan with an AttributeError or TypeError traceback. `dependencies = "requests"` was read
  as six one-letter dependencies (`r`, `e`, `q`, ...). A uv.lock fork whose markers cannot
  be compared counts, as other markers that cannot be evaluated do.
- With packaging older than 26 on Linux, a requirement marker on the kernel's version
  (`platform_release >= '5'`) no longer ends the scan with `InvalidVersion: '6.5.0-1025-azure'`.
- Code that Python itself cannot parse because it nests too deeply (generated code that joins
  thousands of strings with `+`), in the project or in a package, is skipped like a file with a
  syntax error, instead of ending the scan with a RecursionError.
- Archive members whose names end in a dot or a space (`pkg./x.py`, `.../x.py`) are left out:
  on Windows the first overwrote `pkg/x.py`, and the second made a folder in the cache that
  nothing could delete. No module or package is named like that.
- A model's answer that Python cannot parse, because of a NUL byte (Python 3.10 and 3.11) or
  code nested too deeply (3.13), counts as an answer without valid code, instead of ending the
  run with a ValueError or RecursionError, and again on every later run, since the answer was
  cached before it was read.
- `# type: ignore` and `# pyright: ignore` comments in a model's code are always removed before
  it is type-checked. After a form feed or a U+2028 character anywhere in the code, they stayed
  (and another line could be cut short instead), so they could hide the very error the check
  looks for.
- In results.json, the Wilson interval of 0 of n is exactly `[0, ...]` and that of n of n
  `[..., 1]`: rounding gave 2.8e-17 and 0.9999999999999999, which leave out the rate itself.
- MCP `api_changes` reads a symbol with one closing parenthesis too many
  (`client.messages.create(model=m))`) as the call without it, instead of matching nothing.
- A project gets the same versions whatever machine scans it. Environment markers and
  uv.lock forks were resolved for the machine running the scan: with `--python 3.11`,
  private-gpt's uv.lock gave onnxruntime 1.20.1 (its Windows fork) on Windows and 1.25.1
  elsewhere, and on ARM machines unsloth's declarations for Windows on ARM narrowed the
  ranges. Markers are now tried for Linux (x86-64), Windows (x64) and macOS (Apple silicon)
  alike, and of the forks that match, the newest counts.
- A poetry.lock or pdm.lock that locks a package once per Python range is read as uv.lock
  is: the version for the project's Python (`--python` or `.python-version`). With
  python-poetry's own lock and Python 3.12, rapidfuzz was 3.14.6, the version for Python 3.15
  and later, because the last entry won.
- The project itself, installed for its tests from a Pipfile (Pipenv names it after a hash:
  `e1839a8 = {path = ".", editable = true}`), is no longer reported as a dependency that
  could not be checked.
- The GitHub Action runs `actions/cache` pinned to a commit (v6.1.0), as it already did
  `astral-sh/setup-uv`: moving the `v6` tag can no longer change the code that runs in your
  workflow.

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
