"""Render the README screenshot from a real scan (no model calls): python scripts/demo_svg.py <project>"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from rich.console import Console

from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine, Settings
from since_cutoff.project import load_project
from since_cutoff.report import render_console, render_scan_changes


def main() -> None:
    project = load_project(Path(sys.argv[1]))
    store = DiskCache()
    settings = Settings(
        model="anthropic:claude-sonnet-4-5",
        include=["anthropic", "openai", "huggingface-hub", "langchain-core"],
    )
    engine = Engine(settings, store=store, llm_cache=store)
    scan = engine.scan(project, engine.resolve_target(allow_calls=False))
    console = Console(
        file=io.StringIO(),
        record=True,
        width=100,
        force_terminal=True,
        color_system="truecolor",
        emoji=False,
        highlight=False,
        legacy_windows=False,
    )
    console.print("[bold]$[/bold] since-cutoff scan --model anthropic:claude-sonnet-4-5")
    render_console(console, scan)
    render_scan_changes(console, scan, limit=2)
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/img/scan.svg")
    console.save_svg(str(out), title="since-cutoff scan")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
