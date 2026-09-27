"""Regression tests for the 0.2.0 review of the API diff (one test per finding)."""

from __future__ import annotations

import threading
import warnings
from datetime import date
from pathlib import Path

import pytest

from since_cutoff import apidiff
from since_cutoff import engine as engine_module
from since_cutoff.apidiff import (
    DEPRECATED,
    DIFF_SCHEMA,
    KIND_CHANGED,
    MOVED,
    PARAM_REMOVED,
    REMOVED,
    APIChange,
    diff_sources,
)
from since_cutoff.cache import DiskCache, stable_hash
from since_cutoff.engine import CHANGED, Engine, PackageScan, ScanResult, Settings
from since_cutoff.notes import template_bullet
from since_cutoff.project import load_project
from since_cutoff.pypi import SourceTree
from since_cutoff.selection import score
from tests.conftest import FakePyPI, ScriptedModel, write_tree


def diff(
    tmp_path: Path, old: dict[str, str], new: dict[str, str], names=("pkg",)
) -> list[APIChange]:
    a = write_tree(tmp_path / "old", old)
    b = write_tree(tmp_path / "new", new)
    return [APIChange.from_dict(c) for c in diff_sources("pkg", "1", a, "2", b, list(names))]


def by_kind(changes: list[APIChange], kind: str) -> dict[str, APIChange]:
    return {
        c.path if not c.parameter else f"{c.path}({c.parameter})": c
        for c in changes
        if c.kind == kind
    }


# ------------------------------------------------- 1. ``Base = Impl`` aliases
def test_inherited_members_survive_a_base_class_turned_alias(aliaslib):
    old, new = aliaslib
    raw = diff_sources("aliaslib", "1.0", old.root, "2.0", new.root, ["aliaslib"])
    changes = [APIChange.from_dict(c) for c in raw]
    # from_pretrained and encode are still inherited through ``Base = Impl``.
    assert {c.name for c in changes} == {"legacy_decode"}
    (gone,) = changes
    # The one real removal is reported once, on the base class, with its subclasses counted.
    assert (gone.kind, gone.path, gone.occurrences) == (
        REMOVED,
        "aliaslib.base.Base.legacy_decode",
        3,
    )
    assert set(gone.also) == {
        "aliaslib.tok.BertTok.legacy_decode",
        "aliaslib.tok.GptTok.legacy_decode",
    }


def test_unresolvable_bases_never_make_inherited_members_look_removed(tmp_path):
    base = "class Base:\n    def encode(self, text):\n        pass\n"
    sub = "from pkg.base import Base\n\nclass Tok(Base):\n    pass\n"
    old = {"pkg/__init__.py": "", "pkg/base.py": base, "pkg/tok.py": sub}
    new = {
        "pkg/__init__.py": "",
        # A computed base: griffe cannot follow it, so the MRO of Tok comes out empty.
        "pkg/base.py": "def _make():\n    return object\n\nBase = _make()\n",
        "pkg/tok.py": sub,
    }
    assert "pkg.tok.Tok.encode" not in {c.path for c in diff(tmp_path, old, new)}


def test_base_class_removals_are_not_repeated_for_every_subclass(tmp_path):
    base = "class Model:\n    def prune_heads(self):\n        pass\n    def save(self):\n        pass\n"
    subs = "".join(f"class M{i}(Model):\n    pass\n" for i in range(5))
    old = {"pkg/__init__.py": base + subs}
    new = {"pkg/__init__.py": "class Model:\n    def save(self):\n        pass\n" + subs}
    (change,) = diff(tmp_path, old, new)
    assert (change.path, change.occurrences, len(change.also)) == ("pkg.Model.prune_heads", 6, 5)


def test_property_decorator_shadowed_by_an_alias_is_still_a_property(tmp_path):
    # polars 0.19: ``property = sphinx_accessor`` at module level, used as ``@property``.
    utils = "class accessor(property):\n    pass\n"
    old = {
        "pkg/__init__.py": "",
        "pkg/utils.py": utils,
        "pkg/frame.py": (
            "from pkg.utils import accessor\n\nproperty = accessor\n\n"
            "class Frame:\n    @property\n    def str(self) -> int:\n        return 1\n"
        ),
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/utils.py": utils,
        "pkg/frame.py": "class Frame:\n    str: int = 1\n",
    }
    assert KIND_CHANGED not in {c.kind for c in diff(tmp_path, old, new)}


# -------------------------------------------- 2. parallel downloads, fuzzy limit
class BarrierPyPI(FakePyPI):
    """Every source download waits for a second one: sequential downloads would time out."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.barrier = threading.Barrier(2, timeout=10)

    def source(self, name: str, version: str) -> SourceTree:
        self.barrier.wait()
        return super().source(name, version)


def test_sources_are_downloaded_in_parallel(cache, toylib):
    v1, v2 = toylib
    pypi = BarrierPyPI(cache, {}, {("toylib", "1.0"): v1, ("toylib", "2.0"): v2})
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=pypi)
    scan = PackageScan("toylib", "2.0", "test", True, cutoff_version="1.0")
    assert engine.diff_package(scan).status == CHANGED


def test_similar_name_search_stops_on_huge_removals(tmp_path, monkeypatch):
    old = {"pkg/__init__.py": "def get_items():\n    pass\n"}
    new = {"pkg/__init__.py": "def get_item():\n    pass\n"}
    (change,) = diff(tmp_path / "a", old, new)
    assert change.suggestions == ["get_item"]
    monkeypatch.setattr(apidiff, "FUZZY_LIMIT", 0)
    (change,) = diff(tmp_path / "b", old, new)
    assert change.kind == REMOVED and change.suggestions == []


# ------------------------------------------- 3. a same-named class is not a move
def test_unrelated_class_with_the_same_name_is_not_a_move(tmp_path):
    old = {
        "pkg/__init__.py": "from pkg.resources import Completion\n",
        "pkg/resources.py": (
            "class Completion:\n    OBJECT_NAME = 'completions'\n"
            "    @classmethod\n    def create(cls, **kw):\n        pass\n"
            "    @classmethod\n    def acreate(cls, **kw):\n        pass\n"
        ),
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/types.py": "class Completion:\n    id: str\n    choices: list\n    model: str\n",
    }
    changes = diff(tmp_path, old, new)
    assert MOVED not in {c.kind for c in changes}
    top = next(c for c in changes if c.path == "pkg.Completion")
    assert top.kind == REMOVED and top.namesake == "pkg.types.Completion"
    assert top.describe(versioned=False) == (
        "`pkg.Completion` was removed; a different `Completion` now exists at `pkg.types.Completion`"
    )
    assert "different object" in template_bullet(top)


def test_class_that_keeps_its_members_is_still_a_move(tmp_path):
    cls = "class Session:\n    def get(self, url):\n        pass\n    def close(self):\n        pass\n"
    fetch = "def fetch(url):\n    pass\n"
    old = {"pkg/__init__.py": "", "pkg/helpers.py": fetch + cls}
    new = {
        "pkg/__init__.py": "",
        "pkg/helpers.py": fetch,
        "pkg/sessions.py": cls + "    def extra(self):\n        pass\n",
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path, change.moved_to) == (
        MOVED,
        "pkg.helpers.Session",
        "pkg.sessions.Session",
    )


# --------------------------------------------------- 4. kinds in KIND_CHANGED
def test_kind_changes_say_from_what_to_what(tmp_path):
    old = {"pkg/__init__.py": "class Loader:\n    pass\n"}
    new = {"pkg/__init__.py": "def Loader():\n    pass\n"}
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.old_kind, change.new_kind) == (KIND_CHANGED, "class", "function")
    assert change.describe(versioned=False) == "`pkg.Loader` changed from class to function"
    assert "changed from class to function" in template_bullet(change)


# ------------------------------------------------------- 5. non-API modules
def test_test_benchmark_and_example_modules_are_not_api(tmp_path):
    gone = "def helper():\n    pass\n"
    old = {
        "pkg/__init__.py": "class FieldInfo:\n    examples: list = []\n",
        "pkg/testing.py": gone,  # pandas.testing-style helpers directly under the package stay API
        "pkg/sub/__init__.py": "",
        "pkg/sub/testing.py": gone,
        "pkg/sub/test.py": gone,
        "pkg/benchmarks/__init__.py": gone,
        "pkg/examples/demo.py": gone,
    }
    new = {"pkg/__init__.py": "class FieldInfo:\n    pass\n", "pkg/sub/__init__.py": ""}
    removed = set(by_kind(diff(tmp_path, old, new), REMOVED))
    assert removed == {"pkg.FieldInfo.examples", "pkg.testing"}


def test_integration_layers_rank_below_the_main_api():
    def change(path: str) -> APIChange:
        return APIChange("pkg", "1", "2", REMOVED, path, path.rsplit(".", 1)[-1])

    main = score(change("pydantic.fields.ModelField"), set())
    for internal in (
        "pydantic.mypy.PydanticPlugin",
        "sqlalchemy.dialects.mysql.MySQLDialect",
        "sqlalchemy.connectors.pyodbc.PyODBCConnector",
        "polars.interchange.protocol.DtypeKind",
    ):
        assert score(change(internal), set()) < main - 2
    # The package itself and the changed name are not demoted (a package called "plugins").
    assert score(change("plugins.api.run"), set()) == score(change("other.api.run"), set())


# ------------------------------------------------- 6. decorator deprecations
DEPRECATION_HELPERS = """
def deprecate_function(message, *, version):
    return lambda f: f

def deprecate_renamed_parameter(old_name, new_name, *, version):
    return lambda f: f
"""


def test_library_deprecation_decorators_are_reported(tmp_path):
    old = {
        "pkg/__init__.py": "",
        "pkg/_deprecation.py": DEPRECATION_HELPERS,
        "pkg/frame.py": (
            "class Frame:\n"
            "    def apply(self, f):\n        pass\n"
            "    def pivot(self, *, columns=None):\n        pass\n"
        ),
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/_deprecation.py": DEPRECATION_HELPERS,
        "pkg/frame.py": (
            "from pkg._deprecation import deprecate_function, deprecate_renamed_parameter\n\n"
            "class Frame:\n"
            '    @deprecate_function("use `map_elements` instead", version="0.19.0")\n'
            "    def apply(self, f):\n        pass\n"
            '    @deprecate_renamed_parameter("columns", "on", version="1.0.0")\n'
            "    def pivot(self, *, on=None):\n        pass\n"
        ),
    }
    changes = diff(tmp_path, old, new)
    deprecated = by_kind(changes, DEPRECATED)
    apply = deprecated["pkg.frame.Frame.apply"]
    assert apply.deprecation == "use `map_elements` instead"
    assert apply.deprecated_by == "deprecate_function"
    pivot = deprecated["pkg.frame.Frame.pivot(columns)"]
    assert pivot.describe(versioned=False) == (
        "`pkg.frame.Frame.pivot(columns=...)`: parameter `columns` is deprecated: renamed to `on`"
    )
    # Still accepted under the old name, so not a removal.
    assert PARAM_REMOVED not in {c.kind for c in changes}


def test_parameter_deprecation_without_a_new_name(tmp_path):
    # transformers: deprecate_kwarg(old_name, version, new_name=None); the version is no new name.
    helper = "def deprecate_kwarg(old_name, version, new_name=None):\n    return lambda f: f\n"
    old = {
        "pkg/__init__.py": "class Rope:\n    def __init__(self, config, device=None):\n        pass\n"
    }
    new = {
        "pkg/__init__.py": (
            helper + "class Rope:\n"
            '    @deprecate_kwarg("device", "v5")\n'
            "    def __init__(self, config, device=None):\n        pass\n"
        )
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.parameter, change.deprecation) == (DEPRECATED, "device", None)
    assert change.deprecated_by == "deprecate_kwarg"


def test_decorator_deprecations_are_not_probed(tmp_path, cache, fake_pypi, monkeypatch):
    # The type checker cannot see these decorators, so a probe could never fail on them.
    offered: dict[str, list[APIChange]] = {}
    monkeypatch.setattr(engine_module, "select", lambda changes, *a: offered.update(changes) or [])
    engine = Engine(
        Settings(model="scripted:scripted-1", cutoff=date(2025, 7, 31)),
        store=cache,
        llm_cache=cache,
        pypi=fake_pypi,
        provider_factory=lambda spec: ScriptedModel(),
    )
    root = tmp_path / "app"
    root.mkdir()
    (root / "requirements.txt").write_text("toylib==2.0\n")
    pep702 = APIChange("toylib", "1.0", "2.0", DEPRECATED, "toylib.Client.close", "close")
    custom = APIChange(
        "toylib", "1.0", "2.0", DEPRECATED, "toylib.fetch", "fetch", deprecated_by="deprecate"
    )
    pkg = PackageScan("toylib", "2.0", "test", True, status=CHANGED, changes=[pep702, custom])
    engine.run(ScanResult(load_project(root), engine.resolve_target(), [pkg]))
    assert offered == {"toylib": [pep702]}


# ----------------------------------------------------- 7. parser warnings
def test_parsing_old_sources_prints_no_warnings(tmp_path):
    old = {"pkg/__init__.py": 'PATTERN = "\\d+"\n\ndef f(x):\n    pass\n'}
    new = {"pkg/__init__.py": 'PATTERN = "\\d+"\n'}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        changes = diff(tmp_path, old, new)
    assert [c.path for c in changes] == ["pkg.f"]
    assert not [w for w in caught if issubclass(w.category, (SyntaxWarning, DeprecationWarning))]


# ---------------------------------------------------------- 8. schema bump
def test_diffs_cached_by_an_older_schema_are_recomputed(cache, toylib):
    v1, v2 = toylib
    pypi = FakePyPI(cache, {}, {("toylib", "1.0"): v1, ("toylib", "2.0"): v2})
    stale = APIChange("toylib", "1.0", "2.0", REMOVED, "toylib.stale", "stale").to_dict()
    for schema in range(1, DIFF_SCHEMA):
        cache.set("diffs", stable_hash("diff", schema, "toylib", "1.0", "2.0"), [stale])
    engine = Engine(Settings(), store=cache, llm_cache=DiskCache(cache.root / "llm"), pypi=pypi)
    scan = engine.diff_package(PackageScan("toylib", "2.0", "test", True, cutoff_version="1.0"))
    assert "stale" not in {c.name for c in scan.changes}
    assert DIFF_SCHEMA >= 8  # 0.2.0 changed what the diff reports


@pytest.mark.parametrize("kind", [REMOVED, KIND_CHANGED, DEPRECATED])
def test_new_fields_survive_the_cache_round_trip(kind):
    change = APIChange(
        "pkg",
        "1",
        "2",
        kind,
        "pkg.f",
        "f",
        old_kind="class",
        new_kind="function",
        namesake="pkg.types.f",
        deprecated_by="deprecate_function",
    )
    assert APIChange.from_dict(change.to_dict()) == change


# --- descriptors and docstring hints -------------------------------------------------------------


def _ns(**kw: object) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def test_property_like_descriptors_are_attributes_not_deprecations() -> None:
    from since_cutoff.apidiff import _is_attribute_like, decorator_deprecations

    dec = _ns(
        callable_path="pydantic._internal._utils.deprecated_instance_property",
        value="_utils.deprecated_instance_property",
    )
    model_fields = _ns(decorators=[dec], labels={"classmethod"}, is_alias=False)
    assert _is_attribute_like(model_fields)
    assert decorator_deprecations(model_fields) == []


def test_a_parameters_deprecation_is_not_the_objects() -> None:
    from since_cutoff.apidiff import deprecation_hint

    doc = (
        "Download a file.\n\nArgs:\n    resume_download (`bool`, *optional*):\n"
        "        Deprecated and ignored. Will be removed in v5.\n"
        "    proxies (`Dict[str, str]`, *optional*):\n        Proxies."
    )
    assert deprecation_hint(_ns(decorators=[], docstring=_ns(value=doc), is_alias=False)) is None


def test_wrapped_deprecation_sentences_are_joined() -> None:
    from since_cutoff.apidiff import deprecation_hint

    doc = (
        "[`Repository`] is deprecated in favor of the http-based alternatives implemented in\n"
        "[`HfApi`]. Use `upload_file` instead.\n\nMore."
    )
    hint = deprecation_hint(_ns(decorators=[], docstring=_ns(value=doc), is_alias=False))
    assert hint is not None and hint.endswith("Use `upload_file` instead.")
    google = "Do a thing.\n\nDeprecated:\n    Use `invoke` instead."
    assert deprecation_hint(_ns(decorators=[], docstring=_ns(value=google), is_alias=False)) == (
        "Deprecated: Use `invoke` instead."
    )
