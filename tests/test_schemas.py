"""The JSON Schemas shipped in the package (``since_cutoff/schemas``), checked against what the
reports write: ``scan --json`` and results.json (``results.schema.json``), with ``run``'s
additions, and ``status --json`` (``status.schema.json``); and the SARIF report against the
official SARIF 2.1.0 schema (network). jsonschema is a test dependency only: the package never
imports it."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from since_cutoff import __version__, cli, net
from since_cutoff.engine import CHANGED, NEW, ModelTarget, PackageScan, ScanResult
from since_cutoff.project import Dependency, Project, scan_file
from since_cutoff.report import REPORT_SCHEMA, SARIF_SCHEMA, sarif_report, to_json
from tests import test_scan_first, test_status, test_sync
from tests.conftest import ScriptedModel
from tests.test_ci import scan_app
from tests.test_compare import BOTH, LEARNS_FROM, compare_run
from tests.test_dependency_switch import OLD_FORM_CODE, _switch
from tests.test_scan_first import CUTOFF, LOOSE, _frames_pypi, make_app

jsonschema = pytest.importorskip("jsonschema")

# The fixtures of test_scan_first.py (a project using toylib, its scan, the CLI on the fake
# PyPI) and of test_status.py (a project sync wrote notes for, status without network).
app = test_scan_first.app
scan = test_scan_first.scan
fake_cli = test_scan_first.fake_cli
pypi = test_sync.pypi
sc = test_sync.sc
synced = test_status.synced
status = test_status.status

CUTOFF_DAY = date(2025, 7, 31)


def schema(name: str) -> dict[str, Any]:
    """A schema as the installed package has it (importlib.resources: as the wheel ships it)."""
    text = resources.files("since_cutoff").joinpath("schemas", name).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def errors(data: Any, name: str = "results.schema.json") -> list[str]:
    """Where ``data`` does not match the schema, as ``path: message`` lines."""
    validator = jsonschema.Draft202012Validator(
        schema(name), format_checker=jsonschema.FormatChecker()
    )
    return [
        f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator.iter_errors(data)
    ]


def as_written(data: Any) -> Any:
    """``data`` as results.json has it (days and paths as strings)."""
    return json.loads(json.dumps(data, default=str))


# --------------------------------------------------------------------- the files
@pytest.mark.parametrize("name", ["results.schema.json", "status.schema.json"])
def test_the_schemas_are_draft_2020_12_and_describe_every_key(name: str) -> None:
    s = schema(name)
    jsonschema.Draft202012Validator.check_schema(s)
    assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    # At the release's tag, not main: the id of a released schema never changes.
    assert s["$id"].endswith(f"/since-cutoff/v{__version__}/src/since_cutoff/schemas/{name}")
    assert s["additionalProperties"] is False
    undescribed = [
        k for k, p in s["properties"].items() if "description" not in p and "$ref" not in p
    ]
    assert undescribed == []


def test_the_results_schema_is_for_this_report_schema() -> None:
    assert schema("results.schema.json")["properties"]["report_schema"]["const"] == REPORT_SCHEMA


def test_the_package_never_imports_jsonschema() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "since_cutoff"
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n.split(".")[0] == "jsonschema" for n in names), path


# ------------------------------------------------------------------ scan --json
def test_scan_json_and_results_json_match_the_schema(app: Path, capsys, fake_cli) -> None:
    assert cli.main(["scan", str(app), *CUTOFF, "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["used_apis"] and errors(printed) == []
    written = json.loads((app / ".since-cutoff" / "results.json").read_text(encoding="utf-8"))
    assert errors(written) == []


def test_a_key_the_schema_does_not_describe_fails(scan: ScanResult) -> None:
    data = as_written(to_json(scan))
    assert errors(data) == []
    assert errors({**data, "surprise": 1}) == [
        ": Additional properties are not allowed ('surprise' was unexpected)"
    ]
    data["used_apis"][0]["locations"][0]["surprise"] = 1
    assert len(errors(data)) == 1
    del data["used_apis"][0]["locations"][0]["surprise"]
    data["report_schema"] = REPORT_SCHEMA + 1
    assert errors(data) == [f"report_schema: {REPORT_SCHEMA} was expected"]
    data["report_schema"] = REPORT_SCHEMA
    # The settings are closed too, with the keys a run adds only sometimes.
    data["settings"]["surprise_setting"] = 1
    assert errors(data) == [
        "settings: Additional properties are not allowed ('surprise_setting' was unexpected)"
    ]
    del data["settings"]["surprise_setting"]
    data["settings"] |= {"model_source": "Claude Code", "heldout_used": {"fewest": 1, "most": 2}}
    assert errors(data) == []
    data["settings"]["heldout_used"]["surprise"] = 3
    assert len(errors(data)) == 1


def test_lines_name_matches_and_switched_dependencies_match_the_schema(
    scan: ScanResult, tmp_path: Path, cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A place with its line, column and code (issue #8).
    [send, fetch] = scan.used_apis()
    lined = [u._replace(line=2, column=0) if u.file == "main.py" else u for u in send.uses]
    with monkeypatch.context() as m:
        m.setattr(ScanResult, "used_apis", lambda self: [replace(send, uses=lined), fetch])
        data = as_written(to_json(scan))
    assert data["used_apis"][0]["locations"][0]["code"] and errors(data) == []
    # A use matched by a member's name alone, asked for (--include-name-matches).
    root = make_app(tmp_path / "frames", main=LOOSE, other=None, pin="frames==2.0")
    loose = scan_app(root, cache, _frames_pypi(tmp_path, cache), CUTOFF_DAY)
    loose.name_matches = True
    data = as_written(to_json(loose))
    assert data["used_apis"][0]["match"] == "name_only" and errors(data) == []
    # A dependency the package switched away from, which the project still pins.
    package = PackageScan("pkg", "2", "uv.lock", True, True, CHANGED, cutoff_version="1")
    package.import_names, package.changes = ["pkg"], [_switch(tmp_path / "diff")]
    project = Project(
        tmp_path,
        [Dependency("oldhttp", "0.28.1", True, "uv.lock")],
        "uv.lock",
        imported_modules={"pkg", "oldhttp"},
        files=[scan_file(ast.parse(OLD_FORM_CODE), "app/llm.py")],
    )
    data = as_written(to_json(ScanResult(project, ModelTarget.cutoff_only(CUTOFF_DAY), [package])))
    assert data["used_apis"][0]["installed"]["source"] == "uv.lock" and errors(data) == []


def test_a_project_with_nothing_to_report_matches_the_schema(tmp_path: Path) -> None:
    empty = Project(tmp_path, [], "pyproject.toml")
    data = as_written(to_json(ScanResult(empty, ModelTarget.cutoff_only(CUTOFF_DAY), [])))
    assert data["model"] is None and data["notes_preview"] is None and errors(data) == []
    fresh = PackageScan("fresh", "1.0", "uv.lock", True, status=NEW, first_released="2025-11-03")
    skipped = PackageScan("big", "9", "uv.lock", True, reason="big==9 is 200 MB, above the limit")
    project = Project(tmp_path, [], "uv.lock", imported_modules={"fresh"})
    data = as_written(
        to_json(ScanResult(project, ModelTarget.cutoff_only(CUTOFF_DAY), [fresh, skipped]))
    )
    assert data["used"]["new_packages_imported"] == ["fresh"] and errors(data) == []


# ------------------------------------------------------------------------- run
@pytest.mark.pyright
def test_a_run_with_baselines_matches_the_schema(tmp_path: Path, cache, fake_pypi) -> None:
    model = ScriptedModel(knows={"session"}, learns_from=LEARNS_FROM)
    scan, run = compare_run(tmp_path, cache, fake_pypi, BOTH, model)
    data = as_written(to_json(scan, run))
    assert {"probes", "notes", "heldout", "arms", "attempts", "notes_detail", "block"} <= set(data)
    assert errors(data) == []


# ---------------------------------------------------------------- status --json
def test_status_json_matches_its_schema(status, synced: Path, tmp_path: Path) -> None:
    test_sync.set_pins(synced, ("toylib==2.1", "otherlib==1.0"))
    data = json.loads(status(synced, "--json")[1])
    assert data["state"] == "out_of_date" and errors(data, "status.schema.json") == []
    never = test_sync.make_app(tmp_path / "never")
    data = json.loads(status(never, "--json")[1])
    assert data["state"] == "never_run" and errors(data, "status.schema.json") == []


# ----------------------------------------------------------------------- SARIF
@pytest.mark.network
def test_the_sarif_report_matches_the_official_schema(tmp_path: Path, cache, fake_pypi) -> None:
    """SARIF 2.1.0 as OASIS published it (schemastore's copy: the ``$schema`` the report
    names). test_scan_first.py checks offline what GitHub code scanning reads."""
    official = net.get_json(SARIF_SCHEMA)
    for root in (make_app(tmp_path), make_app(tmp_path / "unused", main="import toylib\n")):
        report = sarif_report(scan_app(root, cache, fake_pypi, CUTOFF_DAY))
        jsonschema.validate(json.loads(json.dumps(report)), official)
    # A dependency that could not be checked: the invocation says so, with a notification.
    partial = scan_app(make_app(tmp_path / "partial"), cache, fake_pypi, CUTOFF_DAY)
    partial.packages.append(PackageScan("big", "9", "uv.lock", True, reason="200 MB"))
    report = sarif_report(partial)
    assert report["runs"][0]["invocations"][0]["executionSuccessful"] is False
    jsonschema.validate(json.loads(json.dumps(report)), official)
