"""Held-out statistics, run settings and reusable tasks (``run --tasks-out`` / ``--tasks-from``)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from since_cutoff import __version__, cli, prompts
from since_cutoff.apidiff import DIFF_SCHEMA, PARAM_REMOVED, APIChange
from since_cutoff.engine import (
    CHANGE_BROKEN,
    CHANGE_FIXED,
    CHANGE_NEITHER,
    ERROR,
    OFFTASK,
    PASS,
    STALE,
    UNTOUCHED,
    WRONG,
    Attempt,
    Reporter,
    RunResult,
)
from since_cutoff.errors import ProviderError, SinceCutoffError
from since_cutoff.project import load_project
from since_cutoff.providers.base import Completion
from since_cutoff.report import (
    _heldout_cells,
    _heldout_lines,
    _heldout_md,
    headline,
    pairing_summary,
    render_markdown,
    summary,
    to_json,
)
from since_cutoff.taskfile import (
    NOT_IN_TASKS_FILE,
    TaskFile,
    read_tasks,
    tasks_document,
    write_tasks,
)
from tests.conftest import ScriptedModel
from tests.test_engine import make_engine, make_project


# ------------------------------------------------------------------ helpers
def _change(cid: str) -> APIChange:
    return APIChange("pkg", "1", "2", PARAM_REMOVED, f"pkg.{cid}", cid, None, "p")


def pair(
    cid: str, task: str, without: str, with_notes: str, role: str = "heldout"
) -> list[Attempt]:
    change = _change(cid)
    return [
        Attempt(change, task, role, False, outcome=without),
        Attempt(change, task, role, True, outcome=with_notes),
    ]


def run_with(*pairs: list[Attempt]) -> RunResult:
    run = RunResult(scan=None)  # type: ignore[arg-type]
    run.heldout = [a for p in pairs for a in p]
    return run


def readme_example() -> RunResult:
    """20 counted pairs from 10 changes: 5% -> 65%, 6 changes fixed, 2 pairs excluded."""
    pairs = []
    for i in range(6):  # fixed on both tasks
        pairs += [pair(f"f{i}", "a", STALE, PASS), pair(f"f{i}", "b", WRONG, PASS)]
    pairs += [pair("g", "a", STALE, STALE), pair("g", "b", STALE, WRONG)]
    pairs += [pair("h", "a", PASS, PASS), pair("h", "b", STALE, STALE)]
    pairs += [pair("i", "a", WRONG, WRONG), pair("i", "b", WRONG, WRONG)]
    pairs += [pair("j", "a", STALE, STALE), pair("j", "b", STALE, OFFTASK)]
    pairs += [pair("i", "c", OFFTASK, PASS), pair("j", "c", STALE, ERROR)]  # not counted
    return run_with(*pairs)


# ------------------------------------------------------------ the pairing rule
def test_a_change_is_fixed_only_when_most_of_its_pairs_go_from_wrong_to_right():
    p = run_with(
        # --quick: one held-out task per change.
        pair("quick_fixed", "a", STALE, PASS),
        pair("quick_known", "a", PASS, PASS),  # right without the notes: not a fix
        # Default: two held-out tasks per change; a strict majority of two is both.
        pair("both", "a", STALE, PASS) + pair("both", "b", WRONG, PASS),
        pair("half", "a", STALE, PASS) + pair("half", "b", STALE, STALE),
        pair("half_known", "a", STALE, PASS) + pair("half_known", "b", PASS, PASS),
        # Three held-out tasks: two of three is a majority.
        pair("two_of_three", "a", STALE, PASS)
        + pair("two_of_three", "b", STALE, PASS)
        + pair("two_of_three", "c", STALE, STALE),
        # The mirror image.
        pair("broken", "a", PASS, STALE) + pair("broken", "b", PASS, UNTOUCHED),
    ).pairing("heldout")
    expected = {
        "quick_fixed": CHANGE_FIXED,
        "quick_known": CHANGE_NEITHER,
        "both": CHANGE_FIXED,
        "half": CHANGE_NEITHER,
        "half_known": CHANGE_NEITHER,
        "two_of_three": CHANGE_FIXED,
        "broken": CHANGE_BROKEN,
    }
    assert {c.change_id: c.outcome for c in p.per_change} == {
        _change(name).id: outcome for name, outcome in expected.items()
    }
    assert (p.changes, p.changes_fixed, p.changes_broken) == (7, 3, 1)


def test_excluded_pairs_are_counted_by_reason():
    p = run_with(
        pair("a", "1", OFFTASK, PASS),
        pair("a", "2", UNTOUCHED, PASS),
        pair("a", "3", "invalid", PASS),
        pair("a", "4", ERROR, PASS),
        pair("a", "5", STALE, ERROR),  # the with-notes answer failed
        pair("a", "6", STALE, OFFTASK),  # counted: avoiding the package is not a fix
        [Attempt(_change("b"), "7", "heldout", False, outcome=STALE)],  # one arm only
    ).pairing("heldout")
    assert p.excluded_reasons == {
        "untouched": 1,
        "off-task": 1,
        "invalid": 1,
        "error": 2,
        "missing": 1,
    }
    assert p.excluded == 6
    assert (p.n, p.before, p.after, p.changes, p.changes_fixed) == (1, 0, 0, 1, 0)


def test_roles_are_paired_separately():
    run = run_with(pair("a", "t", STALE, PASS), pair("a", "t", PASS, STALE, role="regression"))
    assert run.pairing("heldout").changes_fixed == 1
    assert run.pairing("regression").changes_broken == 1


# ------------------------------------------------------------ the statistics
def test_every_interval_is_attached_to_its_own_number():
    h = pairing_summary(readme_example().pairing("heldout"))
    assert (h["n"], h["before"], h["after"], h["changes"]) == (20, 1, 13, 10)
    assert (h["changes_fixed"], h["changes_broken"], h["excluded"]) == (6, 0, 2)
    assert h["difference"] == pytest.approx(0.6)
    assert h["ci95_difference"] == (0.3, 0.9)
    assert h["bootstrap"] == {"unit": "change", "resamples": 4000, "seed": 0}
    assert [round(x, 4) for x in h["ci95_changes_fixed"]] == [0.3127, 0.8318]
    assert h["sign_test_p"] == pytest.approx(0.03125)

    lines = [t.plain for t in _heldout_lines(h)]
    assert lines == [
        "Held-out tasks correct without -> with notes: 5% -> 65%, 20 paired tasks from 10 API "
        "changes",
        "Difference: +60 percentage points, 95% CI +30 to +90, bootstrap over API changes",
        "Changes fixed by the notes: 6 of 10, 95% CI 31-83%; 0 broken; sign test p=0.031",
        "Held-out pairs not counted: 1 off-task, 1 error",
    ]
    assert not any("(" in line or ")" in line for line in lines)  # nothing a wrap can split


def test_no_interval_is_printed_when_resampling_says_nothing():
    alike = pairing_summary(
        run_with(
            pair("a", "1", STALE, PASS), pair("b", "1", STALE, PASS), pair("c", "1", STALE, PASS)
        ).pairing("heldout")
    )
    assert alike["ci95_difference"] == (1.0, 1.0)  # the raw value stays in results.json
    lines = [t.plain for t in _heldout_lines(alike)]
    assert lines[1] == (
        "Difference: +100 percentage points, the same in all 3 API changes, so no interval"
    )
    assert (
        lines[2] == "Changes fixed by the notes: 3 of 3, 95% CI 44-100%; 0 broken; sign test p=0.25"
    )

    one = pairing_summary(run_with(pair("a", "1", STALE, PASS)).pairing("heldout"))
    assert one["ci95_difference"] is None
    assert [t.plain for t in _heldout_lines(one)][1] == (
        "Difference: +100 percentage points, too few API changes for an interval"
    )

    nothing = pairing_summary(run_with(pair("a", "1", ERROR, PASS)).pairing("heldout"))
    assert nothing["difference"] is None and nothing["sign_test_p"] == 1.0
    assert [t.plain for t in _heldout_lines(nothing)] == [
        "Held-out tasks correct without -> with notes: n/a -> n/a, 0 paired tasks from 0 API "
        "changes",
        "Held-out pairs not counted: 1 error",
    ]


# ------------------------------------------------------------ end to end
pyright = pytest.mark.pyright


class _TaskWriterDown(ScriptedModel):
    """Answers and notes work; any call to the task writer fails."""

    def complete(self, system: str, user: str) -> Completion:
        if system.startswith("You write evaluation tasks"):
            raise ProviderError("the task writer must not be called")
        return super().complete(system, user)


@pyright
def test_reports_carry_the_statistics_and_the_run_settings(tmp_path, cache, fake_pypi):
    model = ScriptedModel(knows={"session"})
    engine = make_engine(cache, fake_pypi, model, max_probes=10, heldout=2, regression=2)
    engine.settings.today = date(2026, 9, 27)
    scan = engine.scan(load_project(make_project(tmp_path)), engine.resolve_target())
    run = engine.run(scan)

    s = summary(scan, run)
    assert s["settings"] == {
        "tool_version": __version__,
        "diff_schema": DIFF_SCHEMA,
        "date": "2026-09-27",
        "model": "scripted:scripted-1",
        "task_model": "scripted:scripted-1",
        "effort": None,  # only Claude Code takes an effort
        "prompt_version": prompts.PROMPT_VERSION,
        "max_probes": 10,
        "heldout": 2,
        "regression": 2,
        "python_version": "3.12",
        "tasks_from": None,
    }
    h = s["heldout"]["heldout"]
    assert (h["n"], h["changes"], h["changes_fixed"], h["sign_test_p"]) == (8, 4, 4, 0.125)
    lines = [t.plain for t in headline(scan, run, s)]
    assert (
        "Held-out tasks correct without -> with notes: 0% -> 100%, 8 paired tasks from 4 API "
        "changes"
    ) in lines
    assert (
        "Changes fixed by the notes: 4 of 4, 95% CI 51-100%; 0 broken; sign test p=0.125" in lines
    )

    md = render_markdown(scan, run)
    assert "## Run settings" in md and f"| API diff schema | {DIFF_SCHEMA} |" in md
    assert f"| prompt version | {prompts.PROMPT_VERSION} |" in md
    assert "| tasks | written for this run |" in md and "| date | 2026-09-27 |" in md
    assert "An API change is fixed when more than half of its counted pairs" in md
    # The regression check covers changes the model got right: "fixed" means nothing there.
    assert "| changes fixed, 95% CI | 4 of 4, 51-100% | - |" in md
    assert "| changes still correct, 95% CI | - | 1 of 1, 21-100% |" in md
    assert "| sign test, changes fixed vs broken | p=0.125 | - |" in md
    assert "### By API change" in md and "| 2 | 0 | 2 | fixed |" in md
    assert "(regression check) | 1 | 1 | 1 | still correct |" in md

    data = json.loads(json.dumps(to_json(scan, run), default=str))
    assert data["settings"]["prompt_version"] == prompts.PROMPT_VERSION
    assert data["heldout"]["heldout"]["per_change"][0]["outcome"] == "fixed"
    regression = data["heldout"]["regression"]
    assert (regression["changes_still_correct"], regression["changes_fixed"]) == (1, 0)
    assert [round(x, 4) for x in regression["ci95_changes_still_correct"]] == [0.2065, 1.0]
    assert "changes_still_correct" not in data["heldout"]["heldout"]


def test_the_regression_check_counts_changes_still_correct():
    run = run_with(
        pair("kept", "1", PASS, PASS, role="regression"),
        pair("lost", "1", PASS, STALE, role="regression"),
        pair("never", "1", STALE, STALE, role="regression"),
    )
    h = pairing_summary(run.pairing("regression"), "regression")
    assert (h["changes"], h["changes_still_correct"], h["changes_broken"]) == (3, 1, 1)
    cells = dict(_heldout_cells(h, "regression"))
    assert cells["changes still correct, 95% CI"] == "1 of 3, 6-79%"
    assert cells["changes fixed, 95% CI"] is None
    assert cells["sign test, changes fixed vs broken"] is None
    md = "\n".join(_heldout_md({"regression": h}))
    assert "| changes fixed, 95% CI |" not in md and "| sign test, changes" not in md
    assert "| changes still correct, 95% CI | 1 of 3, 6-79% |" in md
    assert "| 1 | 1 | 1 | still correct |" in md
    assert "| 1 | 1 | 0 | broken |" in md
    assert "| 1 | 0 | 0 | wrong with the notes |" in md


def test_the_difference_is_that_of_the_rates_printed_next_to_it():
    # 1 of 8 -> 6 of 8 prints as 12% -> 75%; the exact +62.5 points would round to +62.
    run = run_with(
        pair("a", "1", PASS, PASS) + pair("a", "2", STALE, PASS),
        pair("b", "1", STALE, PASS) + pair("b", "2", STALE, PASS),
        pair("c", "1", STALE, PASS) + pair("c", "2", STALE, STALE),
        pair("d", "1", STALE, PASS) + pair("d", "2", STALE, STALE),
    )
    h = pairing_summary(run.pairing("heldout"))
    assert h["difference"] == pytest.approx(0.625)  # results.json keeps the exact value
    lines = [t.plain for t in _heldout_lines(h)]
    assert lines[0].startswith("Held-out tasks correct without -> with notes: 12% -> 75%")
    assert lines[1].startswith("Difference: +63 percentage points, 95% CI ")
    cells = dict(_heldout_cells(h))
    assert (cells["correct without notes"], cells["correct with notes"]) == ("1 (12%)", "6 (75%)")
    assert cells["difference with - without, 95% CI"].startswith("+63 points, ")

    third = pairing_summary(
        run_with(
            pair("a", "1", PASS, PASS), pair("b", "1", STALE, PASS), pair("c", "1", STALE, STALE)
        ).pairing("heldout")
    )
    assert [t.plain for t in _heldout_lines(third)][1].startswith("Difference: +34 percentage")


def test_a_scan_report_records_the_diff_schema(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted)
    scan = engine.scan(load_project(make_project(tmp_path)), engine.resolve_target())
    settings = summary(scan)["settings"]
    assert set(settings) == {"tool_version", "diff_schema", "date"}
    assert "| API diff schema |" in render_markdown(scan)


@pyright
def test_a_run_can_be_repeated_on_the_tasks_it_used(tmp_path, cache, fake_pypi):
    engine = make_engine(cache, fake_pypi, ScriptedModel(), max_probes=3, heldout=1)
    project = load_project(make_project(tmp_path))
    run = engine.run(engine.scan(project, engine.resolve_target()), fix=False)
    doc = tasks_document(run.used_tasks(), task_model=run.settings["task_model"])
    assert doc["tool"] == f"since-cutoff {__version__}"
    assert doc["prompt_version"] == prompts.PROMPT_VERSION
    assert len(doc["tasks"]) == len(run.probes) == 3
    for a in run.probes:
        entry = doc["tasks"][a.change.fingerprint]
        assert entry == {"change": a.change.describe(), "tasks": run.tasks[a.change.id]}
        assert entry["tasks"][0] == a.task  # the probe comes first

    path = tmp_path / "out" / "tasks.json"
    write_tasks(path, doc)
    tasks = read_tasks(path)
    assert tasks.source == "tasks.json" and tasks.prompt_version == prompts.PROMPT_VERSION

    # The same run from the file: no task-writer call, the same probes on the same tasks.
    second = _TaskWriterDown()
    engine = make_engine(cache, fake_pypi, second, max_probes=3, heldout=1)
    engine.settings.tasks_from = tasks
    again = engine.run(engine.scan(project, engine.resolve_target()), fix=False)
    assert not any(system.startswith("You write evaluation tasks") for system, _ in second.calls)
    assert [(a.change.id, a.task) for a in again.probes] == [
        (a.change.id, a.task) for a in run.probes
    ]
    assert again.tasks == run.tasks
    assert again.settings["tasks_from"] == "tasks.json"
    # Changes past the full budget are not in the file, and not reported as skipped either.
    assert {c.id for c, _ in again.skipped_changes} <= {c.id for c, _ in run.skipped_changes}
    assert all(r.startswith(NOT_IN_TASKS_FILE) for _, r in again.skipped_changes)


@pyright
def test_changes_missing_from_the_tasks_file_are_skipped_with_a_reason(tmp_path, cache, fake_pypi):
    engine = make_engine(cache, fake_pypi, ScriptedModel(), max_probes=3, heldout=1)
    project = load_project(make_project(tmp_path))
    run = engine.run(engine.scan(project, engine.resolve_target()), fix=False)
    kept, dropped = run.probes[0].change, run.probes[1].change
    tasks = {a.change.fingerprint: run.tasks[a.change.id] for a in run.probes}
    del tasks[dropped.fingerprint]
    tasks[kept.fingerprint] = tasks[kept.fingerprint][:1]  # a probe but no held-out task

    engine = make_engine(cache, fake_pypi, _TaskWriterDown(), max_probes=3, heldout=1)
    engine.settings.tasks_from = TaskFile(tasks, "mine.json")
    again = engine.run(engine.scan(project, engine.resolve_target()), fix=False)
    reasons = {c.id: r for c, r in again.skipped_changes}
    assert reasons[dropped.id] == f"{NOT_IN_TASKS_FILE} mine.json"
    assert reasons[kept.id] == "mine.json has 1 task for this change, 2 needed"
    assert {a.change.id for a in again.probes} == {run.probes[2].change.id}


def test_malformed_tasks_files_are_errors(tmp_path):
    with pytest.raises(SinceCutoffError, match="cannot read"):
        read_tasks(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SinceCutoffError, match="not valid JSON"):
        read_tasks(bad)
    bad.write_text('{"tool": "x"}', encoding="utf-8")
    with pytest.raises(SinceCutoffError, match='no "tasks" object'):
        read_tasks(bad)
    bad.write_text('{"tasks": {"abc": {"tasks": ["ok", 3]}}}', encoding="utf-8")
    with pytest.raises(SinceCutoffError, match="list of non-empty strings"):
        read_tasks(bad)
    # A hand-written file may list the tasks directly.
    bad.write_text('{"tasks": {"abc": [" probe ", "held out"]}}', encoding="utf-8")
    assert read_tasks(bad).tasks == {"abc": ["probe", "held out"]}


# ------------------------------------------------------------------- the CLI
# ``scripted_cli`` (conftest.py): the CLI with the fake PyPI and a scripted model per run.
@pyright
def test_cli_writes_and_reuses_the_tasks(tmp_path, capsys, scripted_cli):
    root = make_project(tmp_path)
    tasks = tmp_path / "saved" / "tasks.json"
    argv = ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
    argv += ["--max-probes", "2", "--heldout", "1", "--regression", "0", "--no-fix"]

    scripted_cli.append(ScriptedModel())
    assert cli.main([*argv, "--tasks-out", str(tasks)]) == 0
    assert "repeat this run with --tasks-from" in capsys.readouterr().out
    doc = json.loads(tasks.read_text(encoding="utf-8"))
    assert set(doc) == {"tool", "prompt_version", "task_model", "tasks"}
    assert doc["task_model"] == "anthropic:claude-sonnet-4-5" and len(doc["tasks"]) == 2

    writer_down = _TaskWriterDown()
    scripted_cli.append(writer_down)
    assert cli.main([*argv, "--tasks-from", str(tasks)]) == 0
    assert "Using the test tasks in tasks.json" in capsys.readouterr().out
    results = json.loads((root / ".since-cutoff" / "results.json").read_text(encoding="utf-8"))
    assert results["settings"]["tasks_from"] == "tasks.json"
    assert results["probes"]["total"] == 2
    probed = [a["task"] for a in results["attempts"] if a["role"] == "probe"]
    assert probed == [entry["tasks"][0] for entry in doc["tasks"].values()]
    assert "| tasks | from `tasks.json` (--tasks-from) |" in (
        root / ".since-cutoff" / "report.md"
    ).read_text(encoding="utf-8")


def test_cli_rejects_a_bad_tasks_file_before_any_model_call(tmp_path, capsys, scripted_cli):
    root = make_project(tmp_path)
    bad = tmp_path / "tasks.json"
    bad.write_text("[]", encoding="utf-8")
    argv = ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
    assert cli.main([*argv, "--tasks-from", str(bad)]) == 1
    assert 'no "tasks" object' in capsys.readouterr().err


# ------------------------------------------------------- what a tasks file holds
def test_repeated_tasks_in_a_file_are_dropped_the_probe_included():
    change = _change("x")
    probe, held = "Write f() with pkg.", "Write g() with pkg."

    def tasks_for(tasks: list[str], n: int) -> tuple[list[str], str | None]:
        return TaskFile({change.fingerprint: tasks}, "mine.json").tasks_for(change, n)

    # Case, punctuation and spacing aside, a repeat of the probe or of another task is dropped.
    assert tasks_for([probe, "WRITE  f() with pkg!!", held, held], 3) == ([probe, held], None)
    assert tasks_for([probe, probe], 2) == (
        [],
        "mine.json has 1 task for this change once repeats are dropped, 2 needed",
    )
    assert tasks_for([probe], 2) == ([], "mine.json has 1 task for this change, 2 needed")
    assert tasks_for([probe, held], 1) == ([probe], None)  # --heldout 0: the probe alone


class _Warnings(Reporter):
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


@pyright
def test_a_probe_repeated_in_a_tasks_file_is_never_a_held_out_task(tmp_path, cache, fake_pypi):
    engine = make_engine(cache, fake_pypi, ScriptedModel(), max_probes=10, heldout=2)
    project = load_project(make_project(tmp_path))
    first = engine.run(engine.scan(project, engine.resolve_target()), fix=False)
    # Each change: its probe, the probe again in capitals, then one real held-out task.
    tasks = {}
    for a in first.probes:
        probe, held = first.tasks[a.change.id][:2]
        tasks[a.change.fingerprint] = [probe, probe.upper(), held]

    engine = make_engine(cache, fake_pypi, _TaskWriterDown(), max_probes=10, heldout=2)
    engine.reporter = reporter = _Warnings()
    engine.settings.tasks_from = TaskFile(tasks, "mine.json")
    run = engine.run(engine.scan(project, engine.resolve_target()))
    heldout = [a for a in run.heldout if a.role == "heldout"]
    assert heldout
    probes = {a.change.id: a.task.lower() for a in run.probes}
    assert all(a.task.lower() != probes[a.change.id] for a in heldout)
    # Each failing change had one held-out task, not the two --heldout asked for; the run says so.
    failing = len(run.failing())
    assert len(heldout) == 2 * failing  # without and with the notes, one task each
    assert run.settings["heldout"] == 2
    assert run.settings["heldout_used"] == {"fewest": 1, "most": 1}
    assert reporter.warnings == [
        f"mine.json has fewer than 2 held-out tasks for {failing} of {failing} failing API "
        "changes (as few as 1); those are verified on the ones there are; write it with "
        "--heldout 2 to use 2"
    ]


class _RepeatingWriter(ScriptedModel):
    """A task writer that repeats its first held-out task instead of writing a second."""

    @staticmethod
    def _tasks(prompt: str) -> list[str]:
        tasks = ScriptedModel._tasks(prompt)
        return tasks[:2] + tasks[1:2]


@pyright
def test_a_task_writer_that_repeats_itself_is_reported(tmp_path, cache, fake_pypi):
    engine = make_engine(cache, fake_pypi, _RepeatingWriter(), max_probes=10, heldout=2)
    engine.reporter = reporter = _Warnings()
    run = engine.run(engine.scan(load_project(make_project(tmp_path)), engine.resolve_target()))
    failing = len(run.failing())
    assert failing and run.settings["heldout_used"] == {"fewest": 1, "most": 1}
    assert reporter.warnings == [
        f"The task writer gave fewer than 2 held-out tasks for {failing} of {failing} failing "
        "API changes (as few as 1); those are verified on the ones there are"
    ]


def test_a_tasks_file_may_start_with_a_byte_order_mark(tmp_path):
    path = tmp_path / "bom.json"
    path.write_text('{"tasks": {"abc": ["probe", "held out"]}}', encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # as Notepad and PowerShell 5.1 write
    assert read_tasks(path).tasks == {"abc": ["probe", "held out"]}
    with pytest.raises(SinceCutoffError) as info:
        read_tasks(tmp_path / "missing.json")
    assert str(info.value).count("missing.json") == 1


# ------------------------------------------------------ reusing tasks, the CLI
RUN = ["--cutoff", "2025-07-31", "--max-probes", "2", "--regression", "0"]


@pyright
def test_a_tasks_file_with_fewer_held_out_tasks_says_so(tmp_path, capsys, scripted_cli):
    root = make_project(tmp_path)
    quick = tmp_path / "quick.json"
    argv = ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", *RUN]
    scripted_cli.append(ScriptedModel())
    assert cli.main([*argv, "--heldout", "1", "--no-fix", "--tasks-out", str(quick)]) == 0
    capsys.readouterr()

    scripted_cli.append(_TaskWriterDown())
    assert cli.main([*argv, "--heldout", "2", "--tasks-from", str(quick)]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert (
        "quick.json has fewer than 2 held-out tasks for 2 of 2 failing API changes (as few as 1)"
    ) in out
    results = json.loads((root / ".since-cutoff" / "results.json").read_text(encoding="utf-8"))
    assert results["settings"]["heldout"] == 2
    assert results["settings"]["heldout_used"] == {"fewest": 1, "most": 1}
    report = (root / ".since-cutoff" / "report.md").read_text(encoding="utf-8")
    assert "| held-out tasks per failure | 2 asked, 1 used |" in report


@pyright
def test_reused_tasks_keep_who_wrote_them(tmp_path, capsys, scripted_cli):
    root = make_project(tmp_path)
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    argv = ["run", str(root), *RUN, "--heldout", "1", "--no-fix"]
    scripted_cli.append(ScriptedModel())
    assert (
        cli.main([*argv, "--model", "anthropic:claude-sonnet-4-5", "--tasks-out", str(first)]) == 0
    )
    doc = json.loads(first.read_text(encoding="utf-8"))
    doc["prompt_version"] = 1  # written by an older since-cutoff
    first.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()

    scripted_cli.append(_TaskWriterDown())
    reuse = ["--tasks-from", str(first), "--tasks-out", str(second)]
    assert cli.main([*argv, "--model", "openai:gpt-5.4", *reuse]) == 0
    assert "first.json was written with task prompt version 1" in capsys.readouterr().out
    # The tasks written out again are credited to whoever wrote them, not to this run.
    again = json.loads(second.read_text(encoding="utf-8"))
    assert again == doc

    results = json.loads((root / ".since-cutoff" / "results.json").read_text(encoding="utf-8"))
    assert results["settings"]["task_model"] == "openai:gpt-5.4"  # it writes the notes only
    assert results["settings"]["tasks_written_by"] == {
        "task_model": "anthropic:claude-sonnet-4-5",
        "prompt_version": 1,
        "tool": doc["tool"],
    }
    report = (root / ".since-cutoff" / "report.md").read_text(encoding="utf-8")
    assert "| note writer | `openai:gpt-5.4` |" in report
    assert (
        "| tasks written by | `anthropic:claude-sonnet-4-5`, prompt version 1 (this run's "
        f"prompts are version {prompts.PROMPT_VERSION}), {doc['tool']} |"
    ) in report


def test_bad_tasks_out_paths_are_refused_before_any_model_call(tmp_path, capsys, scripted_cli):
    root = make_project(tmp_path)
    tasks = tmp_path / "tasks.json"
    original = '{"tasks": {"abc": ["probe", "held out"]}}'
    tasks.write_text(original, encoding="utf-8")
    folder = tmp_path / "saved"
    folder.mkdir()
    argv = ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", *RUN]
    model = ScriptedModel()
    scripted_cli.append(model)
    same_file = str(tmp_path / "." / "tasks.json")
    for extra, message in (
        # Writing the run's tasks over the file would drop the changes it did not probe.
        (["--tasks-from", str(tasks), "--tasks-out", same_file], "is the --tasks-from file"),
        (["--tasks-out", str(folder)], "is a folder; give a file name"),
        (["--tasks-out", str(tasks / "tasks.json")], "is a file"),
    ):
        assert cli.main([*argv, *extra]) == 1
        assert message in " ".join(capsys.readouterr().err.split())
    assert model.calls == [] and scripted_cli == [model]  # no engine was even made
    assert tasks.read_text(encoding="utf-8") == original


@pyright
def test_a_failed_tasks_write_keeps_the_result_and_the_notes(
    tmp_path, capsys, scripted_cli, monkeypatch
):
    root = make_project(tmp_path)

    def disk_full(path, document):
        raise SinceCutoffError(f"cannot write the tasks to {path}: No space left on device")

    monkeypatch.setattr(cli, "write_tasks", disk_full)
    scripted_cli.append(ScriptedModel())
    argv = ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", *RUN, "--heldout", "1"]
    assert cli.main([*argv, "--apply", "--tasks-out", str(tmp_path / "tasks.json")]) == 1
    captured = capsys.readouterr()
    assert "What the model got wrong" in captured.out  # the result card
    assert (root / "AGENTS.md").exists()  # --apply
    assert "No space left on device" in " ".join(captured.err.split())
