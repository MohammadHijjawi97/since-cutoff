"""The CLI's progress output: the engine's :class:`~since_cutoff.engine.Reporter` rendered with
rich, as a live bar in a terminal and as lines in a log or a pipe.

Its own module because it needs the engine and rich, which take most of the CLI's start-up
time: ``cli`` imports it for the commands that scan, never for ``--help``, ``cache`` or
``status --hook``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape

from since_cutoff import cli
from since_cutoff.engine import Reporter

if TYPE_CHECKING:
    from rich.console import Console
    from rich.progress import Progress


class RichReporter(Reporter):
    # In a log or a pipe, where there is no live progress bar, a line such as "diffed 40/71
    # (transformers)" comes about this many times per stage whose steps name a package.
    LOG_LINES = 10

    def __init__(self, console: Console) -> None:
        self.console = console
        self.progress: Progress | None = None
        self.task: object | None = None
        self.count = self.total = self.next_line = 0
        self.warned: list[str] = []  # what warn() printed: the results do not print it again

    def stage(self, title: str, total: int | None = None) -> None:
        self.done()
        self.console.print(f"[dim]•[/dim] {escape(title)}")
        self.count, self.total = 0, total or 0
        self.next_line = max(1, self.total // self.LOG_LINES)
        if not self.console.is_terminal or not cli._interactive(self.console.file):
            return  # no live progress bar in logs and pipes: lines from advance() instead
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

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
        self.warned.append(message)

    def done(self) -> None:
        if self.progress is not None:
            self.progress.stop()
            self.progress = None
            self.task = None
