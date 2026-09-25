"""Cross-model study: how often do models write stale code for popular Python libraries?

For every (model, library) pair this builds a one-dependency project pinned to the library's
current release, runs the normal since-cutoff pipeline, and aggregates the probe outcomes.

    python scripts/study.py --models claude-code:claude-haiku-4-5 claude-code:claude-sonnet-4-5 \
        --packages anthropic openai huggingface-hub langchain-core --max-probes 8 --out study

Outputs <out>/study.csv (one row per model x package) and <out>/study.md (tables).
Everything is cached, so interrupted studies resume where they stopped.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from rich.console import Console

from since_cutoff.cache import DiskCache
from since_cutoff.engine import DEPRECATED, PASS, STALE, WRONG, Engine, Settings
from since_cutoff.errors import SinceCutoffError
from since_cutoff.project import load_project
from since_cutoff.pypi import PyPI

DEFAULT_PACKAGES = [
    "anthropic",
    "openai",
    "huggingface-hub",
    "langchain-core",
    "langgraph",
    "pydantic",
    "fastapi",
    "transformers",
    "numpy",
    "pandas",
    "sqlalchemy",
    "httpx",
    "typer",
    "mcp",
    "fastmcp",
    "pydantic-ai",
    "dspy",
    "litellm",
    "google-genai",
    "boto3",
]


def run_pair(
    model: str, package: str, version: str, args: argparse.Namespace, console: Console
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="since-cutoff-study-") as tmp:
        root = Path(tmp)
        (root / "requirements.txt").write_text(f"{package}=={version}\n", encoding="utf-8")
        settings = Settings(
            model=model,
            max_probes=args.max_probes,
            heldout=args.heldout,
            regression=0,
            jobs=args.jobs,
        )
        store = DiskCache()
        engine = Engine(settings, store=store, llm_cache=store)
        target = engine.resolve_target()
        scan = engine.scan(load_project(root), target)
        pkg = scan.package(package.lower())
        row: dict[str, object] = {
            "model": target.model_id,
            "cutoff": target.cutoff.isoformat(),
            "package": package,
            "version": version,
            "model_knows": pkg.cutoff_version or "",
            "status": pkg.status,
            "breaking_changes": len(pkg.changes),
        }
        if not pkg.changes:
            return row
        run = engine.run(scan, fix=args.heldout > 0)
        valid = [a for a in run.probes if a.valid]
        for name, key in (
            (PASS, "correct"),
            (STALE, "stale"),
            (WRONG, "wrong"),
            (DEPRECATED, "deprecated"),
        ):
            row[key] = sum(a.outcome == name for a in valid)
        row["probed"] = len(valid)
        pairs = run.pairing("heldout")
        row["heldout_n"], row["heldout_before"], row["heldout_after"] = pairs.n, pairs.before, pairs.after
        console.print(
            f"  {target.model_id:<32} {package:<18} probed={len(valid):<3} stale={row['stale']:<3} wrong={row['wrong']}"
        )
        return row


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--packages", nargs="+", default=DEFAULT_PACKAGES)
    parser.add_argument("--max-probes", type=int, default=8)
    parser.add_argument(
        "--heldout", type=int, default=0, help="also verify notes on held-out tasks"
    )
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--out", default="study")
    args = parser.parse_args()

    console = Console()
    pypi = PyPI(DiskCache())
    versions = {p: pypi.latest(p).version for p in args.packages}
    rows: list[dict[str, object]] = []
    for model in args.models:
        for package in args.packages:
            try:
                rows.append(run_pair(model, package, versions[package], args, console))
            except SinceCutoffError as exc:
                console.print(f"[yellow]skip {model} {package}: {exc}[/yellow]")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fields = sorted(
        {k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99
    )
    with (out / "study.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    by_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for k in ("probed", "stale", "wrong", "correct", "deprecated"):
            by_model[str(r["model"])][k] += int(r.get(k, 0) or 0)
        by_model[str(r["model"])]["cutoff"] = 0
    lines = [
        "| model | cutoff | probed | stale | wrong | correct |",
        "|---|---|---:|---:|---:|---:|",
    ]
    cutoffs = {str(r["model"]): str(r["cutoff"]) for r in rows}
    for model, c in by_model.items():
        n = c["probed"] or 1
        lines.append(
            f"| {model} | {cutoffs[model]} | {c['probed']} | {c['stale']} ({100 * c['stale'] / n:.0f}%) "
            f"| {c['wrong']} ({100 * c['wrong'] / n:.0f}%) | {c['correct']} ({100 * c['correct'] / n:.0f}%) |"
        )
    (out / "study.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
