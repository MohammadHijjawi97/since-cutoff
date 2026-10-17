---
title: Troubleshooting
description: Find the cause and next step for since-cutoff messages, skipped packages and MCP problems.
---

# Troubleshooting

Find the message printed by `since-cutoff` below. Replace example package, model and file names
with the ones in your output.

## No dependency could be checked

`No dependency could be checked` means the dependency list was read, but every package was
skipped. Read the per-package reasons immediately above it. For connection failures, check
that PyPI is reachable; corporate proxies may need `HTTPS_PROXY` / `NO_PROXY`, and a company
TLS certificate may need `SSL_CERT_FILE`. These settings are used by
[`net.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/net.py).
For private packages, see [private-index support (#43)](https://github.com/MohammadHijjawi97/since-cutoff/issues/43);
do not paste tokens or internal URLs into a public issue.

After correcting the reported cause, retry `since-cutoff scan`. It needs network access for
uncached package metadata and sources, but makes no model calls. The condition for this message
and exit code 1 is in
[`cli.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/cli.py).

## A package was skipped: download limit or no importable modules

For `MB download limit (--max-download-mb)`, the distribution exceeds the configured size
limit. Inspect the package and reported size before raising `--max-download-mb` to allow it.
For `could not find importable modules in`, the named archive has no discoverable Python
sources or stubs; check that the package/version is the one you intended. Raising a download
limit will not add missing sources. Both checks are in
[`pypi.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/pypi.py).

## Not written: there is no terminal to ask in

`Not written: there is no terminal to ask in` means `sync` could not ask for confirmation,
for example in CI. It writes nothing and exits 3. The rest of the message names the fix:
use `since-cutoff sync --check` to check without writing, or `since-cutoff sync --yes` to
apply an intended update without prompting. Review the proposed diff before permitting writes.
See the [sync exit-code table](https://github.com/MohammadHijjawi97/since-cutoff#keep-the-notes-current-sync-and-status)
and the confirmation path in
[`cli.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/cli.py).

## The since-cutoff block was edited by hand

For a block in AGENTS.md, the printed message starts
`The since-cutoff block in AGENTS.md was edited by hand; sync would replace it`.
The file name follows the target you chose. `sync` and `sync --check` write nothing and exit 4.
Preserve your edits by moving them below the closing `<!-- since-cutoff:end -->` marker, as
the message says. If replacing those edits is intentional, use `since-cutoff sync --force`;
in CI, `--yes` is also needed to permit the write without prompting. The message is built by
`edited_text()` in
[`sync.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/sync.py),
and [`cli.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/cli.py)
handles the exit code and confirmation.

## Unknown model or missing training cutoff

`unknown model '<id>'` is followed by `Pass its training cutoff explicitly`; a known model
without a cutoff instead prints `models.dev has no knowledge cutoff for '<id>'`.
Check `since-cutoff models` for a recognized id, or pass `--cutoff YYYY-MM` or
`--cutoff YYYY-MM-DD` with the model's documented training cutoff. Do not guess the date.
These errors are in
[`models.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/models.py);
the explicit-cutoff path bypasses that lookup in
[`engine.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/engine.py).
Choosing a cutoff does not install or configure the model provider used by `run`.

## An MCP client cannot find uvx or the first call times out

See the [MCP setup and timeout guidance](https://github.com/MohammadHijjawi97/since-cutoff#use-it-from-any-agent-mcp)
for installing `uv`, supplying the full `uvx` path, warming the shared cache with
`since-cutoff scan`, and adjusting the client's tool timeout for the first `project_changes`
call. These are client symptoms; their wording depends on the MCP client.

## Claude Code does not read the notes in AGENTS.md

`CLAUDE.md does not import AGENTS.md, so Claude Code does not read the notes in AGENTS.md`
means the notes were written to AGENTS.md while CLAUDE.md does not import them. Add a line
`@AGENTS.md` to CLAUDE.md to keep one copy, as the rest of the message says.
`CLAUDE.md does not import AGENTS.md, so the notes go to both files` means both files got the
block so that every agent reads it;
`CLAUDE.md imports AGENTS.md, so its since-cutoff block is a second copy of the notes`
means the import was added after that. In either case, `since-cutoff unapply --target CLAUDE.md`
(after adding the import) leaves one copy. See the
[file-selection guidance](https://github.com/MohammadHijjawi97/since-cutoff#faq) for keeping
notes in both files, and `agents_import_tip()` in
[`notes.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/notes.py)
for the condition that prints this tip.

## the `claude` CLI was not found on PATH

The message ``the `claude` CLI was not found on PATH`` means the Claude Code provider could
not locate its executable. Install Claude Code and make it available in the environment
running since-cutoff, or select an already configured provider with `--model`.
[`providers/claude_code.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/providers/claude_code.py)
checks the executable with `shutil.which("claude")` before starting the provider.

## ANTHROPIC_API_KEY is not set

`ANTHROPIC_API_KEY is not set` means the Anthropic HTTP provider cannot find its key in the
current process environment. Set it privately in the environment that runs `since-cutoff run`,
or choose a provider you have already configured. Other HTTP providers name their required
key in the same way; see
[`providers/http_api.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/providers/http_api.py).
Never put the key in a bug report or diagnostic log. `scan`, `sync`, `status` and the MCP
inspection tools do not call a model and need no model-provider key.

## run checks the model's answers with the basedpyright type checker, which is not installed

`run checks the model's answers with the basedpyright type checker, which is not installed: pip install "since-cutoff[run]", or uvx --with basedpyright since-cutoff run`
means the Python environment that runs since-cutoff has no basedpyright. Only `run` type-checks
anything, so basedpyright (with the Node.js it brings, about 180 MB) is the `run` extra in
[`pyproject.toml`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/pyproject.toml),
not a required dependency: `scan`, `sync`, `status` and the MCP server never use it. Install the
extra in that same environment with `pip install "since-cutoff[run]"` (or
`pipx install "since-cutoff[run]"`), or add it to one run without installing anything with
`uvx --with basedpyright since-cutoff run`. `run` stops with this message before the scan and
before any model call, so nothing was spent.
[`checker.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/checker.py)
uses a `basedpyright` executable on `PATH`, else the installed module with that environment's
Python executable.

## No API named '...' changed

`No API named 'Client.send' changed in toylib after claude-sonnet-4-5's training cutoff
(2025-07-31)` comes from `since-cutoff explain` (exit code 1): no changed API of the scanned
packages goes by that name. The message lists the closest names (`Close matches: ...`); give
one of them, or the end of the name (`send`). When a notes block still has a bullet about the
name, it is printed below with `the notes may be out of date`: the pin moved back to a release
before the change, for example, and `since-cutoff status` and `since-cutoff sync` bring the
notes in step. `could not be checked (...)` names a package whose releases or sources could not
be read (often PyPI unreachable), so explain cannot say whether the name changed in it. `N
APIs match '...' by the end of the name: ... Give one of them (exit code 1)` means more than 3
APIs end with that name: give a longer one, or `--package`.
`<name> is not a dependency of this project` means `--package` names a package the project
does not depend on; give `--from` and `--to` to diff it all the same (a name it does not have
then says `No API named '...' changed in <package> between <from> and <to>`). The texts are
in [`explain.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/explain.py).

## explain --from and --to need --package

`explain --from and --to need --package` (exit code 2): the two releases are one package's,
so name it, as in `since-cutoff explain Messages.create --package anthropic --from 0.60.0
--to 1.8.0`. `toylib 1.1 is not newer than 2.0: there is no change to explain` (exit code 1)
means `--to` is not a later release than `--from`; swap them. Both are checked in
[`cli.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/cli.py)
and [`explain.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/explain.py).

## A rule file exists and has no since-cutoff block, or is outside the project

`since-cutoff-toylib.md exists and has no since-cutoff block, so since-cutoff leaves it alone`
comes from `sync --target claude-rules` (or `cursor-rules`, `copilot-instructions`): a file of
the name since-cutoff would write is there, without its markers, so it is yours. Rename or
remove it, then run again. `is not UTF-8 text` is the same for a file in another encoding, and
`holds the notes for toylib, not otherlib's` for a file named for one package that holds
another's block (copied or renamed by hand). `.claude/rules is a link to <path>, outside the
project` means the folder (or the file) is a link into a folder shared between projects;
since-cutoff writes rule files only inside the project, so make the folder a real one there, or
keep the notes in AGENTS.md. `is a hard link` is the same for a file. All of them stop `sync`
before anything is written. The same reasons, ending `so since-cutoff leaves it alone:
status, sync and unapply do not see the notes in it`, are a warning of `status`, `sync` and
`unapply` for such a file that has since-cutoff's markers: its notes are not kept in step,
and a plain `sync` writes AGENTS.md beside it, so two copies may load. Convert it to UTF-8, or
replace the link with a file of its own, then run sync again. The checks are in
[`rules.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/rules.py)
and [`sync.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/sync.py).

## had since-cutoff rule files until commit

`.claude/rules had since-cutoff rule files until commit d7f0a25, which removed the last of
them: ... writes the notes there instead of into AGENTS.md` is a warning of a plain `sync` (or
`sync --check`) about to write a first notes block into AGENTS.md or CLAUDE.md: git's history
shows that the project's notes were in rule files until the last of them went, when its
package had no notes left, and a plain sync no longer knows that. Run the command it gives,
which has the `--model` and `--cutoff` the sync was given, else the removed files' model and
cutoff, to write the notes as rule files again, or go on to keep them in AGENTS.md. The sync
that removed the last file said the same: `No since-cutoff rule files are left in
.claude/rules`, with the model and cutoff it ran for. To keep the format whatever happens,
give `--target` where sync runs. The text is in
[`rules.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/rules.py).

## GitHub reads .github/instructions at the repository's root only

`GitHub reads .github/instructions at the repository's root (<path>) only, so Copilot does
not read the files written here` is a warning of `sync --target copilot-instructions` in a
project that is a folder of a git repository: the files are written, but GitHub's Copilot
reads them only from the repository's root. VS Code reads them when that folder is the
workspace. Run sync from the repository's root, or use `claude-rules` or AGENTS.md for that
folder.

## the notes are in rule files

`the notes are in rule files (.claude/rules), which since-cutoff sync keeps in step; run
--apply writes one block into a file` (exit code 2) comes from `since-cutoff run --apply`
without `--target` in a project whose notes are in rule files: it would write AGENTS.md beside
them. Give `--target AGENTS.md` to write the block there as well. `... is a rule file` is the
same for a `--target` that is a rule file's path. Both stop before any model call; see
`_rules_problem()` in
[`cli.py`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/src/since_cutoff/cli.py).

When reporting a bug, include the command, `since-cutoff --version`, OS/Python versions and the
sanitized diagnostic. Leave out API keys, private package-index URLs and customer source code.
