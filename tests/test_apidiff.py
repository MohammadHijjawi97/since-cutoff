from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from since_cutoff.apidiff import (
    DEPRECATED,
    DIFF_SCHEMA,
    HINT_DECORATOR,
    HINT_DOCSTRING,
    HINT_PARAM_DOC,
    HINT_WARNING,
    KIND_CHANGED,
    MOVED,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
    diff_sources,
)
from since_cutoff.cache import DiskCache, stable_hash
from since_cutoff.engine import Engine, PackageScan, Settings
from since_cutoff.notes import template_bullet
from since_cutoff.pypi import SourceTree
from tests.conftest import FakePyPI, write_tree


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


def test_kwargs_is_recognised_by_its_kinds_name_whatever_its_str():
    # griffe 2.3.1 made ParameterKind a StrEnum: str(ParameterKind.var_keyword) became its value,
    # "variadic keyword", where earlier versions gave "ParameterKind.var_keyword", and the check
    # for **kwargs read that text. Modelled here so the test fails with every griffe: Python 3.10
    # cannot install 2.3.1 or later, which need 3.11.
    from enum import Enum
    from types import SimpleNamespace

    import griffe

    from since_cutoff.apidiff import _accepts_var_keyword

    class Kind(str, Enum):  # what StrEnum does, on every supported Python
        positional_or_keyword = "positional or keyword"
        var_keyword = "variadic keyword"

        def __str__(self) -> str:
            return self.value

    def fn(*kinds: Kind) -> SimpleNamespace:
        params = [SimpleNamespace(name=f"p{i}", kind=kind) for i, kind in enumerate(kinds)]
        return SimpleNamespace(parameters=params)

    assert _accepts_var_keyword(fn(Kind.positional_or_keyword, Kind.var_keyword))
    assert not _accepts_var_keyword(fn(Kind.positional_or_keyword))
    kwargs = griffe.Parameter("kwargs", kind=griffe.ParameterKind.var_keyword)
    assert _accepts_var_keyword(griffe.Function("f", parameters=griffe.Parameters(kwargs)))


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


# ------------------------------------------------ import paths (DIFF_SCHEMA 12)
def _pkg(tmp_path: Path, old: dict[str, str], new: dict[str, str]) -> list[APIChange]:
    a = write_tree(tmp_path / "old", old)
    b = write_tree(tmp_path / "new", new)
    return [APIChange.from_dict(c) for c in diff_sources("pkg", "1", a, "2", b, ["pkg"])]


def test_import_paths_list_every_path_to_the_changed_object(toylib):
    changes = _diff(toylib)
    # fetch is defined in toylib.helpers and re-exported as toylib.fetch: both lead to it.
    fetch = _find(changes, PARAM_REQUIRED, "fetch", "timeout")
    assert fetch.path == "toylib.helpers.fetch"
    assert fetch.import_paths == ["toylib.fetch", "toylib.helpers.fetch"]
    # A member: every path of its class (and of the async twin it is merged with).
    send = _find(changes, PARAM_REMOVED, "send", "temperature")
    assert send.import_paths == ["toylib.AsyncClient.send", "toylib.Client.send"]
    # A removal: only the old paths that are gone. toylib.Session still imports (the class
    # moved to toylib.sessions and is re-exported there), toylib.helpers.Session does not.
    (session,) = [c for c in changes if c.kind == MOVED and c.name == "Session"]
    assert session.import_paths == ["toylib.helpers.Session"]
    assert all(c.import_paths and c.path in c.import_paths for c in changes)


def test_a_dropped_re_export_is_not_a_removal_of_the_object(tmp_path: Path):
    changes = _pkg(
        tmp_path,
        {
            "pkg/__init__.py": 'from pkg.dl import fetch\n__all__ = ["fetch"]\n',
            "pkg/dl.py": "def fetch(url):\n    pass\n",
        },
        {"pkg/__init__.py": "__all__ = []\n", "pkg/dl.py": "def fetch(url):\n    pass\n"},
    )
    assert [(c.kind, c.path, c.import_paths) for c in changes] == [
        (REMOVED, "pkg.fetch", ["pkg.fetch"])  # not pkg.dl.fetch, which still works
    ]


# huggingface_hub's __init__: __all__, the imports under `if TYPE_CHECKING:` and a module
# __getattr__ that imports them on first use.
LAZY_INIT = """
    import importlib
    from typing import TYPE_CHECKING

    __all__ = ["Client", "download"]
    _SUBMOD_ATTRS = {"file_download": ["download"], "client": ["Client"]}


    def __getattr__(name):
        for module, names in _SUBMOD_ATTRS.items():
            if name in names:
                return getattr(importlib.import_module("." + module, __name__), name)
        raise AttributeError(name)


    if TYPE_CHECKING:  # pragma: no cover
        from .client import Client
        from .file_download import download
"""


def test_a_lazy_package_init_re_exports_through_type_checking_imports(tmp_path: Path):
    changes = _pkg(
        tmp_path,
        {
            "pkg/__init__.py": LAZY_INIT,
            "pkg/file_download.py": "def download(url, resume_download=None):\n    pass\n",
            "pkg/client.py": "class Client:\n    def get(self, url, proxies=None):\n        pass\n",
        },
        {
            "pkg/__init__.py": LAZY_INIT,
            "pkg/file_download.py": "def download(url):\n    pass\n",
            "pkg/client.py": "class Client:\n    def get(self, url):\n        pass\n",
        },
    )
    assert {c.parameter: c.import_paths for c in changes} == {
        "resume_download": ["pkg.download", "pkg.file_download.download"],
        "proxies": ["pkg.Client.get", "pkg.client.Client.get"],
    }


def test_import_paths_are_not_cut_to_five(tmp_path: Path):
    # One method removed from a base class that 8 public classes inherit: reported once, with
    # 5 other paths to show, but all 9 classes' paths to match the project's code against.
    models = "from pkg.base import Base\n" + "".join(
        f"\n\nclass M{i}(Base):\n    pass\n" for i in range(8)
    )
    changes = _pkg(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/base.py": "class Base:\n    def legacy(self):\n        pass\n",
            "pkg/models.py": models,
        },
        {"pkg/__init__.py": "", "pkg/base.py": "class Base:\n    pass\n", "pkg/models.py": models},
    )
    (legacy,) = changes
    assert (legacy.path, legacy.occurrences, len(legacy.also)) == ("pkg.base.Base.legacy", 9, 5)
    assert legacy.import_paths == [
        "pkg.base.Base.legacy",
        *(f"pkg.models.M{i}.legacy" for i in range(8)),
    ]


# ------------------------------------------------------------------- hints
def _removed_parameter(tmp_path: Path, old_body: str, *, doc: str = "") -> APIChange:
    docstring = f'    """{doc}"""\n' if doc else ""
    old = f"import warnings\n\n\ndef fetch(url, resume=None):\n{docstring}{old_body}"
    (change,) = _pkg(
        tmp_path, {"pkg/__init__.py": old}, {"pkg/__init__.py": "def fetch(url):\n    pass\n"}
    )
    assert (change.kind, change.parameter) == (PARAM_REMOVED, "resume")
    return change


@pytest.mark.parametrize(
    "call",
    [
        'warnings.warn("`resume` is deprecated. Use `force=True` instead.", FutureWarning)',
        'warnings.warn(f"`resume` is deprecated in {url}. Use `force=True` instead.")',
        'warn("`resume` is deprecated. " + "Use `force=True` instead.")',
        'warnings.warn("`resume` is deprecated. Use `force=True` instead. ({})".format(url))',
        'warnings.warn("`resume` is deprecated. Use `force=True` instead. %s" % url)',
    ],
)
def test_the_old_warnings_warn_text_is_a_removed_parameters_hint(tmp_path: Path, call: str):
    change = _removed_parameter(tmp_path, f"    if resume is not None:\n        {call}\n")
    assert change.hint_source == HINT_WARNING
    assert change.hint and change.hint.startswith("`resume` is deprecated")
    assert "Use `force=True` instead." in change.hint


def test_a_warning_that_is_not_about_the_parameter_is_no_hint(tmp_path: Path):
    body = (
        '    warnings.warn("`url` is deprecated, pass a Request")\n'  # another parameter
        '    warnings.warn("`resume` may be slow")\n'  # not a deprecation
        '    warnings.warn("`resumes` is deprecated")\n'  # another name
        "    warnings.warn(MESSAGE)\n"  # no text to read
    )
    change = _removed_parameter(tmp_path, body)
    assert (change.hint, change.hint_source) == (None, None)


@pytest.mark.parametrize(
    "doc",
    [
        # Google style, as huggingface_hub 1.21 documents text_generation's stop_sequences.
        "Fetch.\n\n    Args:\n        url (`str`):\n            The URL.\n"
        "        resume (`bool`, *optional*):\n            Deprecated argument. Use `force`"
        " instead.\n        other (`int`):\n            Deprecated too, not this one.\n    ",
        # numpydoc
        "Fetch.\n\n    Parameters\n    ----------\n    url : str\n        The URL.\n"
        "    resume : bool\n        Deprecated argument. Use `force`\n        instead.\n    ",
    ],
)
def test_the_parameters_own_docstring_entry_is_its_hint(tmp_path: Path, doc: str):
    change = _removed_parameter(tmp_path, "    pass\n", doc=doc)
    assert (change.hint, change.hint_source) == (
        "Deprecated argument. Use `force` instead.",
        HINT_PARAM_DOC,
    )


def test_a_parameter_entry_that_says_nothing_of_a_deprecation_is_no_hint(tmp_path: Path):
    doc = (
        "Fetch.\n\n    Args:\n        resume (`bool`):\n            Resume a download.\n"
        "        other (`int`):\n            Deprecated, not the one removed.\n    "
    )
    change = _removed_parameter(tmp_path, "    pass\n", doc=doc)
    assert (change.hint, change.hint_source) == (None, None)


def test_a_docstring_line_naming_the_parameter_comes_first(tmp_path: Path):
    body = '    warnings.warn("`resume` is deprecated")\n'
    doc = "Fetch.\n\n    Deprecated: `resume` is ignored, use `force`.\n    "
    change = _removed_parameter(tmp_path, body, doc=doc)
    assert (change.hint, change.hint_source) == (
        "Deprecated: `resume` is ignored, use `force`.",
        HINT_DOCSTRING,
    )


def test_hints_of_removed_objects_keep_their_source(tmp_path: Path, toylib):
    legacy = _find(_diff(toylib), REMOVED, "legacy_fetch")
    assert legacy.hint_source == HINT_DOCSTRING
    util = "def deprecate(**kw):\n    return lambda f: f\n"
    changes = _pkg(
        tmp_path,
        {
            "pkg/__init__.py": "from pkg._util import deprecate\n\n"
            '@deprecate(alternative="pkg.fetch")\ndef get(url):\n    pass\n\n'
            "def fetch(url):\n    pass\n",
            "pkg/_util.py": util,
        },
        {"pkg/__init__.py": "def fetch(url):\n    pass\n", "pkg/_util.py": util},
    )
    get = _find(changes, REMOVED, "get")  # (and the re-exported `deprecate` itself)
    assert (get.hint, get.hint_source) == ("use pkg.fetch instead", HINT_DECORATOR)


def test_the_template_baseline_ignores_the_new_hint_sources():
    # `--compare template` is measured against 0.3's text: a parameter's docstring entry or
    # warnings.warn text (read from DIFF_SCHEMA 12 on) must not change it.
    change = APIChange("pkg", "1", "2", PARAM_REMOVED, "pkg.fetch", "fetch", None, "resume")
    plain = template_bullet(change)
    assert plain == "`pkg.fetch(resume=...)`: `resume` was removed in pkg 2. Do not pass it."
    for source in (HINT_WARNING, HINT_PARAM_DOC):
        change.hint, change.hint_source = "Deprecated. Use `force` instead.", source
        assert template_bullet(change) == plain
    for source in (HINT_DOCSTRING, None):  # None: a diff cached before DIFF_SCHEMA 12
        change.hint_source = source
        assert "in pkg 2 (Deprecated. Use 'force' instead). Do" in template_bullet(change)


# ------------------------------------------------------- still handled at run time
VALIDATORS = """
    import functools
    import warnings


    def validate(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            if "legacy" in kwargs:
                warnings.warn("`legacy` is ignored")
            kwargs.get("proxy")
            kwargs = _drop_legacy(kwargs)
            return fn(*args, **kwargs)

        return inner


    def _drop_legacy(kwargs):
        resume = kwargs.pop("resume", None)
        if resume is not None:
            warnings.warn("`resume` is deprecated and ignored")
        return kwargs


    def unrelated(options):
        return options.get("stream")
"""


def test_still_handled_at_points_to_the_decorator_that_reads_the_name(tmp_path: Path):
    # huggingface_hub 2.0's @validate_hf_hub_args pops resume_download and only warns.
    old = (
        "def fetch(url, resume=None, legacy=None, proxy=None, stream=None):\n    pass\n\n\n"
        "def plain(url, resume=None):\n    pass\n\n\n"
        "def cached(url, resume=None):\n    pass\n\n\n"
        "def renamed(url, resume=None):\n    pass\n"
    )
    new = (
        "import functools\n\nfrom pkg._validators import check, validate\n\n\n"
        "@validate\ndef fetch(url):\n    pass\n\n\n"
        'def plain(url):\n    return {}.pop("resume", None)\n\n\n'
        "@functools.lru_cache\ndef cached(url):\n    pass\n\n\n"
        "@check\ndef renamed(url):\n    pass\n"
    )
    validators = textwrap.dedent(VALIDATORS) + "\n\ncheck = validate\n"
    changes = _pkg(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/dl.py": old, "pkg/_validators.py": VALIDATORS},
        {"pkg/__init__.py": "", "pkg/dl.py": new, "pkg/_validators.py": validators},
    )
    assert {(c.name, c.parameter): c.still_handled_at for c in changes} == {
        ("fetch", "resume"): "pkg/_validators.py:18",  # the helper the decorator calls pops it
        ("fetch", "legacy"): "pkg/_validators.py:8",  # "legacy" in kwargs
        ("fetch", "proxy"): "pkg/_validators.py:10",  # kwargs.get("proxy")
        ("fetch", "stream"): None,  # only a function the decorator never calls reads it
        ("plain", "resume"): None,  # not decorated: its own code never gets the keyword
        ("cached", "resume"): None,  # a decorator from outside the package is not read
        ("renamed", "resume"): None,  # nor one bound by assignment, which is not followed
    }


# ------------------------------------------------------------------- moves
def test_moves_record_what_was_compared(tmp_path: Path, toylib):
    session = next(c for c in _diff(toylib) if c.kind == MOVED and c.name == "Session")
    assert session.move_evidence == {"compared": "class", "kept": 1, "of": 1}  # get
    changes = _pkg(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "LIMIT = 10\n\n\ndef f(x, y):\n    pass\n",
            "pkg/b.py": "",
            "pkg/c/__init__.py": "",
            "pkg/c/old.py": "def one():\n    pass\n\n\ndef two():\n    pass\n",
        },
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "",
            "pkg/b.py": "LIMIT = 10\n\n\ndef f(x, y, z=1):\n    pass\n",
            "pkg/c/__init__.py": "",
            "pkg/old.py": "def one():\n    pass\n",
        },
    )
    assert {(c.path, c.moved_to): c.move_evidence for c in changes if c.kind == MOVED} == {
        ("pkg.a.LIMIT", "pkg.b.LIMIT"): {"compared": "value", "kept": 1, "of": 1},
        ("pkg.a.f", "pkg.b.f"): {"compared": "function", "kept": 2, "of": 2},
        # A module's move counts what its old package has left (0: nothing public).
        ("pkg.c.old", "pkg.old"): {"compared": "module", "kept": 1, "of": 2, "left_behind": 0},
    }
    assert all(c.move_evidence is None for c in changes if c.kind != MOVED)


# ---------------------------------------------------------- schema and cache
def test_the_new_fields_round_trip_and_old_diffs_have_no_import_paths():
    change = APIChange(
        "pkg",
        "1",
        "2",
        PARAM_REMOVED,
        "pkg.dl.fetch",
        "fetch",
        parameter="resume",
        import_paths=["pkg.fetch", "pkg.dl.fetch"],
        hint="Deprecated.",
        hint_source=HINT_WARNING,
        move_evidence={"compared": "function", "kept": 1, "of": 1},
        still_handled_at="pkg/_validators.py:12",
    )
    assert APIChange.from_dict(change.to_dict()) == change
    old = change.to_dict()
    for key in ("import_paths", "hint_source", "move_evidence", "still_handled_at"):
        del old[key]
    assert APIChange.from_dict(old).import_paths is None  # selection then matches by name


def test_a_diff_cached_before_import_paths_is_recomputed(cache: DiskCache, toylib):
    assert DIFF_SCHEMA >= 12
    v1, v2 = toylib
    pypi = FakePyPI(cache, {}, {("toylib", "1.0"): v1, ("toylib", "2.0"): v2})
    stale = APIChange("toylib", "1.0", "2.0", REMOVED, "toylib.stale", "stale").to_dict()
    del stale["import_paths"]  # as DIFF_SCHEMA 11 stored it
    cache.set("diffs", stable_hash("diff", 11, "toylib", "1.0", "2.0"), [stale])
    engine = Engine(Settings(), store=cache, llm_cache=DiskCache(cache.root / "llm"), pypi=pypi)
    scan = engine.diff_package(PackageScan("toylib", "2.0", "test", True, cutoff_version="1.0"))
    assert "stale" not in {c.name for c in scan.changes}
    assert scan.changes and all(c.import_paths for c in scan.changes)


def test_an_object_the_new_release_only_re_exports_is_compared_inside(tmp_path: Path):
    """griffe compares an object with an alias to it by resolving the alias, then stops: the
    old object's path is already among the paths it has seen. A field the target renamed went
    unreported (mcp 2.2's ``RegistrationRequest = OAuthClientMetadata`` lost ``root``; mcp 2.3's
    ``mcp.types.Tool``, from ``from mcp_types import *``, spells ``inputSchema``
    ``input_schema``). Such a pair is compared on its own, under the re-exporting path."""
    old = write_tree(
        tmp_path / "a",
        {
            "pkg/__init__.py": "",
            "pkg/types.py": "class Tool:\n    name: str\n    inputSchema: dict\n",
        },
    )
    new = write_tree(
        tmp_path / "b",
        {
            "pkg/__init__.py": "",
            "pkg/_impl.py": "class Tool:\n    name: str\n    input_schema: dict\n",
            "pkg/types.py": "from pkg._impl import Tool\n",
        },
    )
    raw = diff_sources("pkg", "1.0", old, "2.0", new, ["pkg"])
    changes = [APIChange.from_dict(c) for c in raw]
    assert {(c.kind, c.path) for c in changes} == {(REMOVED, "pkg.types.Tool.inputSchema")}


@pytest.mark.parametrize("where", ["sibling", "public", "private"])
def test_a_parameter_a_re_exported_callable_lost_is_reported_under_the_old_path(
    tmp_path: Path, where: str
):
    """griffe hangs a parameter breakage on the new callable, whose path is the target's: in
    another distribution read next to the release (``pkg_types.Client.send``, from ``from
    pkg_types import *``), where no lookup of this package reaches, or in another module of it.
    The old callable is found through the name the old release defined, and the change is
    reported under it; a re-exported function is compared by its signature (griffe's
    ``find_breaking_changes`` compares members, of which a function has none)."""
    old = write_tree(
        tmp_path / "a",
        {
            "pkg/__init__.py": "",
            "pkg/types.py": (
                "class Client:\n    def send(self, a, b):\n        pass\n\n"
                "def go(a, b):\n    pass\n"
            ),
        },
    )
    impl = "class Client:\n    def send(self, a):\n        pass\n\ndef go(a):\n    pass\n"
    roots: list[Path] = []
    if where == "sibling":
        files = {"pkg/__init__.py": "", "pkg/types/__init__.py": "from pkg_types import *\n"}
        roots = [write_tree(tmp_path / "s", {"pkg_types/__init__.py": impl})]
    else:
        module = "impl" if where == "public" else "_impl"
        files = {
            "pkg/__init__.py": "",
            f"pkg/{module}.py": impl,
            "pkg/types.py": f"from pkg.{module} import Client, go\n",
        }
    new = write_tree(tmp_path / "b", files)
    raw = diff_sources("pkg", "1.0", old, "2.0", new, ["pkg"], new_roots=roots)
    changes = [APIChange.from_dict(c) for c in raw]
    assert {(c.kind, c.path, c.parameter) for c in changes} == {
        (PARAM_REMOVED, "pkg.types.Client.send", "b"),
        (PARAM_REMOVED, "pkg.types.go", "b"),
    }
    by_path = {c.path: c for c in changes}
    send = by_path["pkg.types.Client.send"]
    assert (send.owner, send.old_signature, send.new_signature) == (
        "Client",
        "send(self, a, b)",
        "send(self, a)",
    )
    # Reached under the old name and, inside the package, where it is defined now.
    inside = {"pkg.impl.Client.send"} if where == "public" else set()
    assert set(send.import_paths) == {"pkg.types.Client.send"} | inside
    assert by_path["pkg.types.go"].old_signature == "go(a, b)"


def test_a_re_export_of_another_kind_is_a_kind_change(tmp_path: Path):
    """The new release imports a function where the old one defined a class: griffe stops at
    the alias (as for every re-exported pair), so the pair is compared here, kind first."""
    old = write_tree(
        tmp_path / "a", {"pkg/__init__.py": "", "pkg/api.py": "class Tool:\n    pass\n"}
    )
    new = write_tree(
        tmp_path / "b",
        {
            "pkg/__init__.py": "",
            "pkg/_impl.py": "def Tool():\n    pass\n",
            "pkg/api.py": "from pkg._impl import Tool\n",
        },
    )
    raw = diff_sources("pkg", "1.0", old, "2.0", new, ["pkg"])
    changes = [APIChange.from_dict(c) for c in raw]
    assert [(c.kind, c.path, c.old_kind, c.new_kind) for c in changes] == [
        (KIND_CHANGED, "pkg.api.Tool", "class", "function")
    ]
