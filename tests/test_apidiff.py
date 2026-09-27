from __future__ import annotations

from pathlib import Path

from since_cutoff.apidiff import (
    DEPRECATED,
    MOVED,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
    diff_sources,
)
from since_cutoff.pypi import SourceTree
from tests.conftest import write_tree


def _diff(toylib: tuple[SourceTree, SourceTree]) -> list[APIChange]:
    v1, v2 = toylib
    raw = diff_sources("toylib", v1.version, v1.root, v2.version, v2.root, ["toylib"])
    return [APIChange.from_dict(c) for c in raw]


def _find(changes: list[APIChange], kind: str, name: str, param: str | None = None) -> APIChange:
    hits = [c for c in changes if c.kind == kind and c.name == name and c.parameter == param]
    assert hits, f"no {kind} change for {name}({param}) in {[c.describe() for c in changes]}"
    return hits[0]


def test_removed_parameter_is_reported_once_for_sync_and_async_twins(toylib):
    changes = _diff(toylib)
    send = _find(changes, PARAM_REMOVED, "send", "temperature")
    assert send.owner == "Client"
    assert send.occurrences == 2  # Client.send and AsyncClient.send are grouped
    assert "stream" in (send.new_signature or "")


def test_private_module_objects_get_their_public_path(toylib):
    send = _find(_diff(toylib), PARAM_REMOVED, "send", "temperature")
    assert send.path == "toylib.Client.send"


def test_removed_function_carries_the_old_deprecation_hint(toylib):
    legacy = _find(_diff(toylib), REMOVED, "legacy_fetch")
    assert legacy.hint and "fetch" in legacy.hint


def test_moved_class_is_detected(toylib):
    changes = _diff(toylib)
    moved = [c for c in changes if c.kind == MOVED and c.name == "Session"]
    assert moved and moved[0].moved_to == "toylib.Session"  # shortest public import path


def test_new_required_parameter(toylib):
    fetch = _find(_diff(toylib), PARAM_REQUIRED, "fetch", "timeout")
    assert fetch.path == "toylib.helpers.fetch"


def test_pep702_deprecation(toylib):
    close = _find(_diff(toylib), DEPRECATED, "close")
    assert close.deprecation == "Use Client.shutdown() instead."


def test_cosmetic_changes_are_ignored(tmp_path: Path):
    old = write_tree(
        tmp_path / "a",
        {"pkg/__init__.py": "def f(x: int = 1) -> int:\n    return x\nVERSION = '1'\n"},
    )
    new = write_tree(
        tmp_path / "b",
        {"pkg/__init__.py": "def f(x: int = 2) -> str:\n    return str(x)\nVERSION = '2'\n"},
    )
    assert diff_sources("pkg", "1", old, "2", new, ["pkg"]) == []


def test_parameter_absorbed_by_kwargs_is_not_breaking(tmp_path: Path):
    old = write_tree(tmp_path / "a", {"pkg/__init__.py": "def f(x, verbose=False):\n    pass\n"})
    new = write_tree(tmp_path / "b", {"pkg/__init__.py": "def f(x, **kwargs):\n    pass\n"})
    assert diff_sources("pkg", "1", old, "2", new, ["pkg"]) == []


def test_private_and_test_modules_are_ignored(tmp_path: Path):
    old = write_tree(
        tmp_path / "a",
        {
            "pkg/__init__.py": "",
            "pkg/_impl.py": "def hidden():\n    pass\n",
            "pkg/tests/test_x.py": "def test_a():\n    pass\n",
        },
    )
    new = write_tree(tmp_path / "b", {"pkg/__init__.py": ""})
    assert diff_sources("pkg", "1", old, "2", new, ["pkg"]) == []


def test_namespace_packages_report_signature_changes(tmp_path: Path):
    # google-genai ships `google.genai` inside the `google` namespace: its objects must be
    # looked up below `google.genai`, not below `google`.
    old = write_tree(
        tmp_path / "a",
        {
            "nspkg/alpha/__init__.py": "def send(message: str, temperature: float = 1.0) -> str:\n"
            "    return message\n\n\nclass Client:\n    def get(self, url: str) -> str:\n"
            "        return url\n"
        },
    )
    new = write_tree(
        tmp_path / "b",
        {
            "nspkg/alpha/__init__.py": "def send(message: str) -> str:\n    return message\n\n\n"
            "class Client:\n    def get(self, url: str, *, timeout: float) -> str:\n"
            "        return url\n"
        },
    )
    raw = diff_sources("nspkg-alpha", "1", old, "2", new, ["nspkg.alpha"])
    changes = [APIChange.from_dict(c) for c in raw]
    assert {(c.kind, c.path, c.parameter) for c in changes} == {
        (PARAM_REMOVED, "nspkg.alpha.send", "temperature"),
        (PARAM_REQUIRED, "nspkg.alpha.Client.get", "timeout"),
    }


def test_same_named_method_elsewhere_is_not_a_move(tmp_path: Path):
    old = write_tree(
        tmp_path / "a",
        {
            "pkg/__init__.py": "class A:\n    def validate(self):\n        pass\nclass B:\n    def validate(self):\n        pass\n"
        },
    )
    new = write_tree(
        tmp_path / "b",
        {
            "pkg/__init__.py": "class A:\n    pass\nclass B:\n    def validate(self):\n        pass\n"
        },
    )
    changes = [APIChange.from_dict(c) for c in diff_sources("pkg", "1", old, "2", new, ["pkg"])]
    assert [(c.kind, c.path) for c in changes] == [(REMOVED, "pkg.A.validate")]


def test_change_serialisation_round_trip(toylib):
    for c in _diff(toylib):
        again = APIChange.from_dict(c.to_dict())
        assert again == c
        assert again.id == c.id and again.fingerprint == c.fingerprint


def test_descriptions_are_readable(toylib):
    send = _find(_diff(toylib), PARAM_REMOVED, "send", "temperature")
    assert (
        send.describe(short=True)
        == "`Client.send(temperature=...)`: parameter `temperature` was removed (toylib 2.0)"
    )
