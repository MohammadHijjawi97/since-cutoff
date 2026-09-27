"""Human (terminal, Markdown) and machine (JSON) reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from since_cutoff import __version__
from since_cutoff.apidiff import APIChange
from since_cutoff.engine import (
    CHANGED,
    DEPRECATED,
    ERROR,
    INVALID,
    KNOWN,
    NEW,
    OFFTASK,
    PASS,
    SKIPPED,
    STALE,
    UNCHANGED,
    UNTOUCHED,
    WRONG,
    RunResult,
    ScanResult,
)
from since_cutoff.selection import uses_text
from since_cutoff.stats import estimate_tokens, pct, wilson_interval

REPO_URL = "https://github.com/MohammadHijjawi97/since-cutoff"

STATUS_LABEL = {
    CHANGED: "API changed",
    UNCHANGED: "no breaking changes",
    KNOWN: "released before cutoff",
    NEW: "newer than the model",
    SKIPPED: "not checked",
}
# How every report counts: each change once, under its shortest public path.
COUNTING_NOTE = "A change reachable under several import paths is counted once."


# ------------------------------------------------------------------ summary
def per_package(scan: ScanResult, run: RunResult | None) -> list[dict[str, Any]]:
    rows = []
    for p in scan.packages:
        probes = [a for a in (run.probes if run else []) if a.change.package == p.name]
        valid = [a for a in probes if a.valid]
        breaking, deprecations = p.counts
        rows.append(
            {
                "package": p.name,
                "locked": p.locked,
                "locked_date": p.locked_date,
                "cutoff_version": p.cutoff_version,
                "cutoff_version_date": p.cutoff_version_date,
                "status": p.status,
                "breaking_changes": breaking,
                "deprecations": deprecations,
                "probed": len(valid),
                "stale": sum(a.outcome == STALE for a in valid),
                "wrong": sum(a.outcome == WRONG for a in valid),
                "deprecated": sum(a.outcome == DEPRECATED for a in valid),
                "correct": sum(a.outcome == PASS for a in valid),
                "excluded": len(probes) - len(valid),
                "reason": p.reason,
            }
        )
    return rows


def summary(scan: ScanResult, run: RunResult | None = None) -> dict[str, Any]:
    checked = [p for p in scan.packages if p.status != SKIPPED]
    packages = per_package(scan, run)
    out: dict[str, Any] = {
        "tool": f"since-cutoff {__version__}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": scan.target.model_id or None,
        "model_spec": scan.target.spec or None,
        "effort": scan.target.effort,
        "cutoff": scan.target.cutoff.isoformat(),
        "cutoff_source": scan.target.cutoff_source,
        "project": str(scan.project.root),
        "version_source": scan.project.version_source,
        "dependencies_total": len(scan.packages),
        "dependencies_checked": len(checked),
        "dependencies_skipped": len(scan.packages) - len(checked),
        "dependencies_changed": len(scan.changed),
        "dependencies_newer_than_model": sum(p.status == NEW for p in scan.packages),
        "breaking_changes": sum(r["breaking_changes"] for r in packages),
        "deprecations": sum(r["deprecations"] for r in packages),
        "warnings": list(scan.warnings),
        "packages": packages,
    }
    if run is None:
        return out
    valid = [a for a in run.probes if a.valid]
    counts = {k: sum(a.outcome == k for a in valid) for k in (PASS, STALE, WRONG, DEPRECATED)}
    out["probes"] = {
        "total": len(run.probes),
        "valid": len(valid),
        **counts,
        "excluded": {
            k: sum(a.outcome == k for a in run.probes) for k in (UNTOUCHED, OFFTASK, INVALID, ERROR)
        },
        "changes_without_tasks": len(run.skipped_changes),
        "cached_answers": sum(a.cached for a in run.probes),
    }
    out["probed_dependencies"] = sorted({a.change.package for a in valid})
    out["stale_dependencies"] = sorted({a.change.package for a in valid if a.outcome == STALE})
    if run.notes:
        out["notes"] = {
            "count": len(run.notes),
            "verified": sum(n.verified for n in run.notes),
            "from_diff": sum(n.source == "template" for n in run.notes),
            "tokens": estimate_tokens(run.block or ""),
        }
    if run.heldout:
        out["heldout"] = {}
        for role in ("heldout", "regression"):
            p = run.pairing(role)
            if p.n == 0 and p.excluded == 0:
                continue
            entry: dict[str, Any] = p.to_dict()
            entry["ci95_changes_fixed"] = wilson_interval(p.changes_fixed, p.changes)
            out["heldout"][role] = entry
    return out


# ------------------------------------------------------------------ console
def headline(
    scan: ScanResult, run: RunResult | None, s: dict[str, Any] | None = None
) -> list[Text]:
    """The summary lines at the top of every report. ``s`` is :func:`summary`, if at hand.

    Each line is short and self-contained (no parenthesis that a wrap could separate from its
    other half), because the terminal wraps it inside a panel.
    """
    s = s or summary(scan, run)
    lines: list[Text] = []
    if run is not None and run.probes:
        probed = s["probed_dependencies"]
        if not probed:
            ex = s["probes"]["excluded"]
            lines.append(
                Text(
                    f"No probe produced a scorable answer ({ex[ERROR]} errors, {ex[OFFTASK]} off-task, "
                    f"{ex[UNTOUCHED]} untouched, {ex[INVALID]} invalid)",
                    style="bold yellow",
                )
            )
        else:
            stale = len(s["stale_dependencies"])
            lines.append(
                Text.assemble(
                    ("Stale API use in ", "bold"),
                    (f"{stale} of {len(probed)}", "bold red" if stale else "bold green"),
                    (" probed dependencies", "bold"),
                )
            )
    lines.append(
        Text.assemble(
            (f"{s['dependencies_changed']} of {s['dependencies_checked']}", "bold yellow"),
            " dependencies changed their API after the cutoff",
        )
    )
    if s["breaking_changes"] or s["deprecations"]:
        lines.append(
            Text(
                f"Static diff: {_plural(s['breaking_changes'], 'breaking change')}, "
                f"{_plural(s['deprecations'], 'new deprecation')}",
                style="dim",
            )
        )
    if s["dependencies_newer_than_model"]:
        lines.append(
            Text(
                f"{s['dependencies_newer_than_model']} dependencies did not exist yet at the cutoff",
                style="dim",
            )
        )
    if s["dependencies_skipped"]:
        first = next((p.reason for p in scan.skipped if p.reason), "")
        lines.append(
            Text(
                f"{s['dependencies_skipped']} of {s['dependencies_total']} dependencies could not "
                "be checked" + (f"; for example: {first[:110]}" if first else ""),
                style="yellow",
            )
        )
    if run is not None and run.probes:
        p = s["probes"]
        text = Text.assemble(
            f"Probed {p['valid']} API changes: ",
            (f"{p['stale']} stale", "red"),
            " · ",
            (f"{p['wrong']} wrong", "magenta"),
            " · ",
            (f"{p['deprecated']} deprecated", "yellow"),
            " · ",
            (f"{p['pass']} correct", "green"),
        )
        excluded = {k: v for k, v in p["excluded"].items() if v}
        if excluded:
            text.append(
                "  (" + ", ".join(f"{v} {k}" for k, v in excluded.items()) + " not counted)",
                style="dim",
            )
        lines.append(text)
    if run is not None and run.notes:
        n = s["notes"]
        detail = (
            f" ({n['verified']} checked by the type checker, {n['from_diff']} from the API diff)"
            if n["from_diff"]
            else " (all checked by the type checker)"
        )
        lines.append(Text(f"Fix: {n['count']} notes{detail}, about {n['tokens']} tokens"))
    if run is not None and "heldout" in s.get("heldout", {}):
        h = s["heldout"]["heldout"]
        lo, hi = h["ci95_changes_fixed"]
        lines.append(
            Text.assemble(
                "Held-out tasks correct without -> with notes: ",
                (pct(h["before"], h["n"]), "red"),
                " -> ",
                (pct(h["after"], h["n"]), "bold green"),
                (
                    f"  ({h['n']} paired tasks; {h['changes_fixed']} of {h['changes']} changes "
                    f"fixed, 95% CI {100 * lo:.0f}-{100 * hi:.0f}%)",
                    "dim",
                ),
            )
        )
        reg = s["heldout"].get("regression")
        if reg and reg["n"]:
            lines.append(
                Text(
                    f"Previously-correct APIs with notes: {reg['after']}/{reg['n']} still correct"
                    + (f" ({reg['broken']} broken)" if reg["broken"] else ""),
                    style="dim",
                )
            )
        else:
            lines.append(
                Text(
                    "No previously-correct APIs were available for a regression check", style="dim"
                )
            )
    return lines


def render_console(
    console: Console, scan: ScanResult, run: RunResult | None = None, *, verbose: bool = False
) -> None:
    s = summary(scan, run)
    if s["model"]:
        title = f"since-cutoff · {escape(s['model'])} · training cutoff {s['cutoff']}"
    else:
        title = f"since-cutoff · custom cutoff {s['cutoff']}"
    sub = (
        f"{escape(scan.project.root.name)} · {s['dependencies_total']} dependencies"
        + (f" ({s['dependencies_skipped']} not checked)" if s["dependencies_skipped"] else "")
        + f" · versions from {escape(s['version_source'])}"
    )
    lines = headline(scan, run, s)
    # rich fits a panel to its body and title but not to its subtitle, which it would cut: size
    # it here (expand=True then means "exactly this wide").
    width = 6 + max(
        Text.from_markup(title).cell_len,
        Text.from_markup(sub).cell_len,
        *(t.cell_len for t in lines),
    )
    # The CLI's console does not wrap (soft_wrap), which would crop long lines inside the panel.
    console.print(
        Panel(
            Group(*lines),
            title=title,
            subtitle=sub,
            expand=True,
            width=min(width, console.width),
            padding=(1, 2),
        ),
        soft_wrap=False,
    )

    table = Table(show_edge=False, header_style="bold", pad_edge=False)
    table.add_column("package", no_wrap=True)
    table.add_column("you use", justify="right", no_wrap=True)
    table.add_column("at cutoff", justify="right", no_wrap=True)
    table.add_column("status")
    table.add_column("changes", justify="right")
    has_probes = run is not None and bool(run.probes)
    if has_probes:
        table.add_column("probed", justify="right")
        table.add_column("stale", justify="right", style="red")
        table.add_column("wrong", justify="right", style="magenta")
    for row in s["packages"]:
        if row["status"] in (KNOWN, UNCHANGED) and not verbose:
            continue
        status = STATUS_LABEL.get(row["status"], row["status"])
        if row["reason"] and (verbose or row["status"] == SKIPPED):
            status += f" ({row['reason'][:80]})"
        cells = [
            row["package"],
            row["locked"] or "?",
            row["cutoff_version"] or "-",
            escape(status),
            str(row["breaking_changes"] or "") if row["status"] == CHANGED else "",
        ]
        if has_probes:
            cells += [str(row["probed"] or ""), str(row["stale"] or ""), str(row["wrong"] or "")]
        table.add_row(*cells)
    hidden = sum(r["status"] in (KNOWN, UNCHANGED) for r in s["packages"])
    if table.row_count:
        console.print(table)
    if hidden and not verbose:
        console.print(
            Text(
                f"{hidden} more dependencies were not flagged (released before the cutoff, or no "
                "breaking changes); --verbose shows them",
                style="dim",
            )
        )
    for w in scan.warnings:
        console.print(Text(f"! {w}", style="yellow"))

    if has_probes and run is not None:
        failing = [a for a in run.probes if a.outcome in (STALE, WRONG, DEPRECATED)]
        if failing:
            console.print()
            console.print(Text("What the model got wrong", style="bold"))
            for a in failing[: None if verbose else 12]:
                console.print(
                    Text.assemble(
                        (f"  {a.outcome:<11}", _STYLE[a.outcome]),
                        a.change.describe(short=True).replace("`", ""),
                    )
                )
                if verbose and a.errors:
                    console.print(Text(f"             {a.errors[0]}", style="dim"))
            if len(failing) > 12 and not verbose:
                console.print(
                    Text(f"  ... and {len(failing) - 12} more (see the report)", style="dim")
                )


_STYLE = {STALE: "red", WRONG: "magenta", DEPRECATED: "yellow", PASS: "green"}


def render_scan_changes(console: Console, scan: ScanResult, limit: int = 8) -> None:
    limit = max(0, limit)
    for p in scan.changed:
        breaking, deprecated = p.counts
        console.print()
        console.print(
            Text(
                f"{p.name} {p.cutoff_version} -> {p.locked}: {breaking} breaking, "
                f"{deprecated} deprecated",
                style="bold",
            )
        )
        ranked, ids = scan.ranked(p), scan.uses(p)
        for c in ranked[:limit]:
            uses = uses_text(c, ids)
            line = c.describe(short=True, versioned=False)
            line += f" (your code uses {uses})" if uses else ""
            console.print(Text("  - " + line.replace("`", "")))
        if len(ranked) > limit:
            console.print(Text(f"  ... {len(ranked) - limit} more in the report", style="dim"))


# ----------------------------------------------------------------- markdown
def _cell(text: object) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def render_markdown(scan: ScanResult, run: RunResult | None = None) -> str:
    s = summary(scan, run)
    out = [
        "# since-cutoff report",
        "",
        f"- {_cutoff_md(s)}" + (f", thinking effort `{s['effort']}`" if s["effort"] else ""),
        f"- Project: `{scan.project.root.name}`, versions from `{s['version_source']}`",
        f"- Generated by since-cutoff {__version__} at {s['generated_at']}",
        "",
        "## Summary",
        "",
    ]
    out += [f"- {t.plain}" for t in headline(scan, run, s)]
    out += [f"- Warning: {w}" for w in scan.warnings]
    out += [
        "",
        "## Dependencies",
        "",
        "| package | you use | at cutoff | status | changes | probed | stale | wrong |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for r in s["packages"]:
        status = STATUS_LABEL.get(r["status"], r["status"]) + (
            f": {r['reason']}" if r["reason"] else ""
        )
        out.append(
            f"| {r['package']} | {r['locked'] or '?'} ({r['locked_date'] or '-'}) "
            f"| {r['cutoff_version'] or '-'} | {_cell(status)} | {r['breaking_changes'] or ''} "
            f"| {r['probed'] or ''} | {r['stale'] or ''} | {r['wrong'] or ''} |"
        )
    if run is not None and run.block:
        out += ["", "## Notes written for AGENTS.md", "", "```markdown", run.block.strip(), "```"]
    if run is not None and run.probes:
        out += ["", "## Probes", ""]
        for a in run.probes:
            out += [f"### {a.outcome.upper()}: {a.change.describe()}", "", f"> {a.task}", ""]
            if a.errors:
                out += ["Type checker (your version):", ""]
                out += [f"- `{e}`" for e in a.errors[:5]] + [""]
            if a.code:
                out += ["<details><summary>model's code</summary>", "", "```python"]
                out += [a.code.strip(), "```", "", "</details>", ""]
            if a.error:
                out += [f"Error: {a.error}", ""]
    if run is not None and run.skipped_changes:
        out += ["", "## Changes without usable tasks", ""]
        out += [f"- {c.describe()}: {r}" for c, r in run.skipped_changes]
    if run is not None and run.heldout:
        out += ["", "## Held-out verification", ""]
        out += ["| change | task | without notes | with notes |", "|---|---|---|---|"]
        pairs: dict[tuple[str, str, str], dict[bool, str]] = {}
        for a in run.heldout:
            pairs.setdefault((a.role, a.change.describe(), a.task), {})[a.with_notes] = a.outcome
        for (role, change, task), res in pairs.items():
            tag = " (regression check)" if role == "regression" else ""
            out.append(
                f"| {_cell(change)}{tag} | {_cell(task)} | {res.get(False, '-')} | {res.get(True, '-')} |"
            )
    if scan.changed:
        out += [
            "",
            "## All changes found",
            "",
            f"{COUNTING_NOTE} results.json lists every path.",
            "",
        ]
        for p in scan.changed:
            breaking, deprecated = p.counts
            out += [
                f"### {p.name} {p.cutoff_version} -> {p.locked}: {breaking} breaking, "
                f"{deprecated} deprecated",
                "",
            ]
            ids = scan.uses(p)
            out += [_md_change(c, ids, example=True) for c in scan.ranked(p)]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def _cutoff_md(s: dict[str, Any]) -> str:
    if not s["model"]:
        return f"Custom cutoff **{s['cutoff']}** (given with --cutoff, no model)"
    return f"Model `{s['model']}`, training cutoff **{s['cutoff']}** (source: {s['cutoff_source']})"


def _md_change(change: APIChange, ids: set[str], *, example: bool = False) -> str:
    """One Markdown list item: the change, how many paths share it, and what the code uses."""
    similar = ""
    if change.occurrences > 1:
        e_g = f", e.g. `{change.also[0]}`" if example and change.also else ""
        similar = f" (also: {change.occurrences - 1} similar{e_g})"
    uses = uses_text(change, ids)
    mark = f" · **your code uses {uses}**" if uses else ""
    return f"- {change.describe(versioned=False)}{similar}{mark}"


def render_scan_markdown(scan: ScanResult, *, limit: int = 8) -> str:
    """A short GitHub-flavoured Markdown summary of a scan, for CI job summaries and PR comments.

    One table row per flagged dependency, then the top ``limit`` changes of each changed one,
    with changes to names the project's code uses first. The full list stays in report.md.
    Every count is of distinct changes, as in the full report and the MCP tools.
    """
    s = summary(scan)
    project = scan.project
    out = [
        "## since-cutoff scan",
        "",
        f"{_cutoff_md(s)} · project `{project.root.name}`, versions from `{s['version_source']}`",
        "",
    ]
    out += [f"- {t.plain}" for t in headline(scan, None, s)]
    out += [f"- Warning: {w}" for w in scan.warnings]

    imported = {p.name for p in scan.changed if project.imports(p.import_names)}
    changed = sorted(scan.changed, key=lambda p: (p.name not in imported, -p.counts[0], p.name))
    flagged = [*changed, *(p for p in scan.packages if p.status in (NEW, SKIPPED))]
    if flagged:
        out += [
            "",
            "| package | at cutoff | you use | status | breaking | deprecated |",
            "|---|---|---|---|---:|---:|",
        ]
    for p in flagged:
        status = STATUS_LABEL.get(p.status, p.status)
        if p.name in imported:
            status += ", imported by your code"
        elif p.reason:
            status += f": {p.reason[:120]}"
        counts = p.counts if p.status == CHANGED else ("", "")
        out.append(
            f"| {p.name} | {_version_cell(p.cutoff_version, p.cutoff_version_date)} "
            f"| {_version_cell(p.locked, p.locked_date)} | {_cell(status)} "
            f"| {counts[0]} | {counts[1]} |"
        )
    quiet = [p for p in scan.packages if p.status in (KNOWN, UNCHANGED)]
    if quiet:
        names = ", ".join(f"{p.name} {p.locked or ''}".strip() for p in quiet)
        out += ["", f"Not flagged (released before the cutoff, or no breaking changes): {names}"]

    limit = max(0, limit)
    for p in changed:
        ranked, ids = scan.ranked(p), scan.uses(p)
        hits = sum(uses_text(c, ids) is not None for c in ranked)
        breaking, deprecated = p.counts
        detail = f"{breaking} breaking, {deprecated} deprecated" + (
            f", {hits} touching names your code uses" if hits else ""
        )
        out += [
            "",
            f"<details{' open' if hits else ''}><summary><b>{p.name}</b> "
            f"{p.cutoff_version} -> {p.locked}: {detail}</summary>",
            "",
        ]
        out += [_md_change(c, ids) for c in ranked[:limit]]
        if len(ranked) > limit:
            out.append(f"- ... and {len(ranked) - limit} more in the full report")
        out += ["", "</details>"]
    out += [
        "",
        f"<sub>[since-cutoff]({REPO_URL}) {__version__}: static diff of the public API, "
        f"no model calls. {COUNTING_NOTE} Behaviour changes behind an unchanged signature are "
        "not shown.</sub>",
    ]
    return "\n".join(out) + "\n"


def _version_cell(version: str | None, day: str | None) -> str:
    if not version:
        return "-"
    return f"{version} ({day})" if day else version


def _plural(n: int, word: str) -> str:
    return f"{n} {word if n == 1 else word + 's'}"


def to_json(scan: ScanResult, run: RunResult | None = None) -> dict[str, Any]:
    data = summary(scan, run)
    data["scan"] = [p.to_dict() for p in scan.packages]
    if run is not None:
        data["attempts"] = [a.to_dict() for a in run.probes + run.heldout]
        data["notes_detail"] = [n.to_dict() for n in run.notes]
        data["block"] = run.block
        data["skipped_changes"] = [
            {"change": c.describe(), "reason": r} for c, r in run.skipped_changes
        ]
    return data


def write_outputs(
    out_dir: Path, scan: ScanResult, run: RunResult | None = None
) -> tuple[Path, Path]:
    created = not out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    gitignore = out_dir / ".gitignore"
    # Only hide a directory we own; never drop a catch-all .gitignore into an existing folder.
    if (created or out_dir.name == ".since-cutoff") and not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    md = out_dir / "report.md"
    js = out_dir / "results.json"
    md.write_text(render_markdown(scan, run), encoding="utf-8")
    js.write_text(json.dumps(to_json(scan, run), indent=2, default=str), encoding="utf-8")
    return md, js
