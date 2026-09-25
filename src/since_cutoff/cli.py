"""Command-line interface."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import logging
import shutil
import sys
from datetime import date
from pathlib import Path

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
from since_cutoff.cache import DiskCache, default_cache_dir
from since_cutoff.engine import ERROR, STALE, Engine, Reporter, RunResult, ScanResult, Settings
from since_cutoff.errors import SinceCutoffError
from since_cutoff.models import ModelRegistry, parse_cutoff
from since_cutoff.notes import apply_block, remove_block
from since_cutoff.project import load_project
from since_cutoff.report import render_console, render_scan_changes, to_json, write_outputs

COMMANDS = ("run", "scan", "models", "cache", "unapply")
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
  since-cutoff                          probe your Claude Code model on this project
  since-cutoff scan                     list API changes since the model's cutoff (no model calls)
  since-cutoff run --apply              probe, then write verified notes into AGENTS.md
  since-cutoff run --quick --model openai:gpt-5.4
  since-cutoff run --model openai-compatible:my-model --base-url http://localhost:8000/v1
  since-cutoff models sonnet            show known models and their training cutoffs

exit codes: 0 ok, 1 error (including: no model answer could be scored), 2 usage error,
            3 stale API use found with --fail-on-stale
"""


class RichReporter(Reporter):
    def __init__(self, console: Console) -> None:
        self.console = console
        self.progress: Progress | None = None
        self.task: object | None = None

    def stage(self, title: str, total: int | None = None) -> None:
        self.done()
        self.console.print(f"[dim]•[/dim] {escape(title)}")
        if not self.console.is_terminal:
            return  # no live progress bar in logs and pipes; the stage line is enough
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

    def advance(self, n: int = 1) -> None:
        if self.progress is not None and self.task is not None:
            self.progress.advance(self.task, n)  # type: ignore[arg-type]

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
        default="claude-code",
        help="model to test, as provider:model (default: claude-code, your Claude Code model). "
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

    scan = sub.add_parser("scan", help="list API changes since the model's cutoff (no model calls)")
    _common(scan)
    scan.add_argument(
        "--limit", type=_positive, default=8, help="changes shown per package (default: 8)"
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
    """Piped or redirected output must never crash on non-ASCII text (Windows code pages)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and not stream.isatty():
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    err = Console(stderr=True, highlight=False, soft_wrap=True, emoji=False)
    try:
        argv_ = _argv_with_default(list(sys.argv[1:] if argv is None else argv))
    except SinceCutoffError as exc:
        err.print(f"[red]error:[/red] {escape(str(exc))}")
        return 2
    args = build_parser().parse_args(argv_)
    if getattr(args, "debug", False):
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    json_mode = getattr(args, "json", False)
    out = Console(highlight=False, soft_wrap=True, emoji=False)
    ui = err if json_mode else out
    try:
        if args.command == "models":
            return _cmd_models(args, out)
        if args.command == "cache":
            return _cmd_cache(args)
        if args.command == "unapply":
            return _cmd_unapply(args, out)
        return _cmd_run(args, ui, json_mode)
    except SinceCutoffError as exc:
        err.print(f"[red]error:[/red] {escape(str(exc))}")
        return 1
    except KeyboardInterrupt:
        err.print("[yellow]interrupted[/yellow]")
        return 130


def _settings(args: argparse.Namespace) -> Settings:
    try:
        cutoff = parse_cutoff(args.cutoff) if args.cutoff else None
    except ValueError as exc:
        raise SinceCutoffError(str(exc)) from exc
    s = Settings(
        model=args.model,
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
        if args.quick:
            s.max_probes = min(s.max_probes, 12)
            s.heldout = min(s.heldout, 1)
            s.regression = min(s.regression, 3)
    return s


def _cmd_run(args: argparse.Namespace, ui: Console, json_mode: bool) -> int:
    settings = _settings(args)
    project = load_project(Path(args.path), python=settings.python_version)
    if settings.python_version is None:
        settings.python_version = project.python_version
    store = DiskCache()
    llm_cache = DiskCache(store.root, enabled=not getattr(args, "fresh", False))
    reporter = RichReporter(ui)
    engine = Engine(settings, store=store, llm_cache=llm_cache, reporter=reporter)

    target = engine.resolve_target(allow_calls=args.command == "run")
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
    if json_mode:
        sys.stdout.write(json.dumps(to_json(scan, run), indent=2, default=str) + "\n")
        sys.stdout.flush()
    else:
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
    return 0


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
            sys.stdout.write(f"{m.provider}\t{m.id}\t{m.knowledge_raw or ''}\t{released}\n")
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
        sys.stdout.write(f"{root}\n")
        return 0
    removed = []
    for ns in CACHE_NAMESPACES:
        p = root / ns
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(ns)
    with contextlib.suppress(OSError):
        root.rmdir()  # only succeeds if the directory is now empty, i.e. it was ours alone
    sys.stdout.write(f"cleared {', '.join(removed) or 'nothing'} in {root}\n")
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
