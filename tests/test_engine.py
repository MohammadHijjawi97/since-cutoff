"""End-to-end pipeline tests with a fake PyPI and a scripted model (no network, no API keys)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from since_cutoff.cache import DiskCache
from since_cutoff.engine import CHANGED, KNOWN, NEW, PASS, STALE, Engine, Settings
from since_cutoff.notes import BLOCK_END, BLOCK_START
from since_cutoff.project import load_project
from since_cutoff.report import render_markdown, summary, to_json
from tests.conftest import ScriptedModel

pytestmark = pytest.mark.pyright


def make_project(tmp_path: Path, deps: str = '"toylib==2.0"') -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = [{deps}]\n'
    )
    (root / "main.py").write_text("from toylib import Client\nClient().send('hi')\n")
    return root


def make_engine(cache: DiskCache, fake_pypi, model: ScriptedModel, **overrides) -> Engine:
    settings = Settings(
        model="scripted:scripted-1", cutoff=date(2025, 7, 31), python_version="3.12", jobs=2
    )
    for k, v in overrides.items():
        setattr(settings, k, v)
    return Engine(
        settings,
        store=cache,
        llm_cache=cache,
        pypi=fake_pypi,
        provider_factory=lambda spec: model,
    )


def test_scan_finds_the_changes_since_the_cutoff(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted)
    project = load_project(make_project(tmp_path))
    scan = engine.scan(project, engine.resolve_target())
    pkg = scan.package("toylib")
    assert pkg.status == CHANGED
    assert (pkg.cutoff_version, pkg.locked) == ("1.0", "2.0")
    assert {c.name for c in pkg.changes} >= {"send", "fetch", "legacy_fetch", "Session", "close"}
    assert scripted.calls == []  # scanning never calls the model


def test_package_the_model_already_knows(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted)
    project = load_project(make_project(tmp_path, '"toylib==1.0"'))
    scan = engine.scan(project, engine.resolve_target())
    assert scan.package("toylib").status == KNOWN
    assert engine.run(scan).probes == []


def test_package_newer_than_the_model(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted, cutoff=date(2024, 1, 1))
    project = load_project(make_project(tmp_path))
    scan = engine.scan(project, engine.resolve_target())
    assert scan.package("toylib").status == NEW


def test_full_run_measures_fixes_and_verifies(tmp_path, cache, fake_pypi):
    model = ScriptedModel(knows={"session"})
    engine = make_engine(cache, fake_pypi, model, max_probes=10, heldout=2, regression=2)
    project = load_project(make_project(tmp_path))
    scan = engine.scan(project, engine.resolve_target())
    run = engine.run(scan)

    outcomes = {a.change.name: a.outcome for a in run.probes}
    assert outcomes["send"] == STALE
    assert outcomes["legacy_fetch"] == STALE
    assert outcomes["fetch"] == STALE
    assert outcomes["Session"] == PASS  # the model "knows" this one
    assert outcomes["close"] == "deprecated"

    # Every failure gets a verified note, written by the model and grounded in its example.
    assert len(run.notes) == len(run.failing()) == 4
    assert all(n.verified and n.source == "model" for n in run.notes)
    assert (
        run.block and run.block.startswith(BLOCK_START) and run.block.rstrip().endswith(BLOCK_END)
    )
    assert "temperature" in run.block

    heldout = run.pairing("heldout")
    assert heldout.n == 8  # 4 failures x 2 held-out tasks, compared as pairs
    assert (heldout.before, heldout.after, heldout.fixed, heldout.broken) == (0, 8, 8, 0)
    assert (heldout.changes, heldout.changes_fixed, heldout.changes_broken) == (4, 4, 0)
    assert heldout.excluded == 0 and all(c.outcome == "fixed" for c in heldout.per_change)
    regression = run.pairing("regression")
    assert (regression.n, regression.after, regression.broken) == (1, 1, 0)

    s = summary(scan, run)
    assert s["stale_dependencies"] == ["toylib"]
    assert s["probes"]["stale"] == 3
    md = render_markdown(scan, run)
    assert "Held-out verification" in md and "STALE" in md
    assert to_json(scan, run)["attempts"]
    # A run's dependency table has the probe columns after the counts.
    assert (
        "| package | you use | at cutoff | status | breaking | deprecated | probed | stale | wrong |"
        "\n|---|---|---|---|---:|---:|---:|---:|---:|\n"
        "| toylib | 2.0 (2025-10-01) | 1.0 | API changed, imported by your code | 5 | 1 | 5 | 3 |  |"
    ) in md


def test_runs_are_cached(tmp_path, cache, fake_pypi):
    project = load_project(make_project(tmp_path))
    first = ScriptedModel()
    engine = make_engine(cache, fake_pypi, first, max_probes=3, heldout=1, regression=0)
    engine.run(engine.scan(project, engine.resolve_target()))
    assert first.calls

    second = ScriptedModel()
    engine = make_engine(cache, fake_pypi, second, max_probes=3, heldout=1, regression=0)
    run = engine.run(engine.scan(project, engine.resolve_target()))
    assert second.calls == []  # tasks, answers and notes all come from the cache
    assert run.probes


def test_unverifiable_notes_fall_back_to_the_diff(tmp_path, cache, fake_pypi):
    class BadNotes(ScriptedModel):
        def complete(self, system, user):
            if system.startswith("You keep AGENTS.md"):
                from since_cutoff.providers.base import Completion

                return Completion(
                    '{"bullet": "`Client().send(q, temp=0)` works", "example": "from toylib import Client\\nClient().send(\'x\', temp=0)\\n"}'
                )
            return super().complete(system, user)

    engine = make_engine(cache, fake_pypi, BadNotes(), max_probes=2, heldout=1, regression=0)
    project = load_project(make_project(tmp_path))
    run = engine.run(engine.scan(project, engine.resolve_target()))
    assert run.notes and all(n.source == "template" and not n.verified for n in run.notes)
    assert "temp=0" not in (run.block or "")


def test_measure_only_mode_writes_no_notes(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted, max_probes=3)
    project = load_project(make_project(tmp_path))
    run = engine.run(engine.scan(project, engine.resolve_target()), fix=False)
    assert run.probes and not run.notes and run.block is None and not run.heldout
