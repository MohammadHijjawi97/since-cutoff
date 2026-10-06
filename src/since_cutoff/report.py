"""Human (terminal, Markdown) and machine (JSON) reports."""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from since_cutoff import __version__
from since_cutoff.apidiff import (
    DEPENDENCY_SWITCHED,
    DIFF_SCHEMA,
    KIND_CHANGED,
    KIND_PRIORITY,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_POSITIONAL_ONLY,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
)
from since_cutoff.apidiff import (
    DEPRECATED as CHANGE_DEPRECATED,
)
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
    PackageScan,
    Pairing,
    RunResult,
    ScanResult,
    UsedAPI,
)
from since_cutoff.notes import (
    EVIDENCE_LIBRARY,
    EVIDENCE_METADATA,
    EVIDENCE_MOVE,
    EVIDENCE_RENAME,
    NOTE_DIFF,
    NOTE_MODEL,
    SCOPE_IMPORTED,
    TAG_TYPE_CHECKED,
    Note,
    agents_import_tip,
    block_targets,
    dependency_detail,
    rename_text,
    runtime_text,
    similar_text,
    tag_legend,
    tag_text,
)
from since_cutoff.project import LOCKFILES, VENV_DIRS, FileUse
from since_cutoff.selection import (
    CATCHES,
    FORM_LABELS,
    NAME_MATCH,
    OLD_FORM,
    USE_CALL,
    USE_DEPENDENCY,
    USE_KEYWORD,
    USE_MEMBER,
    USES_API,
    Use,
    other_paths_text,
    uses_text,
)
from since_cutoff.selection import uses as selection_uses
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
# results.json and ``scan --json`` gained keys in 0.4 (``used``, ``used_apis``,
# ``notes_preview``); none of 0.3's was removed or changed. 0.3 wrote no report_schema.
REPORT_SCHEMA = 2
# The import paths results.json lists per change (the shortest; ``import_paths_total`` counts
# them all). A method of a base class can have thousands: transformers' PreTrainedModel.
IMPORT_PATHS_SHOWN = 5

STATUS_LABEL = {
    CHANGED: "API changed",
    UNCHANGED: "no breaking changes",
    KNOWN: "released before cutoff",
    NEW: "first released after the cutoff",
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
# Report names of the notes arms (``run --compare``), and what the baselines are. The run's
# own notes are the arm with the id ``verified`` in results.json (its name since 0.2).
ARM_TITLES = {
    ARM_VERIFIED: "type-checked notes",
    ARM_TEMPLATE: "template baseline",
    ARM_SIGNATURES: "signatures baseline",
}
ARMS_NOTE = (
    "Every held-out task and regression check was also answered with each baseline block in "
    "place of the run's notes (the type-checked notes, with the note from the API diff where a "
    "model's example did not type-check). The baseline blocks keep 0.3's header and wording, so "
    "that their numbers stay comparable with 0.3's; the notes' block has the header that says "
    "what its tags mean. The blocks are paired against the same answers without notes, are "
    "counted by the same rules and are compared on the same pairs: a pair counts for every block "
    "only when its answer without notes is scorable and no block's answer with notes is an "
    'error (for the other blocks, such a pair is not counted under "error with another block"). '
    "So the type-checked notes' row can count fewer pairs than their result above, which keeps "
    "every pair they can count. The template baseline states each change in one sentence from "
    "the API diff; the signatures baseline gives the new signature and first docstring "
    "paragraph of each changed API, or of the replacement its library names. Neither calls a "
    "model. The last column counts the API changes that only the type-checked notes fixed and "
    "those that only the baseline fixed, with an exact two-sided sign test."
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
        type_checked = sum(TAG_TYPE_CHECKED in n.tag_list for n in run.notes)
        out["notes"] = {
            "count": len(run.notes),
            # ``verified`` is the name results.json has always had for ``type_checked``.
            "verified": type_checked,
            "type_checked": type_checked,
            "from_diff": sum(n.source != NOTE_MODEL for n in run.notes),
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
    """Each notes arm of a ``--compare`` run, the run's notes first.

    Per arm: the size of its block, its held-out and regression pairings with their statistics
    (:func:`pairing_summary`), and for a baseline, how it did against the run's notes
    (:func:`head_to_head`). Every arm is paired on the same pairs (``RunResult.pairing(...,
    common=True)``): a pair counts only when its answer without notes is scorable and no
    block's answer with notes is an error. So the notes' row here can count fewer
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
            f" ({n['type_checked']} with an example that type-checks against the pinned version, "
            f"{n['from_diff']} stated from the API diff)"
            if n["from_diff"]
            else " (all with an example that type-checks against the pinned version)"
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
    """One line per ``--compare`` baseline, in the terms of the notes' own lines.

    The baselines are counted on the pairs every block could count (:func:`arms_summary`).
    When an error left out pairs the notes' own line counts, a last line gives the notes'
    rates on those same pairs, so the lines compare like with like.
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
                f"type-checked notes on them: {pct(shared['before'], shared['n'])} -> "
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
    console: Console,
    scan: ScanResult,
    run: RunResult | None = None,
    *,
    verbose: bool = False,
    shown: Collection[str] = (),
) -> None:
    """0.3's summary: the headline panel, the dependency table, the scan's warnings (but those
    in ``shown``, printed already) and what the model got wrong."""
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
        if _new_reason(row["status"], row["reason"]) and (verbose or row["status"] == SKIPPED):
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
        if w not in shown:
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


def _new_reason(status: str, reason: str | None) -> str | None:
    """A dependency's reason, unless its status label says it already ("first released after
    the cutoff" is both a status and the reason for it)."""
    if not reason or reason.strip().lower() == STATUS_LABEL.get(status, "").lower():
        return None
    return reason


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
        ranked, files = scan.ranked(p, scope=SCOPE_IMPORTED), scan.uses(p)
        for c in ranked[:limit]:
            uses = uses_text(c, files)
            line = change_text(c, short=True)
            line += f" (your code uses {uses})" if uses else ""
            console.print(Text("  - " + line.replace("`", "")))
            if c.kind == DEPENDENCY_SWITCHED:  # the counts and places the note leaves out
                places_text = f"    Places: {dependency_detail(c)}".replace("`", "")
                console.print(Text(places_text, style="dim"))
            runtime = runtime_text([c])
            if runtime:
                console.print(Text(f"    Runtime: {runtime}".replace("`", ""), style="dim"))
        if len(ranked) > limit:
            console.print(Text(f"  ... {len(ranked) - limit} more in the report", style="dim"))


# ------------------------------------------------------ scan: the project first
# The first section of ``since-cutoff scan`` is at most this wide (or the terminal's width): the
# "old form" / "uses this API" labels stay next to their API in a 160-column log.
USED_WIDTH = 100
# Places shown per API in the terminal and in MCP's project_changes (-v shows every one).
LOCATIONS_SHOWN = 3
# What `scan` prints once there is a note to write, and when the code uses no changed API
# although it imports a package that changed.
NOTES_READY = "{count} ready: `since-cutoff sync` writes {them} to {target} and keeps {them} in step with {versions}."
IMPORTED_HINT = (
    "No notes to write. For the changes most likely to matter in the packages your code imports:"
)
IMPORTED_COMMAND = "  since-cutoff sync --scope imported"


def render_scan(
    console: Console,
    scan: ScanResult,
    *,
    show_all: bool = False,
    verbose: bool = False,
    limit: int = 8,
    shown: Collection[str] = (),
) -> None:
    """What ``since-cutoff scan`` prints: the changed APIs the project's code uses first
    (:func:`scan_lines`), each with where the code uses it and its note, then one line for the
    rest. With ``show_all`` (``--all``), the rest in full: 0.3's summary panel, dependency
    table and the top ``limit`` changes of each changed dependency. Each of the scan's
    warnings is printed once, and not at all when it is in ``shown`` (printed already)."""
    lines = scan_lines(
        scan,
        width=min(console.width, USED_WIDTH),
        page=console.width,
        verbose=verbose,
        show_all=show_all,
        shown=shown,
    )
    for line in lines:
        console.print(line, soft_wrap=True)
    if show_all:
        console.print()
        render_console(console, scan, verbose=verbose, shown=scan.warnings)
        render_scan_changes(console, scan, limit=limit)


def scan_lines(
    scan: ScanResult,
    *,
    width: int = USED_WIDTH,
    page: int | None = None,
    verbose: bool = False,
    show_all: bool = False,
    shown: Collection[str] = (),
) -> list[Text]:
    """The lines of :func:`render_scan` before 0.3's layout (the scan's warnings last, but
    those in ``shown``).

    Per changed API the code uses: its package and versions, what changed, whether the code
    uses it in the old form, where (at most :data:`LOCATIONS_SHOWN` places unless ``verbose``;
    file-level until the scan records lines, issue #8), the note from the API diff with its
    tag, the runtime caveat and names that merely look similar. Then the dependencies first
    released after the cutoff that the code imports, what the notes are ready for, what the
    labels mean, and (unless ``show_all``) one line for everything else that changed.

    An API's lines are at most ``width`` wide, so that its label stays next to it; the other
    lines wrap at ``page`` (the terminal's width; ``width`` by default).
    """
    page = max(page or width, width)
    used = scan.used_apis()
    new = scan.new_imported()
    after = _after(scan)
    out: list[Text] = []

    def prose(text: str, style: str = "", at: int = page) -> None:
        out.extend(Text(line, style) for line in _wrap(text, at, "", "  "))

    if used:
        prose(f"Your code uses {_plural(len(used), 'API')} that changed after {after}", "bold")
    elif scan.changed:
        prose(f"Your code uses none of the APIs that changed after {after}.", "bold")
    else:
        checked = _deps(len(scan.packages) - len(scan.skipped))
        prose(
            f"{checked[0].upper()}{checked[1:]} checked; none changed its API after {after}.",
            "bold",
        )
    group: str | None = None
    for u in used:
        if u.package.name != group:
            group = u.package.name
            out.append(Text(""))
            prose(versions_text(scan, u.package), "bold")
        out += _api_lines(scan, u, width, verbose)
    if new:
        if used:
            out.append(Text(""))
        for p in new:
            prose(_new_package_text(p))
    other = None if show_all else _others_text(scan, used)
    if used:
        out.append(Text(""))
        prose(
            NOTES_READY.format(
                count=_plural(len(used), "note"),
                them="it" if len(used) == 1 else "them",
                target=_target_name(scan),
                versions=scan.project.versions_word,
            ),
            at=width,
        )
        # Issue #13 will choose the files for both AGENTS.md and CLAUDE.md; until then, say
        # what Claude Code needs to read notes written to AGENTS.md.
        tip = agents_import_tip(scan.project.root, block_targets(scan.project.root))
        if tip:
            prose(f"{tip}.", "yellow", at=width)
        out.append(Text(""))
        for line in _form_legend(used) + tag_legend(t for u in used for t in u.note.tag_list):
            prose(line, "dim", at=width)
        if other:
            prose(other, "dim", at=width)  # with the legend above it
    elif scan.changed:
        if other:
            prose(other)
        if any(p.imported for p in scan.changed):
            prose(IMPORTED_HINT)
            out.append(Text(IMPORTED_COMMAND))
        else:
            out.append(Text("No notes to write."))
    if scan.skipped:
        skipped = len(scan.skipped)
        first = next((p.reason for p in scan.skipped if p.reason), "")
        why = ("; for example: " if skipped > 1 else ": ") + clip(first, REASON_WIDTH)
        total = _deps(len(scan.packages))
        prose(f"{skipped} of {total} could not be checked{why if first else ''}", "yellow")
    out += [Text(f"! {w}", "yellow") for w in scan.warnings if w not in shown]
    return out


def _after(scan: ScanResult) -> str:
    """``claude-sonnet-4-5's training cutoff (2025-07-31)``, or ``the cutoff (2025-03-31)``."""
    target = scan.target
    day = target.cutoff.isoformat()
    return (
        f"{target.model_id}'s training cutoff ({day})" if target.model_id else f"the cutoff ({day})"
    )


def versions_text(scan: ScanResult, p: PackageScan) -> str:
    """``anthropic 0.60.0 -> 1.8.0 (0.60.0 was the latest release at the cutoff; uv.lock pins
    1.8.0)``."""
    source = scan.versions_from(p)
    locked = p.locked or "?"
    if source != p.version_source or source in LOCKFILES or source.startswith("pylock."):
        pins = f"{source} pins {locked}"  # a lockfile, or the file that pins it
    elif source == "installed" or source in VENV_DIRS:
        pins = f"{locked} is installed"
    elif source.startswith("latest on PyPI"):
        pins = f"{locked} is the {source}"
    elif source == "pinned":
        pins = f"the project pins {locked}"
    else:
        pins = f"{source}: {locked}"
    return (
        f"{p.name} {p.cutoff_version} -> {locked} ({p.cutoff_version} was the latest release at "
        f"the cutoff; {pins})"
    )


def api_display(u: UsedAPI) -> str:
    """How the scan names a used API under its package: ``Messages.create``, ``Client`` (its
    constructor), ``hf_hub_download`` (a name the package exports at its top level) or a
    dotted path."""
    api = u.note.api
    if u.note.change.owner or api.count(".") != 1:
        return api
    return api.split(".", 1)[1]


def changes_text(changes: Sequence[APIChange], *, code: bool = False) -> tuple[bool, str]:
    """What changed in one API, in words: ``temperature, top_p and top_k were removed``, with
    the names in backticks when ``code``. The flag says whether the text starts with a verb
    about the API itself (``was removed``), so that it follows the API's name without a
    colon."""
    q = (lambda s: f"`{s}`") if code else (lambda s: s)
    parts: list[str] = []
    first_is_api = False
    kinds = sorted(dict.fromkeys(c.kind for c in changes), key=lambda k: KIND_PRIORITY.get(k, 9))
    for kind in kinds:
        group = [c for c in changes if c.kind == kind]
        params = list(dict.fromkeys(c.parameter for c in group if c.parameter))
        names = _joined([q(p) for p in params], "and")
        one = len(params) == 1
        if kind == PARAM_REMOVED:
            if any(c.still_handled_at for c in group):
                text = f"{names} {'is' if one else 'are'} no longer in its signature"
            else:
                text = f"{names} {'was' if one else 'were'} removed"
        elif kind == PARAM_REQUIRED:
            text = f"now requires {names}"
        elif kind == PARAM_KEYWORD_ONLY:
            text = f"{names} {'is' if one else 'are'} now keyword-only"
        elif kind == PARAM_POSITIONAL_ONLY:
            text = f"{names} {'is' if one else 'are'} now positional-only"
        elif kind == CHANGE_DEPRECATED and params:
            text = f"{names} {'is' if one else 'are'} deprecated"
        elif kind == DEPENDENCY_SWITCHED:
            text = group[0].describe() if code else group[0].describe().replace("`", "")
        else:  # a change to the API itself
            c = group[0]
            if kind == REMOVED:
                text = "was removed"
            elif kind == MOVED and c.is_package_move:
                text = f"moved to {q(c.moved_to or '?')}, the whole package"
            elif kind == MOVED and c.renamed_to:
                text = f"moved to {q(c.moved_to or '?')} ({q(c.name)} is now {q(c.renamed_to)})"
            elif kind == MOVED:
                text = f"moved to {q(c.moved_to or '?')}"
            elif kind == KIND_CHANGED and c.old_kind and c.new_kind:
                text = f"changed from {c.old_kind} to {c.new_kind}"
            elif kind == KIND_CHANGED:
                text = "changed kind"
            elif c.call_form:
                text = f"is deprecated when called as {q(' '.join(c.call_form.split()))}"
            else:
                text = "is deprecated"
            first_is_api = first_is_api or not parts
        parts.append(text)
    return first_is_api, "; ".join(parts)


def api_head(u: UsedAPI, *, code: bool = False) -> str:
    """``Messages.create: temperature, top_p and top_k were removed``, ``AI_PROMPT was
    removed``."""
    name = api_display(u)
    about_api, text = changes_text(u.changes, code=code)
    name = f"`{name}`" if code else name
    return f"{name} {text}" if about_api else f"{name}: {text}"


def use_text(uses: Sequence[Use], *, code: bool = False) -> str:
    """What the code does at one place (the uses of one API there, most specific first):
    ``passes temperature to create``, ``calls create``, ``uses hf_hub_download``."""
    q = (lambda s: f"`{s}`") if code else (lambda s: s)
    first = uses[0]
    if first.kind == USE_DEPENDENCY:
        return _dependency_use_text(first.names, q)
    if first.kind == USE_KEYWORD:
        params = list(dict.fromkeys(u.names[1] for u in uses if u.kind == USE_KEYWORD))
        text = f"passes {_joined([q(p) for p in params], 'and')} to {q(first.names[0])}"
    elif first.kind == USE_CALL:
        text = f"calls {q(first.names[0])}"
    elif first.kind == USE_MEMBER:
        text = f"reads {q(first.names[0])}"
    else:
        text = f"uses {q(first.names[0])}"
    return text + (" (matched by name)" if first.how == NAME_MATCH else "")


def _dependency_use_text(names: Sequence[str], q: Callable[[str], str]) -> str:
    """``reads httpx.Client, httpx.Timeout; passes http_client to OpenAI; catches
    httpx.HTTPError``, or ``imports httpx``: what one file does with a distribution its
    package switched away from (selection.dependency_names)."""
    caught = [n.removeprefix(CATCHES) for n in names[1:] if n.startswith(CATCHES)]
    reads = [n for n in names[1:] if "(" not in n and not n.startswith(CATCHES)]
    passed: dict[str, list[str]] = {}
    for n in names[1:]:
        if "(" in n and not n.startswith(CATCHES):
            callable_name, _, keyword = n.rstrip(")").partition("(")
            passed.setdefault(callable_name, []).append(keyword)
    parts = [f"reads {', '.join(q(r) for r in reads)}"] if reads else []
    parts += [
        f"passes {_joined([q(k) for k in keywords], 'and')} to {q(name)}"
        for name, keywords in passed.items()
    ]
    if caught:
        parts.append(f"catches {', '.join(q(c) for c in caught)}")
    return "; ".join(parts) or f"imports {q(names[0])}"


def installed_text(scan: ScanResult, u: UsedAPI) -> str | None:
    """The "Installed:" line of a switched dependency: whether the project still installs the
    distribution its package no longer requires (UsedAPI.installed), and what that means for
    code that imports it; None for another API. Another package may still require it, so
    removing it is never suggested."""
    change = u.note.change
    if change.kind != DEPENDENCY_SWITCHED:
        return None
    old, new = change.name, change.switched_to or "?"
    module = next(iter((change.dependency or {}).get("old_modules") or ()), old)
    found = u.installed
    if found is None:
        if scan.project.transitive:
            where = scan.project.versions_word
            return f"{where} has no {old}: code that imports it needs it installed."
        return (
            f"your declared dependencies do not list {old} (no lockfile or environment "
            "lists the others): code that imports it needs it installed."
        )
    version, source = found.get("version"), str(found.get("source") or "")
    if source == "installed":
        fact = f"your virtual environment also has {old} {version}"
    elif source == "unpinned" or not version:
        fact = f"your project also declares {old}"
    else:
        fact = f"{source} also pins {old} {version}"
    if found.get("direct"):
        fact += " (a direct dependency)"
    return f"{fact}, so `import {module}` still works; {old} objects are not {new} objects."


def places(u: UsedAPI) -> list[tuple[str, list[Use]]]:
    """The places the code uses an API, most specific first: ``(where, uses there)``, where
    ``where`` is ``app/main.py`` (``app/main.py:7`` once the scan records lines, issue #8)."""
    by_place: dict[str, list[Use]] = {}
    for use in u.uses:
        by_place.setdefault(use.where or "a file of your code", []).append(use)
    return list(by_place.items())


def _snippet(root: Path, use: Use, cache: dict[str, list[str]]) -> str | None:
    """The code on the line of a use, read at render time and never stored (only when the
    scan knows the line: issue #8)."""
    if use.line is None or not use.file:
        return None
    if use.file not in cache:
        try:
            cache[use.file] = (
                (root / use.file).read_text(encoding="utf-8", errors="replace").splitlines()
            )
        except OSError:
            cache[use.file] = []
    lines = cache[use.file]
    return lines[use.line - 1].strip() if 0 < use.line <= len(lines) else None


def _api_lines(scan: ScanResult, u: UsedAPI, width: int, verbose: bool) -> list[Text]:
    """One used API in the scan's first section: what changed and the label, where, the note,
    the runtime caveat and the names that merely look similar."""
    label = FORM_LABELS[u.form]
    style = "bold red" if u.form == OLD_FORM else "yellow"
    head = _wrap(api_head(u), max(20, width - len(label) - 4), "  ", "  ")
    out = [Text(line) for line in head[:-1]]
    last = head[-1]
    out.append(Text.assemble(last, " " * max(2, width - len(last) - len(label)), (label, style)))
    shown = places(u)
    cut = None if verbose else LOCATIONS_SHOWN
    column = max(len(where) for where, _ in shown[:cut]) if shown else 0
    snippets: dict[str, list[str]] = {}
    for where, here in shown[:cut]:
        snippet = _snippet(scan.project.root, here[0], snippets)
        what = f"{snippet} - {use_text(here)}" if snippet else use_text(here)
        for i, line in enumerate(
            _wrap(what, width, f"    {where.ljust(column)}   ", " " * (column + 7))
        ):
            out.append(
                Text(line, "dim")
                if i
                else Text.assemble((line[: column + 4], "cyan"), line[column + 4 :])
            )
    if len(shown) > LOCATIONS_SHOWN and not verbose:
        out.append(Text(f"    and {len(shown) - LOCATIONS_SHOWN} more (-v lists them)", "dim"))
    out += [Text(t) for t in _wrap(f"Note: {u.note.line}", width, "    ", "          ")]
    installed = installed_text(scan, u)
    if installed:
        text = f"Installed: {installed.replace('`', '')}"
        out += [Text(t, "dim") for t in _wrap(text, width, "    ", "               ")]
    runtime = runtime_text(u.changes)
    if runtime:
        out += [
            Text(t, "dim")
            for t in _wrap(f"Runtime: {runtime.replace('`', '')}", width, "    ", "             ")
        ]
    if u.note.not_confirmed:
        names = ", ".join(u.note.not_confirmed)
        text = f"Similar names in {u.package.locked}, not confirmed as replacements: {names}"
        out += [Text(t, "dim") for t in _wrap(text, width, "    ", "      ")]
    return out


def _new_package_text(p: PackageScan) -> str:
    if p.cutoff_version:  # the release at the cutoff was an empty placeholder
        when = p.reason or "empty at the cutoff"
    elif p.first_released:
        when = f"first released {p.first_released}, after the cutoff"
    else:
        when = "first released after the cutoff"
    name = f"{p.name} {p.locked}" if p.locked else p.name
    return f"Your code imports {name} ({when}): its whole API is newer than the cutoff."


def _form_legend(used: Sequence[UsedAPI]) -> list[str]:
    """What "old form" and "uses this API" mean, for the labels shown."""
    forms = {u.form for u in used}
    old_form = [u for u in used if u.form == OLD_FORM]
    packages = {(u.package.cutoff_version, u.package.locked) for u in old_form}
    out = []
    if OLD_FORM in forms:
        # What the pinned release did; a switched dependency is another library's types.
        switched = {u.note.change.kind == DEPENDENCY_SWITCHED for u in old_form}
        did = {
            frozenset({False}): "removed, moved or deprecated what it uses",
            frozenset({True}): "switched to another library for the types it uses",
        }.get(
            frozenset(switched),
            "removed, moved or deprecated what it uses, or switched to another library for the "
            "types it uses",
        )
        if len(packages) == 1:
            old, new = next(iter(packages))
            valid = f"valid for {old}; {new} {did}"
        else:
            valid = f"valid for the release at the cutoff; the pinned release {did}"
        out.append(f"old form: {valid} (a static name match; nothing was run)")
    if USES_API in forms:
        out.append(
            "uses this API: not in the old form, but an assistant editing this code may write "
            "the old form"
        )
    return out


def other_changes(scan: ScanResult, used: Sequence[UsedAPI]) -> dict[str, int]:
    """The changes (each once, PackageScan.distinct) the project's code does not use, per
    changed dependency, most first."""
    taken = {c.id for u in used for c in u.changes}
    counts = {p.name: sum(c.id not in taken for c in p.distinct) for p in scan.changed}
    return dict(sorted(((k, v) for k, v in counts.items() if v), key=lambda kv: -kv[1]))


def _others_text(scan: ScanResult, used: Sequence[UsedAPI]) -> str | None:
    counts = other_changes(scan, used)
    if not counts:
        return None
    total = sum(counts.values())
    if len(counts) == 1:
        where = f"in {next(iter(counts))}"
    else:
        top = ", ".join(f"{name} {n}" for name, n in list(counts.items())[:3])
        more = ", ..." if len(counts) > 3 else ""
        where = f"in {len(counts)} packages ({top}{more})"
    lead = (
        "Also changed, not used by your code"
        if used
        else "Changed in your dependencies, not seen in your code"
    )
    return f"{lead}: {_plural(total, 'change')} {where}. `--all` lists them."


def _old_form_count(used: Sequence[UsedAPI]) -> str:
    """``2 of them in the old form``; ``in the old form`` or ``not in the old form`` for one."""
    old = sum(u.form == OLD_FORM for u in used)
    if len(used) == 1:
        return "in the old form" if old else "not in the old form"
    return f"{old} of them in the old form"


def place_form(uses: Sequence[Use]) -> str:
    """OLD_FORM when any use at a place is the old form of its change."""
    return OLD_FORM if any(u.form == OLD_FORM for u in uses) else USES_API


def fail_reason(scan: ScanResult, conditions: set[str]) -> str | None:
    """Why ``scan --fail-on`` exits with code 3, or None: ``old-form`` when the code uses a
    changed API in the old form, ``used`` when it uses one at all, ``changes`` when any
    dependency changed its API after the cutoff (``--fail-on-changes``)."""
    used = scan.used_apis() if conditions & {"used", "old-form"} else []
    old = [u for u in used if u.form == OLD_FORM]
    if "old-form" in conditions and old:
        return (
            f"--fail-on old-form: your code uses {_plural(len(old), 'changed API')} in the old form"
        )
    if "used" in conditions and used:
        apis = _plural(len(used), "API")
        return f"--fail-on used: your code uses {apis} that changed after the cutoff"
    if "changes" in conditions and scan.changed:
        n = len(scan.changed)
        return (
            f"--fail-on changes: {_deps(n)} changed {'its' if n == 1 else 'their'} API after "
            "the cutoff"
        )
    return None


def github_annotations(scan: ScanResult, env: Mapping[str, str] | None = None) -> list[str]:
    """``--annotate github``: one GitHub Actions workflow command per place where the
    project's code uses a changed API: ``::warning`` where it is the old form, ``::notice``
    elsewhere. File-level for now (``file=`` only); once the scan records lines (issue #8),
    ``line=`` and ``col=`` too. Paths are relative to ``GITHUB_WORKSPACE`` when the project is
    inside it. Property values and the message are escaped as the Actions toolkit does."""
    prefix = _workspace_prefix(scan.project.root, env)
    out = []
    for u in scan.used_apis():
        p = u.package
        title = f"since-cutoff: {p.name} {p.locked}"
        message = (
            f"{api_head(u)}. {p.name} {p.cutoff_version} was the latest release at "
            f"{_after(scan)}; {p.locked} is pinned. Note: {u.note.line} Static match; nothing "
            "was run."
        )
        for _, here in places(u):
            use = here[0]
            if not use.file:
                continue
            props = [("file", prefix + use.file)]
            if use.line is not None:
                props.append(("line", str(use.line)))
            if use.column is not None:
                props.append(("col", str(use.column + 1)))  # 1-based in the annotation
            props.append(("title", title))
            level = "warning" if place_form(here) == OLD_FORM else "notice"
            head = ",".join(f"{k}={_escape_property(v)}" for k, v in props)
            out.append(f"::{level} {head}::{_escape_data(message)}")
    return out


def _escape_data(text: str) -> str:
    """A workflow command's message, as ``escapeData`` in actions/toolkit's command.ts."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    """A workflow command's property value, as ``escapeProperty`` in actions/toolkit."""
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def _workspace_prefix(root: Path, env: Mapping[str, str] | None) -> str:
    """The project's folder in the GitHub checkout (``services/api/``), "" at its root or
    outside Actions: the path that annotations and links put before a project file."""
    workspace = (env or {}).get("GITHUB_WORKSPACE")
    if not workspace:
        return ""
    try:
        rel = root.resolve().relative_to(Path(workspace).resolve()).as_posix()
    except (OSError, ValueError):
        return ""
    return "" if rel == "." else f"{rel}/"


def _github_link(root: Path, env: Mapping[str, str] | None) -> Callable[[Use], str] | None:
    """A function that gives a use's link on GitHub (``https://github.com/o/r/blob/<sha>/
    app/main.py``, with ``#L7`` once lines are known), from the ``GITHUB_REPOSITORY`` and
    ``GITHUB_SHA`` an Actions job sets; None outside one."""
    env = env or {}
    repo, sha = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_SHA")
    if not repo or not sha:
        return None
    server = (env.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    prefix = _workspace_prefix(root, env)

    def link(use: Use) -> str:
        anchor = f"#L{use.line}" if use.line is not None else ""
        return f"{server}/{repo}/blob/{sha}/{quote(prefix + use.file)}{anchor}"

    return link


def _wrap(text: str, width: int, first: str, rest: str) -> list[str]:
    """``text`` in lines of at most ``width`` characters (a longer word stays whole: a path, a
    flag), the first starting with ``first``, the others with ``rest``."""
    return textwrap.wrap(
        text,
        width=max(width, len(first) + 10),
        initial_indent=first,
        subsequent_indent=rest,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [first.rstrip()]


def _joined(items: Sequence[str], conjunction: str) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


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
    ]
    out += _used_report_md(scan)
    out += [
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
        reason = _new_reason(r["status"], r["reason"])
        status = _status_text(r) + (f": {reason}" if reason else "")
        counts = " | ".join(_count_cells(r))
        row = (
            f"| {r['package']} | {r['locked'] or '?'} ({r['locked_date'] or '-'}) "
            f"| {r['cutoff_version'] or '-'} | {_cell(status)} | {counts} |"
        )
        if probed:
            row += f" {r['probed'] or ''} | {r['stale'] or ''} | {r['wrong'] or ''} |"
        out.append(row)
    if run is not None and run.block:
        out += _notes_md(run.notes, run.block, run=True)
    elif run is None:
        diff_notes = scan.diff_notes()
        out += _notes_md(diff_notes, scan.notes_block(diff_notes) or "", run=False)
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
            out += [
                _md_change(c, files, example=True, used_in=True)
                for c in scan.ranked(p, scope=SCOPE_IMPORTED)
            ]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def _used_report_md(scan: ScanResult) -> list[str]:
    """report.md's "Used by your code": each changed API the code uses, by package, with
    every file that uses it (the lines are issue #8's), the note with its tag, the runtime
    caveat and the names that merely look similar."""
    used = scan.used_apis()
    new = scan.new_imported()
    if not used and not new:
        if not scan.changed:
            return []
        return [
            "",
            "## Used by your code",
            "",
            f"Your code uses none of the APIs that changed after {_after(scan)}.",
        ]
    out = ["", "## Used by your code", ""]
    if used:
        out.append(
            f"Your code uses {_plural(len(used), 'API')} that changed after {_after(scan)}, "
            f"{_old_form_count(used)}. "
            + " ".join(f"{text[0].upper()}{text[1:]}." for text in _form_legend(used))
        )
    group: str | None = None
    for u in used:
        if u.package.name != group:
            group = u.package.name
            out += ["", f"### {versions_text(scan, u.package)}", ""]
        wheres = [f"`{w}` ({use_text(here, code=True)})" for w, here in places(u)]
        out.append(f"- **{api_head(u, code=True)}** · {FORM_LABELS[u.form]}")
        out.append(f"  - Used in: {', '.join(wheres) or 'a file of your code'}")
        out.append(f"  - Note: {u.note.line}")
        if u.note.change.kind == DEPENDENCY_SWITCHED:
            out.append(f"  - Places: {dependency_detail(u.note.change)}")
        installed = installed_text(scan, u)
        if installed:
            out.append(f"  - Installed: {installed}")
        runtime = runtime_text(u.changes)
        if runtime:
            out.append(f"  - Runtime: {runtime}")
        if u.note.not_confirmed:
            names = ", ".join(f"`{n}`" for n in u.note.not_confirmed)
            out.append(
                f"  - Similar names in {u.package.locked}, not confirmed as replacements: {names}"
            )
    if new:
        out += ["", *(f"- {_new_package_text(p)}" for p in new)]
    return out


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
    """The ``--compare`` table: one row per notes arm, the run's notes first; then each
    baseline's block, folded (the notes' own block is under "Notes for AGENTS.md")."""
    out = ["", "### Compared with baseline notes", "", ARMS_NOTE, ""]
    out += [
        "| notes | tokens | held-out correct without -> with | held-out pairs not counted "
        "| changes fixed, 95% CI | changes broken | regression checks still correct "
        "| fixed only by the type-checked notes / only by this, sign test |",
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
    out = ["", "## Held-out test of the notes", "", PAIRING_RULE]
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


def _md_change(
    change: APIChange, files: Sequence[FileUse], *, example: bool = False, used_in: bool = False
) -> str:
    """One Markdown list item: the change, how many paths share it, and what the code uses
    (with ``used_in``, where: " · used in `app/main.py`"); below it, the "Runtime:" caveat when
    the new version still reads a removed parameter."""
    others = other_paths_text(change, example=example)
    uses = uses_text(change, files)
    mark = f" · **your code uses {uses}**" if uses else ""
    if uses and used_in:
        wheres = list(dict.fromkeys(u.where for u in selection_uses(change, files) if u.file))
        shown = ", ".join(f"`{w}`" for w in wheres[:LOCATIONS_SHOWN])
        more = f" and {len(wheres) - LOCATIONS_SHOWN} more" if len(wheres) > LOCATIONS_SHOWN else ""
        mark += f" · used in {shown}{more}" if shown else ""
    runtime = runtime_text([change])
    below = f"\n  - Runtime: {runtime}" if runtime else ""
    if change.kind == DEPENDENCY_SWITCHED:
        below = f"\n  - Places: {dependency_detail(change)}{below}"
    return f"- {change_text(change)}{f' ({others})' if others else ''}{mark}{below}"


def change_text(change: APIChange, *, short: bool = False) -> str:
    """The change in a sentence, with the parameter that replaced a removed one when the diff
    found it renamed in place (``parameter `pos` was removed (probably renamed to `start`, same
    position and type)``), and the names that merely look similar, labelled as not confirmed.
    """
    text = change.describe(short=short, versioned=False)
    if change.kind == DEPENDENCY_SWITCHED:
        sites = int((change.dependency or {}).get("sites") or 0)
        return (
            f"{text} ({sites} place{'' if sites == 1 else 's'} in its public API that named "
            f"`{change.name}` types name `{change.switched_to}` types)"
        )
    renamed = rename_text(change)
    similar = similar_text(change) if change.kind in (PARAM_REMOVED, REMOVED) else None
    if renamed:
        text += f" ({renamed})"
    elif similar:
        text += f" ({similar})"
    return text


def render_scan_markdown(
    scan: ScanResult, *, limit: int = 8, env: Mapping[str, str] | None = None
) -> str:
    """A short GitHub-flavoured Markdown summary of a scan, for CI job summaries and PR comments.

    First the changed APIs the project's code uses, one table row each (where, API, change,
    form, replacement), with links to the files when ``env`` has an Actions job's
    ``GITHUB_REPOSITORY`` and ``GITHUB_SHA``, and the notes for them, folded. Then one table
    row per flagged dependency and the top ``limit`` changes of each changed one, folded, with
    changes to names the project's code uses first. The full list stays in report.md. Every
    count is of distinct changes, and the dependencies come in the same order, as in the full
    report and the MCP tools.
    """
    s = summary(scan)
    project = scan.project
    out = [
        "## since-cutoff scan",
        "",
        f"{_cutoff_md(s)} · project `{project.root.name}`, versions from `{s['version_source']}`",
        "",
    ]
    out += _used_markdown(scan, env)
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
        elif _new_reason(p.status, p.reason):
            status += f": {clip(p.reason or '', 2 * REASON_WIDTH)}"
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
        ranked, files = scan.ranked(p, scope=SCOPE_IMPORTED), scan.uses(p)
        hits = sum(uses_text(c, files) is not None for c in ranked)
        breaking, deprecated = p.counts
        detail = f"{breaking} breaking, {deprecated} deprecated" + (
            f", {hits} touching names your code uses" if hits else ""
        )
        out += [
            "",
            f"<details><summary><b>{p.name}</b> "
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


_EVIDENCE_WORDS = {
    EVIDENCE_METADATA: "diff + metadata",
    EVIDENCE_LIBRARY: "named in the library's deprecation text",
    EVIDENCE_MOVE: "move checked",
    EVIDENCE_RENAME: "probable rename",
}


def _used_markdown(scan: ScanResult, env: Mapping[str, str] | None) -> list[str]:
    """The Markdown summary's first section: a table of the changed APIs the code uses, the
    dependencies first released after the cutoff that it imports, and the notes, folded."""
    used = scan.used_apis()
    new = scan.new_imported()
    out: list[str] = []
    if used:
        link = _github_link(scan.project.root, env)
        out += [
            f"**Your code uses {_plural(len(used), 'API')} that changed after the cutoff**",
            "",
            "| where | API | change | form | replacement |",
            "|---|---|---|---|---|",
        ]
        for u in used:
            shown = places(u)
            wheres = [
                f"[{w}]({link(here[0])})" if link and here[0].file else f"`{w}`"
                for w, here in shown[:LOCATIONS_SHOWN]
            ]
            if len(shown) > LOCATIONS_SHOWN:
                wheres.append(f"and {len(shown) - LOCATIONS_SHOWN} more")
            p = u.package
            api = f"`{api_display(u)}` ({p.name} {p.cutoff_version} -> {p.locked})"
            if u.note.change.kind == DEPENDENCY_SWITCHED:
                was, now = u.note.change.name, u.note.change.switched_to
                api = f"`{was}` -> `{now}` ({p.name} {p.cutoff_version} -> {p.locked} dependency)"
            _, change = changes_text(u.changes, code=True)
            replacement = ", ".join(
                f"`{r.text}`"
                + (f" for `{r.replaces}`" if r.replaces else "")
                + f" ({_EVIDENCE_WORDS.get(r.evidence, r.evidence)})"
                for r in u.note.replacements
            )
            cells = [
                "<br>".join(wheres),
                api,
                change,
                FORM_LABELS[u.form],
                replacement or "none named",
            ]
            out.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    elif scan.changed:
        out.append("**Your code uses none of the APIs that changed after the cutoff**")
    for p in new:
        out += ["", _new_package_text(p)]
    block = scan.notes_block(scan.diff_notes()) if used else None
    if block:
        count = _plural(len(used), "note")
        out += [
            "",
            f"<details><summary>{count} ready for {_target_name(scan)} (`since-cutoff sync` "
            f"writes {'it' if len(used) == 1 else 'them'}), about {estimate_tokens(block)} "
            "tokens</summary>",
            "",
            "```markdown",
            block.strip(),
            "```",
            "",
            "</details>",
        ]
    return [*out, ""] if out else []


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
    """results.json and ``scan --json``: :func:`summary`, every package's changes, the changed
    APIs the code uses (``used``, ``used_apis``) and the notes block for them. Nothing 0.3
    wrote is left out; ``report_schema`` counts the additions."""
    data = {"report_schema": REPORT_SCHEMA, **summary(scan, run)}
    data["scan"] = [_scan_json(p) for p in scan.packages]
    used = scan.used_apis()
    data["used"] = {
        "apis": len(used),
        "changes": sum(len(u.changes) for u in used),
        "old_form": sum(u.form == OLD_FORM for u in used),
        "files": len({f for u in used for f in u.files}),
        "packages": list(dict.fromkeys(u.package.name for u in used)),
        "new_packages_imported": [p.name for p in scan.new_imported()],
    }
    data["used_apis"] = [_used_api_json(scan, u) for u in used]
    notes = scan.diff_notes()
    block = scan.notes_block(notes)
    data["notes_preview"] = (
        None
        if block is None
        else {
            "targets": target_names(scan),
            "tokens": estimate_tokens(block),
            "block": block,
        }
    )
    if run is not None:
        data["attempts"] = [_attempt_json(run, a) for a in run.probes + run.heldout]
        data["notes_detail"] = [_note_detail(run, n) for n in run.notes]
        data["block"] = run.block
        for arm, entry in data.get("arms", {}).items():
            entry["block"] = run.arms[arm]
        data["skipped_changes"] = [
            {"change": c.describe(), "reason": r} for c, r in run.skipped_changes
        ]
    return data


def _scan_json(p: PackageScan) -> dict[str, Any]:
    """A package's scan for results.json: every change, each with at most
    :data:`IMPORT_PATHS_SHOWN` of its import paths (shortest first) and how many it has
    (``import_paths_total``). The diff cache keeps them all, and matching uses them all: a
    method of a base class that thousands of classes inherit has a path under each."""
    data = p.to_dict()
    for change in data["changes"]:
        found = change.get("import_paths")
        if found is not None:
            change["import_paths_total"] = len(found)
            change["import_paths"] = found[:IMPORT_PATHS_SHOWN]
    return data


def _used_api_json(scan: ScanResult, u: UsedAPI) -> dict[str, Any]:
    """One entry of ``used_apis``: a changed API the project's code uses, its changes, where
    and how the code uses it, and the note from the API diff for it.

    ``locations`` records the file, line, column, kind, names, form, match and source snippet
    for each use. ``used_in`` lists the files."""
    p, note = u.package, u.note
    snippets: dict[str, list[str]] = {}
    locations = []
    for _, here in places(u):
        use = here[0]  # the most specific use there
        code = _snippet(scan.project.root, use, snippets)
        locations.append(
            {
                "file": use.file,
                "line": use.line,
                "column": use.column,
                "kind": use.kind,
                "names": list(use.names),
                "form": place_form(here),
                "match": use.how,
                "code": code[:200] if code else None,
            }
        )
    return {
        "package": p.name,
        "cutoff_version": p.cutoff_version,
        "locked": p.locked,
        "versions_from": scan.versions_from(p),
        "api": note.change.path,
        "display": note.api,
        "form": u.form,
        "match": u.match,
        "changes": [
            {
                "change_id": c.id,
                "kind": c.kind,
                "parameter": c.parameter,
                "form": u.forms.get(c.id, USES_API),
                "runtime": _runtime_json(c),
            }
            for c in u.changes
        ],
        "replacement": note.replacements[0].to_dict() if note.replacements else None,
        "replacements": [r.to_dict() for r in note.replacements],
        "used_in": u.files,
        "locations": locations,
        "locations_total": len(locations),
        "installed": u.installed,
        "note": _note_json(note),
    }


def _runtime_json(change: APIChange) -> dict[str, Any]:
    """What the source of the pinned version shows at run time (nothing was run): where it
    still reads a removed parameter, or, for a switched dependency, still names the
    distribution it no longer requires."""
    out: dict[str, Any] = {"checked": False, "still_handled_at": change.still_handled_at}
    if change.kind == DEPENDENCY_SWITCHED:
        out["still_named_at"] = list((change.dependency or {}).get("still_named_at") or [])
    return out


def _note_json(note: Note) -> dict[str, Any]:
    """A note as ``used_apis[].note`` has it (``notes_detail[]`` has more: the example, ids)."""
    return {
        "bullet": note.bullet,
        "tags": list(note.tag_list),
        "tag_text": tag_text(note.tag_list),
        "applies_to": note.applies_to(),
        "checks": note.checks(),
        "replacements": [r.to_dict() for r in note.replacements],
        "not_confirmed": list(note.not_confirmed),
    }


def _note_detail(run: RunResult, note: Note) -> dict[str, Any]:
    """A note of a run for ``notes_detail``, with what the held-out test measured for it."""
    data = note.to_dict()
    pairs = {c.change_id: c for c in run.pairing("heldout").per_change}
    measured = [pairs[c.id] for c in note.covered if c.id in pairs]
    if measured:
        data["checks"]["measured"] = {
            "pairs": sum(m.n for m in measured),
            "correct_without": sum(m.before for m in measured),
            "correct_with": sum(m.after for m in measured),
            "outcome": measured[0].outcome if len(measured) == 1 else None,
        }
    return data


def target_names(scan: ScanResult) -> list[str]:
    """The files the notes would be written to, as ``sync`` and ``run --apply`` choose them
    (:func:`notes.block_targets`; issue #13 is the case of both AGENTS.md and CLAUDE.md)."""
    root = scan.project.root
    names = []
    for target in block_targets(root):
        try:
            names.append(target.relative_to(root).as_posix())
        except ValueError:  # pragma: no cover - block_targets without --target is in root
            names.append(str(target))
    return names


def _target_name(scan: ScanResult) -> str:
    return _joined(target_names(scan), "and")


def _notes_md(notes: list[Note], block: str, *, run: bool) -> list[str]:
    """The "Notes for AGENTS.md" section: the block, then where each note comes from."""
    if not notes or not block:
        return []
    if run:
        intro = (
            "The notes this run wrote (`run --apply` writes them). A model wrote each one and "
            "kept it only when its example type-checks against the pinned version; the note "
            "from the API diff takes its place otherwise."
        )
    else:
        intro = (
            "Written from the API diff, with no model call, for the changed APIs your code "
            "uses; `since-cutoff sync` writes them into the file and keeps them in step with "
            "the lockfile. Each note's tag says what was checked; the list below says where it "
            "comes from. results.json has the same under `used_apis`."
        )
    out = ["", "## Notes for AGENTS.md (with their sources)", "", intro, ""]
    out += ["```markdown", block.strip(), "```", ""]
    # In the order of the block: by package, then as written.
    by_package = sorted(notes, key=lambda n: (n.change.package, n.change.to_version))
    out += [f"- {_source_md(n)}" for n in by_package]
    return out


def _source_md(note: Note) -> str:
    """Where one note comes from: the diff, the library's text, a model; and what was not
    checked (the runtime caveat, the names that merely look similar)."""
    c = note.change
    parts = [
        f"`{note.api}` {tag_text(note.tag_list)}: {c.package} {c.from_version} -> {c.to_version}"
    ]
    if note.source == NOTE_MODEL:
        writer = f" by `{note.writer}`" if note.writer else ""
        parts.append(f"written{writer}; its example type-checks against {c.package} {c.to_version}")
    elif note.source == NOTE_DIFF and c.kind == DEPENDENCY_SWITCHED:
        parts.append("stated from both releases' Requires-Dist and the API diff")
    elif note.source == NOTE_DIFF:
        parts.append("stated from the API diff")
    for r in note.replacements:
        parts.append(f"`{r.text}` for `{r.replaces}`: {r.source}")
    runtime = runtime_text(note.covered)
    if runtime:
        parts.append(f"Runtime: {runtime}")
    if note.not_confirmed:
        names = ", ".join(f"`{n}`" for n in note.not_confirmed)
        parts.append(
            f"similar names in {c.to_version}, not confirmed as replacements (not in the note): "
            f"{names}"
        )
    return "; ".join(parts)


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
