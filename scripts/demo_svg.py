"""Render README screenshots from real, cached runs.

    python scripts/demo_svg.py scan <project> [out.svg] [--width 92] [--all [--limit 1]]
    python scripts/demo_svg.py run  <project> [out.svg] [model]   # replays a cached `since-cutoff run`
    python scripts/demo_svg.py restyle <capture.svg>              # recolour an earlier capture

The README image is `python scripts/demo_svg.py scan examples/agent-app --width 92`: what
`since-cutoff scan --model anthropic:claude-sonnet-4-5` prints in a terminal 92 columns wide,
from the model line to the last line of the results (the "Full report" path, which names a
folder on the machine that made the image, is left out). A narrower terminal keeps the text
legible when GitHub scales the image to its column width. `--all` adds what `scan --all`
prints after that (0.3's summary, dependency table and changes per package, `--limit` of them
each), and the prompt in the image shows the flags, so it is the command that prints exactly
that. The scan calls no model; with a warm cache it needs no network either.

The `run` mode uses the same settings as the published example run, so every task, answer and
note comes from the cache; no model is called.

Every capture uses THEME: Rich's default SVG colours, with the text colours raised to at least
4.5:1 contrast on the background (Rich's default dim grey, red and magenta are 3.5-4.0:1). The
0.1.0 run cards (docs/img/run.svg, run-opus.svg) were recorded before this theme existed;
`restyle` recolours such a capture in place, which changes colours only, never text.
"""

from __future__ import annotations

import argparse
import io
import re
from pathlib import Path

from rich.color import blend_rgb
from rich.color_triplet import ColorTriplet
from rich.console import Console
from rich.terminal_theme import SVG_EXPORT_THEME, TerminalTheme

from since_cutoff.cache import DiskCache
from since_cutoff.cli import RichReporter, _model_line
from since_cutoff.engine import Engine, Settings
from since_cutoff.project import load_project
from since_cutoff.report import render_console, render_scan

SCAN_DEFAULT_LIMIT = 8  # `since-cutoff scan --limit` default

# Same background as Rich's export theme (#292929); every colour except ANSI black, including
# the dim foreground (Rich blends it 40% towards the background), is at least 5:1 on it.
THEME = TerminalTheme(
    (41, 41, 41),
    (230, 232, 230),
    [
        (75, 78, 85),
        (255, 123, 114),
        (152, 168, 75),
        (208, 179, 68),
        (97, 175, 239),
        (199, 146, 234),
        (86, 182, 194),
        (230, 232, 230),
    ],
    [
        (154, 155, 153),
        (255, 161, 152),
        (126, 231, 135),
        (227, 179, 65),
        (121, 192, 255),
        (210, 168, 255),
        (86, 212, 221),
        (255, 255, 255),
    ],
)
# The SVG's own font stack: Fira Code if installed, else the platform's monospace font. An SVG
# shown through <img> cannot load Rich's web font, so name the usual ones before `monospace`.
FONT_RULE = "font-family: Fira Code, monospace;"
FONT_STACK = (
    "font-family: Fira Code, ui-monospace, SFMono-Regular, Menlo, Consolas, "
    "'Cascadia Mono', 'DejaVu Sans Mono', 'Liberation Mono', monospace;"
)


def _text_colours(theme: TerminalTheme) -> list[ColorTriplet]:
    """Every colour Rich can write as text with ``theme``: foreground, ANSI, and their dim."""
    base = [theme.foreground_color] + [theme.ansi_colors[i] for i in range(16)]
    return base + [blend_rgb(c, theme.background_color, 0.4) for c in base]


def restyle(svg: str) -> str:
    """Map Rich's default export colours to THEME and widen the font stack. Text is untouched."""
    mapping = {
        old.hex.lower(): new.hex
        for old, new in zip(_text_colours(SVG_EXPORT_THEME), _text_colours(THEME), strict=True)
    }
    svg = re.sub(
        r'(fill: |fill=")(#[0-9a-fA-F]{6})',
        lambda m: m.group(1) + mapping.get(m.group(2).lower(), m.group(2)),
        svg,
    )
    return svg.replace(FONT_RULE, FONT_STACK)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["scan", "run", "restyle"])
    parser.add_argument("project", type=Path, help="the project, or for restyle the SVG")
    parser.add_argument("out", nargs="?", type=Path)
    parser.add_argument("model", nargs="?", default="claude-code:claude-haiku-4-5")
    parser.add_argument(
        "--width", type=int, default=None, help="terminal columns (default: scan 92, run 118)"
    )
    parser.add_argument("--all", action="store_true", help="scan: as `scan --all`")
    parser.add_argument(
        "--limit",
        type=int,
        default=SCAN_DEFAULT_LIMIT,
        help="scan --all: changes shown per package",
    )
    args = parser.parse_args()
    if args.limit != SCAN_DEFAULT_LIMIT and not (args.mode == "scan" and args.all):
        parser.error("--limit only applies to scan --all")
    mode, project_dir = args.mode, args.project
    if mode == "restyle":
        out = args.out or project_dir
        out.write_text(
            restyle(project_dir.read_text(encoding="utf-8")), encoding="utf-8", newline="\n"
        )
        print(f"wrote {out}")
        return
    out = args.out or Path(f"docs/img/{mode}.svg")
    project = load_project(project_dir)
    store = DiskCache()
    console = Console(
        file=io.StringIO(),
        record=True,
        width=args.width or (92 if mode == "scan" else 118),
        force_terminal=True,
        color_system="truecolor",
        emoji=False,
        highlight=False,
        legacy_windows=False,
    )
    if mode == "scan":
        flags = " --all" if args.all else ""
        if args.limit != SCAN_DEFAULT_LIMIT:
            flags += f" --limit {args.limit}"
        console.print(
            f"[bold]$[/bold] since-cutoff scan --model anthropic:claude-sonnet-4-5{flags}"
        )
        # As `since-cutoff scan` prints it: the model line, the stages (the reporter prints
        # them on the same console), then the results.
        settings = Settings(model="anthropic:claude-sonnet-4-5")
        reporter = RichReporter(console)
        engine = Engine(settings, store=store, llm_cache=store, reporter=reporter)
        target = engine.resolve_target(allow_calls=False)
        console.print(_model_line(target))
        scan = engine.scan(project, target)
        reporter.done()
        console.print()
        render_scan(console, scan, show_all=args.all, limit=args.limit, shown=reporter.warned)
    else:
        model = args.model
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
    # export_svg + write_text: LF line endings on every OS (save_svg writes CRLF on Windows)
    svg = console.export_svg(title=f"since-cutoff {mode}", theme=THEME)
    out.write_text(svg.replace(FONT_RULE, FONT_STACK), encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
