"""Command-line interface.

Importing this module costs little: ``--help``, ``--version`` and ``cache`` need nothing but
argparse, and ``status --hook`` runs at every Claude Code session start. The engine, the
reports, rich and the other modules that take time to import are imported by the commands that
use them, and the few of their names the tests replace (``Engine``, ``detect_model``,
``write_tasks``, ``RichReporter``) stand at module level as :class:`_LazyImport` proxies.
``tests/test_startup.py`` checks what each command imports.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import importlib
import json
import os
import sys
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

from since_cutoff import __version__
from since_cutoff.errors import METAPACKAGE, SinceCutoffError

if TYPE_CHECKING:
    from datetime import date

    from _typeshed import SupportsWrite
    from rich.console import Console
    from rich.text import Text

    from since_cutoff.cache import DiskCache, NamespaceStats
    from since_cutoff.engine import Engine, ModelTarget, Reporter, RunResult, ScanResult, Settings
    from since_cutoff.hosts import detect_model
    from since_cutoff.models import ModelInfo
    from since_cutoff.progress import RichReporter
    from since_cutoff.project import Project
    from since_cutoff.sync import DetectedModel, TargetFile
    from since_cutoff.taskfile import write_tasks

    # A target file, the model and cutoff its block is written for, its scope, --suggestions
    # and --per-package.
    _Plan = tuple[TargetFile, ModelTarget, str, bool, int]


class _LazyImport:
    """A callable from a module that is imported when it is first called, so that the
    commands which do not need it do not pay for the import. The name stays a module attribute
    of ``cli``, which is where the tests replace it (``monkeypatch.setattr(cli, "Engine",
    ...)``)."""

    def __init__(self, module: str, name: str) -> None:
        self.module, self.name = module, name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return getattr(importlib.import_module(self.module), self.name)(*args, **kwargs)


if not TYPE_CHECKING:
    Engine = _LazyImport("since_cutoff.engine", "Engine")
    RichReporter = _LazyImport("since_cutoff.progress", "RichReporter")
    detect_model = _LazyImport("since_cutoff.hosts", "detect_model")
    write_tasks = _LazyImport("since_cutoff.taskfile", "write_tasks")

COMMANDS = ("run", "scan", "sync", "status", "models", "cache", "unapply", "mcp")
# ``scan --fail-on``: what makes it exit with code 3 (report.fail_reason).
FAIL_ON = ("changes", "used", "old-form")
# ``sync --per-package``'s default: since_cutoff.notes.IMPORTED_APIS, copied so that building
# the parser (--help) does not import notes and the API diff; test_startup.py keeps them equal.
IMPORTED_APIS = 5
# ``--cutoff-margin``'s default and environment variable: since_cutoff.models'
# DEFAULT_CUTOFF_MARGIN and CUTOFF_MARGIN_VAR, copied so that building the parser does not
# import models (and the network code with it); test_startup.py keeps them equal.
DEFAULT_CUTOFF_MARGIN = 30
CUTOFF_MARGIN_VAR = "SINCE_CUTOFF_CUTOFF_MARGIN"
EXAMPLES = """examples:
  since-cutoff                          probe your coding agent's model on this project
  since-cutoff scan                     the changed APIs your code uses, with a note for each
                                        (no model calls; --all lists every change)
  since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown - >> "$GITHUB_STEP_SUMMARY"
  since-cutoff scan --annotate github --fail-on old-form   annotate and fail a CI job
  since-cutoff scan --sarif since-cutoff.sarif   for GitHub code scanning (upload-sarif)
  since-cutoff scan --gitlab-codequality gl-code-quality.json   for GitLab merge requests
  since-cutoff scan --cutoff 2025-03   list API changes since a date, with no model involved
  since-cutoff sync                     write the notes into AGENTS.md (shows the diff and asks
                                        first) and keep them in step with the lockfile
  since-cutoff sync --check             exit 3 when the notes are out of date (CI, pre-commit)
  since-cutoff status                   are the notes current for the lockfile? (offline)
  since-cutoff run --apply              probe the model, then write notes (model-written ones
                                        only if their example type-checks)
  since-cutoff run --quick --model openai:gpt-5.4
  since-cutoff run --compare template   compare the notes with a baseline built without a model
  since-cutoff run --model openai-compatible:my-model --base-url http://localhost:8000/v1
  since-cutoff models sonnet            show known models and their training cutoffs
  since-cutoff cache info               the size of the cache by kind, and where it is
  since-cutoff cache clear --sources    remove the extracted sources only (also --diffs, --pypi)
  since-cutoff mcp                      serve the read-only tools to coding agents over MCP (stdio)

exit codes: 0 ok, 1 error (including: no model answer could be scored), 2 usage error,
            3 stale API use found with --fail-on-stale, or with scan --fail-on: API changes
            (changes, also --fail-on-changes), changed APIs your code uses (used), or uses of
            them in the old form (old-form); sync --check and status: the notes are out of
            date (sync: out of date and not written),
            4 sync: the notes block was edited by hand; nothing written without --force,
            141 the output was closed early (e.g. piped into head)
"""
# The exit code when the reader of the output goes away (``since-cutoff scan | head``): the
# code a shell reports for a writer stopped by SIGPIPE.
OUTPUT_CLOSED = 141
# Output width when it goes to a file, a pipe or a CI log: wide enough that the tables are not
# squeezed into rich's default of 80 columns (COLUMNS still sets it).
LOG_WIDTH = 160


# ------------------------------------------------------------------ output
class OutputClosed(Exception):
    """stdout or stderr went away mid-run: a pipe whose reader stopped (``| head``)."""

    def __init__(self, stream: str) -> None:
        super().__init__(f"{stream} was closed")
        self.stream = stream


class _Output:
    """``sys.stdout`` or ``sys.stderr``, looked up at every use (as rich does), where a write to
    a closed pipe raises :class:`OutputClosed` instead of an OSError that would end in a
    traceback. POSIX reports a closed pipe as EPIPE; Windows as EINVAL."""

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def stream(self) -> IO[str]:
        return cast(IO[str], getattr(sys, self.name))

    def write(self, text: str) -> int:
        try:
            return self.stream.write(text)
        except OSError as exc:
            raise self._closed(exc) from exc

    def flush(self) -> None:
        try:
            self.stream.flush()
        except OSError as exc:
            raise self._closed(exc) from exc

    def _closed(self, exc: OSError) -> Exception:
        return OutputClosed(self.name) if exc.errno in (errno.EPIPE, errno.EINVAL) else exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


STDOUT = _Output("stdout")
STDERR = _Output("stderr")


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose help, usage, ``--version`` and errors go through STDOUT and
    STDERR. argparse writes them itself, so with an unbuffered stream (PYTHONUNBUFFERED, common
    in containers) a closed pipe shows up at its write, not at the last flush that :func:`main`
    turns into OUTPUT_CLOSED: Python 3.11 and later ignore the error there and exit with 0,
    3.10 lets it out as a traceback. Subcommands get the same class."""

    def _print_message(self, message: str, file: SupportsWrite[str] | None = None) -> None:
        if message:
            out: SupportsWrite[str] = file if file is not None else STDERR
            if out is sys.stdout:
                out = STDOUT
            elif out is sys.stderr:
                out = STDERR
            out.write(message)


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _interactive(stream: Any) -> bool:
    """Whether ``stream`` is a terminal someone watches, where live progress makes sense.

    On Windows the NUL device claims to be a TTY (isatty() is True, ``> NUL`` or Git Bash's
    ``> /dev/null``) but is no console: it takes neither the progress bar nor its characters.
    """
    if not _isatty(stream):
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(ctypes.c_ulong())))
    except (AttributeError, OSError, ValueError):
        return False


def _console(output: _Output) -> Console:
    """A rich console for stdout or stderr that suits where it goes: a terminal as usual; a
    file, pipe or CI log without terminal features and :data:`LOG_WIDTH` columns wide."""
    from rich.console import Console

    options: dict[str, Any] = {}
    if not _interactive(output):
        if _isatty(output):
            options["force_terminal"] = False  # Windows' NUL device
        if not os.environ.get("COLUMNS"):
            options["width"] = LOG_WIDTH
    return Console(
        file=cast(IO[str], output), highlight=False, soft_wrap=True, emoji=False, **options
    )


def _escape(text: str) -> str:
    """``rich.markup.escape``, imported with the first rich console."""
    from rich.markup import escape

    return escape(text)


def _error(message: str) -> None:
    _console(STDERR).print(f"[red]error:[/red] {_escape(message)}")


def _usage_error(message: str) -> int:
    _error(message)
    return 2


# ------------------------------------------------------------------ parsing
def _csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _arms(value: str) -> list[str]:
    """``--compare``: baseline arms, comma-separated; ``none`` for no baseline."""
    from since_cutoff.baselines import BASELINE_ARMS

    arms = list(dict.fromkeys(_csv(value)))
    choices = (*BASELINE_ARMS, "none")
    unknown = [a for a in arms if a not in choices]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown baseline '{unknown[0]}' (choose from {', '.join(choices)})"
        )
    if "none" in arms and len(arms) > 1:
        raise argparse.ArgumentTypeError("'none' cannot be combined with a baseline")
    return [a for a in arms if a != "none"]


def _positive(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value}") from None
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def _at_least_one(value: str) -> int:
    """A count that 0 would leave undefined (``--per-package 0`` is no budget)."""
    n = _positive(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


# ``scan`` and ``sync`` --include-name-matches (selection.NAME_ONLY).
_NAME_MATCHES_HELP = (
    "also show the uses matched by a method's name alone, on a value whose class the code "
    "does not show (tagged [name match]): the reports list them, their notes are written, and "
    "scan's --fail-on and --annotate see them. Off by default (no report has them), and not "
    "kept by the block: give it on every sync"
)


def _margin_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--cutoff-margin",
        type=_positive,
        metavar="DAYS",
        help="compare from the latest release published this many days before the cutoff "
        f"(default: {DEFAULT_CUTOFF_MARGIN}, or {CUTOFF_MARGIN_VAR}; models know the weeks "
        "before their cutoff least; 0 compares from the latest release at the cutoff itself)",
    )


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "path", nargs="?", default=".", help="project directory (default: current directory)"
    )
    p.add_argument(
        "--model",
        help="model to test, as provider:model (default: the model your coding agent is set up "
        "with: SINCE_CUTOFF_MODEL; inside Claude Code, Claude Code's model; elsewhere the "
        "Claude Code, Codex, Gemini CLI, OpenCode and Aider settings, the project's before the "
        "user's; else "
        "claude-code, Claude Code's default; scan with --cutoff alone uses no model). "
        "Providers: claude-code, anthropic, openai, openrouter, deepseek, ollama, openai-compatible",
    )
    p.add_argument(
        "--base-url", help="API base URL (for openai-compatible, or to override a provider's)"
    )
    p.add_argument("--cutoff", help="override the model's training cutoff (YYYY-MM or YYYY-MM-DD)")
    _margin_flag(p)
    p.add_argument(
        "--all-deps",
        action="store_true",
        help="include transitive dependencies, not only direct ones",
    )
    p.add_argument(
        "--only",
        action="extend",
        type=_csv,
        default=[],
        metavar="PKG[,PKG]",
        help="only these packages (repeatable or comma-separated)",
    )
    p.add_argument(
        "--exclude",
        action="extend",
        type=_csv,
        default=[],
        metavar="PKG[,PKG]",
        help="skip these packages (repeatable or comma-separated)",
    )
    p.add_argument(
        "--python",
        dest="python_version",
        help="Python version for type checking (default: project's)",
    )
    p.add_argument(
        "--max-download-mb",
        type=float,
        default=80.0,
        help="skip packages whose wheel is larger (default: 80)",
    )
    p.add_argument(
        "--out",
        default=".since-cutoff",
        help="report directory, relative to the project (default: .since-cutoff)",
    )
    p.add_argument("--json", action="store_true", help="print the full JSON result to stdout")
    p.add_argument(
        "-v", "--verbose", action="store_true", help="show every dependency and error detail"
    )
    p.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="since-cutoff",
        description="Find which dependency APIs your code uses changed after your coding model's "
        "training cutoff, and write short AGENTS.md notes from the API diff, each with its source.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument("--version", action="version", version=f"since-cutoff {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run", help="scan, probe the model, write notes and test them on held-out tasks (default)"
    )
    _common(run)
    run.add_argument(
        "--max-probes", type=_positive, default=30, help="API changes to probe (default: 30)"
    )
    run.add_argument(
        "--heldout", type=_positive, default=2, help="held-out tasks per failure (default: 2)"
    )
    run.add_argument(
        "--regression",
        type=_positive,
        default=6,
        help="previously-correct APIs re-checked with notes (default: 6)",
    )
    run.add_argument(
        "--quick",
        action="store_true",
        help="smaller run: 12 probes, 1 held-out task, 3 regression checks",
    )
    run.add_argument(
        "--task-model", help="model that writes tasks and notes (default: the tested model)"
    )
    run.add_argument(
        "--effort",
        choices=["low", "medium", "high", "max", "default"],
        default="low",
        help="Claude Code thinking effort for all calls (default: low; 'default' keeps the CLI's own)",
    )
    run.add_argument("--jobs", type=_positive, default=4, help="parallel model calls (default: 4)")
    run.add_argument(
        "--no-fix", action="store_true", help="only measure; do not write or test notes"
    )
    run.add_argument(
        "--apply",
        action="store_true",
        help="write the notes into the agent instructions file",
    )
    run.add_argument(
        "--target",
        help="file to write notes into (default: AGENTS.md and CLAUDE.md where they already have "
        "a since-cutoff block; else AGENTS.md, or CLAUDE.md if only that one exists)",
    )
    run.add_argument(
        "--fresh",
        action="store_true",
        help="ignore cached tasks and answers and ask the model again",
    )
    run.add_argument(
        "--fail-on-stale",
        action="store_true",
        help="exit with code 3 if any stale API use is found (for CI)",
    )
    run.add_argument(
        "--tasks-out",
        metavar="FILE",
        help="save the tasks this run used as JSON, to repeat it with --tasks-from "
        "(relative to the current directory)",
    )
    run.add_argument(
        "--tasks-from",
        metavar="FILE",
        help="use the tasks in FILE (from --tasks-out) instead of asking the task writer; API "
        "changes it does not cover are skipped",
    )
    run.add_argument(
        "--compare",
        type=_arms,
        default=[],
        metavar="ARM[,ARM]",
        help="also answer the held-out tasks with baseline notes built without a model and "
        "compare them with the notes: template (each change stated from the API "
        "diff), signatures (the new signature and docstring of each changed or replacement "
        "API), none (default)",
    )

    scan = sub.add_parser(
        "scan",
        help="the APIs your code uses that changed after the model's cutoff, with a note for each "
        "(no model calls)",
    )
    _common(scan)
    scan.add_argument(
        "--limit", type=_positive, default=8, help="changes shown per package (default: 8)"
    )
    scan.add_argument(
        "--markdown",
        metavar="PATH",
        help="also write a short GitHub-flavoured Markdown summary to PATH, relative to the "
        "current directory ('-' for stdout), e.g. for a CI job summary or a PR comment",
    )
    scan.add_argument(
        "--all",
        action="store_true",
        help="after the changed APIs your code uses, show everything that changed: the "
        "summary, the dependency table and the top --limit changes of each changed dependency",
    )
    scan.add_argument(
        "--internal",
        action="store_true",
        help="list the changes to internal APIs too (not reached from the package's top "
        "level, named in an __all__ or documented), which every count gives apart; implies "
        "--all",
    )
    scan.add_argument(
        "--fail-on",
        choices=FAIL_ON,
        action="append",
        help="exit with code 3 (for CI) when a dependency changed its API after the cutoff "
        "(changes), when your code uses an API that changed (used), or when it uses one in the "
        "old form (old-form); repeat it for several",
    )
    scan.add_argument(
        "--fail-on-changes",
        action="store_true",
        help="the same as --fail-on changes",
    )
    scan.add_argument(
        "--annotate",
        choices=["github"],
        help="also print GitHub Actions annotations to stdout: a warning on each file that uses "
        "a changed API in the old form, a notice on each file that uses one otherwise",
    )
    scan.add_argument(
        "--sarif",
        metavar="FILE",
        help="also write a SARIF 2.1.0 report to FILE, relative to the current directory ('-' "
        "for stdout), for GitHub code scanning (github/codeql-action/upload-sarif): a result "
        "on each file that uses a changed API, a warning for the old form, a note otherwise; "
        "not written when no dependency could be checked",
    )
    scan.add_argument(
        "--gitlab-codequality",
        metavar="FILE",
        help="also write a GitLab Code Quality report to FILE, relative to the current "
        "directory ('-' for stdout), for merge requests (artifacts: reports: codequality): an "
        "issue on each file that uses a changed API, major for the old form, info otherwise; "
        "not written when no dependency could be checked",
    )
    scan.add_argument("--include-name-matches", action="store_true", help=_NAME_MATCHES_HELP)

    sync = sub.add_parser(
        "sync",
        help="write the notes for the changed APIs your code uses into AGENTS.md / CLAUDE.md, "
        "and update them when the lockfile or the code changes (no model calls)",
        description="Write the notes from the API diff for the changed APIs your code uses into "
        "the since-cutoff block of AGENTS.md / CLAUDE.md, or update them: notes for newly used "
        "APIs are added, those of a bumped package checked again, and those of a package that "
        "is no longer a dependency, no longer newer than the release at the cutoff, or no "
        "longer used, dropped. Text outside the block stays byte for byte. It shows the diff "
        "and asks before writing (--yes writes without asking). It keeps the model and cutoff "
        "the block was written for unless --model or --cutoff is given, and the [type-checked] "
        "notes of `since-cutoff run` while their package's version is the same.",
    )
    sync.add_argument(
        "path", nargs="?", default=".", help="project directory (default: current directory)"
    )
    sync.add_argument(
        "--model",
        action="extend",
        type=_csv,
        default=[],
        metavar="PROVIDER:MODEL[,...]",
        help="write the notes for this model's training cutoff instead of the one the block was "
        "written for; several (repeated or comma-separated): the earliest of their cutoffs "
        "(default without a block: the model your coding agent is set up with, as for run)",
    )
    sync.add_argument("--cutoff", help="override the training cutoff (YYYY-MM or YYYY-MM-DD)")
    _margin_flag(sync)
    sync.add_argument(
        "--target",
        help="file to write notes into (default: AGENTS.md and CLAUDE.md where they already have "
        "a since-cutoff block; else AGENTS.md, or CLAUDE.md if only that one exists)",
    )
    sync.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit with code 3 when the notes are out of date (for CI and "
        "pre-commit)",
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="show the diff; write nothing (exit code 0)"
    )
    sync.add_argument("-y", "--yes", action="store_true", help="write without asking")
    sync.add_argument(
        "--json",
        action="store_true",
        help="print proposals and write results as JSON (needs --yes, --check or --dry-run)",
    )
    sync.add_argument(
        "--force",
        action="store_true",
        help="replace a block that was edited by hand (otherwise sync exits with code 4)",
    )
    sync.add_argument(
        "--scope",
        choices=["used", "imported"],
        help="used: notes for the changed APIs your code uses (default); imported: also for "
        "the changes most likely to matter in each changed package your code imports (import "
        "paths that no longer work first, then changes with a known replacement, removals, "
        "members; up to --per-package APIs per package). The block keeps the scope it was "
        "written with",
    )
    sync.add_argument(
        "--per-package",
        type=_at_least_one,
        metavar="N",
        help=f"with --scope imported: APIs noted per package (default: {IMPORTED_APIS}, or what "
        "the block was written with; a package whose modules all moved counts as one)",
    )
    sync.add_argument(
        "--suggestions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="also write into the notes the names in the pinned version that merely look "
        "similar to what was removed, tagged [not confirmed] (off by default; the block keeps "
        "the choice)",
    )
    sync.add_argument(
        "--max-download-mb",
        type=float,
        default=80.0,
        help="skip packages whose wheel is larger (default: 80)",
    )
    sync.add_argument("--include-name-matches", action="store_true", help=_NAME_MATCHES_HELP)
    sync.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)

    status = sub.add_parser(
        "status",
        help="are the notes in AGENTS.md / CLAUDE.md current for the lockfile? (offline)",
        description="Compare the since-cutoff block with the project's dependencies as they are, "
        "without the network: the versions its notes are for, a hash of the other "
        "dependencies, where the versions come from, and the training cutoff of the model your "
        "coding agent is set up with. It does not read your code for new uses of changed APIs: "
        "`since-cutoff sync --check` does. Exit code 3 when a block is out of date (no block "
        "is not: `since-cutoff sync --check` says whether your code needs one).",
    )
    status.add_argument(
        "path", nargs="?", default=".", help="project directory (default: current directory)"
    )
    status.add_argument(
        "--target",
        help="file to check (default: AGENTS.md and CLAUDE.md where they have a since-cutoff "
        "block; else AGENTS.md, or CLAUDE.md if only that one exists)",
    )
    status.add_argument(
        "--hook",
        action="store_true",
        help="for a coding agent's session-start hook: print one line only when the notes are "
        "out of date, nothing otherwise; always exit with code 0",
    )
    status.add_argument("--json", action="store_true", help="print the status as JSON")
    _margin_flag(status)
    status.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)

    models = sub.add_parser("models", help="list known models and their training cutoffs")
    models.add_argument(
        "query", nargs="?", default="", help="filter by id substring, e.g. 'sonnet'"
    )
    models.add_argument(
        "--makers",
        action="store_true",
        help="only the model makers' own listings, not gateways' and resellers'",
    )
    models.add_argument("--json", action="store_true", help="print the models as JSON")
    models.add_argument("--offline", action="store_true", help="use the bundled snapshot only")

    cache = sub.add_parser(
        "cache",
        help="show, measure or clear the cache",
        description="path: where the cache is. info: its size, by kind; tasks, answers and "
        "notes hold what the model answered in `run`, which costs money to make again. clear: "
        "remove it all, or with --sources, --diffs or --pypi only those kinds. The extracted "
        "sources are capped at SINCE_CUTOFF_CACHE_MAX_MB (default 2048; 0 for no cap): over "
        "it, the least recently used ones go, never those a running scan uses.",
    )
    cache.add_argument("action", choices=["path", "info", "clear"])
    cache.add_argument(
        "--sources", action="store_true", help="clear: only the extracted package sources"
    )
    cache.add_argument("--diffs", action="store_true", help="clear: only the API diffs")
    cache.add_argument("--pypi", action="store_true", help="clear: only the PyPI metadata")

    unapply = sub.add_parser(
        "unapply", help="remove the since-cutoff block from AGENTS.md / CLAUDE.md"
    )
    unapply.add_argument("path", nargs="?", default=".")
    unapply.add_argument("--target", help="file to clean (default: AGENTS.md and CLAUDE.md)")

    mcp = sub.add_parser(
        "mcp",
        help="run an MCP server on stdio so coding agents can ask what changed since their "
        "cutoff (no model calls)",
    )
    mcp.add_argument(
        "--max-download-mb",
        type=float,
        default=80.0,
        help="skip packages whose wheel is larger (default: 80)",
    )
    mcp.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    return parser


def _argv_with_default(argv: list[str]) -> list[str]:
    if not argv:
        return ["run"]
    first = argv[0]
    if first in COMMANDS or first.startswith("-"):
        return (
            argv if first in COMMANDS or first in ("-h", "--help", "--version") else ["run", *argv]
        )
    if not Path(first).exists():
        import difflib

        close = difflib.get_close_matches(first, COMMANDS, n=1, cutoff=0.6)
        if close or first.isalpha():
            hint = f" Did you mean '{close[0]}'?" if close else ""
            raise SinceCutoffError(
                f"unknown command '{first}'.{hint} Commands: {', '.join(COMMANDS)}"
            )
    return ["run", *argv]


# ------------------------------------------------------------------- main
def _utf8_streams() -> None:
    """Output must never crash on non-ASCII text (Windows code pages): a file, pipe or device
    gets UTF-8; a terminal keeps its encoding and shows what it cannot encode as '?'."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            if _interactive(stream):
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")


def _discard(stream: str) -> None:
    """Send what is left for a closed stream to the null device, so that Python's own flush
    at exit does not fail on it again ("Exception ignored ... Errno 22", exit code 120)."""
    with contextlib.suppress(AttributeError, OSError, ValueError):
        fd = getattr(sys, stream).fileno()
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, fd)
        os.close(null)


def main(argv: list[str] | None = None) -> int:
    try:
        try:
            return _main(argv)
        finally:
            # A closed pipe can also show up only here, at the last flush (argparse's --help).
            STDOUT.flush()
            STDERR.flush()
    except OutputClosed as exc:
        _discard(exc.stream)
        return OUTPUT_CLOSED


def _main(argv: list[str] | None) -> int:
    _utf8_streams()
    try:
        argv_ = _argv_with_default(list(sys.argv[1:] if argv is None else argv))
    except SinceCutoffError as exc:
        return _usage_error(str(exc))
    args = build_parser().parse_args(argv_)
    if getattr(args, "debug", False):
        import logging

        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    json_mode = getattr(args, "json", False)
    if args.command == "sync" and json_mode and not (args.yes or args.check or args.dry_run):
        return _usage_error("sync --json requires --yes, --check or --dry-run")
    markdown = getattr(args, "markdown", None)
    to_stdout = [
        flag
        for flag, on in (
            ("--json", json_mode),
            ("--markdown -", markdown == "-"),
            ("--sarif -", getattr(args, "sarif", None) == "-"),
            ("--gitlab-codequality -", getattr(args, "gitlab_codequality", None) == "-"),
            ("--annotate", getattr(args, "annotate", None)),
        )
        if on
    ]
    if len(to_stdout) > 1:
        return _usage_error(f"{' and '.join(to_stdout)} cannot both write to stdout")
    problem = _compare_problem(args) or _cache_problem(args) or _report_files_problem(args)
    if problem:
        return _usage_error(problem)
    try:
        if args.command == "cache":
            return _cmd_cache(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "mcp":
            return _cmd_mcp(args)
        out = _console(STDOUT)
        if args.command == "models":
            return _cmd_models(args, out)
        if args.command == "unapply":
            return _cmd_unapply(args, out)
        ui = _console(STDERR) if json_mode or _report_to_stdout(args) else out
        if args.command == "sync":
            return _cmd_sync(args, ui)
        return _cmd_run(args, ui, json_mode)
    except SinceCutoffError as exc:
        _error(str(exc))
        return 1
    except KeyboardInterrupt:
        _console(STDERR).print("[yellow]interrupted[/yellow]")
        return 130


def _report_files_problem(args: argparse.Namespace) -> str | None:
    """Why the report files of ``scan`` cannot be written as asked, if they cannot: ``--sarif``
    or ``--gitlab-codequality`` with no file name (which would write nothing, silently), or two
    reports in the same file (the second would overwrite the first)."""
    reports = [
        (flag, getattr(args, dest, None))
        for flag, dest in (
            ("--markdown", "markdown"),
            ("--sarif", "sarif"),
            ("--gitlab-codequality", "gitlab_codequality"),
        )
    ]
    for flag, target in reports[1:]:
        if target is not None and not target.strip():
            return f"{flag} needs a file name, or '-' for stdout"
    seen: dict[str, str] = {}
    for flag, target in reports:
        if not target or target == "-":
            continue
        key = os.path.normcase(Path(target).resolve())
        if key in seen:
            return f"{seen[key]} and {flag} cannot both write to {target}"
        seen[key] = flag
    return None


def _compare_problem(args: argparse.Namespace) -> str | None:
    """Why ``--compare`` cannot work with the other options, if it cannot: the baselines are
    measured on held-out tasks, which ``--no-fix`` and ``--heldout 0`` leave out."""
    if not getattr(args, "compare", None):
        return None
    if args.no_fix:
        return "--compare measures notes on held-out tasks, which --no-fix skips"
    if args.heldout == 0:
        return "--compare measures notes on held-out tasks; --heldout 0 leaves none"
    return None


def _cache_problem(args: argparse.Namespace) -> str | None:
    """``cache path --sources``: the kind flags choose what ``clear`` removes, nothing else."""
    if args.command != "cache" or args.action == "clear":
        return None
    flags = [f"--{kind}" for kind in ("sources", "diffs", "pypi") if getattr(args, kind)]
    if not flags:
        return None
    return f"cache {args.action} takes no {', '.join(flags)}: the kinds go with clear"


def _cutoff_margin(args: argparse.Namespace) -> int:
    """``--cutoff-margin``, else ``SINCE_CUTOFF_CUTOFF_MARGIN``, else the default: how many days
    before the cutoff the comparison release must have been published."""
    from since_cutoff.models import parse_margin

    given = getattr(args, "cutoff_margin", None)
    if given is not None:
        return int(given)
    value = os.environ.get(CUTOFF_MARGIN_VAR)
    if value is None or not value.strip():
        return DEFAULT_CUTOFF_MARGIN
    try:
        return parse_margin(value)
    except ValueError as exc:
        raise SinceCutoffError(f"{CUTOFF_MARGIN_VAR}: {exc}") from exc


def _settings(args: argparse.Namespace) -> Settings:
    from datetime import date

    from since_cutoff.engine import Settings
    from since_cutoff.models import parse_cutoff
    from since_cutoff.taskfile import read_tasks

    try:
        cutoff = parse_cutoff(args.cutoff) if args.cutoff else None
    except ValueError as exc:
        raise SinceCutoffError(str(exc)) from exc
    s = Settings(
        model=args.model or "claude-code",
        cutoff=cutoff,
        cutoff_margin=_cutoff_margin(args),
        all_deps=args.all_deps,
        include=args.only,
        exclude=args.exclude,
        max_download_mb=args.max_download_mb,
        python_version=args.python_version,
        base_url=args.base_url,
        today=date.today(),
        include_name_matches=getattr(args, "include_name_matches", False),
    )
    if args.command == "run":
        s.task_model = args.task_model
        s.max_probes = args.max_probes
        s.heldout = args.heldout
        s.regression = args.regression
        s.jobs = max(1, args.jobs)
        s.effort = None if args.effort == "default" else args.effort
        s.compare = list(args.compare)
        if args.tasks_from:
            s.tasks_from = read_tasks(Path(args.tasks_from))
        if args.tasks_out:
            _check_tasks_out(Path(args.tasks_out), args.tasks_from)
        if args.quick:
            s.max_probes = min(s.max_probes, 12)
            s.heldout = min(s.heldout, 1)
            s.regression = min(s.regression, 3)
    return s


def _check_tasks_out(path: Path, tasks_from: str | None) -> None:
    """Refuse a ``--tasks-out`` path that cannot be written, before any model call: after the
    run, a failed write would come too late to keep the result from being lost."""
    if path.is_dir():
        raise SinceCutoffError(f"--tasks-out {path} is a folder; give a file name")
    folder = next((p for p in path.absolute().parents if p.exists()), None)
    if folder is not None and not folder.is_dir():
        raise SinceCutoffError(f"cannot write the tasks to {path}: {folder} is a file")
    if tasks_from and path.exists() and path.samefile(tasks_from):
        # The run only keeps the changes it probed, with --heldout tasks each: writing them
        # over the file would drop the rest of it.
        raise SinceCutoffError(
            f"--tasks-out {path} is the --tasks-from file; write the tasks to another file"
        )


def _cmd_run(args: argparse.Namespace, ui: Console, json_mode: bool) -> int:
    from collections import Counter

    from since_cutoff.cache import DiskCache
    from since_cutoff.engine import ERROR, STALE, TASK_WRITER_FAILED, ModelTarget
    from since_cutoff.notes import agents_import_tip, apply_block, block_targets
    from since_cutoff.project import load_project
    from since_cutoff.report import (
        fail_reason,
        github_annotations,
        gitlab_codequality,
        render_console,
        render_scan,
        render_scan_markdown,
        sarif_report,
        to_json,
        write_outputs,
    )

    if args.command == "run":
        # The type checker scores every answer: without it, stop before the scan and the
        # model calls, with the install line (basedpyright is the ``since-cutoff[run]`` extra).
        from since_cutoff.checker import pyright_command

        pyright_command()
    settings = _settings(args)
    project = load_project(Path(args.path), python=settings.python_version)
    if settings.python_version is None:
        settings.python_version = project.python_version
    store = DiskCache()
    llm_cache = DiskCache(store.root, enabled=not getattr(args, "fresh", False))
    reporter = RichReporter(ui)
    _choose_model(args, settings, project.root, reporter)
    engine = Engine(settings, store=store, llm_cache=llm_cache, reporter=reporter)

    if args.command == "scan" and args.model is None and settings.cutoff is not None:
        # A date alone: there is no model to look up, name or guess (and no CLI to ask).
        target = ModelTarget.cutoff_only(settings.cutoff, margin=settings.cutoff_margin)
        ui.print(_model_line(target, "--cutoff; no model given"))
    else:
        target = _resolve_target(engine, allow_calls=args.command == "run")
        ui.print(_model_line(target))
    scan: ScanResult = engine.scan(project, target)
    run: RunResult | None = None
    if args.command == "run" and scan.changed:
        run = engine.run(scan, fix=not args.no_fix)
    reporter.done()

    md, _ = write_outputs(project.root / args.out, scan, run)
    tasks_out = getattr(args, "tasks_out", None)
    summary_md = getattr(args, "markdown", None)
    if summary_md:
        _write_markdown(summary_md, render_scan_markdown(scan, limit=args.limit, env=os.environ))
    # With no dependency checked (PyPI unreachable), the exit code is 1 (below) and neither CI
    # report is written: an empty one would close every open alert when uploaded.
    nothing_checked = bool(scan.packages) and all(p.status == "skipped" for p in scan.packages)
    withheld = [
        what
        for what, target in (
            ("the SARIF report", getattr(args, "sarif", None)),
            ("the GitLab Code Quality report", getattr(args, "gitlab_codequality", None)),
        )
        if target and nothing_checked
    ]
    sarif = None if nothing_checked else getattr(args, "sarif", None)
    if sarif:
        _write_json(sarif, sarif_report(scan, env=os.environ), "the SARIF report")
    codequality = None if nothing_checked else getattr(args, "gitlab_codequality", None)
    if codequality:
        issues = gitlab_codequality(scan, env=os.environ)
        _write_json(codequality, issues, "the GitLab Code Quality report")
    if json_mode:
        STDOUT.write(json.dumps(to_json(scan, run), indent=2, default=str) + "\n")
        STDOUT.flush()
    elif not _report_to_stdout(args):  # else stdout has that report, stderr the progress
        ui.print()
        # A warning printed while scanning (on this same console) is not printed again.
        shown = reporter.warned
        if args.command == "scan":
            render_scan(
                ui,
                scan,
                show_all=args.all or args.internal,
                verbose=args.verbose,
                limit=args.limit,
                shown=shown,
                internal=args.internal,
            )
        else:
            render_console(ui, scan, run, verbose=args.verbose, shown=shown)

    if args.command == "run" and run is not None:
        if run.block:
            if args.apply:
                paths = block_targets(project.root, args.target)
                for path in paths:
                    action = apply_block(path, run.block)
                    ui.print(
                        f"\n[green]✓[/green] {action.capitalize()} {_escape(str(path))} with "
                        f"{len(run.notes)} notes"
                    )
                tip = agents_import_tip(project.root, paths)  # as sync says it (issue #13)
                if tip:
                    ui.print(f"[yellow]![/yellow] {_escape(tip)}.")
            elif not json_mode:
                ui.print(
                    "\n[bold]Notes for AGENTS.md[/bold] [dim](run again with --apply to write them)[/dim]"
                )
                ui.print(run.block, markup=False, highlight=False)
        elif args.apply:
            ui.print("\n[dim]Nothing to apply: no failures needed a note.[/dim]")
    ui.print(f"[dim]Full report: {_escape(str(md))}[/dim]")
    if tasks_out:
        # Last, so that a failed write cannot cost the result card, the JSON or --apply.
        _write_tasks_out(Path(tasks_out), run, settings)
        ui.print(f"[dim]Tasks: {_escape(tasks_out)} (repeat this run with --tasks-from)[/dim]")
    if summary_md and summary_md != "-":
        ui.print(f"[dim]Markdown summary: {_escape(summary_md)}[/dim]")
    if sarif and sarif != "-":
        ui.print(f"[dim]SARIF report: {_escape(sarif)}[/dim]")
    if codequality and codequality != "-":
        ui.print(f"[dim]GitLab Code Quality report: {_escape(codequality)}[/dim]")
    if getattr(args, "annotate", None) == "github":
        # Plain lines, never wrapped: the runner reads each "::warning ..." line as one command.
        STDOUT.write("".join(line + "\n" for line in github_annotations(scan, env=os.environ)))
        STDOUT.flush()

    if nothing_checked:
        ui.print(
            "[red]No dependency could be checked[/red] (see the reasons above; is PyPI reachable?)."
        )
        if withheld:
            what = " and ".join(withheld)
            ui.print(
                f"[dim]{what[0].upper()}{what[1:]} {'were' if len(withheld) > 1 else 'was'} "
                "not written (empty, an upload would show every open finding as fixed).[/dim]"
            )
        return 1
    if args.command == "run" and not scan.changed:
        ui.print(
            "[green]Nothing to probe:[/green] no checked dependency changed its API after the cutoff."
        )
    if run is not None and run.all_errored:
        first = next((a.error for a in run.probes if a.error), "unknown error")
        ui.print(f"[red]Every model call failed[/red]: {_escape(first or '')}")
        return 1
    if run is not None and scan.changed and not run.probes and run.skipped_changes:
        # Nothing was measured, so a clean exit would let `--fail-on-stale` pass silently.
        reason = Counter(r for _, r in run.skipped_changes).most_common(1)[0][0]
        ui.print(f"[red]No API change could be probed[/red]: {_escape(reason)}")
        return 1
    writer_failed = [
        r for _, r in (run.skipped_changes if run else []) if r.startswith(TASK_WRITER_FAILED)
    ]
    if (
        run is not None
        and writer_failed
        and len(writer_failed) * 2 >= len(run.skipped_changes) + len(run.probes)
    ):
        ui.print(
            f"[red]The task writer failed for {len(writer_failed)} of "
            f"{len(run.skipped_changes) + len(run.probes)} API changes[/red], so this run measured "
            f"too little to trust: {_escape(writer_failed[0])}"
        )
        return 1
    if (
        getattr(args, "fail_on_stale", False)
        and run is not None
        and any(a.outcome == STALE for a in run.probes)
    ):
        return 3
    if run is not None and any(a.outcome == ERROR for a in run.probes):
        ui.print(
            "[yellow]Some model calls failed; their probes are not counted (see the report).[/yellow]"
        )
    fail_on = set(getattr(args, "fail_on", None) or ())  # each --fail-on given counts
    if getattr(args, "fail_on_changes", False):
        fail_on.add("changes")
    failing = fail_reason(scan, fail_on)
    if failing:
        ui.print(f"[dim]Exit code 3: {_escape(failing)}[/dim]")
        return 3
    return 0


def _write_tasks_out(path: Path, run: RunResult | None, settings: Settings) -> None:
    """``--tasks-out``: the tasks this run used, credited to whoever wrote them: this run's task
    writer, or the writer, prompt version and tool a ``--tasks-from`` file names."""
    from since_cutoff.taskfile import tasks_document

    used = run.used_tasks() if run is not None else []
    source = settings.tasks_from
    if source is None:
        document = tasks_document(used, task_model=settings.task_model or settings.model)
    else:
        document = tasks_document(
            used,
            task_model=source.task_model,
            prompt_version=source.prompt_version,
            tool=source.tool,
        )
    write_tasks(path, document)


def _choose_model(
    args: argparse.Namespace, settings: Settings, root: Path, reporter: Reporter
) -> None:
    """Without --model, test the model the user's coding agent is set up with
    (:func:`since_cutoff.hosts.detect_model`); when no setting names one, say that the default
    is a guess and how to choose."""
    if args.model is not None or (args.command == "scan" and settings.cutoff is not None):
        return  # a date alone needs no model
    _use_detected_model(settings, root, reporter, probes=args.command == "run")


def _use_detected_model(
    settings: Settings, root: Path, reporter: Reporter, *, probes: bool = True
) -> None:
    """The model the coding agent is set up with, into ``settings``; when no setting names
    one, say that Claude Code's default is used, which ``run`` tests (``probes``) and ``scan``
    and ``sync`` only take the training cutoff of."""
    from since_cutoff.hosts import DEFAULT_SOURCE, not_found_hint

    found = detect_model(root)
    if found is None:
        settings.model_source = DEFAULT_SOURCE
        reporter.warn(not_found_hint(probes=probes))
        return
    if found.problem:
        raise SinceCutoffError(found.problem)
    settings.model, settings.model_source = found.spec, found.source


def _model_line(target: ModelTarget, source: str | None = None) -> str:
    """``• Model claude-sonnet-4-5, training cutoff 2025-07-31, comparing from releases up to
    2025-07-01 (from models.dev)``, as rich markup; a cutoff alone; several models (``sync
    --model a,b``) with their earliest cutoff. With ``--cutoff-margin 0`` the comparison day is
    the cutoff, and the line does not repeat it."""
    from since_cutoff.notes import model_names

    day = target.cutoff.isoformat()
    from_day = (
        f", comparing from releases up to [bold]{target.compare_date.isoformat()}[/bold]"
        if target.margin > 0
        else ""
    )
    where = f"[dim](from {_escape(source or target.cutoff_source)})[/dim]"
    if not target.model_id:
        return f"[dim]•[/dim] Custom cutoff [bold]{day}[/bold]{from_day} {where}"
    names = model_names(target.model_id)
    if len(names) > 1:
        shown = ", ".join(f"[bold]{_escape(n)}[/bold]" for n in names)
        return (
            f"[dim]•[/dim] Models {shown}, earliest training cutoff [bold]{day}[/bold]"
            f"{from_day} {where}"
        )
    return (
        f"[dim]•[/dim] Model [bold]{_escape(target.model_id)}[/bold], training cutoff "
        f"[bold]{day}[/bold]{from_day} {where}"
    )


def _resolve_target(engine: Engine, *, allow_calls: bool) -> ModelTarget:
    """The engine's target; an error about a model read from a setting names that setting."""
    from since_cutoff.hosts import DEFAULT_SOURCE

    settings = engine.settings
    spec, source = settings.model, settings.model_source
    try:
        return engine.resolve_target(allow_calls=allow_calls)
    except SinceCutoffError as exc:
        if not source or source == DEFAULT_SOURCE:
            raise
        raise type(exc)(
            f"{exc} (the model '{spec}' is set in {source}; pass --model provider:model to "
            "test another)"
        ) from exc


def _report_to_stdout(args: argparse.Namespace) -> bool:
    """Whether ``--markdown``, ``--sarif`` or ``--gitlab-codequality`` writes to stdout
    (``-``): then the terminal report is left out, and the progress goes to stderr."""
    return "-" in (
        getattr(args, "markdown", None),
        getattr(args, "sarif", None),
        getattr(args, "gitlab_codequality", None),
    )


def _write_markdown(target: str, text: str, what: str = "the Markdown summary") -> None:
    """``text`` into the file ``target``, relative to the current directory, or to stdout
    for ``-``."""
    if target == "-":
        STDOUT.write(text)
        STDOUT.flush()
        return
    path = Path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise SinceCutoffError(f"cannot write {what} to {path}: {exc}") from exc


def _write_json(target: str, data: object, what: str) -> None:
    """``--sarif`` and ``--gitlab-codequality``: ``data`` as JSON, written as
    :func:`_write_markdown` writes text."""
    _write_markdown(target, json.dumps(data, indent=2, ensure_ascii=False) + "\n", what)


# ------------------------------------------------------------ sync and status
def _cmd_sync(args: argparse.Namespace, out: Console) -> int:
    """``since-cutoff sync``: write or update the notes block (see :mod:`since_cutoff.sync`)."""
    from since_cutoff.cache import DiskCache
    from since_cutoff.models import parse_cutoff
    from since_cutoff.notes import (
        IMPORTED_APIS,
        SCOPE_USED,
        agents_import_tip,
        apply_block,
        block_targets,
        remove_block,
    )
    from since_cutoff.project import load_project
    from since_cutoff.sync import (
        EXIT_EDITED,
        EXIT_OK,
        EXIT_OUT_OF_DATE,
        diff_lines,
        edited_text,
        out_of_date_text,
        propose,
        read_target,
        sticky_target,
        sync_json,
        up_to_date_text,
        written_text,
    )

    try:
        cutoff = parse_cutoff(args.cutoff) if args.cutoff else None
    except ValueError as exc:
        raise SinceCutoffError(str(exc)) from exc
    margin = _cutoff_margin(args)
    project = load_project(Path(args.path))
    targets = [read_target(project.root, p) for p in block_targets(project.root, args.target)]
    store = DiskCache()
    reporter = RichReporter(out)
    plans: list[_Plan] = []
    given: ModelTarget | None = None
    for t in targets:
        # The model and cutoff the block was written for, unless --model or --cutoff is given.
        basis = None if args.model or cutoff else sticky_target(t, margin)
        if basis is None:
            given = given or _sync_model(args.model, cutoff, project.root, store, reporter, margin)
            basis = given
        scope = args.scope or t.scope or SCOPE_USED
        suggestions = t.meta.get("suggestions") is True
        # As --suggestions: the block keeps the choice unless the flag says otherwise.
        written_with = t.meta.get("per_package")
        if args.per_package is not None:
            per_package = args.per_package
        elif isinstance(written_with, int) and written_with > 0:
            per_package = written_with
        else:
            per_package = IMPORTED_APIS
        plans.append(
            (
                t,
                basis,
                scope,
                suggestions if args.suggestions is None else args.suggestions,
                per_package,
            )
        )
    scans = _sync_scans(args, project, plans, store, reporter, out)
    proposals = [
        propose(
            scans[(basis.model_id, basis.cutoff)],
            t,
            scope=scope,
            suggestions=suggestions,
            per_package=per_package,
        )
        for t, basis, scope, suggestions, per_package in plans
    ]
    changed = [p for p in proposals if p.changed]
    written: set[str] = set()
    # Both AGENTS.md and CLAUDE.md, and no `@AGENTS.md` in CLAUDE.md: how to keep one copy, or
    # what Claude Code needs to read a block in AGENTS.md alone; or a second copy (issue #13).
    tip = agents_import_tip(project.root, [t.path for t in targets])
    if not any(p.block for p in proposals):
        tip = None

    def finish(code: int) -> int:
        if args.json:
            listed = sync_json(proposals, code, written, tip=tip)
            STDOUT.write(json.dumps(listed, indent=2) + "\n")
        return code

    for p in changed:
        out.print()
        for line in diff_lines(p):
            out.print(_diff_text(line))
    out.print()
    if tip:
        out.print(f"[yellow]![/yellow] {_escape(tip)}.")
    edited = [p for p in changed if p.edited and not args.force]
    if edited:
        for p in edited:
            out.print(f"[yellow]![/yellow] {_escape(edited_text(p))}")
        return finish(EXIT_OK if args.dry_run and not args.check else EXIT_EDITED)
    for p in proposals:
        if not p.changed:
            out.print(_escape(up_to_date_text(p, scans[(p.model, p.cutoff)])))
    if not changed:
        return finish(EXIT_OK)
    if args.check:
        for p in changed:
            out.print(_escape(out_of_date_text(p)))
        return finish(EXIT_OUT_OF_DATE)
    if args.dry_run:
        out.print("Nothing written (--dry-run).")
        return finish(EXIT_OK)
    if not args.yes:
        names = " and ".join(p.target.name for p in changed)
        answer = _confirm(f"Write {'this' if len(changed) == 1 else 'these'} to {names}?", out)
        if answer is None:
            out.print(
                "Not written: there is no terminal to ask in. `since-cutoff sync --yes` writes "
                "it; `since-cutoff sync --check` only checks."
            )
            return EXIT_OUT_OF_DATE
        if not answer:
            out.print("Nothing written.")
            return EXIT_OUT_OF_DATE
    for p in changed:
        if p.block is None:
            remove_block(p.target.path)
        else:
            apply_block(p.target.path, p.block)
        written.add(p.target.name)
        out.print(f"[green]✓[/green] {_escape(written_text(p))}")
    if any(p.target.text for p in changed):
        out.print("[dim]Text outside the since-cutoff markers is unchanged.[/dim]")
    return finish(EXIT_OK)


def _sync_scans(
    args: argparse.Namespace,
    project: Project,
    plans: list[_Plan],
    store: DiskCache,
    reporter: RichReporter,
    out: Console,
) -> dict[tuple[str, date], ScanResult]:
    """One scan per model and cutoff the blocks are written for (the diffs are cached, so a
    second one costs little). Raises SinceCutoffError when nothing could be checked, or when a
    package the notes are about could not be (PyPI unreachable): sync then writes nothing."""
    from datetime import date

    from since_cutoff.engine import SKIPPED, Settings
    from since_cutoff.sync import blocked_by, lockfile_changes, target_status

    settings = Settings(
        max_download_mb=args.max_download_mb,
        python_version=project.python_version,
        today=date.today(),
        include_name_matches=args.include_name_matches,
    )
    engine = Engine(settings, store=store, llm_cache=store, reporter=reporter)
    scans: dict[tuple[str, date], ScanResult] = {}
    for _, basis, *_ in plans:
        key = (basis.model_id, basis.cutoff)
        if key in scans:
            continue
        out.print(_model_line(basis))
        for t, *_ in plans:
            if t.basis == key:
                moved = lockfile_changes(project, target_status(project, t))
                if moved:
                    reporter.info(moved)
        scans[key] = engine.scan(project, basis)
    reporter.done()
    for scan in scans.values():
        if scan.packages and all(p.status == SKIPPED for p in scan.packages):
            first = next((p.reason for p in scan.skipped if p.reason), "")
            raise SinceCutoffError(f"no dependency could be checked: {first} (is PyPI reachable?)")
    for t, basis, *_ in plans:
        for p in blocked_by(scans[(basis.model_id, basis.cutoff)], [t]):
            raise SinceCutoffError(
                f"cannot tell whether the notes for {p.name} in {t.name} still hold: it could "
                f"not be checked ({p.reason}). Nothing written."
            )
    for scan in scans.values():
        for p in scan.skipped:
            if p.reason and METAPACKAGE in p.reason:  # information: its parts are checked
                reporter.info(p.reason)
            else:
                reporter.warn(f"{p.name} could not be checked ({p.reason}), so it has no notes")
    return scans


def _sync_model(
    models: list[str],
    cutoff: date | None,
    root: Path,
    store: DiskCache,
    reporter: Reporter,
    margin: int = DEFAULT_CUTOFF_MARGIN,
) -> ModelTarget:
    """The model(s) and cutoff given on the command line, else the model the coding agent is
    set up with, as for ``scan``; several models: the earliest of their cutoffs."""
    from datetime import date

    from since_cutoff.engine import ModelTarget, Settings

    if not models and cutoff is not None:
        return ModelTarget.cutoff_only(cutoff, "--cutoff; no model given", margin=margin)
    found: list[ModelTarget] = []
    specs: list[str | None] = [*dict.fromkeys(models)] or [None]
    for spec in specs:
        settings = Settings(
            model=spec or "claude-code", cutoff=cutoff, cutoff_margin=margin, today=date.today()
        )
        if spec is None:
            _use_detected_model(settings, root, reporter, probes=False)
        engine = Engine(settings, store=store, llm_cache=store, reporter=reporter)
        found.append(_resolve_target(engine, allow_calls=False))
    if len(found) == 1:
        return found[0]
    earliest = min(t.cutoff for t in found)
    each = "; ".join(f"{t.model_id} {t.cutoff.isoformat()}" for t in found)
    return ModelTarget(
        ", ".join(t.spec for t in found),
        ", ".join(dict.fromkeys(t.model_id for t in found)),
        earliest,
        f"the earliest of {each}",
        margin=margin,
    )


def _confirm(question: str, out: Console) -> bool | None:
    """Ask a yes/no question on the terminal; None when stdin is not one (a pipe, CI, a coding
    agent's shell), where nobody can answer."""
    if not _interactive(sys.stdin):
        return None
    out.print(f"{_escape(question)} [y/N] ", end="")
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _diff_text(line: str) -> Text:
    from rich.text import Text

    style = ""
    if line.startswith(("---", "+++")):
        style = "bold"
    elif line.startswith("@@"):
        style = "cyan"
    elif line.startswith("+"):
        style = "green"
    elif line.startswith("-"):
        style = "red"
    return Text(line, style)


def _cmd_status(args: argparse.Namespace) -> int:
    """``since-cutoff status``, offline. With ``--hook``, one line when the notes are out of
    date, else nothing, and always exit code 0: a coding agent's session start must never
    fail or wait on it (and it prints without rich, which it would wait for)."""
    if not args.hook:
        return _status(args, _console(STDOUT))
    try:
        _status(args, None)
    except Exception:  # whatever goes wrong, the session starts
        import logging

        logging.getLogger(__name__).debug("status --hook failed", exc_info=True)
    return 0


def _status(args: argparse.Namespace, out: Console | None) -> int:
    from since_cutoff.notes import block_targets
    from since_cutoff.project import load_project
    from since_cutoff.sync import (
        EXIT_OK,
        current_deps_hash,
        exit_code,
        hook_line,
        note_import_tip,
        read_target,
        status_json,
        status_lines,
        target_status,
    )

    project = load_project(Path(args.path))
    targets = [read_target(project.root, p) for p in block_targets(project.root, args.target)]
    detected = _detected_offline(project.root)
    deps = current_deps_hash(project)
    margin = _cutoff_margin(args)
    statuses = [
        target_status(project, t, deps=deps, detected=detected, margin=margin) for t in targets
    ]
    note_import_tip(project.root, statuses)
    code = exit_code(statuses)
    if args.hook:
        line = hook_line(statuses)
        if line:
            STDOUT.write(line + "\n")
        return EXIT_OK
    if args.json:
        STDOUT.write(json.dumps(status_json(project, statuses, detected), indent=2) + "\n")
        return code
    console = out if out is not None else _console(STDOUT)
    for line in status_lines(project, statuses):
        console.print(_escape(line))
    return code


def _detected_offline(root: Path) -> DetectedModel | None:
    """The model the coding agent is set up with and its cutoff, from the agents' settings and
    the model registry's cache or bundled snapshot: no network. None when no setting names a
    model (Claude Code's default is only a guess) or it cannot be resolved offline."""
    found = detect_model(root)
    if found is None or found.problem or not found.spec:
        return None
    from datetime import date

    from since_cutoff.cache import DiskCache
    from since_cutoff.engine import Settings
    from since_cutoff.models import ModelRegistry
    from since_cutoff.sync import DetectedModel

    store = DiskCache()
    settings = Settings(model=found.spec, model_source=found.source, today=date.today())
    engine = Engine(
        settings,
        store=store,
        llm_cache=store,
        registry=ModelRegistry(store, offline=True),
    )
    try:
        target = engine.resolve_target(allow_calls=False)
    except SinceCutoffError:
        return None
    return DetectedModel(target, found.spec, found.source)


def _cmd_models(args: argparse.Namespace, out: Console) -> int:
    from datetime import date

    from rich.table import Table

    from since_cutoff.cache import DiskCache
    from since_cutoff.models import FIRST_PARTY, ModelRegistry

    registry = ModelRegistry(DiskCache(), offline=args.offline)
    q = args.query.lower()
    models = [m for m in registry.all_models() if q in m.id.lower() and m.knowledge]
    if args.makers:
        models = [m for m in models if m.provider in FIRST_PARTY]
    priority = {
        "anthropic": 0,
        "openai": 1,
        "google": 2,
        "deepseek": 3,
        "xai": 4,
        "mistral": 5,
        "alibaba": 6,
        "moonshotai": 7,
        "zai": 8,
        "llama": 9,
    }
    models.sort(
        key=lambda m: (
            priority.get(m.provider, len(priority)),
            m.provider,
            m.release_date or date.min,
            m.id,
        )
    )
    if args.json:
        listed = {"source": registry.source, "models": [_model_json(m) for m in models]}
        STDOUT.write(json.dumps(listed, indent=2) + "\n")
        return 0
    if not out.is_terminal:
        for m in models:
            released = m.release_date.isoformat() if m.release_date else ""
            STDOUT.write(f"{m.provider}\t{m.id}\t{m.knowledge_raw or ''}\t{released}\n")
        return 0
    table = Table(show_edge=False, header_style="bold")
    table.add_column("provider", no_wrap=True)
    table.add_column("model", overflow="fold")
    table.add_column("training cutoff", no_wrap=True)
    table.add_column("released", no_wrap=True)
    for m in models:
        table.add_row(
            m.provider,
            m.id,
            m.knowledge_raw or "",
            m.release_date.isoformat() if m.release_date else "",
        )
    out.print(table)
    out.print(f"[dim]{len(models)} models (source: {_escape(registry.source)})[/dim]")
    # Which of the listings `--model <query>` takes the cutoff of (issue #87).
    used = registry.lookup(args.query) if args.query.strip() else None
    if used is not None and used.knowledge is not None:
        out.print(
            f"[dim]--model {_escape(args.query)} takes the training cutoff of "
            f"{_escape(used.provider)}'s listing {_escape(used.id)}, "
            f"{_escape(used.knowledge_raw or '')}[/dim]"
        )
    return 0


def _model_json(m: ModelInfo) -> dict[str, Any]:
    """One model of ``models --json``: the cutoff as models.dev writes it and as a date."""
    return {
        "provider": m.provider,
        "id": m.id,
        "name": m.name,
        "family": m.family,
        "training_cutoff": m.knowledge_raw,
        "cutoff_date": m.knowledge.isoformat() if m.knowledge else None,
        "released": m.release_date.isoformat() if m.release_date else None,
    }


# ------------------------------------------------------------------- cache
def _cmd_cache(args: argparse.Namespace) -> int:
    """``since-cutoff cache path | info | clear [--sources] [--diffs] [--pypi]``: plain lines
    on stdout, for scripts and without rich."""
    from since_cutoff import cache

    root = cache.default_cache_dir()
    if args.action == "path":
        STDOUT.write(f"{root}\n")
        return 0
    if args.action == "info":
        for line in cache_info_lines(root, cache.stats(root), cache.max_sources_bytes()):
            STDOUT.write(line + "\n")
        return 0
    chosen = {
        ns for kind, kinds in cache.CLEAR_FLAGS.items() if getattr(args, kind) for ns in kinds
    }
    namespaces = [ns for ns in cache.NAMESPACES if not chosen or ns in chosen]
    removed = cache.clear(root, namespaces)
    STDOUT.write(f"cleared {', '.join(removed) or 'nothing'} in {root}\n")
    return 0


def cache_info_lines(root: Path, kinds: list[NamespaceStats], cap: int | None) -> list[str]:
    """``cache info``: the path, one row per kind (size, entries, files, and the oldest: when
    its oldest entry was written, or for the sources, when the least recently used tree was
    last used), the total, and the cap on the sources."""
    from datetime import date

    from since_cutoff.cache import MB

    def day(stamp: float | None) -> str:
        return date.fromtimestamp(stamp).isoformat() if stamp else ""

    rows = [("kind", "size", "entries", "files", "oldest", "")]
    for k in kinds:
        note = "model output: costs money to make again" if k.model_output else ""
        rows.append(
            (k.name, f"{k.bytes / MB:,.1f} MB", str(k.entries), str(k.files), day(k.oldest), note)
        )
    used = [k.oldest for k in kinds if k.oldest]
    rows.append(
        (
            "total",
            f"{sum(k.bytes for k in kinds) / MB:,.1f} MB",
            str(sum(k.entries for k in kinds)),
            str(sum(k.files for k in kinds)),
            day(min(used) if used else None),
            "",
        )
    )
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = [str(root)]
    for name, size, entries, files, oldest, note in rows:
        cells = [name.ljust(widths[0]), size.rjust(widths[1]), entries.rjust(widths[2])]
        cells += [files.rjust(widths[3]), oldest.ljust(widths[4])]
        lines.append(("  ".join(cells) + "  " + note).rstrip())
    if cap is None:
        setting = os.environ.get("SINCE_CUTOFF_CACHE_MAX_MB", "").strip()
        lines.append(f"sources cap: none (SINCE_CUTOFF_CACHE_MAX_MB={setting})")
    else:
        lines.append(
            f"sources cap: {cap // MB:,} MB (SINCE_CUTOFF_CACHE_MAX_MB); over it, the least "
            "recently used trees go, never those a running scan uses"
        )
    return lines


def _cmd_mcp(args: argparse.Namespace) -> int:
    # stdout carries the protocol from here on: nothing else may print to it.
    from since_cutoff.mcp_server import serve

    serve(max_download_mb=args.max_download_mb, debug=args.debug)
    return 0


def _cmd_unapply(args: argparse.Namespace, out: Console) -> int:
    from since_cutoff.notes import remove_block

    root = Path(args.path).resolve()
    if not root.is_dir():
        raise SinceCutoffError(f"{root} is not a directory")
    targets = [Path(args.target)] if args.target else [root / "AGENTS.md", root / "CLAUDE.md"]
    removed = [t for t in targets if remove_block(t if t.is_absolute() else root / t)]
    if removed:
        out.print(
            "removed the since-cutoff block from " + ", ".join(_escape(t.name) for t in removed)
        )
    else:
        out.print("no since-cutoff block found")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
