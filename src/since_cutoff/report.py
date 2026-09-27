"""Human (terminal, Markdown) and machine (JSON) reports."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from since_cutoff import __version__
from since_cutoff.apidiff import DIFF_SCHEMA, PARAM_REMOVED, APIChange
from since_cutoff.baselines import ARM_SIGNATURES, ARM_TEMPLATE, ARM_VERIFIED
from since_cutoff.engine import (
    CHANGE_BROKEN,
    CHANGE_FIXED,
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
    Attempt,
    Pairing,
    RunResult,
    ScanResult,
)
from since_cutoff.project import FileUse
from since_cutoff.selection import other_paths_text, uses_text
from since_cutoff.stats import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    cluster_bootstrap_interval,
    estimate_tokens,
    format_p,
    pct,
    points,
    points_between,
    sign_test,
    wilson_interval,
)

REPO_URL = "https://github.com/MohammadHijjawi97/since-cutoff"

STATUS_LABEL = {
    CHANGED: "API changed",
    UNCHANGED: "no breaking changes",
    KNOWN: "released before cutoff",
    NEW: "newer than the model",
    SKIPPED: "not checked",
}
# Added to the status of a dependency the project's code imports (PackageScan.imported).
IMPORTED = "imported by your code"
# Characters of a skip reason shown in the terminal (the reports have it in full).
REASON_WIDTH = 120
# How every report counts: each change once, under its shortest public path.
COUNTING_NOTE = "A change reachable under several import paths is counted once."
# How held-out answers are counted (RunResult.pairing, ChangePairs.outcome).
PAIRING_RULE = (
    "A held-out task counts as a pair when its answer without the notes is scorable and its "
    "answer with the notes is not an error; with the notes, only a passing answer is correct "
    "(an answer that avoids the package counts as wrong). An API change is fixed when more than "
    "half of its counted pairs are wrong without the notes and correct with them (with one or "
    "two held-out tasks per change: all of them), and broken in the mirror case."
)
STATS_NOTE = (
    "The difference is with minus without, in percentage points over all counted pairs; its 95% "
    "CI is a percentile bootstrap that resamples API changes with all their pairs "
    f"({BOOTSTRAP_RESAMPLES} resamples, seed {BOOTSTRAP_SEED}). The interval for changes fixed is "
    "a Wilson score interval. The sign test is exact and two-sided, over changes fixed vs "
    "changes broken (changes that are neither are left out). The regression check covers changes "
    "the model already got right, so it counts the changes still correct with the notes (more "
    "than half of their pairs), with a Wilson interval, instead. With few API changes (under about "
    "ten) the bootstrap interval tends to be too narrow; the sign test is exact at any size."
)
# Report names of the notes arms (``run --compare``), and what the baselines are.
ARM_TITLES = {
    ARM_VERIFIED: "verified notes",
    ARM_TEMPLATE: "template baseline",
    ARM_SIGNATURES: "signatures baseline",
}
ARMS_NOTE = (
    "Every held-out task and regression check was also answered with each baseline block in "
    "place of the verified notes. The blocks share their header, are paired against the same "
    "answers without notes, are counted by the same rules and are compared on the same pairs: "
    "a pair counts for every block only when its answer without notes is scorable and no "
    "block's answer with notes is an error (for the other blocks, such a pair is not counted "
    'under "error with another block"). So the verified notes\' row can count fewer pairs than '
    "their result above, which keeps every pair they can count. The template baseline states each "
    "change in one sentence from the API diff; the signatures baseline gives the new signature "
    "and first docstring paragraph of each changed API, or of the replacement its library names. "
    "Neither calls a model. The last column counts the API changes that only the verified notes "
    "fixed and those that only the baseline fixed, with an exact two-sided sign test."
)


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
                "imported": p.imported,
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
    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {
        "tool": f"since-cutoff {__version__}",
        "generated_at": now.isoformat(timespec="seconds"),
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
        "settings": {
            "tool_version": __version__,
            "diff_schema": DIFF_SCHEMA,
            "date": now.date().isoformat(),
            **(run.settings if run is not None else {}),
        },
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
            out["heldout"][role] = pairing_summary(p, role)
        if run.arms:
            out["arms"] = arms_summary(run)
    return out


def arms_summary(run: RunResult) -> dict[str, dict[str, Any]]:
    """Each notes arm of a ``--compare`` run, the verified notes first.

    Per arm: the size of its block, its held-out and regression pairings with their statistics
    (:func:`pairing_summary`), and for a baseline, how it did against the verified notes
    (:func:`head_to_head`). Every arm is paired on the same pairs (``RunResult.pairing(...,
    common=True)``): a pair counts only when its answer without notes is scorable and no
    block's answer with notes is an error. So the verified notes' row here can count fewer
    pairs than their own result (``summary()["heldout"]``), which keeps every pair they can
    count, as in a run without ``--compare``.
    """
    verified = run.pairing("heldout", common=True)
    out: dict[str, dict[str, Any]] = {}
    for arm, block in run.arms.items():
        heldout = run.pairing("heldout", arm, common=True)
        regression = run.pairing("regression", arm, common=True)
        out[arm] = {
            "tokens": estimate_tokens(block),
            "bullets": sum(line.startswith("- ") for line in block.splitlines()),
            "heldout": pairing_summary(heldout),
            "regression": pairing_summary(regression, "regression"),
            "versus_verified": None if arm == ARM_VERIFIED else head_to_head(verified, heldout),
        }
    return out


def head_to_head(verified: Pairing, baseline: Pairing) -> dict[str, Any]:
    """The API changes one arm fixed and the other did not, with the exact sign test.

    Both pairings must count the same pairs (``RunResult.pairing(..., common=True)``), so that
    each change's outcome rests on the same tasks in both arms, answered the same way without
    notes: then what differs is the notes alone. (Paired separately, an error on the one task a
    baseline did not fix would drop that task from the baseline alone and hand it a "fix".)
    Only changes counted in both arms are compared.
    """
    both = {c.change_id for c in verified.per_change} & {c.change_id for c in baseline.per_change}
    fixed_verified = {c.change_id for c in verified.per_change if c.outcome == CHANGE_FIXED}
    fixed_baseline = {c.change_id for c in baseline.per_change if c.outcome == CHANGE_FIXED}
    only_verified = len((fixed_verified - fixed_baseline) & both)
    only_baseline = len((fixed_baseline - fixed_verified) & both)
    return {
        "changes": len(both),
        "fixed_by_verified_only": only_verified,
        "fixed_by_this_only": only_baseline,
        "sign_test_p": sign_test(only_verified, only_baseline),
    }


def pairing_summary(p: Pairing, role: str = "heldout") -> dict[str, Any]:
    """A pairing's counts with its statistics (see PAIRING_RULE and STATS_NOTE).

    ``difference`` is the task-level after - before rate and ``ci95_difference`` its cluster
    bootstrap interval (None with fewer than two changes); ``ci95_changes_fixed`` is the Wilson
    interval of ``changes_fixed`` of ``changes`` and nothing else; ``sign_test_p`` compares
    changes fixed with changes broken.

    The regression check asks something else of changes the model already got right: that
    they stay right. Its entry adds ``changes_still_correct`` (more than half of a change's
    pairs correct with the notes) with its Wilson interval ``ci95_changes_still_correct``;
    ``changes_fixed`` there counts wrong -> right, like everywhere, and is about 0.
    """
    entry = p.to_dict()
    if role == "regression":
        entry["changes_still_correct"] = p.changes_still_correct
        entry["ci95_changes_still_correct"] = wilson_interval(p.changes_still_correct, p.changes)
    entry["difference"] = (p.after - p.before) / p.n if p.n else None
    entry["ci95_difference"] = cluster_bootstrap_interval(
        [(c.n, c.after - c.before) for c in p.per_change]
    )
    entry["bootstrap"] = {
        "unit": "change",
        "resamples": BOOTSTRAP_RESAMPLES,
        "seed": BOOTSTRAP_SEED,
    }
    entry["ci95_changes_fixed"] = wilson_interval(p.changes_fixed, p.changes)
    entry["sign_test_p"] = sign_test(p.changes_fixed, p.changes_broken)
    return entry


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
                    (f" probed {_deps_word(len(probed))}", "bold"),
                )
            )
    checked = s["dependencies_checked"]
    lines.append(
        Text.assemble(
            (f"{s['dependencies_changed']} of {checked}", "bold yellow"),
            f" {_deps_word(checked)} changed {'its' if checked == 1 else 'their'} API "
            "after the cutoff",
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
        new = s["dependencies_newer_than_model"]
        lines.append(Text(f"{_deps(new)} did not exist yet at the cutoff", style="dim"))
    if s["dependencies_skipped"]:
        skipped, total = s["dependencies_skipped"], s["dependencies_total"]
        first = next((p.reason for p in scan.skipped if p.reason), "")
        if first:
            first = ("; for example: " if skipped > 1 else ": ") + clip(first, REASON_WIDTH)
        lines.append(
            Text(
                f"{skipped} of {_deps(total)} could not be checked{first}",
                style="yellow",
            )
        )
    if run is not None and run.probes:
        p = s["probes"]
        text = Text.assemble(
            f"Probed {_plural(p['valid'], 'API change')}: ",
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
        lines.append(
            Text(f"Fix: {_plural(n['count'], 'note')}{detail}, about {n['tokens']} tokens")
        )
    if run is not None and "heldout" in s.get("heldout", {}):
        lines += _heldout_lines(s["heldout"]["heldout"])
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
    if run is not None and "arms" in s:
        lines += _baseline_lines(s["arms"], s.get("heldout", {}).get("heldout"))
    return lines


def _baseline_lines(
    arms: dict[str, dict[str, Any]], verified_alone: dict[str, Any] | None = None
) -> list[Text]:
    """One line per ``--compare`` baseline, in the terms of the verified notes' lines.

    The baselines are counted on the pairs every block could count (:func:`arms_summary`).
    When an error left out pairs the verified notes' own line counts, a last line gives the
    verified notes' rates on those same pairs, so the lines compare like with like.
    """
    lines = []
    for arm, a in arms.items():
        if arm == ARM_VERIFIED:
            continue
        h = a["heldout"]
        text = (
            f"Baseline {arm}, about {a['tokens']} tokens: held-out "
            f"{pct(h['before'], h['n'])} -> {pct(h['after'], h['n'])}"
        )
        if h["changes"]:
            lo, hi = h["ci95_changes_fixed"]
            text += (
                f"; changes fixed {h['changes_fixed']} of {h['changes']}, 95% CI "
                f"{100 * lo:.0f}-{100 * hi:.0f}%; {h['changes_broken']} broken"
            )
        lines.append(Text(text, style="dim"))
    shared = arms.get(ARM_VERIFIED, {}).get("heldout")
    if lines and shared and verified_alone and shared["n"] != verified_alone["n"]:
        lines.append(
            Text(
                f"Blocks compared on the {_plural(shared['n'], 'held-out pair')} all could count; "
                f"verified notes on them: {pct(shared['before'], shared['n'])} -> "
                f"{pct(shared['after'], shared['n'])}; not counted: {_excluded_text(shared)}",
                style="dim",
            )
        )
    return lines


def _heldout_lines(h: dict[str, Any]) -> list[Text]:
    """The held-out result: task-level rates, their difference, then the change-level counts.

    Each interval sits next to the number it belongs to: the bootstrap interval next to the
    difference of the task-level rates, the Wilson interval next to "changes fixed".
    """
    lines = [
        Text.assemble(
            "Held-out tasks correct without -> with notes: ",
            (pct(h["before"], h["n"]), "red"),
            " -> ",
            (pct(h["after"], h["n"]), "bold green"),
            (
                f", {_plural(h['n'], 'paired task')} from {_plural(h['changes'], 'API change')}",
                "dim",
            ),
        )
    ]
    if h["n"]:
        lines.append(Text(_difference_text(h), style="dim"))
        lo, hi = h["ci95_changes_fixed"]
        lines.append(
            Text(
                f"Changes fixed by the notes: {h['changes_fixed']} of {h['changes']}, 95% CI "
                f"{100 * lo:.0f}-{100 * hi:.0f}%; {h['changes_broken']} broken; "
                f"sign test {format_p(h['sign_test_p'])}",
                style="dim",
            )
        )
    if h["excluded"]:
        lines.append(Text(f"Held-out pairs not counted: {_excluded_text(h)}", style="dim"))
    return lines


def _shown_difference(h: dict[str, Any]) -> str:
    """The difference of the two rates as printed next to it (``12%`` -> ``75%`` is ``+63``),
    so that readers can check it; results.json keeps the exact ``difference``."""
    return points_between(h["before"], h["after"], h["n"])


def _difference_text(h: dict[str, Any]) -> str:
    text = f"Difference: {_shown_difference(h)} percentage points, "
    bounds = _bootstrap_bounds(h)
    if bounds is None:
        return text + _no_interval(h)
    return text + f"95% CI {bounds}, bootstrap over API changes"


def _bootstrap_bounds(h: dict[str, Any]) -> str | None:
    """``+40 to +80``, or None when there is no informative bootstrap interval.

    With fewer than two API changes there is nothing to resample; when every change has the
    same difference, every resample does too and the "interval" is a single point.
    """
    ci = h["ci95_difference"]
    if ci is None or len(_change_differences(h)) == 1:
        return None
    return f"{points(ci[0])} to {points(ci[1])}"


def _no_interval(h: dict[str, Any]) -> str:
    if h["ci95_difference"] is None:
        return "too few API changes for an interval"
    return f"the same in all {h['changes']} API changes, so no interval"


def _change_differences(h: dict[str, Any]) -> set[Fraction]:
    return {Fraction(c["after"] - c["before"], c["n"]) for c in h["per_change"]}


def _excluded_text(h: dict[str, Any]) -> str:
    reasons = [f"{n} {reason}" for reason, n in h["excluded_reasons"].items() if n]
    return ", ".join(reasons) or "none"


def render_console(
    console: Console, scan: ScanResult, run: RunResult | None = None, *, verbose: bool = False
) -> None:
    s = summary(scan, run)
    if s["model"]:
        title = f"since-cutoff · {escape(s['model'])} · training cutoff {s['cutoff']}"
    else:
        title = f"since-cutoff · custom cutoff {s['cutoff']}"
    sub = Text(
        f"{scan.project.root.name} · {_deps(s['dependencies_total'])}"
        + (f" ({s['dependencies_skipped']} not checked)" if s["dependencies_skipped"] else "")
        + f" · versions from {s['version_source']}"
    )
    lines = headline(scan, run, s)
    # rich fits a panel to its body and title but not to its subtitle, which it would cut: size
    # it here (expand=True then means "exactly this wide"). A subtitle too long for the console
    # goes into the body instead, where it wraps.
    subtitle: Text | None = sub
    if sub.cell_len + 6 > console.width:
        lines, subtitle = [*lines, Text(sub.plain, style="dim")], None
    width = 6 + max(
        Text.from_markup(title).cell_len,
        subtitle.cell_len if subtitle else 0,
        *(t.cell_len for t in lines),
    )
    # The CLI's console does not wrap (soft_wrap), which would crop long lines inside the panel.
    console.print(
        Panel(
            Group(*lines),
            title=title,
            subtitle=subtitle,
            expand=True,
            width=min(width, console.width),
            padding=(1, 2),
        ),
        soft_wrap=False,
    )

    # Every column may wrap, and a long name, version or flag folds: in a narrow terminal the
    # columns then share the width. (With the names kept whole, a long package name used to
    # squeeze the status and changes columns to nothing.) The rows come in the order of
    # Engine.scan: changed packages first, those the code imports first among them.
    has_probes = run is not None and bool(run.probes)
    rows = [r for r in s["packages"] if verbose or r["status"] not in (KNOWN, UNCHANGED)]
    widths = _narrow_widths(console.width, rows, has_probes)
    table = Table(show_edge=False, header_style="bold", pad_edge=False)
    table.add_column("package", overflow="fold", width=widths.get("package"))
    table.add_column("you use", justify="right", overflow="fold", width=widths.get("you use"))
    table.add_column("at cutoff", justify="right", overflow="fold", width=widths.get("at cutoff"))
    table.add_column("status", overflow="fold")
    table.add_column("breaking", justify="right", overflow="fold")
    table.add_column("deprecated", justify="right", overflow="fold")
    if has_probes:
        table.add_column("probed", justify="right", overflow="fold")
        table.add_column("stale", justify="right", style="red", overflow="fold")
        table.add_column("wrong", justify="right", style="magenta", overflow="fold")
    for row in rows:
        status = _status_text(row, short=bool(widths))
        if row["reason"] and (verbose or row["status"] == SKIPPED):
            status += f" ({clip(row['reason'], REASON_WIDTH)})"
        name = row["package"]
        cells = [
            escape(_fold_at_hyphens(name, widths["package"]) if widths else name),
            escape(row["locked"] or "?"),
            escape(row["cutoff_version"] or "-"),
            escape(status),
            *_count_cells(row),
        ]
        if has_probes:
            cells += [str(row["probed"] or ""), str(row["stale"] or ""), str(row["wrong"] or "")]
        table.add_row(*cells)
    hidden = sum(r["status"] in (KNOWN, UNCHANGED) for r in s["packages"])
    if table.row_count:
        console.print(table)
    if hidden and not verbose:
        more = " more" if table.row_count else ""
        were = "was" if hidden == 1 else "were"
        console.print(
            Text(
                f"{hidden}{more} {_deps_word(hidden)} {were} not flagged (released before the "
                "cutoff, or no breaking changes); --verbose shows them",
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


def _status_text(row: dict[str, Any], *, short: bool = False) -> str:
    """A dependency's status label, with :data:`IMPORTED` when the code imports it; with
    ``short``, in the form that fits a narrow table ("changed, imported")."""
    if short:
        status = _STATUS_SHORT.get(row["status"]) or STATUS_LABEL.get(row["status"], "")
        return f"{status}, imported" if row["imported"] else status
    status = STATUS_LABEL.get(row["status"], row["status"])
    return f"{status}, {IMPORTED}" if row["imported"] else status


# In a terminal narrower than this, the table uses the short status labels, the version
# columns get the width their versions need and the package names the rest, folded at "-".
# (At 80 columns every row took three lines, and names broke as "opentelemetr" / "y-sdk".)
NARROW_TABLE = 100
_STATUS_SHORT = {CHANGED: "changed", NEW: "new"}
_STATUS_WIDTH = len("changed, imported")


def _narrow_widths(width: int, rows: list[dict[str, Any]], probes: bool) -> dict[str, int]:
    """The widths of the package and version columns of a narrow table (see
    :data:`NARROW_TABLE`); empty when the terminal is wide, or too narrow for this layout."""
    if width >= NARROW_TABLE or not rows:
        return {}
    locked = max(len("you use"), *(len(r["locked"] or "?") for r in rows))
    cutoff = max(len("at cutoff"), *(len(r["cutoff_version"] or "-") for r in rows))
    counts = ["breaking", "deprecated", *(["probed", "stale", "wrong"] if probes else [])]
    columns = 4 + len(counts)
    # Each column is padded by a space on either side, but for the outer edges, and a line
    # separates the columns: 3 characters between two columns.
    room = width - 3 * (columns - 1) - locked - cutoff - _STATUS_WIDTH - sum(map(len, counts))
    if room < 10:
        return {}
    longest = max(len(r["package"]) for r in rows)
    return {"package": min(room, longest), "you use": locked, "at cutoff": cutoff}


def _fold_at_hyphens(name: str, width: int) -> str:
    """``opentelemetry-sdk`` in lines of at most ``width`` characters, broken after a "-"
    where it can be (``opentelemetry-`` / ``sdk``), or before it when the part with its "-"
    is one character too long (``opentelemetry`` / ``-sdk``)."""
    lines = [""]
    carry = ""
    for part in re.split(r"(?<=-)", name):
        part, carry = carry + part, ""
        if part.endswith("-") and len(part) == width + 1:
            part, carry = part[:-1], "-"
        if lines[-1] and len(lines[-1]) + len(part) > width:
            lines.append(part)
        else:
            lines[-1] += part
    return "\n".join(lines)


def _count_cells(row: dict[str, Any]) -> list[str]:
    """The breaking and deprecated cells of a changed dependency (``0`` included); blank for
    the others, which were not diffed or have nothing to count."""
    if row["status"] != CHANGED:
        return ["", ""]
    return [str(row["breaking_changes"]), str(row["deprecations"])]


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
        ranked, files = scan.ranked(p), scan.uses(p)
        for c in ranked[:limit]:
            uses = uses_text(c, files)
            line = change_text(c, short=True)
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
    out += _settings_md(s["settings"])
    # The probe columns only when there were probes (never in a scan).
    probed = run is not None and bool(run.probes)
    out += [
        "",
        "## Dependencies",
        "",
        "| package | you use | at cutoff | status | breaking | deprecated |"
        + (" probed | stale | wrong |" if probed else ""),
        "|---|---|---|---|---:|---:|" + ("---:|---:|---:|" if probed else ""),
    ]
    for r in s["packages"]:
        status = _status_text(r) + (f": {r['reason']}" if r["reason"] else "")
        counts = " | ".join(_count_cells(r))
        row = (
            f"| {r['package']} | {r['locked'] or '?'} ({r['locked_date'] or '-'}) "
            f"| {r['cutoff_version'] or '-'} | {_cell(status)} | {counts} |"
        )
        if probed:
            row += f" {r['probed'] or ''} | {r['stale'] or ''} | {r['wrong'] or ''} |"
        out.append(row)
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
        out += _heldout_md(s.get("heldout", {}))
        if "arms" in s:
            out += _arms_md(s["arms"], run.arms)
        out += _pairs_md(run)
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
            files = scan.uses(p)
            out += [_md_change(c, files, example=True) for c in scan.ranked(p)]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def _pairs_md(run: RunResult) -> list[str]:
    """Every held-out task's outcomes: without notes, then with each arm's block."""
    arms = list(run.arms) or [ARM_VERIFIED]
    heads = [ARM_TITLES.get(arm, arm) for arm in arms] if run.arms else ["with notes"]
    out = ["", "### Pairs", ""]
    out += [
        "| change | task | without notes | " + " | ".join(heads) + " |",
        "|---|---|---|" + "---|" * len(heads),
    ]
    pairs: dict[tuple[str, str, str], dict[str | None, str]] = {}
    for a in run.heldout:
        key = (a.role, a.change.describe(), a.task)
        pairs.setdefault(key, {})[a.arm if a.with_notes else None] = a.outcome
    for (role, change, task), res in pairs.items():
        tag = " (regression check)" if role == "regression" else ""
        cells = [res.get(None, "-"), *(res.get(arm, "-") for arm in arms)]
        out.append(f"| {_cell(change)}{tag} | {_cell(task)} | " + " | ".join(cells) + " |")
    return out


def _arms_md(arms: dict[str, dict[str, Any]], blocks: dict[str, str]) -> list[str]:
    """The ``--compare`` table: one row per notes arm, the verified notes first; then each
    baseline's block, folded (the verified block is under "Notes written for AGENTS.md")."""
    out = ["", "### Compared with baseline notes", "", ARMS_NOTE, ""]
    out += [
        "| notes | tokens | held-out correct without -> with | held-out pairs not counted "
        "| changes fixed, 95% CI | changes broken | regression checks still correct "
        "| fixed only by the verified notes / only by this, sign test |",
        "|---|---:|---|---|---|---:|---|---|",
    ]
    for arm, a in arms.items():
        h, reg, versus = a["heldout"], a["regression"], a["versus_verified"]
        heldout = f"{pct(h['before'], h['n'])} -> {pct(h['after'], h['n'])} of {h['n']}"
        lo, hi = h["ci95_changes_fixed"]
        fixed = f"{h['changes_fixed']} of {h['changes']}, {100 * lo:.0f}-{100 * hi:.0f}%"
        regression = f"{reg['after']} of {reg['n']}" + (
            f", {reg['broken']} broken" if reg["broken"] else ""
        )
        head = "-"
        if versus is not None:
            head = (
                f"{versus['fixed_by_verified_only']} / {versus['fixed_by_this_only']}, "
                f"{format_p(versus['sign_test_p'])}"
            )
        cells = [
            ARM_TITLES.get(arm, arm),
            str(a["tokens"]),
            heldout if h["n"] else "n/a",
            _excluded_text(h),
            fixed if h["changes"] else "n/a",
            str(h["changes_broken"]),
            regression if reg["n"] else "none",
            head,
        ]
        out.append("| " + " | ".join(cells) + " |")
    for arm, block in blocks.items():
        if arm != ARM_VERIFIED:
            out += ["", f"<details><summary>{ARM_TITLES.get(arm, arm)} block</summary>", ""]
            out += ["```markdown", block.strip(), "```", "", "</details>"]
    return out


# Run settings in the order report.md lists them (Engine.run_settings, summary()["settings"]).
SETTING_LABELS = (
    ("tool_version", "since-cutoff version"),
    ("model", "model under test"),
    ("model_source", "model taken from"),
    ("task_model", "task and note writer"),
    ("effort", "Claude Code thinking effort"),
    ("prompt_version", "prompt version"),
    ("diff_schema", "API diff schema"),
    ("max_probes", "API changes probed, at most"),
    ("heldout", "held-out tasks per failure"),
    ("regression", "regression checks, at most"),
    ("python_version", "Python for type checking"),
    ("tasks_from", "tasks"),
    ("tasks_written_by", "tasks written by"),
    ("compare", "baseline notes compared"),
    ("date", "date"),
)


def _settings_md(settings: dict[str, Any]) -> list[str]:
    """What the numbers depend on besides the model's answers, as a table."""
    out = ["", "## Run settings", "", "| setting | value |", "|---|---|"]
    for key, label in SETTING_LABELS:
        if key not in settings:
            continue
        value = settings[key]
        if key == "task_model" and "tasks_written_by" in settings:
            label = "note writer"  # the tasks came from a file, written by another run
        if key == "tasks_from":
            text = f"from `{value}` (--tasks-from)" if value else "written for this run"
        elif key == "tasks_written_by":
            text = _written_by(value, settings.get("prompt_version"))
        elif key == "heldout" and settings.get("heldout_used"):
            used = settings["heldout_used"]
            fewest, most = used["fewest"], used["most"]
            text = f"{value} asked, {fewest if fewest == most else f'{fewest} to {most}'} used"
        elif key == "compare":
            text = ", ".join(value) + " (--compare)"
        elif key in ("model", "task_model") and value:
            text = f"`{value}`"
        else:
            text = "-" if value is None else str(value)
        out.append(f"| {label} | {_cell(text)} |")
    return out


def _written_by(provenance: dict[str, Any], prompt_version: object) -> str:
    """Who wrote the tasks of a ``--tasks-from`` file, as the file says, and whether its task
    prompt differs from this run's."""
    model = provenance.get("task_model")
    version = provenance.get("prompt_version")
    parts = [f"`{model}`" if model else "unknown model"]
    if version is None:
        parts.append("prompt version unknown")
    else:
        parts.append(f"prompt version {version}")
        if version != prompt_version:
            parts[-1] += f" (this run's prompts are version {prompt_version})"
    if provenance.get("tool"):
        parts.append(str(provenance["tool"]))
    return ", ".join(parts)


_ROLE_TITLES = {
    "heldout": "held-out tasks of failing changes",
    "regression": "regression check on correct changes",
}


def _heldout_md(heldout: dict[str, dict[str, Any]]) -> list[str]:
    """The held-out statistics, one column per role, then the counts of each API change."""
    out = ["", "## Held-out verification", "", PAIRING_RULE]
    roles = [(role, heldout[role]) for role in _ROLE_TITLES if role in heldout]
    if not roles:
        return out
    columns = [_heldout_cells(h, role) for role, h in roles]
    out += ["", "| measure | " + " | ".join(_ROLE_TITLES[r] for r, _ in roles) + " |"]
    out.append("|---|" + "---:|" * len(roles))
    for i, (label, _) in enumerate(columns[0]):
        values = [col[i][1] for col in columns]
        if any(v is not None for v in values):  # a row that applies to no role is left out
            out.append(f"| {label} | " + " | ".join(v or "-" for v in values) + " |")
    out += ["", STATS_NOTE, "", "### By API change", ""]
    out += [
        "| change | pairs | correct without | correct with | result |",
        "|---|---:|---:|---:|---|",
    ]
    for role, h in roles:
        tag = " (regression check)" if role == "regression" else ""
        for c in h["per_change"]:
            out.append(
                f"| {_cell(c['change'])}{tag} | {c['n']} | {c['before']} | {c['after']} "
                f"| {_change_result(c, role)} |"
            )
    return out


def _change_result(c: dict[str, Any], role: str) -> str:
    """What the notes did to one API change: fixed / broken / neither for a failing change;
    for the regression check (a change the model got right), still correct or broken."""
    if role != "regression" or c["outcome"] == CHANGE_BROKEN:
        return str(c["outcome"])
    return "still correct" if c["after"] * 2 > c["n"] else "wrong with the notes"


def _heldout_cells(h: dict[str, Any], role: str = "heldout") -> list[tuple[str, str | None]]:
    """(label, value) rows for one role of the held-out statistics table; None where a row
    does not apply to the role.

    The regression check covers changes the model already got right, so "changes fixed" (wrong
    without the notes, right with them) and its sign test say nothing there; it shows how many
    changes are still correct instead.
    """
    n = h["n"]
    regression = role == "regression"
    lo, hi = h["ci95_changes_fixed"]
    fixed = f"{h['changes_fixed']} of {h['changes']}, {100 * lo:.0f}-{100 * hi:.0f}%"
    still = None
    if regression:
        s_lo, s_hi = h["ci95_changes_still_correct"]
        still = (
            f"{h['changes_still_correct']} of {h['changes']}, {100 * s_lo:.0f}-{100 * s_hi:.0f}%"
        )
    difference = "n/a"
    if h["difference"] is not None:
        bounds = _bootstrap_bounds(h)
        difference = f"{_shown_difference(h)} points, {bounds or _no_interval(h)}"
    return [
        ("counted pairs", f"{n} from {_plural(h['changes'], 'API change')}"),
        ("correct without notes", f"{h['before']} ({pct(h['before'], n)})"),
        ("correct with notes", f"{h['after']} ({pct(h['after'], n)})"),
        ("difference with - without, 95% CI", difference),
        ("pairs fixed / broken", f"{h['fixed']} / {h['broken']}"),
        ("changes fixed, 95% CI", None if regression else fixed if h["changes"] else "n/a"),
        (
            "changes still correct, 95% CI",
            (still if h["changes"] else "n/a") if regression else None,
        ),
        ("changes broken", str(h["changes_broken"])),
        ("sign test, changes fixed vs broken", None if regression else format_p(h["sign_test_p"])),
        ("pairs not counted", _excluded_text(h)),
    ]


def _cutoff_md(s: dict[str, Any]) -> str:
    if not s["model"]:
        return f"Custom cutoff **{s['cutoff']}** (given with --cutoff, no model)"
    return f"Model `{s['model']}`, training cutoff **{s['cutoff']}** (source: {s['cutoff_source']})"


def _md_change(change: APIChange, files: Sequence[FileUse], *, example: bool = False) -> str:
    """One Markdown list item: the change, how many paths share it, and what the code uses."""
    others = other_paths_text(change, example=example)
    uses = uses_text(change, files)
    mark = f" · **your code uses {uses}**" if uses else ""
    return f"- {change_text(change)}{f' ({others})' if others else ''}{mark}"


def change_text(change: APIChange, *, short: bool = False) -> str:
    """The change in a sentence, with the parameter that replaced a removed one, when the
    diff found it renamed in place: ``parameter `pos` was removed (now `start`?)``."""
    text = change.describe(short=short, versioned=False)
    if change.kind == PARAM_REMOVED and change.suggestions:
        text += " (now " + " or ".join(f"`{s}`" for s in change.suggestions[:2]) + "?)"
    return text


def render_scan_markdown(scan: ScanResult, *, limit: int = 8) -> str:
    """A short GitHub-flavoured Markdown summary of a scan, for CI job summaries and PR comments.

    One table row per flagged dependency, then the top ``limit`` changes of each changed one,
    with changes to names the project's code uses first. The full list stays in report.md.
    Every count is of distinct changes, and the dependencies come in the same order, as in
    the full report and the MCP tools.
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

    changed = scan.changed
    flagged = [*changed, *(p for p in scan.packages if p.status in (NEW, SKIPPED))]
    if flagged:
        out += [
            "",
            "| package | you use | at cutoff | status | breaking | deprecated |",
            "|---|---|---|---|---:|---:|",
        ]
    for p in flagged:
        status = STATUS_LABEL.get(p.status, p.status)
        if p.imported:
            status += f", {IMPORTED}"
        elif p.reason:
            status += f": {clip(p.reason, 2 * REASON_WIDTH)}"
        counts = p.counts if p.status == CHANGED else ("", "")
        out.append(
            f"| {p.name} | {_version_cell(p.locked, p.locked_date)} "
            f"| {_version_cell(p.cutoff_version, p.cutoff_version_date)} | {_cell(status)} "
            f"| {counts[0]} | {counts[1]} |"
        )
    quiet = [p for p in scan.packages if p.status in (KNOWN, UNCHANGED)]
    if quiet:
        names = ", ".join(f"{p.name} {p.locked or ''}".strip() for p in quiet)
        out += ["", f"Not flagged (released before the cutoff, or no breaking changes): {names}"]

    limit = max(0, limit)
    for p in changed:
        ranked, files = scan.ranked(p), scan.uses(p)
        hits = sum(uses_text(c, files) is not None for c in ranked)
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
        out += [_md_change(c, files) for c in ranked[:limit]]
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


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def _deps(n: int) -> str:
    """``1 dependency``, ``2 dependencies``."""
    return _plural(n, "dependency", "dependencies")


def _deps_word(n: int) -> str:
    return "dependency" if n == 1 else "dependencies"


def clip(text: str, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters at a word boundary with "..." when it
    is longer: never in the middle of a word such as a command-line flag."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:(") + "..."


def to_json(scan: ScanResult, run: RunResult | None = None) -> dict[str, Any]:
    data = summary(scan, run)
    data["scan"] = [p.to_dict() for p in scan.packages]
    if run is not None:
        data["attempts"] = [_attempt_json(run, a) for a in run.probes + run.heldout]
        data["notes_detail"] = [n.to_dict() for n in run.notes]
        data["block"] = run.block
        for arm, entry in data.get("arms", {}).items():
            entry["block"] = run.arms[arm]
        data["skipped_changes"] = [
            {"change": c.describe(), "reason": r} for c, r in run.skipped_changes
        ]
    return data


def _attempt_json(run: RunResult, attempt: Attempt) -> dict[str, Any]:
    """An attempt for results.json; in a ``--compare`` run, with the arm whose notes it saw
    (None for an answer without notes)."""
    data = attempt.to_dict()
    if run.arms:
        data["arm"] = attempt.arm if attempt.with_notes else None
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
