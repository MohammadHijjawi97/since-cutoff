---
name: since-cutoff
description: Check which of this project's Python dependencies changed their API after the model's training cutoff, measure which of those changes the model actually gets wrong, and write short, verified notes into AGENTS.md or CLAUDE.md. Use when the user asks whether the model knows their library versions, when code keeps failing on renamed or removed library APIs, or after upgrading dependencies.
argument-hint: "[scan | run] [--apply] [--quick] [--model provider:model]"
allowed-tools: Bash(since-cutoff scan:*), Bash(since-cutoff run:*), Bash(since-cutoff models:*), Bash(uvx --from git+https://github.com/MohammadHijjawi97/since-cutoff since-cutoff:*), Read
license: MIT
---

# since-cutoff

`since-cutoff` is a command-line tool. It does the measuring itself by calling a fresh copy of
the model with no tools and no project context, so **do not answer the probe tasks yourself and
do not guess results**. Run the tool and report what it prints.

## Steps

1. Work from the project root (the directory with `pyproject.toml`, `requirements.txt` or a lockfile).
2. Pick the command. Use `$ARGUMENTS` if the user gave any; otherwise:
   - quick look, no model calls: `since-cutoff scan`
   - full measurement with verified notes: `since-cutoff run --quick`
3. Run it. If `since-cutoff` is not installed, use
   `uvx --from git+https://github.com/MohammadHijjawi97/since-cutoff since-cutoff <args>`
   (or `pipx run --spec git+https://github.com/MohammadHijjawi97/since-cutoff since-cutoff <args>`).
   A full run makes many model calls and can take 5-20 minutes: tell the user before starting,
   and run it in the background (or with a long timeout) rather than a 2-minute foreground call.
   Everything is cached, so re-running after an interruption resumes quickly.
4. Summarise the result card for the user: the model and its training cutoff, how many
   dependencies changed after the cutoff, and what was stale, with the held-out before/after numbers.
5. Only write notes into the user's files if they asked for it: re-run with `--apply`
   (it writes a marked block into AGENTS.md, or CLAUDE.md if that is the file the project uses).
   `since-cutoff unapply` removes the block again.
6. For the rest of the session, follow the notes in the block: they describe APIs that changed
   after your training data.

The full report is written to `.since-cutoff/report.md`.
