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
    APIChange,
    diff_sources,
    diff_sources_with_unread,
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
    for kept in (f"from {module} import gone, speedy\n", f"from {module} import *\n"):
        new = {"pkg/__init__.py": kept}
        assert unread(tmp_path / kept[-9:-1], old, new, compiled=[module]) == (set(), [module])


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
