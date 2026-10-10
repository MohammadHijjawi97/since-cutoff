"""Regression tests for false or unhelpful breaking changes found on real projects.

Each fixture is a small reduction of a real release pair (named in the test): what the static
diff reported before, and what it must report now.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from since_cutoff.apidiff import (
    DEPRECATED,
    KIND_CHANGED,
    MOVED,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    TIER_INTERNAL,
    TIER_PUBLIC,
    UNREAD_BASE,
    APIChange,
    diff_sources,
    diff_sources_with_unread,
    unread_text,
)
from since_cutoff.selection import collapse
from tests.conftest import write_tree


def diff(
    tmp_path: Path, old: dict[str, str], new: dict[str, str], names=("pkg",), compiled=()
) -> list[APIChange]:
    """``compiled``: the modules the new release ships compiled, without a source or a stub
    (SourceTree.compiled; the extension modules themselves are never extracted)."""
    a = write_tree(tmp_path / "old", old)
    b = write_tree(tmp_path / "new", new)
    result = diff_sources("pkg", "1", a, "2", b, list(names), new_compiled=list(compiled))
    return [APIChange.from_dict(c) for c in result]


def found(changes: list[APIChange]) -> set[tuple[str, str, str | None]]:
    return {(c.kind, c.path, c.parameter) for c in changes}


# ------------------------------------------------------------- *args, **kwargs
def test_removed_var_arguments_are_described_as_such(tmp_path):
    # tqdm 4.61 -> 4.70: tqdm_rich.close(self, *args, **kwargs) -> close(self)
    old = {"pkg/__init__.py": "class Bar:\n    def close(self, *args, **kwargs):\n        pass\n"}
    new = {"pkg/__init__.py": "class Bar:\n    def close(self):\n        pass\n"}
    changes = diff(tmp_path, old, new)
    assert found(changes) == {
        (PARAM_REMOVED, "pkg.Bar.close", "*args"),
        (PARAM_REMOVED, "pkg.Bar.close", "**kwargs"),
    }
    kw = next(c for c in changes if c.parameter == "**kwargs")
    assert kw.display == "Bar.close(**kwargs)"
    assert kw.describe(versioned=False) == (
        "`pkg.Bar.close(**kwargs)`: `**kwargs` was removed, so extra keyword arguments are no "
        "longer accepted"
    )
    assert kw.new_signature == "close(self)"


def test_kwargs_replaced_by_the_parameters_it_took_is_not_a_break(tmp_path):
    # dulwich 0.22 -> 1.2: get_transport_and_path(location, **kwargs) -> explicit keywords
    old = {"pkg/__init__.py": "def connect(url, **kwargs):\n    pass\n"}
    new = {"pkg/__init__.py": "def connect(url, *, timeout=None, username=None):\n    pass\n"}
    assert diff(tmp_path, old, new) == []


# ------------------------------------------------------------ parameter names
def test_dunder_positional_parameters_renamed_are_not_breaks(tmp_path):
    # pydantic 2.10 -> 2.12 model_post_init(self, __context) -> (self, context, /);
    # typing_extensions 4.2 -> 4.13 assert_type(__val, __typ) -> (val, typ, /)
    old = {
        "pkg/__init__.py": "class Model:\n    def post_init(self, __context):\n        pass\n"
        "def assert_type(__val, __typ):\n    pass\n"
    }
    new = {
        "pkg/__init__.py": "class Model:\n    def post_init(self, context, /):\n        pass\n"
        "def assert_type(val, typ, /):\n    pass\n"
    }
    assert diff(tmp_path, old, new) == []


def test_a_positional_rename_is_one_change(tmp_path):
    # markdown-it-py 3 -> 4: parseLinkTitle(string, pos, maximum) -> (string, start, maximum, ...)
    old = {"pkg/__init__.py": "def parse(string, pos, maximum):\n    pass\n"}
    new = {"pkg/__init__.py": "def parse(string, start, maximum, prev=None):\n    pass\n"}
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.parameter, change.suggestions) == (PARAM_REMOVED, "pos", ["start"])


def test_a_positional_rename_behind_kwargs_is_still_reported(tmp_path):
    # django-allauth: complete_login(self, request, app, access_token, **kwargs) -> token
    old = {"pkg/__init__.py": "def login(request, access_token, **kwargs):\n    pass\n"}
    new = {"pkg/__init__.py": "def login(request, token, **kwargs):\n    pass\n"}
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.parameter, change.suggestions) == (
        PARAM_REMOVED,
        "access_token",
        ["token"],
    )


def test_signatures_keep_the_positional_only_marker(tmp_path):
    old = {"pkg/__init__.py": "def f(x, /, y=1, *args, z=2):\n    pass\n"}
    new = {"pkg/__init__.py": "def f(x, /, *args, z=2):\n    pass\n"}
    (change,) = diff(tmp_path, old, new)
    assert change.new_signature == "f(x, /, *args, z = 2)"


def test_a_function_wrapped_by_a_class_keeps_its_call_signature(tmp_path):
    # typing_extensions 4.12 -> 4.15: TypedDict became @_TypedDictSpecialForm def TypedDict(self, args)
    old = {"pkg/__init__.py": "def TypedDict(typename, fields=None, /, *, total=True):\n    pass\n"}
    new = {
        "pkg/__init__.py": "class _SpecialForm:\n    def __init__(self, getitem):\n        pass\n"
        "    def __call__(self, *args, **kwargs):\n        pass\n\n"
        "@_SpecialForm\ndef TypedDict(self, args):\n    pass\n"
    }
    assert diff(tmp_path, old, new) == []


def test_the_parameters_of_a_pytest_fixture_are_not_a_call_api(tmp_path):
    # time-machine 2.16 -> 2.19: time_machine_fixture(request) is a fixture; pytest passes
    # `request`, nobody calls it.
    old = {
        "pkg/__init__.py": "import pytest\nfrom pytest import fixture\n\n"
        "@pytest.fixture(name='time_machine')\ndef time_machine_fixture():\n    yield 1\n\n"
        "@fixture\ndef clock(tz='UTC'):\n    yield 1\n\n"
        "def travel(destination, tick=True):\n    pass\n"
    }
    new = {
        "pkg/__init__.py": "import pytest\nfrom pytest import fixture\n\n"
        "@pytest.fixture(name='time_machine')\ndef time_machine_fixture(request):\n    yield 1\n\n"
        "@fixture\ndef clock():\n    yield 1\n\n"
        "def travel(destination):\n    pass\n"
    }
    assert found(diff(tmp_path, old, new)) == {(PARAM_REMOVED, "pkg.travel", "tick")}


def test_new_required_parameter_of_an_overloaded_function(tmp_path):
    # starlette 0.46 -> 1.7: TemplateResponse had an overload without ``request``
    overloads = (
        "from typing import overload\n"
        "class T:\n"
        "    @overload\n    def render(self, request, name):\n        ...\n"
        "    @overload\n    def render(self, name):\n        ...\n"
        "    def render(self, *args, **kwargs):\n        pass\n"
    )
    new = {"pkg/__init__.py": "class T:\n    def render(self, request, name):\n        pass\n"}
    assert found(diff(tmp_path, {"pkg/__init__.py": overloads}, new)) == {
        (PARAM_REQUIRED, "pkg.T.render", "request")  # ``name`` was required by both
    }


# ------------------------------------------------------ names still available
def test_names_still_in_all_are_not_removed(tmp_path):
    # typing_extensions 4.12 -> 4.15: re-exports bound by globals().update(...)
    old = {
        "pkg/__init__.py": "import typing\n__all__ = ['Callable', 'Dict', 'decorator']\n"
        "Callable = typing.Callable\nDict = typing.Dict\ndecorator = typing.no_type_check\n"
    }
    new = {
        "pkg/__init__.py": "import sys\nimport typing\n__all__ = ['Callable', 'Dict']\n"
        "_names = ['Callable', 'Dict']\n"
        "if sys.version_info < (3, 15):\n"
        "    _names.append('decorator')\n    __all__.append('decorator')\n"
        "globals().update({n: getattr(typing, n) for n in _names})\n"
    }
    assert diff(tmp_path, old, new) == []


def test_a_name_still_in_all_that_nothing_binds_is_removed(tmp_path):
    # black 25.1 -> 26.5.1: blib2to3.pgen2.tokenize keeps "generate_tokens" in __all__, but
    # nothing defines it any more, so ``from ... import generate_tokens`` fails.
    old = {
        "pkg/__init__.py": "",
        "pkg/tokenize.py": "__all__ = ['tokenize', 'generate_tokens']\n\n"
        "def tokenize(readline):\n    pass\n\ndef generate_tokens(readline):\n    pass\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/tokenize.py": "__all__ = ['tokenize', 'generate_tokens']\n\n"
        "def tokenize(readline):\n    pass\n",
    }
    assert found(diff(tmp_path, old, new)) == {(REMOVED, "pkg.tokenize.generate_tokens", None)}


def test_names_a_module_getattr_still_serves_are_deprecations(tmp_path):
    # click 8.1 -> 8.2 (BaseCommand), mypy_extensions 1.0 -> 1.1 (NoReturn)
    old = {"pkg/__init__.py": "class Command:\n    pass\nclass BaseCommand:\n    pass\n"}
    new = {
        "pkg/__init__.py": "class Command:\n    pass\nclass _BaseCommand:\n    pass\n\n"
        "def __getattr__(name):\n"
        "    if name == 'BaseCommand':\n"
        "        import warnings\n"
        "        warnings.warn(\n"
        "            \"'BaseCommand' is deprecated and will be removed in 9.0.\"\n"
        "            \" Use 'Command' instead.\",\n"
        "            DeprecationWarning,\n"
        "        )\n"
        "        return _BaseCommand\n"
        "    raise AttributeError(name)\n"
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path, change.deprecated_by) == (
        DEPRECATED,
        "pkg.BaseCommand",
        "__getattr__",
    )
    assert change.deprecation == (
        "'BaseCommand' is deprecated and will be removed in 9.0. Use 'Command' instead."
    )


def test_names_served_lazily_without_a_warning_are_not_removed(tmp_path):
    old = {"pkg/__init__.py": "class Heavy:\n    pass\n"}
    new = {
        "pkg/__init__.py": "def __getattr__(name):\n    if name == 'Heavy':\n"
        "        from pkg._heavy import Heavy\n        return Heavy\n    raise AttributeError(name)\n",
        "pkg/_heavy.py": "class Heavy:\n    pass\n",
    }
    assert diff(tmp_path, old, new) == []


def test_names_a_lazy_module_class_serves_by_suffix_are_not_removed(tmp_path):
    # transformers 4.49 -> 5.7: `sys.modules[__name__] = _LazyModule(...)`, whose __getattr__
    # hands out any *TokenizerFast name as the tokenizer without "Fast" (100 false removals),
    # and any name a module below it lists in __all__ (models/bart: BartTokenizerFast).
    lazy = (
        "import warnings\nfrom types import ModuleType\n\n"
        "class _LazyModule(ModuleType):\n"
        "    def __getattr__(self, name):\n"
        "        if name.endswith('TokenizerFast'):\n"
        "            fallback_name = name[:-4]\n"
        "            return getattr(self, fallback_name)\n"
        "        if name.endswith('Tokenizer') or name.endswith('TokenizerFast'):\n"
        "            lookup_name = name[:-4] if name.endswith('TokenizerFast') else name\n"
        "            from .convert import CONVERTERS\n"
        "            if lookup_name in CONVERTERS:\n"
        "                return CONVERTERS[lookup_name]\n"
        "        if name.endswith('ProcessorFast'):\n"
        "            warnings.warn(f'{name} is deprecated, use the slow processor')\n"
        "            return getattr(self, name[:-4])\n"
        "        raise AttributeError(name)\n"
    )
    old = {
        "pkg/__init__.py": "from pkg.tok import T5Tokenizer, T5TokenizerFast, BertTokenizerFast\n"
        "from pkg.tok import ImageProcessor, ImageProcessorFast, BartTokenizerFast\n",
        "pkg/tok.py": "class T5Tokenizer:\n    pass\nclass T5TokenizerFast:\n    pass\n"
        "class BertTokenizerFast:\n    pass\nclass BartTokenizerFast:\n    pass\n"
        "class ElectraTokenizerFast:\n    pass\n"
        "class ImageProcessor:\n    pass\nclass ImageProcessorFast:\n    pass\n",
    }
    old["pkg/__init__.py"] += "from pkg.tok import ElectraTokenizerFast\n"
    new = {
        "pkg/__init__.py": "import sys\nfrom typing import TYPE_CHECKING\n"
        "from pkg.utils import _LazyModule\n\n"
        "if TYPE_CHECKING:\n    from pkg.tok import T5Tokenizer, ImageProcessor\n"
        "else:\n    sys.modules[__name__] = _LazyModule(__name__)\n",
        "pkg/tok.py": "class T5Tokenizer:\n    pass\nclass ImageProcessor:\n    pass\n",
        "pkg/utils.py": lazy,
        # transformers' SLOW_TO_FAST_CONVERTERS: ElectraTokenizer is served as BertTokenizer.
        "pkg/convert.py": "CONVERTERS = {'ElectraTokenizer': object}\n",
        "pkg/models/__init__.py": "",
        "pkg/models/bart.py": "from pkg.tok import T5Tokenizer as _Roberta\n\n"
        "BartTokenizerFast = _Roberta\n__all__ = ['BartTokenizerFast']\n",
    }
    changes = {(c.kind, c.path): c for c in diff(tmp_path, old, new)}
    assert set(changes) == {
        (REMOVED, "pkg.BertTokenizerFast"),  # no BertTokenizer to fall back on
        (DEPRECATED, "pkg.ImageProcessorFast"),
        (REMOVED, "pkg.tok.T5TokenizerFast"),  # tok is a plain module
        (REMOVED, "pkg.tok.BertTokenizerFast"),
        (REMOVED, "pkg.tok.BartTokenizerFast"),
        (REMOVED, "pkg.tok.ElectraTokenizerFast"),
        (REMOVED, "pkg.tok.ImageProcessorFast"),
    }
    assert changes[(DEPRECATED, "pkg.ImageProcessorFast")].deprecation == (
        "ImageProcessorFast is deprecated, use the slow processor"
    )


def test_deprecated_alias_tables_are_deprecations(tmp_path):
    # anyio 4.8 -> 4.15: anyio.abc.CapacityLimiter via set_deprecated_aliases({...})
    old = {
        "pkg/__init__.py": "from pkg._sync import Lock\n",
        "pkg/_sync.py": "class Lock:\n    pass\n",
        "pkg/abc/__init__.py": "from pkg._sync import Lock as Lock\n",
    }
    new = {
        **old,
        "pkg/_lazy.py": "def set_deprecated_aliases(aliases):\n    pass\n",
        "pkg/abc/__init__.py": "from pkg._lazy import set_deprecated_aliases\n\n"
        "set_deprecated_aliases({'Lock': 'pkg.Lock'})\n",
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path, change.deprecation) == (
        DEPRECATED,
        "pkg.abc.Lock",
        "use `pkg.Lock` instead",
    )


def test_a_metaclass_property_keeps_a_class_attribute(tmp_path):
    # urllib3 1.26.6 -> 1.26.14: Retry.BACKOFF_MAX moved to a deprecated metaclass property
    old = {"pkg/__init__.py": "class Retry:\n    BACKOFF_MAX = 120\n"}
    new = {
        "pkg/__init__.py": "import six\nimport warnings\n\n"
        "class _RetryMeta(type):\n"
        "    @property\n"
        "    def BACKOFF_MAX(cls):\n"
        "        warnings.warn(\"Using 'Retry.BACKOFF_MAX' is deprecated\", DeprecationWarning)\n"
        "        return cls.DEFAULT_BACKOFF_MAX\n\n"
        "@six.add_metaclass(_RetryMeta)\n"
        "class Retry:\n    DEFAULT_BACKOFF_MAX = 120\n"
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path) == (DEPRECATED, "pkg.Retry.BACKOFF_MAX")
    assert change.deprecation == "Using 'Retry.BACKOFF_MAX' is deprecated"


def test_a_star_import_from_another_package_hides_nothing(tmp_path):
    # mcp 1.x -> 2.0: mcp/types/__init__.py became ``from mcp_types import *``;
    # pywin32 308 -> 311: an unresolved ``from dde import *`` was reported as removed 'dde/*'
    old = {
        "pkg/__init__.py": "",
        "pkg/types.py": "class Tool:\n    pass\nclass TextContent:\n    pass\n",
        "pkg/ide.py": "from dde import *\n\ndef start():\n    pass\n",
        "pkg/models.py": "class FastTokenizer:\n    pass\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/types.py": "from other_types import *\nfrom other_types import __all__ as __all__\n",
        "pkg/ide.py": "from dde import CreateServer\n\ndef start():\n    pass\n",
        # transformers 5: a star import of a module of this package that no longer exists
        "pkg/models.py": "from pkg.tokenization_fast import *\n",
    }
    assert found(diff(tmp_path, old, new)) == {(REMOVED, "pkg.models.FastTokenizer", None)}


def test_a_function_now_a_method_of_its_class_is_not_removed(tmp_path):
    # pymupdf 1.25 -> 1.27: ``Document.new_page = utils.new_page`` became a real method
    old = {
        "pkg/__init__.py": "from pkg import utils\n\nclass Document:\n    pass\n\n"
        "Document.new_page = utils.new_page\n",
        "pkg/utils.py": "def new_page(doc, pno=-1):\n    pass\ndef helper():\n    pass\n",
    }
    new = {
        "pkg/__init__.py": "class Document:\n    def new_page(self, pno=-1):\n        pass\n",
        "pkg/utils.py": "",
    }
    assert found(diff(tmp_path, old, new)) == {(REMOVED, "pkg.utils.helper", None)}


# ------------------------------------------------------- compiled modules
# Only sources and stubs are extracted: a module the new release ships as an extension module
# (``fast.cpython-312-x86_64-linux-gnu.so``, ``fast.pyd``) with no stub looked removed, with
# everything re-exported from it (issue #52).
FAST = "def speedy(x: int) -> int:\n    return x\n\ndef gone() -> None: ...\n"


def unread(
    tmp_path: Path, old: dict[str, str], new: dict[str, str], names=("pkg",), compiled=()
) -> tuple[set[tuple[str, str, str | None]], list[str]]:
    """What :func:`diff_sources_with_unread` finds, and the compiled modules it names."""
    a = write_tree(tmp_path / "old", old)
    b = write_tree(tmp_path / "new", new)
    result, hid = diff_sources_with_unread(
        "pkg", "1", a, "2", b, list(names), new_compiled=list(compiled)
    )
    return found([APIChange.from_dict(c) for c in result]), hid


def test_a_module_that_became_compiled_is_not_removed(tmp_path):
    old = {"pkg/__init__.py": "from pkg.fast import speedy\n", "pkg/fast.py": FAST}
    new = {"pkg/__init__.py": "from pkg.fast import speedy\n"}  # and fast.cpython-312-*.so
    assert unread(tmp_path, old, new, compiled=["pkg.fast"]) == (set(), ["pkg.fast"])
    # What it reported before the scan knew of the compiled module:
    assert (REMOVED, "pkg.fast", None) in found(diff(tmp_path / "unaware", old, new))


def test_a_compiled_module_that_lost_its_stub_is_not_removed(tmp_path):
    stub = "def speedy(x: int) -> int: ...\n"
    old = {"pkg/__init__.py": "", "pkg/fast.pyi": stub}  # and fast.cpython-312-*.so
    new = {"pkg/__init__.py": ""}  # the same extension module, no stub any more
    assert unread(tmp_path, old, new, compiled=["pkg.fast"]) == (set(), ["pkg.fast"])
    assert (REMOVED, "pkg.fast", None) in found(diff(tmp_path / "unaware", old, new))


def test_a_top_level_module_that_became_compiled_is_not_removed(tmp_path):
    old = {"fastmod.py": FAST}
    new: dict[str, str] = {}  # fastmod.abi3.so
    names = ("fastmod",)
    assert unread(tmp_path, old, new, names, compiled=["fastmod"]) == (set(), ["fastmod"])
    assert found(diff(tmp_path / "unaware", old, new, names=names)) == {(REMOVED, "fastmod", None)}


def test_a_module_removed_with_nothing_in_its_place_is_still_removed(tmp_path):
    old = {"pkg/__init__.py": "", "pkg/fast.py": FAST, "pkg/slow.py": FAST}
    new = {"pkg/__init__.py": ""}  # fast is compiled now; slow is gone
    assert found(diff(tmp_path, old, new, compiled=["pkg.fast"])) == {(REMOVED, "pkg.slow", None)}
    # A name that only starts like a compiled module is not inside it.
    old = {"pkg/__init__.py": "", "pkg/fast.py": FAST, "pkg/faster.py": FAST}
    new = {"pkg/__init__.py": ""}
    assert found(diff(tmp_path / "prefix", old, new, compiled=["pkg.fast"])) == {
        (REMOVED, "pkg.faster", None)
    }


@pytest.mark.parametrize("module", ["pkg.fast", "pkg._core"], ids=["public", "private"])
def test_a_name_a_readable_module_no_longer_imports_from_a_compiled_one_is_removed(
    tmp_path, module
):
    """``from pkg import gone`` fails once ``pkg/__init__.py`` stops importing it, whether or
    not the module it came from is compiled now."""
    old = {
        "pkg/__init__.py": f"from {module} import gone, speedy\n",
        f"pkg/{module.split('.')[1]}.py": FAST,
    }
    new = {"pkg/__init__.py": f"from {module} import speedy\n"}
    assert unread(tmp_path, old, new, compiled=[module]) == (
        {(REMOVED, "pkg.gone", None)},
        [module],  # private too: pkg.speedy comes from it
    )
    # Still imported, or star-imported from the compiled module: hidden, not removed.
    for i, kept in enumerate((f"from {module} import gone, speedy\n", f"from {module} import *\n")):
        new = {"pkg/__init__.py": kept}
        assert unread(tmp_path / f"kept{i}", old, new, compiled=[module]) == (set(), [module])


def test_a_compiled_package_init_hides_its_own_names_not_its_submodules(tmp_path):
    """``pkg/__init__.cpython-312-x86_64-linux-gnu.so`` is ``pkg.__init__``: the names
    ``pkg`` defines are hidden, the readable ``pkg/sub.py`` is still compared."""
    old = {
        "pkg/__init__.py": "def foo() -> None: ...\n",
        "pkg/sub.py": "def bar() -> None: ...\n\ndef baz() -> None: ...\n",
        "pkg/old.py": "",
    }
    new = {"pkg/sub.py": "def bar() -> None: ...\n"}  # and __init__.cpython-312-*.so
    assert unread(tmp_path, old, new, compiled=["pkg.__init__"]) == (
        {(REMOVED, "pkg.sub.baz", None), (REMOVED, "pkg.old", None)},
        ["pkg.__init__"],
    )


def test_a_module_compiled_in_both_releases_is_not_unread(tmp_path):
    # No source or stub at the cutoff either: nothing was compared then, nothing is lost now.
    old = new = {"pkg/__init__.py": "from pkg._core import speedy\n"}
    assert unread(tmp_path, old, new, compiled=["pkg._core"]) == (set(), [])


# ------------------------------------------------------------------- stubs
def test_functions_declared_only_by_overloads_are_not_removed(tmp_path):
    # numpy 2.2.3 -> 2.2.6 ndarray.partition, types-cachetools cachetools.cached
    old = {
        "pkg-stubs/__init__.pyi": "class ndarray:\n    def partition(self, kth: int) -> None: ...\n"
        "def cached(cache: object) -> object: ...\n"
    }
    new = {
        "pkg-stubs/__init__.pyi": "from typing import overload\n\nclass ndarray:\n"
        "    @overload\n    def partition(self, kth: int) -> None: ...\n"
        "    @overload\n    def partition(self, kth: list[int]) -> None: ...\n\n"
        "@overload\ndef cached(cache: object) -> object: ...\n"
        "@overload\ndef cached(cache: object, key: object) -> object: ...\n"
    }
    assert diff(tmp_path, old, new, names=("pkg-stubs",)) == []


def test_a_deprecated_overload_is_a_deprecated_call_form(tmp_path):
    # pydantic 2.10 -> 2.12: only the ``with_config(*, config=...)`` overload is @deprecated;
    # types-cachetools: only the overload with a positional ``info``.
    old = {
        "pkg/__init__.py": "def with_config(config=None, /, **kwargs):\n    pass\n",
        "pkg/cache.pyi": "def cached(cache: object, key: object = ..., info: bool = ...) -> object:"
        " ...\n",
    }
    new = {
        "pkg/__init__.py": "from typing import overload\n"
        "from typing_extensions import deprecated\n\n"
        "@overload\ndef with_config(config: dict, /) -> None: ...\n"
        "@overload\n@deprecated('Passing `config` as a keyword argument is deprecated.')\n"
        "def with_config(*, config: dict) -> None: ...\n"
        "@overload\ndef with_config(**kwargs: object) -> None: ...\n"
        "def with_config(config=None, /, **kwargs):\n    pass\n",
        "pkg/cache.pyi": "from typing import overload\n"
        "from typing_extensions import deprecated\n\n"
        "@overload\n@deprecated('Passing `info` as positional parameter is deprecated.')\n"
        "def cached(cache: object, key: object, lock: object, info: bool) -> object: ...\n"
        "@overload\ndef cached(cache: object, key: object = ..., *, info: bool = ...) -> object:"
        " ...\n",
    }
    changes = {c.path: c for c in diff(tmp_path, old, new)}
    assert set(changes) == {"pkg.with_config", "pkg.cache.cached"}
    assert changes["pkg.with_config"].describe(versioned=False) == (
        "`pkg.with_config` called as `with_config(*, config: dict)` is deprecated: Passing "
        "`config` as a keyword argument is deprecated."
    )
    assert changes["pkg.cache.cached"].call_form == (
        "cached(cache: object, key: object, lock: object, info: bool)"
    )
    # Deprecated in every form, it is the function that is deprecated.
    whole = {
        "pkg/__init__.py": "from typing_extensions import deprecated\n\n"
        "@deprecated('Use configure() instead.')\n"
        "def with_config(config=None, /, **kwargs):\n    pass\n"
    }
    (change,) = diff(tmp_path / "whole", {"pkg/__init__.py": old["pkg/__init__.py"]}, whole)
    assert change.call_form is None and change.describe(versioned=False) == (
        "`pkg.with_config` is deprecated: Use configure() instead."
    )


# A library that supports Pythons older than 3.13 imports PEP 702's decorator once, in a compat
# module, and uses it from there. Issue #44: it was taken for the library's own decorator
# (``deprecated_by="deprecated"``, so ``run`` never probed it), and missed on an overload.
PEP702_SHIMS = {
    "re-export": "from typing_extensions import deprecated\n",
    "conditional": "import sys\n\n"
    "if sys.version_info >= (3, 13):\n"
    "    from warnings import deprecated\n"
    "else:\n"
    "    from typing_extensions import deprecated\n",
}
SHIM_IMPORTS = {
    "absolute": "from pkg._compat import deprecated",
    "relative": "from ._compat import deprecated",
}


@pytest.mark.parametrize("shim", PEP702_SHIMS.values(), ids=PEP702_SHIMS.keys())
@pytest.mark.parametrize("imported", SHIM_IMPORTS.values(), ids=SHIM_IMPORTS.keys())
def test_pep702_deprecated_through_a_compat_module_is_pep702(tmp_path, shim, imported):
    old = {"pkg/__init__.py": "def f():\n    pass\n\ndef g():\n    pass\n"}
    new = {
        "pkg/_compat.py": shim,
        "pkg/__init__.py": f"{imported}\n\n"
        "@deprecated('use g instead')\ndef f():\n    pass\n\ndef g():\n    pass\n",
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path, change.deprecated_by, change.call_form) == (
        DEPRECATED,
        "pkg.f",
        None,
        None,
    )
    assert change.deprecation == "use g instead"


@pytest.mark.parametrize("shim", PEP702_SHIMS.values(), ids=PEP702_SHIMS.keys())
@pytest.mark.parametrize("imported", SHIM_IMPORTS.values(), ids=SHIM_IMPORTS.keys())
def test_pep702_deprecated_overload_through_a_compat_module_is_a_call_form(
    tmp_path, shim, imported
):
    old = {"pkg/__init__.py": "def h(x):\n    pass\n"}
    new = {
        "pkg/_compat.py": shim,
        "pkg/__init__.py": f"from typing import overload\n{imported}\n\n"
        "@overload\ndef h(x: int) -> None: ...\n"
        "@overload\n@deprecated('Passing a str is deprecated.')\ndef h(x: str) -> None: ...\n"
        "def h(x):\n    pass\n",
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path, change.deprecated_by, change.call_form) == (
        DEPRECATED,
        "pkg.h",
        None,
        "h(x: str)",
    )
    assert change.deprecation == "Passing a str is deprecated."


def test_a_library_decorator_named_deprecated_is_still_the_librarys(tmp_path):
    # Not every ``deprecated`` is PEP 702's: a compat module that defines its own decorator
    # (it warns at run time; type checkers do not see it) stays ``deprecated_by`` it.
    old = {"pkg/__init__.py": "def f():\n    pass\n"}
    new = {
        "pkg/_compat.py": "def deprecated(message):\n"
        "    def wrap(fn):\n        return fn\n    return wrap\n",
        "pkg/__init__.py": "from pkg._compat import deprecated\n\n"
        "@deprecated('use g instead')\ndef f():\n    pass\n",
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path, change.deprecated_by) == (DEPRECATED, "pkg.f", "deprecated")


def test_pep702_deprecated_through_a_chain_of_compat_modules_is_pep702(tmp_path):
    # One private module imports it under another name, the next one renames it back.
    old = {"pkg/__init__.py": "def f():\n    pass\n"}
    new = {
        "pkg/_typing.py": "from typing_extensions import deprecated as _deprecated\n",
        "pkg/_compat.py": "from pkg._typing import _deprecated as deprecated\n",
        "pkg/__init__.py": "from pkg._compat import deprecated\n\n"
        "@deprecated('use g instead')\ndef f():\n    pass\n",
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path, change.deprecated_by, change.deprecation) == (
        DEPRECATED,
        "pkg.f",
        None,
        "use g instead",
    )


def test_overloads_declared_in_a_stub_next_to_the_module_are_kept(tmp_path):
    # pytest-django 4.10 -> 4.14: asserts.py builds the functions, asserts.pyi overloads them
    old = {"pkg/__init__.py": "", "pkg/asserts.py": "def assertNumQueries(num):\n    pass\n"}
    new = {
        "pkg/__init__.py": "",
        "pkg/asserts.py": "for _name in ['assertNumQueries']:\n    globals()[_name] = print\n",
        "pkg/asserts.pyi": "from typing import overload\n\n"
        "@overload\ndef assertNumQueries(num: int) -> None: ...\n"
        "@overload\ndef assertNumQueries(num: int, func: object) -> None: ...\n",
    }
    assert diff(tmp_path, old, new) == []


def test_names_a_stub_declares_reach_star_imports(tmp_path):
    # qdrant-client 1.13 -> 1.17: grpc/*_pb2.py build their classes, *_pb2.pyi declare them
    old = {
        "pkg/__init__.py": "from pkg.points_pb2 import *\n",
        "pkg/points_pb2.py": "from pkg import _reflection\n"
        "Filter = _reflection.Message('Filter', ())\n",
        "pkg/_reflection.py": "def Message(name, fields):\n    pass\n",
    }
    new = {
        "pkg/__init__.py": "from pkg.points_pb2 import *\n",
        "pkg/points_pb2.py": "from pkg import _builder\n_builder.build(globals())\n",
        "pkg/points_pb2.pyi": "class Filter:\n    must: list[object]\n",
        "pkg/_builder.py": "def build(namespace):\n    pass\n",
    }
    assert diff(tmp_path, old, new) == []


def test_a_protobuf_message_that_moved_to_another_stub_is_a_move(tmp_path):
    # qdrant-client 1.13 -> 1.17: points_pb2.Filter, built with GeneratedProtocolMessageType,
    # is now declared as a class in qdrant_common_pb2.pyi (21 plain removals before).
    built = (
        "from google.protobuf import reflection as _reflection\n"
        "from google.protobuf import message as _message\n"
        "{} = _reflection.GeneratedProtocolMessageType('{}', (_message.Message,), {{}})\n"
    )
    builder = "from google.protobuf.internal import builder as _builder\n_globals = globals()\n"
    stub = (
        "import google.protobuf.message\n\nclass {}(google.protobuf.message.Message):\n    x: int\n"
    )
    old = {
        "pkg/__init__.py": "",
        "pkg/grpc/__init__.py": "from .points_pb2 import *\n",
        "pkg/grpc/points_pb2.py": built.format("Filter", "Filter")
        + "PointId = _reflection.GeneratedProtocolMessageType('PointId', (_message.Message,), {})\n"
        + "Gone = _reflection.GeneratedProtocolMessageType('Gone', (_message.Message,), {})\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/grpc/__init__.py": "from .points_pb2 import *\nfrom .common_pb2 import *\n",
        "pkg/grpc/points_pb2.py": builder,
        "pkg/grpc/points_pb2.pyi": stub.format("PointId"),
        "pkg/grpc/common_pb2.py": builder,
        "pkg/grpc/common_pb2.pyi": stub.format("Filter"),
    }
    changes = {c.path: c for c in diff(tmp_path, old, new)}
    assert {p: (c.kind, c.moved_to) for p, c in changes.items()} == {
        "pkg.grpc.points_pb2.Filter": (MOVED, "pkg.grpc.Filter"),
        "pkg.grpc.points_pb2.Gone": (REMOVED, None),
        "pkg.grpc.Gone": (REMOVED, None),
    }


def test_imports_that_a_star_import_copies_are_not_api(tmp_path):
    # av 14 -> 18 (av.container.Any), types-pyyaml (yaml.Composer), nltk 3.9 -> 3.10
    # (nltk.corpus.reader.ieer.os), copier 9.5 (copier.asdict)
    old = {
        "pkg/__init__.pyi": "from pkg.core import *\n",
        "pkg/core.pyi": "from typing import Any\nfrom fractions import Fraction\n"
        "from pkg.frames import Frame\nfrom pkg.util import Real as Real\n"
        "class Container:\n    def open(self, file: Any) -> None: ...\n",
        "pkg/frames.pyi": "class Frame: ...\n",
        "pkg/util.pyi": "class Real: ...\n",
        "pkg/reader.py": "import os\nfrom pkg.api import *\n\ndef read():\n    pass\n",
        "pkg/api.py": "import pickle\n\nclass CorpusReader:\n    pass\n",
    }
    new = {
        **old,
        "pkg/core.pyi": "from typing import Any\n\nclass Container:\n"
        "    def open(self, file: Any) -> None: ...\n",
        "pkg/reader.py": "from pkg.api import CorpusReader\n\ndef read():\n    pass\n",
    }
    # ``from pkg.util import Real as Real`` was a re-export; the plain imports were not, and
    # neither was CorpusReader for the plain module that star-imported it.
    assert found(diff(tmp_path, old, new)) == {(REMOVED, "pkg.Real", None)}


# ------------------------------------------------ module-level name bindings
def test_names_bound_under_a_main_guard_or_on_some_paths_are_not_api(tmp_path):
    # rich 10 -> 13 (rich.diagnose.console), virtualenv 20 -> 21 (py_info.argv),
    # torchvision 0.21 -> 0.29 (torchvision.extension.lib_path), certifi (core.Package)
    old = {
        "pkg/__init__.py": "",
        "pkg/diagnose.py": "def report():\n    pass\n\n"
        "if __name__ == '__main__':\n    console = object()\n    argv = []\n\n"
        "    def main():\n        pass\n",
        "pkg/extension.py": "import sys\n\ntry:\n    lib_path = sys.prefix\n    loaded = True\n"
        "except ImportError:\n    loaded = False\n\n"
        "if sys.version_info >= (3, 11):\n    def where():\n        pass\n"
        "else:\n    Package = str\n\n    def where():\n        pass\n\nLIMIT = 3\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/diagnose.py": "def report():\n    pass\n",
        "pkg/extension.py": "import sys\n\ndef where():\n    pass\n",
    }
    # ``loaded`` was bound on every path, LIMIT unconditionally: those are real removals.
    assert found(diff(tmp_path, old, new)) == {
        (REMOVED, "pkg.extension.loaded", None),
        (REMOVED, "pkg.extension.LIMIT", None),
    }


def test_loggers_type_variables_and_version_file_names_are_not_api(tmp_path):
    # filelock 3.17 -> 3.29 (version.TYPE_CHECKING, VERSION_TUPLE), virtualenv (LOGGER)
    old = {
        "pkg/__init__.py": "import logging\nfrom typing import TypeVar\n"
        "LOGGER = logging.getLogger(__name__)\nT = TypeVar('T')\n",
        "pkg/version.py": "TYPE_CHECKING = False\nif TYPE_CHECKING:\n"
        "    from typing import Tuple, Union\n    VERSION_TUPLE = Tuple[Union[int, str], ...]\n"
        "else:\n    VERSION_TUPLE = object\n\nversion = '1'\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/version.py": "__all__ = ['version']\nversion = '2'\n",
    }
    changes = diff(tmp_path, old, new)
    assert found(changes) == {(REMOVED, "pkg.version.VERSION_TUPLE", None)}
    assert collapse(changes) == []  # generated version-file metadata, like version_tuple


# ------------------------------------------------------------ inherited members
def test_members_a_standard_library_base_still_provides_are_not_removed(tmp_path):
    # transformers 4.49 -> 5.17: BatchFeature(UserDict) dropped its own keys/items;
    # attrs 25 -> 26: FrozenError(AttributeError) dropped its own ``args``
    old = {
        "pkg/__init__.py": "from collections import UserDict\n\nclass Batch(UserDict):\n"
        "    def keys(self):\n        return self.data.keys()\n"
        "    def to(self, device):\n        pass\n\n"
        "class FrozenError(AttributeError):\n    args = ['frozen']\n"
    }
    new = {
        "pkg/__init__.py": "from collections import UserDict\n\nclass Batch(UserDict):\n"
        "    pass\n\nclass FrozenError(AttributeError):\n    pass\n"
    }
    assert found(diff(tmp_path, old, new)) == {(REMOVED, "pkg.Batch.to", None)}


def test_overrides_of_a_base_from_another_package_are_still_inherited(tmp_path):
    # starlette 0.46 -> 1.7: TestClient(httpx.Client) stopped overriding get/post
    old = {
        "pkg/__init__.py": "import httpx\n\nclass TestClient(httpx.Client):\n"
        "    def get(self, url, **kwargs):\n        return super().get(url, **kwargs)\n"
        "    def helper(self):\n        pass\n"
    }
    new = {"pkg/__init__.py": "import httpx\n\nclass TestClient(httpx.Client):\n    pass\n"}
    assert found(diff(tmp_path, old, new)) == {(REMOVED, "pkg.TestClient.helper", None)}


def test_members_of_a_base_the_package_vendors_are_still_inherited(tmp_path):
    # setuptools 75.8 -> 80.9: Command stopped overriding distutils' ensure_string_list, which
    # setuptools itself serves from setuptools/_distutils (and pip vendors in pip/_vendor).
    vendored = {
        "pkg/_distutils/__init__.py": "",
        "pkg/_distutils/cmd.py": "class Command:\n"
        "    def ensure_string_list(self, option):\n        pass\n",
        "pkg/_distutils/core.py": "from .cmd import Command\n",
        "pkg/_vendor/__init__.py": "",
        "pkg/_vendor/rich/__init__.py": "",
        "pkg/_vendor/rich/console.py": "class Console:\n    def print(self, text):\n        pass\n",
    }
    old = {
        **vendored,
        "pkg/__init__.py": "from distutils.core import Command as _Command\n"
        "from rich.console import Console as _Console\n\n"
        "class Command(_Command):\n"
        "    def ensure_string_list(self, option):\n        pass\n"
        "    def helper(self):\n        pass\n\n"
        "class Console(_Console):\n    def print(self, text):\n        pass\n",
        "pkg/command/__init__.py": "",
        "pkg/command/alias.py": "from pkg import Command\n\nclass alias(Command):\n    pass\n",
    }
    new = {
        **old,
        "pkg/__init__.py": "from distutils.core import Command as _Command\n"
        "from rich.console import Console as _Console\n\n"
        "class Command(_Command):\n    pass\n\nclass Console(_Console):\n    pass\n",
    }
    assert found(diff(tmp_path, old, new)) == {(REMOVED, "pkg.Command.helper", None)}


# -------------------------------------------------------------- kind changes
def test_values_that_stay_callable_are_not_kind_changes(tmp_path):
    # dulwich (write_pack_index = write_pack_index_v2), markdown-it-py (namedtuple -> NamedTuple),
    # structlog (callable instance -> function), Django 6.1 (BadHeaderError = ValueError),
    # numpy stubs (ones: Final[_Constructor] -> def ones)
    old = {
        "pkg/__init__.py": "from collections import namedtuple\nfrom typing import Final\n\n"
        "class _Formatter:\n    def __call__(self, text):\n        pass\n\n"
        "def write_v2(f):\n    pass\n\n"
        "write = write_v2\nScanned = namedtuple('Scanned', ['length'])\n"
        "rich_traceback = _Formatter()\nones: Final[_Formatter]\n"
        "class BadHeaderError(ValueError):\n    pass\n\nLIMIT = 5\n"
    }
    new = {
        "pkg/__init__.py": "from typing import NamedTuple\n\n"
        "def write_v2(f):\n    pass\n\ndef write(f):\n    pass\n\n"
        "class Scanned(NamedTuple):\n    length: int\n\n"
        "def rich_traceback(text):\n    pass\n\ndef ones(shape):\n    pass\n\n"
        "BadHeaderError = ValueError\n\ndef LIMIT():\n    pass\n"
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.path, change.old_kind, change.new_kind) == (
        KIND_CHANGED,
        "pkg.LIMIT",
        "attribute",
        "function",
    )


def test_a_definition_that_depends_on_the_python_version_is_not_a_kind_change(tmp_path):
    # tomlkit 0.13 -> 0.15: Encoder became a Protocol defined under ``if TYPE_CHECKING:``
    old = {"pkg/__init__.py": "from typing import Any, Callable\nEncoder = Callable[[Any], Any]\n"}
    new = {
        "pkg/__init__.py": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
        "    from typing import Protocol\n\n    class Encoder(Protocol):\n        pass\n"
    }
    assert KIND_CHANGED not in {c.kind for c in diff(tmp_path, old, new)}


# ---------------------------------------------------- moves and similar names
def test_moves_need_the_same_object_not_just_the_same_name(tmp_path):
    # virtualenv (LOGGER), dulwich (PEELED_TAG_SUFFIX to protocol, not server), opik, llama-index
    old = {
        "pkg/__init__.py": "",
        "pkg/refs.py": "PEELED = b'^{}'\n\ndef serialize(refs):\n    pass\n\n"
        "def trace(name):\n    pass\n\nconsole = object()\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/refs.py": "",
        "pkg/protocol.py": "PEELED = b'^{}'\n\ndef serialize(refs, extra=None):\n    pass\n",
        "pkg/server.py": "from pkg.protocol import PEELED\n\ndef trace():\n    pass\n\n"
        "console = object()\n",
    }
    changes = {c.path: c for c in diff(tmp_path, old, new)}
    assert (changes["pkg.refs.PEELED"].kind, changes["pkg.refs.PEELED"].moved_to) == (
        MOVED,
        "pkg.protocol.PEELED",
    )
    assert changes["pkg.refs.serialize"].moved_to == "pkg.protocol.serialize"
    assert changes["pkg.refs.trace"].kind == REMOVED  # a trace() that takes other arguments
    assert changes["pkg.refs.console"].kind == REMOVED  # another object built the same way


def test_a_module_moves_to_the_one_that_kept_its_contents(tmp_path):
    # sentence-transformers 3.4 -> 6.1: losses went to sentence_transformer.losses, not
    # sparse_encoder.losses
    old = {
        "pkg/__init__.py": "",
        "pkg/losses.py": "class CosineLoss:\n    pass\nclass TripletLoss:\n    pass\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/dense/__init__.py": "",
        "pkg/dense/losses.py": "class CosineLoss:\n    pass\nclass TripletLoss:\n    pass\n",
        "pkg/sparse/__init__.py": "",
        "pkg/sparse/losses.py": "class SpladeLoss:\n    pass\n",
    }
    (change,) = diff(tmp_path, old, new)
    assert (change.kind, change.moved_to) == (MOVED, "pkg.dense.losses")


def test_similar_names_are_new_names_of_the_same_sort(tmp_path):
    # typing_extensions (Collection -> `collections`), prompt_toolkit (is_zero -> `zero`)
    old = {
        "pkg/__init__.py": "import operator\n\ndef Collection():\n    pass\n"
        "class Dimension:\n    def is_zero(self):\n        pass\n"
        "    @classmethod\n    def zero(cls):\n        pass\n"
        "    def get_items(self):\n        pass\n"
    }
    new = {
        "pkg/__init__.py": "import operator\nimport collections\n\n"
        "class Dimension:\n    @classmethod\n    def zero(cls):\n        pass\n"
        "    def get_item(self):\n        pass\n    items_count = 0\n"
    }
    suggestions = {c.name: c.suggestions for c in diff(tmp_path, old, new)}
    assert suggestions == {"Collection": [], "is_zero": [], "get_items": ["get_item"]}


def test_objects_get_their_shortest_public_path(tmp_path):
    # cryptography 44 -> 50: AESGCM was reported under a chain of module imports
    # (``cryptography.x509.verification.rust_x509...aead.AESGCM``)
    tree = {
        "pkg/__init__.py": "",
        "pkg/_rust/__init__.py": "",
        "pkg/ciphers.py": "from pkg._rust.aead import AESGCM\n\n__all__ = ['AESGCM']\n",
        "pkg/zz_x509.py": "from pkg._rust import aead as rust_aead\n",
    }
    old = {
        **tree,
        "pkg/_rust/aead.py": "class AESGCM:\n    def generate_key(self, key_size):\n        pass\n",
    }
    new = {
        **tree,
        "pkg/_rust/aead.py": "class AESGCM:\n    def generate_key(self):\n        pass\n",
    }
    (change,) = diff(tmp_path, old, new)
    assert change.path == "pkg.ciphers.AESGCM.generate_key"


def test_placeholder_modules_for_missing_backends_are_not_api(tmp_path):
    # transformers 4.49 -> 5.17: 1,446 removals from utils.dummy_pt_objects and friends
    old = {
        "pkg/__init__.py": "",
        "pkg/utils/__init__.py": "",
        "pkg/utils/dummy_pt_objects.py": "class AutoModel:\n    pass\n",
    }
    new = {"pkg/__init__.py": "", "pkg/utils/__init__.py": ""}
    assert diff(tmp_path, old, new) == []


# ------------------------------------------------- **kwargs: Unpack[TypedDict] (issue #60)
# pandas 2.3.1's read_csv: the implementation and its overloads name every keyword.
READ_CSV_2 = '''
from typing import Literal, overload


@overload
def read_csv(path, *, iterator: Literal[True], chunksize: int | None = ..., sep: str = ...,
             delim_whitespace: bool = ..., date_parser=..., infer_datetime_format: bool = ...) -> int: ...
@overload
def read_csv(path, *, iterator: bool = ..., chunksize: None = ..., sep: str = ...,
             delim_whitespace: bool = ..., date_parser=..., infer_datetime_format: bool = ...) -> str: ...
def read_csv(path, *, iterator: bool = False, chunksize: int | None = None, sep: str = ",",
             delim_whitespace: bool = False, date_parser=None, infer_datetime_format: bool = False):
    """Read a csv.

    delim_whitespace : bool, default False
        .. deprecated:: 2.2.0
            Use ``sep="\\\\s+"`` instead.
    """
'''
# pandas 3.0.6's: they take the shared keywords through ``**kwds: Unpack[_read_shared[T]]``,
# a TypedDict with a base (``sep`` is the base's key) and ``total=False``.
READ_CSV_3 = '''
from typing import Generic, Literal, TypedDict, TypeVar, overload
from typing_extensions import Unpack

T = TypeVar("T")


class _base(TypedDict, total=False):
    sep: str


class _read_shared(_base, Generic[T], total=False):
    header: int | None


@overload
def read_csv(path, *, iterator: Literal[True], chunksize: int | None = ..., **kwds: Unpack[_read_shared[T]]) -> int: ...
@overload
def read_csv(path, *, iterator: bool = ..., chunksize: None = ..., **kwds: Unpack[_read_shared[T]]) -> str: ...
def read_csv(path, *, iterator: bool = False, chunksize: int | None = None, **kwds: Unpack[_read_shared[T]]):
    """Read a csv."""
'''


def test_keywords_a_typed_kwargs_no_longer_takes_are_removed_parameters(tmp_path):
    """pandas 2.3.1 -> 3.0.6 (issue #60): ``delim_whitespace``, ``date_parser`` and
    ``infer_datetime_format`` are gone from ``read_csv``, whose signatures now end in
    ``**kwds: Unpack[_read_shared]``. griffe takes a ``**kwargs`` for any keyword and reports
    nothing; the TypedDict's keys say which keywords it takes."""
    changes = diff(tmp_path, {"pkg/__init__.py": READ_CSV_2}, {"pkg/__init__.py": READ_CSV_3})
    assert found(changes) == {
        (PARAM_REMOVED, "pkg.read_csv", "date_parser"),
        (PARAM_REMOVED, "pkg.read_csv", "delim_whitespace"),
        (PARAM_REMOVED, "pkg.read_csv", "infer_datetime_format"),
    }
    whitespace = next(c for c in changes if c.parameter == "delim_whitespace")
    assert whitespace.tier == TIER_PUBLIC
    assert 'Use ``sep="\\s+"`` instead.' in (whitespace.hint or "")  # its docstring entry
    assert (whitespace.new_signature or "").endswith("**kwds)")
    assert whitespace.describe(versioned=False) == (
        "`pkg.read_csv(delim_whitespace=...)`: parameter `delim_whitespace` was removed"
    )


def test_a_bare_kwargs_still_takes_every_keyword(tmp_path):
    """Without a TypedDict nothing says what ``**kwargs`` takes, so nothing is removed: as
    before, on the implementation and on overloads alike."""
    new = READ_CSV_3.replace("**kwds: Unpack[_read_shared[T]]", "**kwds")
    assert diff(tmp_path, {"pkg/__init__.py": READ_CSV_2}, {"pkg/__init__.py": new}) == []


def test_a_typed_dict_key_that_moved_to_a_base_is_not_removed(tmp_path):
    """A key both versions take through the TypedDict: moved to a base TypedDict, it is still
    a key (``sep``); dropped from it, it is a removed parameter (``verbose``), although the
    two signatures read the same."""
    old = {
        "pkg/__init__.py": "from typing import TypedDict\nfrom typing_extensions import Unpack\n\n"
        "class _opts(TypedDict, total=False):\n    sep: str\n    verbose: bool\n\n"
        "def read(path, **kwds: Unpack[_opts]):\n    pass\n"
    }
    new = {
        "pkg/__init__.py": "from typing import TypedDict\nfrom typing_extensions import Unpack\n\n"
        "class _base(TypedDict, total=False):\n    sep: str\n\n"
        "class _opts(_base, total=False):\n    pass\n\n"
        "def read(path, **kwds: Unpack[_opts]):\n    pass\n"
    }
    assert found(diff(tmp_path, old, new)) == {(PARAM_REMOVED, "pkg.read", "verbose")}
    # A positional parameter that became a key of the TypedDict is still accepted.
    old = {"pkg/__init__.py": "def read(path, sep=','):\n    pass\n"}
    assert diff(tmp_path / "key", old, new) == []


def test_a_typed_dict_from_outside_the_package_takes_any_keyword(tmp_path):
    """The keys of a TypedDict of another package, or of one with a base there, cannot be read
    (nothing is imported): such a ``**kwargs`` takes any keyword, as a bare one does."""
    old = {"pkg/__init__.py": "def read(path, sep=',', verbose=False):\n    pass\n"}
    new = {
        "pkg/__init__.py": "from typing_extensions import Unpack\nfrom otherlib import Options\n\n"
        "def read(path, **kwds: Unpack[Options]):\n    pass\n"
    }
    assert diff(tmp_path, old, new) == []
    new = {
        "pkg/__init__.py": "from typing_extensions import Unpack\nfrom otherlib import Options\n\n"
        "class _opts(Options, total=False):\n    sep: str\n\n"
        "def read(path, **kwds: Unpack[_opts]):\n    pass\n"
    }
    assert diff(tmp_path / "base", old, new) == []


def test_a_method_made_static_with_typed_kwargs_does_not_lose_self(tmp_path):
    """``self`` and ``cls`` are no keywords: a method that became a static or a class method,
    keeping its ``**kw: Unpack[_Opts]``, was reported as losing parameter ``self``. A key the
    TypedDict of a method drops is still a removed parameter."""
    opts = (
        "from typing import TypedDict\nfrom typing_extensions import Unpack\n\n"
        "class _Opts(TypedDict, total=False):\n    sep: str\n\n"
    )
    old = {
        "pkg/__init__.py": opts + "class R:\n    def read(self, path, **kw: Unpack[_Opts]):\n"
        "        pass\n"
    }
    static = {
        "pkg/__init__.py": opts + "class R:\n    @staticmethod\n"
        "    def read(path, **kw: Unpack[_Opts]):\n        pass\n"
    }
    assert diff(tmp_path, old, static) == []
    classmethod_ = {
        "pkg/__init__.py": opts + "class R:\n    @classmethod\n"
        "    def read(cls, path, **kw: Unpack[_Opts]):\n        pass\n"
    }
    assert diff(tmp_path / "cls", old, classmethod_) == []
    fewer = {"pkg/__init__.py": old["pkg/__init__.py"].replace("    sep: str\n", "    pass\n")}
    assert found(diff(tmp_path / "key", old, fewer)) == {(PARAM_REMOVED, "pkg.R.read", "sep")}


@pytest.mark.parametrize(
    "keywords", ["extra_items=int", "closed=False"], ids=["extra_items", "open"]
)
def test_a_typed_dict_that_allows_other_keys_takes_any_keyword(tmp_path, keywords):
    """PEP 728: a TypedDict with ``extra_items=`` or ``closed=False`` takes keys it does not
    name, so a ``**kwargs`` unpacking it takes any keyword, as a bare one does; ``read(path,
    verbose=1)`` is accepted. A closed one takes its keys only."""
    old = {"pkg/__init__.py": "def read(path, sep=',', verbose=False):\n    pass\n"}
    new = {
        "pkg/__init__.py": "from typing_extensions import TypedDict, Unpack\n\n"
        f"class _Opts(TypedDict, total=False, {keywords}):\n    sep: str\n\n"
        "def read(path, **kw: Unpack[_Opts]):\n    pass\n"
    }
    assert diff(tmp_path, old, new) == []
    closed = {"pkg/__init__.py": new["pkg/__init__.py"].replace(f", {keywords}", ", closed=True")}
    assert found(diff(tmp_path / "closed", old, closed)) == {(PARAM_REMOVED, "pkg.read", "verbose")}


def test_keys_of_a_typed_kwargs_the_new_signature_does_not_name_are_removed(tmp_path):
    """The other way round: ``read(path, **kw: Unpack[_Opts])`` became ``read(path, sep=",")``.
    The TypedDict's keys the new signature does not name are removed parameters, one by one,
    in place of a ``**kw`` removal that names none of them."""
    old = {
        "pkg/__init__.py": "from typing import TypedDict\nfrom typing_extensions import Unpack\n\n"
        "class _Opts(TypedDict, total=False):\n    sep: str\n    verbose: bool\n\n"
        "def read(path, **kw: Unpack[_Opts]):\n    pass\n"
    }
    new = {"pkg/__init__.py": "def read(path, sep=','):\n    pass\n"}
    assert found(diff(tmp_path, old, new)) == {(PARAM_REMOVED, "pkg.read", "verbose")}
    none = {"pkg/__init__.py": "def read(path):\n    pass\n"}
    assert found(diff(tmp_path / "none", old, none)) == {
        (PARAM_REMOVED, "pkg.read", "sep"),
        (PARAM_REMOVED, "pkg.read", "verbose"),
    }


# ------------------------------------------------------ inherited methods (audit item 3b)
# pandas 2.3.1 -> 3.0.6: NDFrame.fillna lost ``method``. DataFrame inherits fillna, Series
# overrides it (and lost ``method`` too).
INIT = 'from pkg.core.frame import DataFrame\nfrom pkg.core.series import Series\n__all__ = ["DataFrame", "Series"]\n'
FRAMES = {
    "pkg/__init__.py": INIT,
    "pkg/core/__init__.py": "",
    "pkg/core/frame.py": "from pkg.core.generic import NDFrame\n\nclass DataFrame(NDFrame):\n    pass\n",
}
FILLNA_OLD = {
    **FRAMES,
    "pkg/core/generic.py": "class NDFrame:\n    def fillna(self, value=None, method=None):\n        pass\n",
    "pkg/core/series.py": "from pkg.core.generic import NDFrame\n\nclass Series(NDFrame):\n"
    "    def fillna(self, value=None, method=None):\n        return super().fillna(value, method)\n",
}
FILLNA_NEW = {
    **FRAMES,
    "pkg/core/generic.py": "class NDFrame:\n    def fillna(self, value=None):\n        pass\n",
    "pkg/core/series.py": "from pkg.core.generic import NDFrame\n\nclass Series(NDFrame):\n"
    "    def fillna(self, value=None):\n        return super().fillna(value)\n",
}


def test_a_parameter_change_to_an_inherited_method_carries_the_subclass_paths(tmp_path):
    """Code calls ``df.fillna(method=...)`` on a DataFrame, which inherits ``fillna`` from
    NDFrame without overriding it: the change to ``NDFrame.fillna`` is reported under the
    DataFrame's paths too (``import_paths``, which the scan matches the code against; ``also``
    and ``occurrences`` show them). Series overrides it, so its change is its own."""
    changes = diff(tmp_path, FILLNA_OLD, FILLNA_NEW)
    assert found(changes) == {
        (PARAM_REMOVED, "pkg.core.generic.NDFrame.fillna", "method"),
        (PARAM_REMOVED, "pkg.core.series.Series.fillna", "method"),
    }
    fillna = next(c for c in changes if c.owner == "NDFrame")
    assert fillna.import_paths == [
        "pkg.DataFrame.fillna",
        "pkg.core.frame.DataFrame.fillna",
        "pkg.core.generic.NDFrame.fillna",
    ]
    assert fillna.also == ["pkg.DataFrame.fillna", "pkg.core.frame.DataFrame.fillna"]
    assert fillna.occurrences == 2  # NDFrame, and the one subclass that inherits it
    series = next(c for c in changes if c.owner == "Series")
    assert series.import_paths == ["pkg.Series.fillna", "pkg.core.series.Series.fillna"]
    assert series.occurrences == 1
    # NDFrame itself is undocumented and not exported: the public DataFrame makes it public.
    assert fillna.tier == TIER_PUBLIC


def test_a_subclass_that_gets_the_method_from_another_base_is_not_a_path(tmp_path):
    old = {
        **FILLNA_OLD,
        "pkg/core/series.py": "from pkg.core.generic import NDFrame\n\nclass Mixin:\n"
        "    def fillna(self, value=None):\n        pass\n\n"
        "class Series(Mixin, NDFrame):\n    pass\n",
    }
    new = {**FILLNA_NEW, "pkg/core/series.py": old["pkg/core/series.py"]}
    (fillna,) = diff(tmp_path, old, new)
    assert "pkg.Series.fillna" not in (fillna.import_paths or [])  # Series.fillna is Mixin's
    assert fillna.occurrences == 2


def test_a_parameter_change_to_a_private_bases_method_is_reported_on_the_public_subclass(
    tmp_path,
):
    """A method a private class defines and a public one inherits: a change to it was never
    reported (the owner's path is private), so ``df.fillna(method=...)`` on the public class
    was never a use. It is reported under the shortest public inheritor, as a removal folded
    over the subclasses is, with the inheritor as its owner."""
    base = "class _Base:\n    def fillna(self, value=None, method=None):\n        pass\n"
    frames = {
        "pkg/__init__.py": "from pkg.frame import DataFrame\n__all__ = ['DataFrame']\n",
        "pkg/frame.py": "from pkg._base import _Base\n\nclass DataFrame(_Base):\n    pass\n",
    }
    old = {**frames, "pkg/_base.py": base}
    new = {**frames, "pkg/_base.py": base.replace(", method=None", "")}
    (fillna,) = diff(tmp_path, old, new)
    assert (fillna.kind, fillna.path, fillna.parameter, fillna.owner) == (
        PARAM_REMOVED,
        "pkg.DataFrame.fillna",
        "method",
        "DataFrame",
    )
    assert fillna.import_paths == ["pkg.DataFrame.fillna", "pkg.frame.DataFrame.fillna"]
    assert fillna.tier == TIER_PUBLIC
    assert fillna.describe(versioned=False) == (
        "`pkg.DataFrame.fillna(method=...)`: parameter `method` was removed"
    )
    # No public class inherits it: nothing to report.
    old["pkg/frame.py"] = new["pkg/frame.py"] = "class DataFrame:\n    pass\n"
    assert diff(tmp_path / "none", old, new) == []


# ------------------------------------------ attributes of a base outside the package (item 7)
def test_attributes_the_new_class_sets_where_griffe_does_not_look_are_not_removed(tmp_path):
    """fastapi 0.116.1 -> 0.143.0: ``APIRoute.__init__`` no longer assigns ``path``,
    ``endpoint`` and ``methods`` itself; ``_populate_api_route_state(cast(_APIRouteLike,
    self), path, ...)``, a function of its module, sets them on the route, and
    ``APIWebSocketRoute.__init__`` sets ``dependant`` in a tuple target. griffe lists neither
    as an attribute, and the four were reported as removed. ``secure_cloned_response_field``,
    which 0.143.0 sets nowhere and hands to no base, is removed, as is the method
    ``secure``."""
    old = {
        "pkg/__init__.py": "",
        "pkg/routing.py": "from starlette import routing\n\nclass APIRoute(routing.Route):\n"
        "    def __init__(self, path, endpoint, secure=True):\n        self.path = path\n"
        "        self.endpoint = endpoint\n        self.methods = ['GET']\n"
        "        self.secure_cloned_response_field = secure\n"
        "        self.dependant = build(path)\n"
        "    def secure(self):\n        pass\n\ndef build(path):\n    return path\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/routing.py": "from typing import cast\nfrom starlette import routing\n\n"
        "class APIRoute(routing.Route):\n    def __init__(self, path, endpoint, secure=True):\n"
        "        _populate(cast(routing.Route, self), path, endpoint=endpoint)\n"
        "        self.dependant, _ = build(path), None\n\n"
        "def _populate(route, path, *, endpoint):\n    route.path = path\n"
        "    route.endpoint = endpoint\n    route.methods = ['GET']\n\n"
        "def build(path):\n    return path\n",
    }
    assert unread(tmp_path, old, new) == (
        {
            (REMOVED, "pkg.routing.APIRoute.secure", None),
            (REMOVED, "pkg.routing.APIRoute.secure_cloned_response_field", None),
        },
        [],
    )


def test_an_attribute_the_constructor_hands_to_a_base_outside_the_package_is_unknown(tmp_path):
    """fastapi 0.143.0's ``Param(pydantic.fields.FieldInfo)`` no longer assigns
    ``self.deprecated``: it puts ``kwargs["deprecated"]`` and calls ``super().__init__(
    **use_kwargs)``, so pydantic's ``FieldInfo``, which since-cutoff did not read, may set it.
    The attribute is unknown, not removed, and the scan says the class's attributes were not
    compared. So is one the constructor passes by name, one its own ``**kwargs`` passes on,
    and one of a class that lost its constructor (the base's runs)."""

    def param(body: str) -> dict[str, str]:
        return {
            "pkg/__init__.py": "",
            "pkg/params.py": "from pydantic.fields import FieldInfo\n\nclass Param(FieldInfo):\n"
            + body,
        }

    old = param(
        "    def __init__(self, default, deprecated=None, **extra):\n"
        "        self.deprecated = deprecated\n        super().__init__(default=default, **extra)\n"
    )
    entry = f"pkg.params.Param{UNREAD_BASE}pydantic.fields.FieldInfo"
    new = param(
        "    def __init__(self, default, deprecated=None, **extra):\n"
        "        kwargs = dict(default=default, **extra)\n        kwargs['deprecated'] = deprecated\n"
        "        use_kwargs = {k: v for k, v in kwargs.items() if v is not None}\n"
        "        super().__init__(**use_kwargs)\n"
    )
    assert unread(tmp_path, old, new) == (set(), [entry])
    assert unread_text("fastapi", "0.143.0", "0.116.1", [entry]) == (
        "fastapi 0.143.0: pkg.params.Param inherits from pydantic.fields.FieldInfo, which "
        "since-cutoff did not read; the attributes it sets are not compared"
    )
    by_name = param(
        "    def __init__(self, default, deprecated=None, **extra):\n"
        "        super().__init__(default, deprecated=deprecated, **extra)\n"
    )
    assert unread(tmp_path / "name", old, by_name) == (set(), [entry])
    passed_on = param(
        "    def __init__(self, default, **kwargs):\n        super().__init__(default, **kwargs)\n"
    )
    assert unread(tmp_path / "kwargs", old, passed_on) == (set(), [entry])
    assert unread(tmp_path / "none", old, param("    pass\n")) == (set(), [entry])


def test_a_field_or_attribute_no_base_outside_the_package_is_given_is_removed(tmp_path):
    """A pydantic model's field (``usage: int``, a name of the class body), a constant of a
    class deriving from another package's, and an attribute the old constructor set that the
    new one neither sets nor hands to the base (fastapi 0.143.0's
    ``APIRoute.secure_cloned_response_field``) are removed, as for any class: the base cannot
    set what it was never given."""
    old = {
        "pkg/__init__.py": "import httpx\nimport pydantic\n\nclass Resp(pydantic.BaseModel):\n"
        "    id: str\n    usage: int\n\nclass Client(httpx.Client):\n    DEFAULT_TIMEOUT = 10\n\n"
        "    def __init__(self, base_url, secure=True):\n        self.secure = secure\n"
        "        super().__init__(base_url=base_url)\n"
    }
    new = {
        "pkg/__init__.py": "import httpx\nimport pydantic\n\nclass Resp(pydantic.BaseModel):\n"
        "    id: str\n\nclass Client(httpx.Client):\n"
        "    def __init__(self, base_url, secure=True):\n"
        "        super().__init__(base_url=base_url)\n"
    }
    assert unread(tmp_path, old, new) == (
        {
            (REMOVED, "pkg.Resp.usage", None),
            (REMOVED, "pkg.Client.DEFAULT_TIMEOUT", None),
            (REMOVED, "pkg.Client.secure", None),
        },
        [],
    )


@pytest.mark.parametrize("module", ["typing", "typing_extensions"])
def test_a_key_dropped_from_a_typed_dict_is_removed(tmp_path, module):
    """anthropic 0.60 -> 1.8: ``MessageCreateParamsBase(TypedDict)`` lost ``top_k``. typing's
    constructs are bases outside the package that set no attributes: the key is removed, and
    the class is not an unread entry."""
    old = {
        "pkg/__init__.py": f"from {module} import TypedDict\n\n"
        "class Params(TypedDict, total=False):\n    model: str\n    top_k: int\n"
    }
    new = {
        "pkg/__init__.py": f"from {module} import TypedDict\n\n"
        "class Params(TypedDict, total=False):\n    model: str\n"
    }
    assert unread(tmp_path, old, new) == ({(REMOVED, "pkg.Params.top_k", None)}, [])


def test_a_removed_attribute_of_a_class_with_an_in_package_base_is_still_reported(tmp_path):
    base = "class Base:\n    def __init__(self):\n        self.x = 1\n\n"
    old = {
        "pkg/__init__.py": base + "class Route(Base):\n    def __init__(self):\n"
        "        super().__init__()\n        self.path = '/'\n"
    }
    new = {
        "pkg/__init__.py": base + "class Route(Base):\n    def __init__(self):\n"
        "        super().__init__()\n"
    }
    assert unread(tmp_path, old, new) == ({(REMOVED, "pkg.Route.path", None)}, [])


def test_the_unread_warning_names_compiled_modules_and_unread_bases():
    entries = [
        "pkg._core",
        f"pkg.routing.APIRoute{UNREAD_BASE}starlette.routing.Route",
        f"pkg.routing.APIWebSocketRoute{UNREAD_BASE}starlette.routing.WebSocketRoute",
        f"pkg.params.Body{UNREAD_BASE}pydantic.fields.FieldInfo",
        f"pkg.params.Param{UNREAD_BASE}pydantic.fields.FieldInfo",
    ]
    assert unread_text("pkg", "2.0", "1.0", entries) == (
        "pkg 2.0: pkg._core is a compiled module without a .py source or a .pyi stub, unlike "
        "in 1.0; since-cutoff does not run code, so changes to it and to the names taken from "
        "it are not reported. pkg.routing.APIRoute inherits from starlette.routing.Route, and "
        "pkg.routing.APIWebSocketRoute inherits from starlette.routing.WebSocketRoute, and "
        "pkg.params.Body and pkg.params.Param inherit from pydantic.fields.FieldInfo, which "
        "since-cutoff did not read; the attributes they set are not compared"
    )
    # The compiled-module warning reads as before (test_engine.py asserts it in a scan).
    assert unread_text("pkg", "2.0", "1.0", ["pkg.a", "pkg.b"]) == (
        "pkg 2.0: pkg.a, pkg.b are compiled modules without a .py source or a .pyi stub, "
        "unlike in 1.0; since-cutoff does not run code, so changes to them and to the names "
        "taken from them are not reported"
    )


# ---------------------------------------------------------- public and internal (item 12)
def tiers(changes: list[APIChange]) -> dict[str, str | None]:
    return {f"{c.path}({c.parameter})" if c.parameter else c.path: c.tier for c in changes}


def test_the_tier_says_whether_the_changed_api_is_public(tmp_path):
    """fastapi 0.116.1 -> 0.143.0's "35 breaking changes" were mostly internal helpers
    (``dependencies.utils.get_flat_dependant``), uvicorn's ``config.LOOP_SETUPS`` and
    ``loops.*``. An API is public when the package's top level exports it, a module names it
    in ``__all__``, or it is documented; a member takes its class's tier."""
    old = {
        "pkg/__init__.py": "from pkg.core import Client\n__all__ = ['Client']\n",
        "pkg/core.py": "__all__ = ['Client', 'Listed']\n\nclass Client:\n    def gone(self):\n"
        "        pass\n\nclass Listed:\n    pass\n",
        "pkg/utils.py": 'def documented():\n    """Does a thing."""\n\ndef helper():\n'
        "    pass\n\nLOOP_SETUPS = {}\n\nclass Quiet:\n    def gone(self):\n        pass\n",
        "pkg/loops/__init__.py": "",
        "pkg/loops/asyncio.py": "def asyncio_setup():\n    pass\n",
    }
    new = {
        "pkg/__init__.py": "from pkg.core import Client\n__all__ = ['Client']\n",
        "pkg/core.py": "__all__ = ['Client']\n\nclass Client:\n    pass\n",
        "pkg/utils.py": "class Quiet:\n    pass\n",
        "pkg/loops/__init__.py": "",
    }
    assert tiers(diff(tmp_path, old, new)) == {
        "pkg.core.Client.gone": TIER_PUBLIC,  # its class is exported at the top level
        "pkg.core.Listed": TIER_PUBLIC,  # in its module's __all__ (the old version's)
        "pkg.utils.documented": TIER_PUBLIC,
        "pkg.utils.helper": TIER_INTERNAL,
        "pkg.utils.LOOP_SETUPS": TIER_INTERNAL,
        "pkg.utils.Quiet.gone": TIER_INTERNAL,
        "pkg.loops.asyncio": TIER_INTERNAL,  # a module below the top level, undocumented
    }
    # A top-level module is public, documented or not; so is a top-level import name.
    old = {"pkg/__init__.py": "", "pkg/loops.py": "def setup():\n    pass\n"}
    assert tiers(diff(tmp_path / "module", old, {"pkg/__init__.py": ""})) == {
        "pkg.loops": TIER_PUBLIC
    }
    assert tiers(diff(tmp_path / "top", {"fastmod.py": "X = 1\n"}, {}, names=("fastmod",))) == {
        "fastmod": TIER_PUBLIC
    }


def test_a_member_inherited_by_a_public_class_is_public(tmp_path):
    """A removal folded over the classes that inherited it (one change) is public when one of
    them is: fastapi's ``BaseModelWithConfig.Config``, removed under 34 paths, is internal
    because none of the 34 models is exported or documented."""
    old = {
        "pkg/__init__.py": "from pkg.models import Contact\n__all__ = ['Contact']\n",
        "pkg/models.py": "class Base:\n    class Config:\n        extra = 'allow'\n\n"
        "class Contact(Base):\n    pass\n\nclass Tag(Base):\n    pass\n",
    }
    new = {
        "pkg/__init__.py": old["pkg/__init__.py"],
        "pkg/models.py": "class Base:\n    pass\n\nclass Contact(Base):\n    pass\n\n"
        "class Tag(Base):\n    pass\n",
    }
    (config,) = diff(tmp_path, old, new)
    assert (config.path, config.occurrences, config.tier) == (
        "pkg.models.Base.Config",
        3,
        TIER_PUBLIC,
    )
    old["pkg/__init__.py"] = new["pkg/__init__.py"] = ""
    (config,) = diff(tmp_path / "internal", old, new)
    assert config.tier == TIER_INTERNAL
    # Twins merged into one change: public when either is (``Client`` is documented,
    # ``AsyncClient`` is not).
    old = {
        "pkg/__init__.py": "",
        "pkg/clients.py": 'class Client:\n    """The client."""\n\n'
        "    def send(self, x, temperature=1.0):\n        pass\n\n"
        "class AsyncClient:\n    async def send(self, x, temperature=1.0):\n        pass\n",
    }
    new = {
        **old,
        "pkg/clients.py": 'class Client:\n    """The client."""\n\n    def send(self, x):\n'
        "        pass\n\nclass AsyncClient:\n    async def send(self, x):\n        pass\n",
    }
    (send,) = diff(tmp_path / "twins", old, new)
    assert (send.path, send.occurrences, send.tier) == ("pkg.clients.Client.send", 2, TIER_PUBLIC)
    new["pkg/clients.py"] = old["pkg/clients.py"].replace(
        "async def send(self, x, temperature=1.0)", "async def send(self, x)"
    )
    (send,) = diff(tmp_path / "async", old, new)
    assert (send.path, send.tier) == ("pkg.clients.AsyncClient.send", TIER_INTERNAL)


def test_a_name_re_exported_as_itself_or_copied_to_the_top_by_a_star_import_is_public(tmp_path):
    """anthropic 0.60.0 -> 1.8.0: ``anthropic/types/__init__.py`` re-exports
    ``CompletionCreateParams as CompletionCreateParams`` (no ``__all__``, no docstring), and
    ``anthropic/__init__.py``, which has an ``__all__``, does ``from .lib.bedrock import *``,
    copying ``AnthropicBedrock`` (``as AnthropicBedrock`` in ``lib/bedrock/__init__.py``): the
    client ``from anthropic import AnthropicBedrock`` gives. Both counted as internal. A plain
    import in a package ``__init__`` is a path to the object, not a re-export on purpose."""
    old = {
        "pkg/__init__.py": "from .lib.bedrock import *\n__all__ = ['Client']\n\n"
        "class Client:\n    pass\n",
        "pkg/lib/__init__.py": "",
        "pkg/lib/bedrock/__init__.py": "from ._client import Bedrock as Bedrock\n",
        "pkg/lib/bedrock/_client.py": "class Bedrock:\n    def completions(self):\n        pass\n",
        "pkg/types/__init__.py": "from .message import Message as Message\n"
        "from .other import Other\n",
        "pkg/types/message.py": "class Message:\n    usage: int\n",
        "pkg/types/other.py": "class Other:\n    usage: int\n",
    }
    new = {
        **old,
        "pkg/lib/bedrock/_client.py": "class Bedrock:\n    pass\n",
        "pkg/types/message.py": "class Message:\n    pass\n",
        "pkg/types/other.py": "class Other:\n    pass\n",
    }
    assert tiers(diff(tmp_path, old, new)) == {
        "pkg.lib.bedrock.Bedrock.completions": TIER_PUBLIC,
        "pkg.types.message.Message.usage": TIER_PUBLIC,
        "pkg.types.other.Other.usage": TIER_INTERNAL,
    }


def test_a_value_made_of_a_documented_function_is_public(tmp_path):
    """langchain-core 0.3.72 -> 1.6.5: ``convert_pydantic_to_openai_function = deprecated(
    "0.1.16", ...)(_convert_pydantic_to_openai_function)`` has no docstring of its own; the
    function it is made of has one (and langchain's API reference lists it). It counted as
    internal. A value made of an undocumented function stays internal."""
    old = {
        "pkg/__init__.py": "",
        "pkg/utils.py": "from pkg._deprecation import deprecated\n\n"
        'def _convert(fn):\n    """Convert a function."""\n\n'
        "convert = deprecated('0.1.16', removal='1.0')(_convert)\n\n"
        "def _plain(fn):\n    pass\n\nplain = deprecated('0.1.16')(_plain)\n",
        "pkg/_deprecation.py": "def deprecated(since, removal=None):\n"
        "    def wrap(fn):\n        return fn\n    return wrap\n",
    }
    new = {**old, "pkg/utils.py": ""}
    assert tiers(diff(tmp_path, old, new)) == {
        "pkg.utils.convert": TIER_PUBLIC,
        "pkg.utils.plain": TIER_INTERNAL,
    }
