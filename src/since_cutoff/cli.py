"""Command-line interface."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import errno
import json
import logging
import os
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import IO, Any, cast

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from since_cutoff import __version__
from since_cutoff.baselines import BASELINE_ARMS
from since_cutoff.cache import DiskCache, default_cache_dir
from since_cutoff.engine import (
    ERROR,
    STALE,
    TASK_WRITER_FAILED,
    Engine,
    ModelTarget,
    Reporter,
    RunResult,
    ScanResult,
    Settings,
)
from since_cutoff.errors import SinceCutoffError
from since_cutoff.hosts import DEFAULT_SOURCE, detect_model, not_found_hint
from since_cutoff.models import ModelRegistry, parse_cutoff
from since_cutoff.notes import apply_block, remove_block
from since_cutoff.project import load_project
from since_cutoff.report import (
    render_console,
    render_scan_changes,
    render_scan_markdown,
    to_json,
    write_outputs,
)
from since_cutoff.taskfile import read_tasks, tasks_document, write_tasks

COMMANDS = ("run", "scan", "models", "cache", "unapply", "mcp")
CACHE_NAMESPACES = (
    "pypi",
    "sources",
    "sources-meta",
    "diffs",
    "tasks",
    "answers",
    "notes",
    "envs",
    "models",
)
EXAMPLES = """examples:
  since-cutoff                          probe your coding agent's model on this project
  since-cutoff scan                     list API changes since the model's cutoff (no model calls)
  since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown - >> "$GITHUB_STEP_SUMMARY"
  since-cutoff scan --cutoff 2025-03   list API changes since a date, with no model involved
  since-cutoff run --apply              probe, then write verified notes into AGENTS.md
  since-cutoff run --quick --model openai:gpt-5.4
  since-cutoff run --compare template   compare the notes with a baseline built without a model
  since-cutoff run --model openai-compatible:my-model --base-url http://localhost:8000/v1
  since-cutoff models sonnet            show known models and their training cutoffs
  since-cutoff mcp                      serve the read-only tools to coding agents over MCP (stdio)

exit codes: 0 ok, 1 error (including: no model answer could be scored), 2 usage error,
            3 stale API use found with --fail-on-stale, or API changes found with
            scan --fail-on-changes, 141 the output was closed early (e.g. piped into head)
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
    options: dict[str, Any] = {}
    if not _interactive(output):
        if _isatty(output):
            options["force_terminal"] = False  # Windows' NUL device
        if not os.environ.get("COLUMNS"):
            options["width"] = LOG_WIDTH
    return Console(
        file=cast(IO[str], output), highlight=False, soft_wrap=True, emoji=False, **options
    )


class RichReporter(Reporter):
    # In a log or a pipe, where there is no live progress bar, a line such as "diffed 40/71
    # (transformers)" comes about this many times per stage whose steps name a package.
    LOG_LINES = 10

    def __init__(self, console: Console) -> None:
        self.console = console
        self.progress: Progress | None = None
        self.task: object | None = None
        self.count = self.total = self.next_line = 0

    def stage(self, title: str, total: int | None = None) -> None:
        self.done()
        self.console.print(f"[dim]•[/dim] {escape(title)}")
        self.count, self.total = 0, total or 0
        self.next_line = max(1, self.total // self.LOG_LINES)
        if not self.console.is_terminal or not _interactive(self.console.file):
            return  # no live progress bar in logs and pipes: lines from advance() instead
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self.console,
            transient=True,
        )
        self.progress.start()
        self.task = self.progress.add_task(escape(title), total=total)

    def advance(self, n: int = 1, *, label: str | None = None) -> None:
        self.count += n
        if self.progress is not None and self.task is not None:
            self.progress.advance(self.task, n)  # type: ignore[arg-type]
        elif label and self.total and (self.count >= self.next_line or self.count == self.total):
            # A slow stage (a cold diff can take minutes) says how far it got in a log too.
            self.console.print(f"  [dim]diffed {self.count}/{self.total} ({escape(label)})[/dim]")
            self.next_line = self.count + max(1, self.total // self.LOG_LINES)

    def info(self, message: str) -> None:
        self.console.print(f"[dim]•[/dim] {escape(message)}")

    def warn(self, message: str) -> None:
        self.console.print(f"[yellow]![/yellow] {escape(message)}")

    def done(self) -> None:
        if self.progress is not None:
            self.progress.stop()
            self.progress = None
            self.task = None


# ------------------------------------------------------------------ parsing
def _csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _arms(value: str) -> list[str]:
    """``--compare``: baseline arms, comma-separated; ``none`` for no baseline."""
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


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "path", nargs="?", default=".", help="project directory (default: current directory)"
    )
    p.add_argument(
        "--model",
        help="model to test, as provider:model (default: the model your coding agent is set up "
        "with: SINCE_CUTOFF_MODEL; inside Claude Code, Claude Code's model; elsewhere the "
        "Claude Code, Codex, OpenCode and Aider settings, the project's before the user's; else "
        "claude-code, Claude Code's default; scan with --cutoff alone uses no model). "
        "Providers: claude-code, anthropic, openai, openrouter, deepseek, ollama, openai-compatible",
    )
    p.add_argument(
        "--base-url", help="API base URL (for openai-compatible, or to override a provider's)"
    )
    p.add_argument("--cutoff", help="override the model's training cutoff (YYYY-MM or YYYY-MM-DD)")
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
    parser = argparse.ArgumentParser(
        prog="since-cutoff",
        description="Find which of your exact dependency versions your coding agent writes wrong, "
        "and fix it with a small, verified AGENTS.md note.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument("--version", action="version", version=f"since-cutoff {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="scan, probe the model, write and verify notes (default)")
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
        "--no-fix", action="store_true", help="only measure; do not write or verify notes"
    )
    run.add_argument(
        "--apply",
        action="store_true",
        help="write the verified notes into the agent instructions file",
    )
    run.add_argument(
        "--target",
        help="file to write notes into (default: AGENTS.md, or CLAUDE.md if that is what exists)",
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
        "compare them with the verified notes: template (each change stated from the API "
        "diff), signatures (the new signature and docstring of each changed or replacement "
        "API), none (default)",
    )

    scan = sub.add_parser("scan", help="list API changes since the model's cutoff (no model calls)")
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
        "--fail-on-changes",
        action="store_true",
        help="exit with code 3 if any dependency changed its API after the cutoff (for CI)",
    )

    models = sub.add_parser("models", help="list known models and their training cutoffs")
    models.add_argument(
        "query", nargs="?", default="", help="filter by id substring, e.g. 'sonnet'"
    )
    models.add_argument("--offline", action="store_true", help="use the bundled snapshot only")

    cache = sub.add_parser("cache", help="show or clear the cache")
    cache.add_argument("action", choices=["path", "clear"])

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
    err = _console(STDERR)
    try:
        argv_ = _argv_with_default(list(sys.argv[1:] if argv is None else argv))
    except SinceCutoffError as exc:
        err.print(f"[red]error:[/red] {escape(str(exc))}")
        return 2
    args = build_parser().parse_args(argv_)
    if getattr(args, "debug", False):
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    json_mode = getattr(args, "json", False)
    markdown = getattr(args, "markdown", None)
    if json_mode and markdown == "-":
        err.print("[red]error:[/red] --json and --markdown - cannot both write to stdout")
        return 2
    problem = _compare_problem(args)
    if problem:
        err.print(f"[red]error:[/red] {escape(problem)}")
        return 2
    out = _console(STDOUT)
    ui = err if json_mode or markdown == "-" else out
    try:
        if args.command == "models":
            return _cmd_models(args, out)
        if args.command == "cache":
            return _cmd_cache(args)
        if args.command == "unapply":
            return _cmd_unapply(args, out)
        if args.command == "mcp":
            return _cmd_mcp(args)
        return _cmd_run(args, ui, json_mode)
    except SinceCutoffError as exc:
        err.print(f"[red]error:[/red] {escape(str(exc))}")
        return 1
    except KeyboardInterrupt:
        err.print("[yellow]interrupted[/yellow]")
        return 130


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


def _settings(args: argparse.Namespace) -> Settings:
    try:
        cutoff = parse_cutoff(args.cutoff) if args.cutoff else None
    except ValueError as exc:
        raise SinceCutoffError(str(exc)) from exc
    s = Settings(
        model=args.model or "claude-code",
        cutoff=cutoff,
        all_deps=args.all_deps,
        include=args.only,
        exclude=args.exclude,
        max_download_mb=args.max_download_mb,
        python_version=args.python_version,
        base_url=args.base_url,
        today=date.today(),
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
        target = ModelTarget.cutoff_only(settings.cutoff)
        ui.print(
            f"[dim]•[/dim] Custom cutoff [bold]{target.cutoff.isoformat()}[/bold] "
            "[dim](from --cutoff; no model given)[/dim]"
        )
    else:
        target = _resolve_target(engine, allow_calls=args.command == "run")
        ui.print(
            f"[dim]•[/dim] Model [bold]{escape(target.model_id)}[/bold], training cutoff "
            f"[bold]{target.cutoff.isoformat()}[/bold] [dim](from {escape(target.cutoff_source)})[/dim]"
        )
    scan: ScanResult = engine.scan(project, target)
    run: RunResult | None = None
    if args.command == "run" and scan.changed:
        run = engine.run(scan, fix=not args.no_fix)
    reporter.done()

    md, _ = write_outputs(project.root / args.out, scan, run)
    tasks_out = getattr(args, "tasks_out", None)
    summary_md = getattr(args, "markdown", None)
    if summary_md:
        _write_markdown(summary_md, render_scan_markdown(scan, limit=args.limit))
    if json_mode:
        STDOUT.write(json.dumps(to_json(scan, run), indent=2, default=str) + "\n")
        STDOUT.flush()
    elif summary_md != "-":  # stdout has the Markdown, stderr only the progress, as for --json
        ui.print()
        render_console(ui, scan, run, verbose=args.verbose)
        if args.command == "scan":
            render_scan_changes(ui, scan, limit=args.limit)

    if args.command == "run" and run is not None:
        if run.block:
            if args.apply:
                path = _target_file(project.root, args.target)
                action = apply_block(path, run.block)
                ui.print(
                    f"\n[green]✓[/green] {action.capitalize()} {escape(str(path))} with {len(run.notes)} notes"
                )
            elif not json_mode:
                ui.print(
                    "\n[bold]Notes for AGENTS.md[/bold] [dim](run again with --apply to write them)[/dim]"
                )
                ui.print(run.block, markup=False, highlight=False)
        elif args.apply:
            ui.print("\n[dim]Nothing to apply: no failures needed a note.[/dim]")
    ui.print(f"[dim]Full report: {escape(str(md))}[/dim]")
    if tasks_out:
        # Last, so that a failed write cannot cost the result card, the JSON or --apply.
        _write_tasks_out(Path(tasks_out), run, settings)
        ui.print(f"[dim]Tasks: {escape(tasks_out)} (repeat this run with --tasks-from)[/dim]")
    if summary_md and summary_md != "-":
        ui.print(f"[dim]Markdown summary: {escape(summary_md)}[/dim]")

    checked = [p for p in scan.packages if p.status != "skipped"]
    if not checked and scan.packages:
        ui.print(
            "[red]No dependency could be checked[/red] (see the reasons above; is PyPI reachable?)."
        )
        return 1
    if args.command == "run" and not scan.changed:
        ui.print(
            "[green]Nothing to probe:[/green] no checked dependency changed its API after the cutoff."
        )
    if run is not None and run.all_errored:
        first = next((a.error for a in run.probes if a.error), "unknown error")
        ui.print(f"[red]Every model call failed[/red]: {escape(first or '')}")
        return 1
    if run is not None and scan.changed and not run.probes and run.skipped_changes:
        # Nothing was measured, so a clean exit would let `--fail-on-stale` pass silently.
        reason = Counter(r for _, r in run.skipped_changes).most_common(1)[0][0]
        ui.print(f"[red]No API change could be probed[/red]: {escape(reason)}")
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
            f"too little to trust: {escape(writer_failed[0])}"
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
    if getattr(args, "fail_on_changes", False) and scan.changed:
        return 3
    return 0


def _write_tasks_out(path: Path, run: RunResult | None, settings: Settings) -> None:
    """``--tasks-out``: the tasks this run used, credited to whoever wrote them: this run's task
    writer, or the writer, prompt version and tool a ``--tasks-from`` file names."""
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
    found = detect_model(root)
    if found is None:
        settings.model_source = DEFAULT_SOURCE
        reporter.warn(not_found_hint())
        return
    if found.problem:
        raise SinceCutoffError(found.problem)
    settings.model, settings.model_source = found.spec, found.source


def _resolve_target(engine: Engine, *, allow_calls: bool) -> ModelTarget:
    """The engine's target; an error about a model read from a setting names that setting."""
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


def _write_markdown(target: str, text: str) -> None:
    if target == "-":
        STDOUT.write(text)
        STDOUT.flush()
        return
    path = Path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise SinceCutoffError(f"cannot write the Markdown summary to {path}: {exc}") from exc


def _target_file(root: Path, explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else root / p
    agents, claude = root / "AGENTS.md", root / "CLAUDE.md"
    if agents.exists() or not claude.exists():
        return agents
    return claude


def _cmd_models(args: argparse.Namespace, out: Console) -> int:
    registry = ModelRegistry(DiskCache(), offline=args.offline)
    q = args.query.lower()
    models = [m for m in registry.all_models() if q in m.id.lower() and m.knowledge]
    priority = {"anthropic": 0, "openai": 1, "google": 2, "deepseek": 3, "xai": 4, "mistral": 5}
    models.sort(
        key=lambda m: (priority.get(m.provider, 9), m.provider, m.release_date or date.min, m.id)
    )
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
    out.print(f"[dim]{len(models)} models (source: {escape(registry.source)})[/dim]")
    return 0


def _cmd_cache(args: argparse.Namespace) -> int:
    root = default_cache_dir()
    if args.action == "path":
        STDOUT.write(f"{root}\n")
        return 0
    removed = []
    for ns in CACHE_NAMESPACES:
        p = root / ns
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(ns)
    with contextlib.suppress(OSError):
        root.rmdir()  # only succeeds if the directory is now empty, i.e. it was ours alone
    STDOUT.write(f"cleared {', '.join(removed) or 'nothing'} in {root}\n")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    # stdout carries the protocol from here on: nothing else may print to it.
    from since_cutoff.mcp_server import serve

    serve(max_download_mb=args.max_download_mb, debug=args.debug)
    return 0


def _cmd_unapply(args: argparse.Namespace, out: Console) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise SinceCutoffError(f"{root} is not a directory")
    targets = [Path(args.target)] if args.target else [root / "AGENTS.md", root / "CLAUDE.md"]
    removed = [t for t in targets if remove_block(t if t.is_absolute() else root / t)]
    if removed:
        out.print(
            "removed the since-cutoff block from " + ", ".join(escape(t.name) for t in removed)
        )
    else:
        out.print("no since-cutoff block found")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
