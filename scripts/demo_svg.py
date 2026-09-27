"""Render README screenshots from real, cached runs.

    python scripts/demo_svg.py scan <project> [out.svg]
    python scripts/demo_svg.py run  <project> [out.svg] [model]   # replays a cached `since-cutoff run`

The `run` mode uses the same settings as the published example run, so every task, answer and
note comes from the cache; no model is called.
"""

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
    mode, project_dir = sys.argv[1], Path(sys.argv[2])
    out = Path(sys.argv[3] if len(sys.argv) > 3 else f"docs/img/{mode}.svg")
    project = load_project(project_dir)
    store = DiskCache()
    console = Console(
        file=io.StringIO(),
        record=True,
        width=118,
        force_terminal=True,
        color_system="truecolor",
        emoji=False,
        highlight=False,
        legacy_windows=False,
    )
    if mode == "scan":
        settings = Settings(model="anthropic:claude-sonnet-4-5")
        engine = Engine(settings, store=store, llm_cache=store)
        scan = engine.scan(project, engine.resolve_target(allow_calls=False))
        console.print("[bold]$[/bold] since-cutoff scan --model anthropic:claude-sonnet-4-5")
        render_console(console, scan)
        render_scan_changes(console, scan, limit=2)
    else:
        model = sys.argv[4] if len(sys.argv) > 4 else "claude-code:claude-haiku-4-5"
        settings = Settings(
            model=model,
            task_model="claude-code:claude-opus-4-6",
            max_probes=30,
            heldout=2,
            regression=6,
        )
        engine = Engine(settings, store=store, llm_cache=store)
        target = engine.resolve_target()
        scan = engine.scan(project, target)
        run = engine.run(scan)
        console.print(
            f"[bold]$[/bold] since-cutoff run --model {model} --task-model claude-code:claude-opus-4-6"
        )
        render_console(console, scan, run)
    console.save_svg(str(out), title=f"since-cutoff {mode}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
