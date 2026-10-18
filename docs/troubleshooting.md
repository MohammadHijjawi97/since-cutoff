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

When reporting a bug, include the command, `since-cutoff --version`, OS/Python versions and the
sanitized diagnostic. Leave out API keys, private package-index URLs and customer source code.
