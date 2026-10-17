"""Regression tests for false or unhelpful breaking changes found on real projects.

Each fixture is a small reduction of a real release pair (named in the test): what the static
diff reported before, and what it must report now.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from since_cutoff.apidiff import (
    DEPRECATED,
    HINT_WARNING,
    KIND_CHANGED,
    MOVED,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    TIER_INTERNAL,
    TIER_PUBLIC,
    UNREAD_BASE,
    WARNS_ACCESS,
    WARNS_ALWAYS,
    WARNS_CONDITION,
    WARNS_DEFAULT,
    WARNS_PARAMETER,
    APIChange,
    diff_sources,
    diff_sources_with_unread,
    unread_text,
)
from since_cutoff.selection import OLD_FORM, USES_API, collapse, form
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
    # It warns when the name is read, as the deprecations read from warnings.warn say (#113).
    assert change.warning == {
        "when": WARNS_ACCESS,
        "category": "DeprecationWarning",
        "at": "pkg/__init__.py:7",
        "access": "read",
    }


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


def test_a_keyword_of_another_call_puts_nothing_in_the_dict_the_base_is_given(tmp_path):
    """The constructor gives the base ``super().__init__(**options)``: the keys of ``options``
    are handed on, and a keyword of another call in the constructor (``_warn(secure=secure)``)
    is not one. The attribute it no longer sets is removed; it was unknown."""
    old = {
        "pkg/__init__.py": "import httpx\n\nclass Client(httpx.Client):\n"
        "    def __init__(self, base_url, secure=True):\n        self.secure = secure\n"
        "        super().__init__(base_url=base_url)\n"
    }
    new = {
        "pkg/__init__.py": "import httpx\n\ndef _warn(**flags):\n    return flags\n\n"
        "class Client(httpx.Client):\n    def __init__(self, base_url, secure=True):\n"
        "        options = {'base_url': base_url}\n        _warn(secure=secure)\n"
        "        super().__init__(**options)\n"
    }
    assert unread(tmp_path, old, new) == ({(REMOVED, "pkg.Client.secure", None)}, [])
    # A key of the dict the base is given is handed on, as before.
    handed = new["pkg/__init__.py"].replace(
        "{'base_url': base_url}", "dict(base_url=base_url, secure=secure)"
    )
    entry = f"pkg.Client{UNREAD_BASE}httpx.Client"
    assert unread(tmp_path / "dict", old, {"pkg/__init__.py": handed}) == (set(), [entry])


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


# ------------------------------------------ deprecations announced by warnings.warn (#53)
def warned(changes: list[APIChange]) -> dict[tuple[str, str | None], dict]:
    """The deprecations read from a ``warnings.warn``, by path and parameter: when they warn,
    with their category, and their text."""
    return {
        (c.path, c.parameter): {**(c.warning or {}), "text": c.deprecation}
        for c in changes
        if c.kind == DEPRECATED and c.deprecated_by == HINT_WARNING
    }


def test_a_warning_at_function_entry_deprecates_the_function(tmp_path):
    """issue #53: ``warnings.warn("f is deprecated, use g", DeprecationWarning)`` at the top of
    the pinned release's ``f`` gave an empty diff."""
    old = {"pkg/__init__.py": "def f(x):\n    return x\n\ndef g(x):\n    return x\n"}
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        'def f(x):\n    """Old."""\n'
        '    warnings.warn("f is deprecated, use `g` instead", DeprecationWarning, stacklevel=2)\n'
        "    return g(x)\n\n"
        "def g(x):\n    return x\n"
    }
    changes = diff(tmp_path, old, new)
    assert warned(changes) == {
        ("pkg.f", None): {
            "when": WARNS_ALWAYS,
            "category": "DeprecationWarning",
            "at": "pkg/__init__.py:5",
            "text": "f is deprecated, use `g` instead",
        }
    }
    [f] = changes
    assert f.deprecation_file == "pkg/__init__.py"
    assert f.library_names == ["pkg.g"]  # the stated-replacement logic, as for @deprecated
    assert f.describe(versioned=False) == (
        "`pkg.f` is deprecated, with a DeprecationWarning at run time: f is deprecated, use `g` "
        "instead"
    )


def test_a_warning_under_a_sentinel_guard_deprecates_the_parameter(tmp_path):
    """pandas 3.0.6 ``DataFrame.set_index``: ``if verify_integrity is not lib.no_default:
    warnings.warn(..., Pandas4Warning, stacklevel=find_stack_level())``; the release at the
    cutoff took the parameter without a warning. The ``else`` of ``is <default>`` and
    ``is not None`` for a None default are the same shape; a warning when a parameter is
    passed that the old release did not take is not reported."""
    lib = "no_default = object()\n"
    old = {
        "pkg/__init__.py": "from pkg.frame import Frame\n",
        "pkg/_lib.py": lib,
        "pkg/frame.py": "from pkg import _lib as lib\n\nclass Frame:\n"
        "    def set_index(self, keys, verify_integrity=lib.no_default, axis=None, how=None):\n"
        "        return keys\n",
    }
    new = {
        **old,
        "pkg/_exceptions.py": "def find_stack_level():\n    return 2\n",
        "pkg/frame.py": "import warnings\n\nfrom pkg import _lib as lib\n"
        "from pkg._exceptions import find_stack_level\n\nclass Frame:\n"
        "    def set_index(\n        self, keys, verify_integrity=lib.no_default, axis=None,\n"
        "        how=None, later=None,\n    ):\n"
        "        if verify_integrity is not lib.no_default:\n"
        "            warnings.warn(\n"
        "                \"The 'verify_integrity' keyword in Frame.set_index is deprecated.\",\n"
        "                FutureWarning,\n                stacklevel=find_stack_level(),\n"
        "            )\n"
        "        if axis is lib.no_default:\n            pass\n        else:\n"
        "            pass\n"
        "        if axis is not None:\n"
        '            warnings.warn("axis= is deprecated", FutureWarning)\n'
        "        if how is None:\n            how = 'left'\n        else:\n"
        '            warnings.warn("how= is deprecated", FutureWarning)\n'
        "        if later is not None:\n"
        '            warnings.warn("later= is deprecated", FutureWarning)\n'
        "        return keys\n",
    }
    found = warned(diff(tmp_path, old, new))
    assert set(found) == {
        ("pkg.frame.Frame.set_index", "verify_integrity"),
        ("pkg.frame.Frame.set_index", "axis"),
        ("pkg.frame.Frame.set_index", "how"),
    }
    assert found["pkg.frame.Frame.set_index", "verify_integrity"] == {
        "when": WARNS_PARAMETER,
        "category": "FutureWarning",
        "at": "pkg/frame.py:12",
        "text": "The 'verify_integrity' keyword in Frame.set_index is deprecated.",
    }


def test_a_warning_when_a_keyword_is_in_kwargs_deprecates_that_keyword(tmp_path):
    """``if "limit" in kwargs:`` (and ``kwargs.get("limit") is not None``) warn when the
    caller passes ``limit=``; the release at the cutoff took it through its ``**kwargs``."""
    old = {"pkg/__init__.py": "def fill(value, **kwargs):\n    return value\n"}
    new = {
        "pkg/__init__.py": "import warnings\n\ndef fill(value, **kwargs):\n"
        '    if "limit" in kwargs:\n'
        '        warnings.warn("limit is deprecated", PendingDeprecationWarning)\n'
        '    if kwargs.get("axis") is not None:\n'
        '        warnings.warn("axis is deprecated", FutureWarning)\n'
        "    return value\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v["category"]) for k, v in found.items()} == {
        ("pkg.fill", "limit"): (WARNS_PARAMETER, "PendingDeprecationWarning"),
        ("pkg.fill", "axis"): (WARNS_PARAMETER, "FutureWarning"),
    }


def test_a_helper_that_warns_for_its_callers_counts_as_their_warning(tmp_path):
    """A module-local ``_deprecate(msg)``, a helper the module imports from the package
    (``warn_deprecated(name, alternative)`` with its own message), and pandas 3's method
    ``self._check_copy_deprecation(copy)``, which warns while ``copy`` is not the sentinel: the
    caller's ``copy`` is deprecated. A helper whose name says neither "deprecat" nor "warn"
    is not followed, and neither is a helper that warns only on some paths."""
    lib = "no_default = object()\n"
    old = {
        "pkg/__init__.py": "from pkg.api import *\n",
        "pkg/_lib.py": lib,
        "pkg/api.py": "from pkg import _lib as lib\n\n__all__ = ['a', 'b', 'c', 'd', 'Frame']\n\n"
        "def a(x):\n    return x\n\ndef b(x):\n    return x\n\n"
        "def c(x):\n    return x\n\ndef d(x, flag=False):\n    return x\n\n"
        "class Frame:\n    def astype(self, dtype, copy=lib.no_default):\n        return dtype\n"
        "    def other(self, copy=None):\n        return copy\n",
    }
    new = {
        **old,
        "pkg/_compat.py": "import warnings\n\n"
        "def warn_deprecated(name, alternative=None):\n"
        '    warnings.warn(f"{name} is deprecated", category=FutureWarning, stacklevel=3)\n',
        "pkg/api.py": "import warnings\n\nfrom pkg import _lib as lib\n"
        "from pkg._compat import warn_deprecated\n\n"
        "__all__ = ['a', 'b', 'c', 'd', 'Frame']\n\n"
        "def _deprecate(msg, category=DeprecationWarning):\n"
        "    warnings.warn(msg, category, stacklevel=3)\n\n"
        "def _notify(msg):\n    warnings.warn(msg, DeprecationWarning)\n\n"
        "def _maybe_warn(msg, flag):\n"
        "    if flag:\n        warnings.warn(msg, DeprecationWarning)\n\n"
        'def a(x):\n    _deprecate("a is deprecated; use `b` instead")\n    return x\n\n'
        'def b(x):\n    warn_deprecated("b", "c")\n    return x\n\n'
        'def c(x):\n    _notify("c is deprecated")\n    return x\n\n'
        'def d(x, flag=False):\n    _maybe_warn("d is deprecated", flag)\n    return x\n\n'
        "class Frame:\n"
        "    @staticmethod\n    def _check_copy_deprecation(copy):\n"
        "        if copy is not lib.no_default:\n"
        '            warnings.warn("The copy keyword is deprecated.", FutureWarning)\n\n'
        "    def astype(self, dtype, copy=lib.no_default):\n"
        "        self._check_copy_deprecation(copy)\n        return dtype\n\n"
        "    def other(self, copy=None):\n"
        "        self._check_copy_deprecation(copy)\n        return copy\n",
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v["category"], v["text"]) for k, v in found.items()} == {
        ("pkg.api.a", None): (
            WARNS_ALWAYS,
            "DeprecationWarning",
            "a is deprecated; use `b` instead",
        ),
        # f"{name} is deprecated" with what the call gives the helper's name (#113)
        ("pkg.api.b", None): (WARNS_ALWAYS, "FutureWarning", "b is deprecated"),
        ("pkg.api.Frame.astype", "copy"): (
            WARNS_PARAMETER,
            "FutureWarning",
            "The copy keyword is deprecated.",
        ),
    }  # other() hands the helper a parameter whose default is not its sentinel


def test_the_message_is_read_from_an_f_string_or_a_constant(tmp_path):
    """An f-string's literal parts (each value as ``{...}``), a name assigned the message
    earlier in the function, a constant of the module, and one imported from elsewhere in the
    package."""
    old = {
        "pkg/__init__.py": "from pkg.api import f, g, h, k\n",
        "pkg/api.py": "def f(x):\n    return x\n\ndef g(x):\n    return x\n\n"
        "def h(x):\n    return x\n\ndef k(x):\n    return x\n",
    }
    new = {
        **old,
        "pkg/_messages.py": 'K_MESSAGE = "k is deprecated; use `f` instead"\n',
        "pkg/api.py": "import warnings\nfrom warnings import warn\n\n"
        "from pkg._messages import K_MESSAGE\n\n"
        '_H_MESSAGE = (\n    "h is deprecated "\n    "and will be removed."\n)\n\n'
        "def f(x):\n"
        '    warn(f"Passing {type(x).__name__} to f is deprecated", FutureWarning)\n'
        "    return x\n\n"
        "def g(x):\n"
        '    msg = "g is deprecated"\n'
        '    msg2 = "g: " + msg\n'
        "    warnings.warn(msg, DeprecationWarning)\n    return x\n\n"
        "def h(x):\n    warnings.warn(_H_MESSAGE, DeprecationWarning)\n    return x\n\n"
        "def k(x):\n    warnings.warn(K_MESSAGE, DeprecationWarning)\n    return x\n",
    }
    found = warned(diff(tmp_path, old, new))
    assert {k[0]: v["text"] for k, v in found.items()} == {
        "pkg.api.f": "Passing {...} to f is deprecated",
        "pkg.api.g": "g is deprecated",
        "pkg.api.h": "h is deprecated and will be removed.",
        "pkg.api.k": "k is deprecated; use `f` instead",
    }


def test_a_warning_that_is_not_a_deprecation_does_not_count(tmp_path):
    """A UserWarning, a ``warnings.warn`` without a category (a UserWarning), a RuntimeWarning
    and a warning class of the package that derives from UserWarning are not deprecations;
    nor is a call of another ``warn`` (a logger's)."""
    old = {"pkg/__init__.py": "def f(x):\n    return x\n"}
    new = {
        "pkg/__init__.py": "import logging\nimport warnings\n\n"
        "log = logging.getLogger(__name__)\n\n"
        "class PkgWarning(UserWarning):\n    pass\n\n"
        "def f(x):\n"
        '    warnings.warn("f returns a copy", UserWarning)\n'
        '    warnings.warn("f is slow")\n'
        '    warnings.warn("overflow", RuntimeWarning)\n'
        '    warnings.warn("f is deprecated", PkgWarning)\n'
        '    log.warn("f is deprecated")\n'
        "    return x\n"
    }
    assert diff(tmp_path, old, new) == []


def test_a_warning_the_release_at_the_cutoff_issued_already_does_not_count(tmp_path):
    """The same warning in both releases (its version number changed), a parameter whose
    warning the old release issued already, and a function the old release warned of at every
    call are not new; nor is a function new in the pinned release."""
    old = {
        "pkg/__init__.py": "import warnings\n\n"
        "def f(x):\n"
        '    warnings.warn("f is deprecated and will be removed in 3.0", FutureWarning)\n'
        "    return x\n\n"
        "def g(x, y=None):\n    if y is not None:\n"
        '        warnings.warn("y is deprecated", FutureWarning)\n    return x\n\n'
        "def h(x, y=None):\n"
        '    warnings.warn("h is deprecated", FutureWarning)\n    return x\n'
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def f(x):\n"
        '    warnings.warn("f is deprecated and will be removed in 4.0", FutureWarning)\n'
        "    return x\n\n"
        "def g(x, y=None):\n    if y is not None:\n"
        '        warnings.warn("y= is deprecated: use x", FutureWarning)\n    return x\n\n'
        "def h(x, y=None):\n"
        '    warnings.warn("h is deprecated", FutureWarning)\n'
        "    if y is not None:\n"
        '        warnings.warn("y is deprecated", FutureWarning)\n    return x\n\n'
        "def brand_new(x):\n"
        '    warnings.warn("brand_new is deprecated", FutureWarning)\n    return x\n'
    }
    assert diff(tmp_path, old, new) == []


def test_a_package_subclass_of_a_deprecation_warning_counts(tmp_path):
    """pandas 3: ``class Pandas4Warning(PandasDeprecationWarning)`` and
    ``PandasDeprecationWarning(PandasChangeWarning, DeprecationWarning)``, imported from
    ``pkg.errors``; also through a module alias (``errors.Pandas4Warning``) and a name bound
    to a built-in category."""
    old = {"pkg/__init__.py": "from pkg.api import f, g, h\n"} | {
        "pkg/api.py": "def f(x):\n    return x\n\ndef g(x):\n    return x\n\n"
        "def h(x):\n    return x\n"
    }
    new = {
        "pkg/__init__.py": old["pkg/__init__.py"],
        "pkg/errors.py": "class PkgChangeWarning(Warning):\n    pass\n\n"
        "class PkgDeprecationWarning(PkgChangeWarning, DeprecationWarning):\n    pass\n\n"
        "class Pkg4Warning(PkgDeprecationWarning):\n    pass\n\n"
        "_DEPRECATION = FutureWarning\n",
        "pkg/api.py": "import warnings\n\nfrom pkg import errors\n"
        "from pkg.errors import Pkg4Warning, _DEPRECATION\n\n"
        'def f(x):\n    warnings.warn("f is deprecated", Pkg4Warning)\n    return x\n\n'
        'def g(x):\n    warnings.warn("g is deprecated", errors.Pkg4Warning)\n    return x\n\n'
        'def h(x):\n    warnings.warn("h is deprecated", _DEPRECATION)\n    return x\n',
    }
    found = warned(diff(tmp_path, old, new))
    assert {k[0]: v["category"] for k, v in found.items()} == {
        "pkg.api.f": "Pkg4Warning",
        "pkg.api.g": "Pkg4Warning",
        "pkg.api.h": "FutureWarning",
    }


def test_a_warning_in_init_at_entry_deprecates_the_class(tmp_path):
    """``Client.__init__`` warning at every call deprecates ``Client``; under a guard on one
    of its parameters, that parameter of ``Client.__init__``."""
    old = {
        "pkg/__init__.py": "class Client:\n    def __init__(self, url):\n        self.url = url\n\n"
        "class Session:\n    def __init__(self, url, proxies=None):\n        self.url = url\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "class Client:\n    def __init__(self, url):\n"
        '        warnings.warn("Client is deprecated", DeprecationWarning)\n'
        "        self.url = url\n\n"
        "class Session:\n    def __init__(self, url, proxies=None):\n"
        "        if proxies is not None:\n"
        '            warnings.warn("proxies is deprecated", DeprecationWarning)\n'
        "        self.url = url\n"
    }
    changes = diff(tmp_path, old, new)
    assert set(warned(changes)) == {("pkg.Client", None), ("pkg.Session.__init__", "proxies")}
    client = next(c for c in changes if c.path == "pkg.Client")
    assert client.owner is None and client.new_signature == "class Client(self, url)"


def test_a_warning_under_another_condition_deprecates_the_function_under_it(tmp_path):
    """pandas 3's ``Index.join``: ``if not isinstance(other, Index): warnings.warn(...)``.
    The function is deprecated under that condition (``warning["condition"]``), and a call of
    it is "uses this API", not the old form. What follows an ``if <test>: return`` at the top
    of the body is under ``not <test>`` (#113); a warning after a return in a nested block
    and one in a loop are issued in some calls too (nothing to quote: the condition is None);
    an early ``raise`` does not make one conditional."""
    old = {
        "pkg/__init__.py": "def join(other):\n    return other\n\n"
        "def at_time(t):\n    return t\n\ndef each(xs):\n    return xs\n\n"
        "def checked(x):\n    return x\n\ndef late(t):\n    return t\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def join(other):\n    if not isinstance(other, list):\n"
        '        warnings.warn("Passing non-lists to join is deprecated", FutureWarning)\n'
        "    return other\n\n"
        "def at_time(t):\n    if t is None:\n        return t\n"
        '    warnings.warn("at_time is deprecated", FutureWarning)\n    return t\n\n'
        "def each(xs):\n    for x in xs:\n"
        '        warnings.warn("each is deprecated", FutureWarning)\n    return xs\n\n'
        "def checked(x):\n    if x is None:\n        raise ValueError(x)\n"
        '    warnings.warn("checked is deprecated", FutureWarning)\n    return x\n\n'
        "def late(t):\n    if t:\n        if t > 1:\n            return t\n"
        '    warnings.warn("late is deprecated", FutureWarning)\n    return t\n'
    }
    changes = diff(tmp_path, old, new)
    found = warned(changes)
    assert {k[0]: (v["when"], v.get("condition")) for k, v in found.items()} == {
        "pkg.join": (WARNS_CONDITION, "not isinstance(other, list)"),
        "pkg.at_time": (WARNS_CONDITION, "t is not None"),
        "pkg.each": (WARNS_CONDITION, None),
        "pkg.checked": (WARNS_ALWAYS, None),
        "pkg.late": (WARNS_CONDITION, None),
    }
    join = next(c for c in changes if c.path == "pkg.join")
    assert join.describe(versioned=False) == (
        "`pkg.join` is deprecated when `not isinstance(other, list)`, with a FutureWarning at "
        "run time: Passing non-lists to join is deprecated"
    )
    assert form(join, ("pkg.join",)) == USES_API
    checked = next(c for c in changes if c.path == "pkg.checked")
    assert form(checked, ("pkg.checked",)) == OLD_FORM


def test_a_function_without_a_body_or_a_source_is_read_without_error(tmp_path):
    """A stub (``.pyi``) has no body to warn in; a function that only re-raises reads as
    nothing."""
    old = {"pkg/__init__.pyi": "def f(x: int) -> int: ...\n"}
    new = {"pkg/__init__.pyi": "import warnings\n\ndef f(x: int) -> int: ...\n"}
    assert diff(tmp_path, old, new) == []


def test_pandas_deprecate_kwarg_with_the_category_first_names_the_parameter(tmp_path):
    """pandas 3's ``@deprecate_kwarg(Pandas4Warning, "method", new_arg_name=None)`` passes the
    category before the parameter's name: the parameter was read as the category (not a
    string) and the deprecation dropped (``reindex_like(method=...)``)."""
    old = {
        "pkg/__init__.py": "from pkg._decorators import deprecate_kwarg\n\n"
        "def reindex_like(other, method=None):\n    return other\n",
        "pkg/_decorators.py": "def deprecate_kwarg(*args, **kwargs):\n    return lambda f: f\n",
    }
    new = {
        **old,
        "pkg/__init__.py": "from pkg._decorators import deprecate_kwarg\n\n"
        '@deprecate_kwarg(FutureWarning, "method", new_arg_name=None)\n'
        "def reindex_like(other, method=None):\n    return other\n",
    }
    assert found(diff(tmp_path, old, new)) == {(DEPRECATED, "pkg.reindex_like", "method")}


# ---------------------------- deprecations announced by warnings.warn: review fixes (#113)
def test_a_helper_that_hands_its_message_to_a_wrapper_taking_the_category(tmp_path):
    """polars 1.44: ``issue_deprecation_warning(message, *, version="")`` calls
    ``issue_warning(message, DeprecationWarning)``, whose ``category`` has no default. Neither
    was a helper, so ``read_csv(rechunk=...)`` and 28 more were missed; and a helper warning
    through another kept the inner one's message, ``f"{message} (Deprecated in version
    {version})"``, not its caller's."""
    old = {
        "pkg/__init__.py": "",
        "pkg/io.py": "def read_csv(source, rechunk=None):\n    return source\n\n"
        "def scan_csv(source, retries=None):\n    return source\n",
    }
    new = {
        **old,
        "pkg/_warnings.py": "import warnings\n\n"
        "def issue_warning(message, category, **kwargs):\n"
        "    warnings.warn(message=message, category=category, stacklevel=2, **kwargs)\n",
        "pkg/_deprecation.py": "from pkg._warnings import issue_warning\n\n"
        'def issue_deprecation_warning(message, *, version=""):\n'
        "    if version:\n"
        '        message = f"{message.strip()}\\n(Deprecated in version {version})"\n'
        "    issue_warning(message, DeprecationWarning)\n",
        "pkg/io.py": "from pkg._deprecation import issue_deprecation_warning\n\n"
        "def read_csv(source, rechunk=None):\n    if rechunk is not None:\n"
        "        issue_deprecation_warning(\n"
        '            "`rechunk` parameter on read_csv() will be removed.", version="1.43.2"\n'
        "        )\n    return source\n\n"
        "def scan_csv(source, retries=None):\n    if retries is not None:\n"
        '        msg = "the `retries` parameter was deprecated in 1.37.1"\n'
        "        issue_deprecation_warning(msg)\n    return source\n",
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v["category"], v["text"]) for k, v in found.items()} == {
        ("pkg.io.read_csv", "rechunk"): (
            WARNS_PARAMETER,
            "DeprecationWarning",
            "`rechunk` parameter on read_csv() will be removed.",
        ),
        ("pkg.io.scan_csv", "retries"): (
            WARNS_PARAMETER,
            "DeprecationWarning",
            "the `retries` parameter was deprecated in 1.37.1",
        ),
    }


def test_a_name_a_function_imports_itself_is_read(tmp_path):
    """matplotlib 3.10's ``_api.warn_deprecated`` runs ``from . import warn_external`` in its
    body, and polars' ``Enum.union`` imports its helper in the method: neither name was
    resolved, so all of matplotlib 3.10's in-body deprecations were missed. ``from warnings
    import warn as w`` in a function is ``warnings.warn`` too."""
    old = {
        "pkg/__init__.py": "from pkg.api import Enum, f, g\n",
        "pkg/_api/__init__.py": "",
        "pkg/api.py": "def f(x):\n    return x\n\ndef g(x):\n    return x\n\n"
        "class Enum:\n    def union(self, other):\n        return other\n",
    }
    new = {
        **old,
        "pkg/_api/__init__.py": "import warnings\n\n"
        "def warn_external(message, category=None):\n"
        "    warnings.warn(message, category, stacklevel=3)\n",
        "pkg/_api/deprecation.py": "class PkgDeprecationWarning(DeprecationWarning):\n"
        '    pass\n\ndef warn_deprecated(since, message=""):\n'
        "    from . import warn_external\n"
        "    warn_external(message, category=PkgDeprecationWarning)\n",
        "pkg/api.py": "from pkg._api.deprecation import warn_deprecated\n\n"
        'def f(x):\n    warn_deprecated("3.10", message="f is deprecated")\n    return x\n\n'
        "def g(x):\n    from warnings import warn as w\n\n"
        '    w("g is deprecated", DeprecationWarning)\n    return x\n\n'
        "class Enum:\n    def union(self, other):\n"
        "        from pkg._api.deprecation import warn_deprecated as wd\n\n"
        '        wd("1.38", message="`Enum.union()` is deprecated")\n        return other\n',
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v["category"], v["text"]) for k, v in found.items()} == {
        ("pkg.api.f", None): (WARNS_ALWAYS, "PkgDeprecationWarning", "f is deprecated"),
        ("pkg.api.g", None): (WARNS_ALWAYS, "DeprecationWarning", "g is deprecated"),
        ("pkg.api.Enum.union", None): (
            WARNS_ALWAYS,
            "PkgDeprecationWarning",
            "`Enum.union()` is deprecated",
        ),
    }


def test_a_warning_the_release_at_the_cutoff_gave_in_another_category_is_not_new(tmp_path):
    """scikit-learn 1.6 warned "The `cv='prefit'` option is deprecated in 1.6 ..." as a
    UserWarning, 1.7 as a FutureWarning; statsmodels 0.14 warned "Argument %s not used by
    MixedLM.fit" without a category, 0.15 the same text and "Future versions will raise". A
    category that changed is no new deprecation; a UserWarning with another text, under
    another test, does not hide one."""
    prefit = "The `cv='prefit'` option is deprecated in 1.6 and will be removed in 1.8."
    head = (
        "import warnings\n\n"
        "class Calibrated:\n    def __init__(self, cv=None):\n        self.cv = cv\n\n"
        "    def fit(self, X):\n        if self.cv == 'prefit':\n"
    )
    old = {
        "pkg/__init__.py": f"{head}            warnings.warn({prefit!r})\n        return self\n\n"
        "def fit(X, **kwargs):\n    if kwargs:\n"
        '        warnings.warn("Argument %s not used by fit" % list(kwargs))\n    return X\n\n'
        "def g(x, y=None):\n    if x:\n"
        '        warnings.warn("x is ignored", UserWarning)\n    return x\n'
    }
    new = {
        "pkg/__init__.py": f"{head}            warnings.warn({prefit!r}, FutureWarning)\n"
        "        return self\n\n"
        "def fit(X, **kwargs):\n    disallowed = list(kwargs)\n    if disallowed:\n"
        "        warnings.warn(\n"
        '            "Argument %s not used by fit. Future versions will raise." % disallowed,\n'
        "            FutureWarning,\n        )\n    return X\n\n"
        "def g(x, y=None):\n    if x:\n"
        '        warnings.warn("x is ignored", UserWarning)\n'
        "    if y is not None:\n"
        '        warnings.warn("y is deprecated", FutureWarning)\n    return x\n'
    }
    assert set(warned(diff(tmp_path, old, new))) == {("pkg.g", "y")}


def test_a_warning_while_a_parameter_is_left_out_deprecates_its_default(tmp_path):
    """scikit-learn 1.7's ``smacof(n_init="warn")`` warns under ``if n_init == "warn":`` "The
    default value of `n_init` will change from 8 to 1 in 1.9.", and ``MDS.fit`` under ``if
    self.n_init == "warn":`` for its constructor's; polars under ``if plan_stage is None:``;
    statsmodels 0.15 under ``if result_object is None and not store:``, for a new
    ``result_object``. Each read as "the function is deprecated in some calls": its default is
    (WARNS_DEFAULT), and passing the parameter keeps the old behaviour."""
    old = {
        "pkg/__init__.py": "def smacof(X, n_init=8):\n    return X\n\n"
        "class MDS:\n    def __init__(self, n_init=4):\n        self.n_init = n_init\n\n"
        "    def fit(self, X):\n        return X\n\n"
        "def show_graph(plan_stage=None):\n    return plan_stage\n\n"
        "def acorr_lm(resid, store=False):\n    return resid\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def smacof(X, n_init='warn'):\n    if n_init == 'warn':\n"
        "        warnings.warn(\n"
        "            'The default value of `n_init` will change from 8 to 1 in 1.9.',\n"
        "            FutureWarning,\n        )\n        n_init = 8\n    return X\n\n"
        "class MDS:\n    def __init__(self, n_init='warn'):\n        self.n_init = n_init\n\n"
        "    def fit(self, X):\n        if self.n_init == 'warn':\n"
        "            warnings.warn(\n"
        "                'The default value of `n_init` will change from 4 to 1 in 1.9.',\n"
        "                FutureWarning,\n            )\n        return X\n\n"
        "def show_graph(plan_stage=None):\n    if plan_stage is None:\n"
        "        warnings.warn(\n"
        "            \"The default value of `plan_stage` will change from 'ir' to 'physical'.\",\n"
        "            FutureWarning,\n        )\n    return plan_stage\n\n"
        "def acorr_lm(resid, store=False, result_object=None):\n"
        "    if result_object is None and not store:\n"
        "        warnings.warn(\n"
        "            'acorr_lm currently returns a plain tuple. In 0.16 the default behavior '\n"
        "            'will switch to a result object. Set result_object=True to switch now.',\n"
        "            FutureWarning,\n        )\n    return resid\n"
    }
    changes = diff(tmp_path, old, new)
    found = warned(changes)
    assert {k: (v["when"], v.get("condition"), v.get("behavior")) for k, v in found.items()} == {
        ("pkg.smacof", "n_init"): (WARNS_DEFAULT, None, True),
        ("pkg.MDS.__init__", "n_init"): (WARNS_DEFAULT, None, True),
        ("pkg.show_graph", "plan_stage"): (WARNS_DEFAULT, None, True),
        ("pkg.acorr_lm", "result_object"): (WARNS_DEFAULT, "not store", True),
    }
    smacof = next(c for c in changes if c.path == "pkg.smacof")
    assert form(smacof, ("pkg.smacof",)) == USES_API
    assert form(smacof, ("pkg.smacof", "n_init")) == USES_API
    assert smacof.describe(versioned=False) == (
        "`pkg.smacof(n_init=...)`: the default of `n_init` will change, with a FutureWarning "
        "at run time when it is left out: The default value of `n_init` will change from 8 to "
        "1 in 1.9."
    )


def test_a_method_named_like_a_test_is_read(tmp_path):
    """statsmodels 0.15's ``DescStatUV.test_mean`` and ``OLSResults.el_test`` warn of their
    default: methods of a library class, never read because their names look like tests'."""
    old = {
        "pkg/__init__.py": "class DescStat:\n    def test_mean(self, mu):\n        return mu\n\n"
        "    def el_test(self, b):\n        return b\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\nclass DescStat:\n"
        "    def test_mean(self, mu):\n"
        "        warnings.warn('test_mean is deprecated', FutureWarning)\n        return mu\n\n"
        "    def el_test(self, b):\n"
        "        warnings.warn('el_test is deprecated', FutureWarning)\n        return b\n"
    }
    assert set(warned(diff(tmp_path, old, new))) == {
        ("pkg.DescStat.test_mean", None),
        ("pkg.DescStat.el_test", None),
    }


def test_a_function_moved_into_a_private_module_is_compared_by_its_public_path(tmp_path):
    """statsmodels 0.15 moved ``tsa/seasonal.py`` to ``tsa/seasonal/_seasonal.py`` and
    re-exports ``seasonal_decompose`` where it was: the old function was looked up at the new
    canonical path only, so its new warning was dropped as one of a new function."""
    old = {
        "pkg/__init__.py": "",
        "pkg/seasonal.py": "def decompose(x, extrapolate_trend=0):\n    return x\n",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/seasonal/__init__.py": "from pkg.seasonal._seasonal import decompose\n\n"
        "__all__ = ['decompose']\n",
        "pkg/seasonal/_seasonal.py": "import warnings\n\n"
        "def decompose(x, extrapolate_trend=0):\n    if extrapolate_trend == 'freq':\n"
        "        warnings.warn(\"`extrapolate_trend='freq'` is deprecated\", FutureWarning)\n"
        "    return x\n",
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: v.get("condition") for k, v in found.items()} == {
        ("pkg.seasonal.decompose", "extrapolate_trend"): "extrapolate_trend == 'freq'"
    }


def test_an_alias_that_became_a_function_that_warns_is_deprecated(tmp_path):
    """networkx 3.5: ``graph_could_be_isomorphic = could_be_isomorphic`` became a function
    that warns at every call, "graph_could_be_isomorphic is deprecated, use
    `could_be_isomorphic` instead.": the old object, an alias, was no function, so it was
    skipped. A warning of such a name for a parameter is not read."""
    old = {
        "pkg/__init__.py": "def could_be(g1, g2):\n    return True\n\ngraph_could_be = could_be\n\n"
        "def other(g1, g2):\n    return True\n\nalso_other = other\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\ndef could_be(g1, g2):\n    return True\n\n"
        "def graph_could_be(g1, g2):\n"
        "    warnings.warn(\n"
        '        "graph_could_be is deprecated, use `could_be` instead.", DeprecationWarning\n'
        "    )\n    return could_be(g1, g2)\n\n"
        "def other(g1, g2):\n    return True\n\n"
        "def also_other(g1, g2, strict=None):\n    if strict is not None:\n"
        '        warnings.warn("strict is deprecated", DeprecationWarning)\n    return True\n'
    }
    assert set(warned(diff(tmp_path, old, new))) == {("pkg.graph_could_be", None)}


def test_a_helper_that_warns_of_a_key_of_its_kwargs(tmp_path):
    """Django 5.2's ``_check_deprecated_find_param(find_all=False, **kwargs)`` warns under ``if
    "all" in kwargs:``, and ``find(path, find_all=False, **kwargs)`` hands it its own
    ``**kwargs`` (the finders through a method of theirs): ``find(all=...)`` is deprecated,
    which no change said, since ``all`` went into ``**kwargs``."""
    old = {
        "pkg/__init__.py": "def find(path, all=False):\n    return path\n\n"
        "class Finder:\n    def find(self, path, all=False):\n        return path\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def _check_deprecated_find_param(find_all=False, **kwargs):\n"
        "    if 'all' in kwargs:\n"
        "        msg = 'Passing the `all` argument to find() is deprecated. Use `find_all`.'\n"
        "        warnings.warn(msg, DeprecationWarning)\n"
        "        find_all = kwargs.pop('all')\n"
        "    return find_all\n\n"
        "def find(path, find_all=False, **kwargs):\n    if kwargs:\n"
        "        find_all = _check_deprecated_find_param(find_all=find_all, **kwargs)\n"
        "    return path\n\n"
        "class Finder:\n    def _check_deprecated_find_param(self, **kwargs):\n"
        "        return _check_deprecated_find_param(**kwargs)\n\n"
        "    def find(self, path, find_all=False, **kwargs):\n        if kwargs:\n"
        "            find_all = self._check_deprecated_find_param(find_all=find_all, **kwargs)\n"
        "        return path\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], "condition" in v) for k, v in found.items()} == {
        ("pkg.find", "all"): (WARNS_PARAMETER, False),
        ("pkg.Finder.find", "all"): (WARNS_PARAMETER, False),
    }


def test_a_code_generators_template_is_no_api(tmp_path):
    """transformers' ``models/voxtral/modular_voxtral.py`` is the template its
    ``modeling_voxtral.py`` is generated from (the generated file says so): its changes
    doubled the real ones, under a path nobody imports. A ``modular_`` module with no
    generated sibling stays API."""
    model = "class Vox:\n    def embeds(self, x):\n        return x\n\n    def gone(self):\n        pass\n"
    warn = (
        "import warnings\n\nclass Vox:\n    def embeds(self, x):\n"
        "        warnings.warn('embeds is deprecated', FutureWarning)\n        return x\n"
    )
    old = {
        "pkg/__init__.py": "",
        "pkg/models/__init__.py": "",
        "pkg/models/vox/__init__.py": "",
        "pkg/models/vox/modeling_vox.py": model,
        "pkg/models/vox/modular_vox.py": model,
        "pkg/models/other/__init__.py": "",
        "pkg/models/other/modular_other.py": "def f(x):\n    return x\n",
    }
    new = {
        **old,
        "pkg/models/vox/modeling_vox.py": warn,
        "pkg/models/vox/modular_vox.py": warn,
        "pkg/models/other/modular_other.py": "import warnings\n\ndef f(x):\n"
        "    warnings.warn('f is deprecated', FutureWarning)\n    return x\n",
    }
    changes = diff(tmp_path, old, new)
    assert not [c.path for c in changes if "modular_vox" in c.path]
    assert set(warned(changes)) == {
        ("pkg.models.vox.modeling_vox.Vox.embeds", None),
        ("pkg.models.other.modular_other.f", None),
    }


def test_every_test_a_warning_is_under_counts(tmp_path):
    """statsmodels' internal ``check_random_state(seed, deprecated=False, warn=True)`` warns
    under ``if deprecated:`` and ``if warn:``: a warning its caller asks for (opt-in), not a
    deprecation; it was "deprecated in some calls" under ``warn``. scikit-learn's
    ``LogisticRegression.fit`` warns under ``if solver == "liblinear":`` and ``if
    len(classes) > 2:``: both are the condition, not the inner one alone."""
    old = {
        "pkg/__init__.py": "def check(seed=None, deprecated=False, warn=True):\n    return seed\n\n"
        "def fit(X, solver='lbfgs'):\n    return X\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def check(seed=None, deprecated=False, warn=True):\n"
        "    if deprecated:\n        if warn:\n"
        "            warnings.warn('passing an integer will change in 0.16', FutureWarning)\n"
        "    return seed\n\n"
        "def fit(X, solver='lbfgs'):\n    classes = set(X)\n"
        "    if solver == 'liblinear':\n        if len(classes) > 2:\n"
        "            warnings.warn(\n"
        "                \"Using the 'liblinear' solver for multiclass is deprecated.\",\n"
        "                FutureWarning,\n            )\n    return X\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v.get("condition")) for k, v in found.items()} == {
        ("pkg.fit", "solver"): (WARNS_PARAMETER, "solver == 'liblinear' and len(classes) > 2")
    }


def test_a_parameter_read_through_a_type_guard_or_through_self_is_the_parameter(tmp_path):
    """scikit-learn 1.7's ``from_predictions(..., y_pred="deprecated")`` warns under ``if not
    (isinstance(y_pred, str) and y_pred == "deprecated"):``, which holds whenever ``y_pred``
    is passed; ``ColumnTransformer.fit_transform`` under ``if self.force_int_remainder_cols !=
    "deprecated":``, a parameter of the constructor: ``ColumnTransformer(
    force_int_remainder_cols=...)`` is deprecated, not ``fit_transform`` in some calls."""
    old = {
        "pkg/__init__.py": "def from_predictions(y_true, y_score=None, y_pred=None):\n"
        "    return y_true\n\n"
        "class ColumnTransformer:\n"
        "    def __init__(self, transformers, force_int_remainder_cols=True):\n"
        "        self.force_int_remainder_cols = force_int_remainder_cols\n\n"
        "    def fit_transform(self, X):\n        return X\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def from_predictions(y_true, y_score=None, y_pred='deprecated'):\n"
        "    if not (isinstance(y_pred, str) and y_pred == 'deprecated'):\n"
        "        warnings.warn(\n"
        "            'y_pred is deprecated in 1.7. Please use `y_score` instead.', FutureWarning\n"
        "        )\n        y_score = y_pred\n    return y_true\n\n"
        "class ColumnTransformer:\n"
        "    def __init__(self, transformers, force_int_remainder_cols='deprecated'):\n"
        "        self.force_int_remainder_cols = force_int_remainder_cols\n\n"
        "    def fit_transform(self, X):\n"
        "        if self.force_int_remainder_cols != 'deprecated':\n"
        "            warnings.warn(\n"
        "                'The parameter `force_int_remainder_cols` is deprecated.', FutureWarning\n"
        "            )\n        return X\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], "condition" in v) for k, v in found.items()} == {
        ("pkg.from_predictions", "y_pred"): (WARNS_PARAMETER, False),
        ("pkg.ColumnTransformer.__init__", "force_int_remainder_cols"): (WARNS_PARAMETER, False),
    }


def test_a_warning_that_a_behaviour_changes_is_not_one_that_the_api_goes(tmp_path):
    """pandas 3's ``to_pytimedelta`` warns "The behavior of X.to_pytimedelta is deprecated, in
    a future version this will return a Series ...": the result changes, the method stays.
    The change says so (``warning["behavior"]``), and a call of it only uses the API. A
    message that deprecates a parameter and then says another default will change is about
    the parameter (scikit-learn 1.7's ``n_alphas``)."""
    old = {
        "pkg/__init__.py": "def to_pytimedelta(x):\n    return x\n\ndef read(path):\n"
        "    return path\n\ndef b(x):\n    return x\n\ndef gone(x):\n    return x\n\n"
        "def fit(x, n_alphas=100):\n    return x\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def to_pytimedelta(x):\n    warnings.warn(\n"
        "        'The behavior of to_pytimedelta is deprecated, in a future version this will '\n"
        "        'return a Series instead of an ndarray. To retain the old behavior, call '\n"
        "        '`np.array` on the result', FutureWarning\n    )\n    return x\n\n"
        "def read(path):\n    warnings.warn(\n"
        "        'In a future version, read will return a list instead of a tuple', FutureWarning\n"
        "    )\n    return path\n\n"
        "def b(x):\n"
        "    warnings.warn('b will return a copy in the future', PendingDeprecationWarning)\n"
        "    return x\n\n"
        "def gone(x):\n"
        "    warnings.warn('gone is deprecated and will be removed in 2.0', FutureWarning)\n"
        "    return x\n\n"
        "def fit(x, n_alphas='deprecated'):\n    if n_alphas != 'deprecated':\n"
        "        warnings.warn(\n"
        "            \"'n_alphas' was deprecated in 1.7 and will be removed in 1.9. The default \"\n"
        "            \"value of 'alphas' will change from None to 100 in 1.9.\",\n"
        "            FutureWarning,\n        )\n    return x\n"
    }
    changes = diff(tmp_path, old, new)
    assert {k[0]: v.get("behavior", False) for k, v in warned(changes).items()} == {
        "pkg.to_pytimedelta": True,
        "pkg.read": True,
        "pkg.b": True,
        "pkg.gone": False,
        "pkg.fit": False,
    }
    by = {c.path: c for c in changes}
    assert form(by["pkg.to_pytimedelta"], ("pkg.to_pytimedelta",)) == USES_API
    assert form(by["pkg.gone"], ("pkg.gone",)) == OLD_FORM
    assert (
        by["pkg.read"]
        .describe(versioned=False)
        .startswith("`pkg.read` will change behavior, with a FutureWarning at run time: ")
    )


def test_a_warning_with_the_text_of_another_parameters_warning_is_new(tmp_path):
    """The release at the cutoff warned for ``a`` only: through ``_warn_kw("a")`` with
    f"The {name!r} keyword is deprecated", the same text written inline, and "axis=0 is
    deprecated". The pinned release's warning for ``b`` (or ``axis=2``) was taken for that
    one: texts were compared whatever their parameter, and without their digits."""
    helper = (
        "import warnings\n\n"
        "def _warn_kw(name):\n"
        "    warnings.warn(f'The {name!r} keyword is deprecated', FutureWarning)\n\n"
    )
    old_f = "def f(x, a=None, b=None):\n    if a is not None:\n        _warn_kw('a')\n"
    old_g = (
        "def g(x, a=None, b=None):\n    if a is not None:\n"
        "        warnings.warn(f\"The {'a'} keyword is deprecated\", FutureWarning)\n"
    )
    old_h = (
        "def h(x, a=None, axis=1):\n    if a is not None:\n"
        "        warnings.warn('axis=0 is deprecated', FutureWarning)\n"
    )
    old = {
        "pkg/__init__.py": f"{helper}{old_f}    return x\n\n{old_g}    return x\n\n"
        f"{old_h}    return x\n"
    }
    new = {
        "pkg/__init__.py": f"{helper}{old_f}    if b is not None:\n        _warn_kw('b')\n"
        f"    return x\n\n{old_g}    if b is not None:\n"
        "        warnings.warn(f\"The {'b'} keyword is deprecated\", FutureWarning)\n"
        f"    return x\n\n{old_h}    if axis != 1:\n"
        "        warnings.warn('axis=2 is deprecated', FutureWarning)\n    return x\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: v["text"] for k, v in found.items()} == {
        ("pkg.f", "b"): "The 'b' keyword is deprecated",
        ("pkg.g", "b"): "The b keyword is deprecated",
        ("pkg.h", "axis"): "axis=2 is deprecated",
    }


def test_the_parameter_a_message_names_is_the_deprecated_one(tmp_path):
    """``if verbose and x is not None:`` with "The x keyword is deprecated" deprecated
    ``verbose``; "Passing both a and b is deprecated" under ``b is not None and a is not
    None`` deprecated ``b``; "The 'how' keyword is deprecated when axis is given" under ``axis
    is not None`` deprecated ``axis``. The message decides among the tests' parameters, and
    one that names another makes it a condition."""
    old = {
        "pkg/__init__.py": "def g(x=None, verbose=False):\n    return x\n\n"
        "def h(a=None, b=None):\n    return a\n\ndef k(axis=None, how=None):\n    return axis\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def g(x=None, verbose=False):\n    if verbose and x is not None:\n"
        "        warnings.warn('The x keyword is deprecated', FutureWarning)\n    return x\n\n"
        "def h(a=None, b=None):\n    if b is not None and a is not None:\n"
        "        warnings.warn('Passing both a and b is deprecated; pass only a', FutureWarning)\n"
        "    return a\n\n"
        "def k(axis=None, how=None):\n    if axis is not None:\n"
        "        warnings.warn(\"The 'how' keyword is deprecated when axis is given\", FutureWarning)\n"
        "    return axis\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v.get("condition")) for k, v in found.items()} == {
        ("pkg.g", "x"): (WARNS_PARAMETER, "verbose"),
        ("pkg.h", None): (WARNS_CONDITION, "b is not None and a is not None"),
        ("pkg.k", None): (WARNS_CONDITION, "axis is not None"),
    }


def test_a_warning_for_some_values_of_a_parameter_keeps_its_test(tmp_path):
    """``if errors != "raise":``, ``if axis:`` (default 0), pandas 3's ``if not future_stack:``
    (default True) and ``if mode == "fast":`` then ``if y is not None:`` warn in some of the
    calls that pass the parameter: the test is kept (``warning["condition"]``, readable), so
    that no note says the parameter warns whenever it is passed."""
    old = {
        "pkg/__init__.py": "def b(x, errors='raise'):\n    return x\n\n"
        "def c(x, axis=0):\n    return x\n\ndef stack(future_stack=False):\n    return 1\n\n"
        "def d(x, mode='slow', y=None):\n    return x\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def b(x, errors='raise'):\n    if errors != 'raise':\n"
        "        warnings.warn(\"errors='ignore' is deprecated\", FutureWarning)\n    return x\n\n"
        "def c(x, axis=0):\n    if axis:\n"
        "        warnings.warn('axis=1 is deprecated', FutureWarning)\n    return x\n\n"
        "def stack(future_stack=True):\n    if not future_stack:\n"
        "        warnings.warn('The previous implementation of stack is deprecated', FutureWarning)\n"
        "    return 1\n\n"
        "def d(x, mode='slow', y=None):\n    if mode == 'fast':\n        if y is not None:\n"
        "            warnings.warn(\"The y keyword is deprecated when mode is 'fast'\", FutureWarning)\n"
        "    return x\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v.get("condition"), v.get("readable")) for k, v in found.items()} == {
        ("pkg.b", "errors"): (WARNS_PARAMETER, "errors != 'raise'", True),
        ("pkg.c", "axis"): (WARNS_PARAMETER, "axis", True),
        ("pkg.stack", "future_stack"): (WARNS_PARAMETER, "not future_stack", True),
        ("pkg.d", "y"): (WARNS_PARAMETER, "mode == 'fast'", True),
    }


def test_a_keyword_read_into_a_local_or_past_an_early_return_is_the_parameter(tmp_path):
    """transformers 5.20's ``from_pretrained``: ``tp_plan = kwargs.pop("tp_plan", None)``, then
    ``if tp_plan is not None: warnings.warn(...)``, read as "from_pretrained is deprecated in
    some calls" for every transformers project; ``self.x = kwargs.pop("x", None)`` in a
    constructor; ``p = kwargs.pop("p", no_default)`` with a sentinel; ``if y is no_default:
    return x`` before the warning. A fast path that returns first, and reads not the
    parameter (polars' ``read_ipc`` for a Hugging Face path), or returns in a block, does not
    make the parameter's warning conditional (httpx's ``create_ssl_context(cert=...)``)."""
    old = {
        "pkg/__init__.py": "no_default = object()\n\nclass Model:\n    @classmethod\n"
        "    def from_pretrained(cls, name, **kwargs):\n        return cls()\n\n"
        "class Config:\n    def __init__(self, **kwargs):\n        self.batching = None\n\n"
        "def a(x, **kwargs):\n    return x\n\ndef e(x, y=no_default):\n    return x\n\n"
        "def read_ipc(source, rechunk=None):\n    return source\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\nno_default = object()\n\nclass Model:\n"
        "    @classmethod\n    def from_pretrained(cls, name, **kwargs):\n"
        "        tp_plan = kwargs.pop('tp_plan', None)\n"
        "        device = kwargs.pop('device', None)\n"
        "        if tp_plan is not None:\n            warnings.warn(\n"
        "                'Passing `tp_plan` directly to `from_pretrained` is deprecated.',\n"
        "                FutureWarning,\n            )\n        return cls()\n\n"
        "class Config:\n    def __init__(self, **kwargs):\n"
        "        self.batching = kwargs.pop('batching', None)\n"
        "        if self.batching is not None:\n"
        "            warnings.warn('Passing batching is deprecated', FutureWarning)\n\n"
        "def a(x, **kwargs):\n    p = kwargs.pop('p', no_default)\n"
        "    if p is not no_default:\n"
        "        warnings.warn('p is deprecated', FutureWarning)\n    return x\n\n"
        "def e(x, y=no_default):\n    if y is no_default:\n        return x\n"
        "    warnings.warn('y is deprecated', FutureWarning)\n    return x\n\n"
        "def read_ipc(source, rechunk=None):\n"
        "    if str(source).startswith('hf://'):\n        return source\n"
        "    if source is None:\n        if rechunk:\n            return source\n"
        "    if rechunk is not None:\n"
        "        warnings.warn('`rechunk` parameter on read_ipc() will be removed.', FutureWarning)\n"
        "    return source\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], "condition" in v) for k, v in found.items()} == {
        ("pkg.Model.from_pretrained", "tp_plan"): (WARNS_PARAMETER, False),
        ("pkg.Config.__init__", "batching"): (WARNS_PARAMETER, False),
        ("pkg.a", "p"): (WARNS_PARAMETER, False),
        ("pkg.e", "y"): (WARNS_PARAMETER, False),
        ("pkg.read_ipc", "rechunk"): (WARNS_PARAMETER, False),
    }


def test_every_warning_of_one_api_is_kept(tmp_path):
    """pandas 3's ``to_json`` warns when ``date_format`` is left out for datetimes, and when
    it is "epoch": only the first was reported, and its text suggested that passing "epoch"
    was fine. One change per API and parameter keeps each warning (``warning["also"]``) and
    each message; so does one for several conditions of the function. A warning in the
    ``except`` of a ``try`` under an ``if`` has no condition to quote: the ``if`` alone does
    not make it warn."""
    old = {
        "pkg/__init__.py": "def to_json(obj, date_format=None, orient=None):\n    return obj\n\n"
        "def shift(x, fill=None):\n    return x\n\ndef at(time):\n    return time\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "def to_json(obj, date_format=None, orient=None):\n"
        "    if date_format is None and orient == 'table':\n        date_format = 'iso'\n"
        "    elif date_format is None:\n        if obj:\n"
        "            warnings.warn(\"The default 'epoch' date format is deprecated.\", FutureWarning)\n"
        "    elif date_format == 'epoch':\n"
        "        warnings.warn(\"'epoch' date format is deprecated.\", FutureWarning)\n"
        "    return obj\n\n"
        "def shift(x, fill=None):\n    if x == 1:\n"
        "        warnings.warn('shifting ones is deprecated.', FutureWarning)\n"
        "    if x == 2:\n"
        "        warnings.warn('shifting twos is deprecated.', FutureWarning)\n    return x\n\n"
        "def at(time):\n    if isinstance(time, str):\n        try:\n            time = int(time)\n"
        "        except ValueError:\n            warnings.warn(\n"
        "                'The string cannot be parsed and in a future version will raise.',\n"
        "                FutureWarning,\n            )\n    return time\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert set(found) == {("pkg.to_json", "date_format"), ("pkg.shift", None), ("pkg.at", None)}
    to_json = found["pkg.to_json", "date_format"]
    assert (to_json["when"], to_json["condition"]) == (WARNS_PARAMETER, "date_format == 'epoch'")
    assert [(a["when"], a["condition"]) for a in to_json["also"]] == [
        (WARNS_DEFAULT, "not (date_format is None and orient == 'table') and obj")
    ]
    assert to_json["text"] == (
        "'epoch' date format is deprecated. The default 'epoch' date format is deprecated."
    )
    shift = found["pkg.shift", None]
    assert shift["condition"] == "x == 1"
    assert [a["condition"] for a in shift["also"]] == ["x == 2"]
    assert found["pkg.at", None]["condition"] is None


def test_a_property_that_warns_when_read_or_set_is_deprecated(tmp_path):
    """pandas 3's ``BaseGroupBy.groups`` warns in its getter, matplotlib 3.10's
    ``Figure.number`` in its setter: griffe loads a property as an attribute, so neither was
    read. Reading (or setting) it warns (WARNS_ACCESS); only the old form of a getter is the
    reading."""
    old = {
        "pkg/__init__.py": "class C:\n    @property\n    def p(self):\n        return 1\n\n"
        "    @property\n    def q(self):\n        return 2\n\n"
        "    @q.setter\n    def q(self, value):\n        self._q = value\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\nclass C:\n    @property\n    def p(self):\n"
        "        warnings.warn('C.p is deprecated', FutureWarning)\n        return 1\n\n"
        "    @property\n    def q(self):\n        return 2\n\n"
        "    @q.setter\n    def q(self, value):\n"
        "        warnings.warn('Setting C.q is deprecated', DeprecationWarning)\n"
        "        self._q = value\n"
    }
    changes = diff(tmp_path, old, new)
    found = warned(changes)
    assert {k: (v["when"], v.get("access")) for k, v in found.items()} == {
        ("pkg.C.p", None): (WARNS_ACCESS, "read"),
        ("pkg.C.q", None): (WARNS_ACCESS, "set"),
    }
    by = {c.path: c for c in changes}
    assert form(by["pkg.C.p"], ("pkg.C.p",)) == OLD_FORM
    assert form(by["pkg.C.q"], ("pkg.C.q",)) == USES_API
    assert by["pkg.C.q"].describe(versioned=False) == (
        "setting `pkg.C.q` is deprecated, with a DeprecationWarning at run time: Setting C.q is "
        "deprecated"
    )


def test_a_name_a_module_getattr_serves_with_a_new_warning_is_deprecated(tmp_path):
    """A PEP 562 ``__getattr__`` that served ``Old`` in both releases (or a key of a table of
    the module), and warns of it in the pinned one only, deprecates ``pkg.Old``; a
    ``__new__`` that warns at every call deprecates its class, as an ``__init__`` does."""
    old = {
        "pkg/__init__.py": "class New:\n    pass\n\nclass Thing:\n"
        "    def __new__(cls, *args):\n        return super().__new__(cls)\n\n"
        "def __getattr__(name):\n    if name == 'Old':\n        return New\n"
        "    if name in _ALIASES:\n        return New\n    raise AttributeError(name)\n\n"
        "_ALIASES = {'Older': 'New'}\n"
    }
    new = {
        "pkg/__init__.py": "import warnings\n\nclass New:\n    pass\n\nclass Thing:\n"
        "    def __new__(cls, *args):\n"
        "        warnings.warn('Thing is deprecated', DeprecationWarning)\n"
        "        return super().__new__(cls)\n\n"
        "def __getattr__(name):\n    if name == 'Old':\n"
        "        warnings.warn('Old is deprecated, use New', DeprecationWarning)\n"
        "        return New\n    if name in _ALIASES:\n"
        "        warnings.warn(f'{name} is deprecated, use New', DeprecationWarning)\n"
        "        return New\n    raise AttributeError(name)\n\n"
        "_ALIASES = {'Older': 'New'}\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v["text"]) for k, v in found.items()} == {
        ("pkg.Old", None): (WARNS_ACCESS, "Old is deprecated, use New"),
        ("pkg.Older", None): (WARNS_ACCESS, "Older is deprecated, use New"),
        ("pkg.Thing", None): (WARNS_ALWAYS, "Thing is deprecated"),
    }


def test_a_warning_the_release_at_the_cutoff_gave_another_way_is_not_new(tmp_path):
    """A parameter the old release's ``@deprecate_kwarg("y")`` deprecated, and parameters a
    helper of another name (``_check(y)``) and a private function the old one delegated to
    warned of with the same text: the pinned release warning of them inline is no new
    deprecation."""
    decorators = {
        "pkg/_decorators.py": "def deprecate_kwarg(*args, **kwargs):\n    return lambda f: f\n"
    }
    old = {
        **decorators,
        "pkg/__init__.py": "import warnings\n\nfrom pkg._decorators import deprecate_kwarg\n\n"
        "@deprecate_kwarg('y', None)\ndef a(x, y=None):\n    return x\n\n"
        "def _check(y):\n    if y is not None:\n"
        "        warnings.warn('The y keyword is deprecated', FutureWarning)\n\n"
        "def b(x, y=None):\n    _check(y)\n    return x\n\n"
        "def _impl(x, z):\n    if z is not None:\n"
        "        warnings.warn('The z keyword is deprecated', FutureWarning)\n    return x\n\n"
        "def c(x, z=None):\n    return _impl(x, z)\n",
    }
    inline = (
        "    if {p} is not None:\n"
        "        warnings.warn('The {p} keyword is deprecated', FutureWarning)\n    return x\n\n"
    )
    new = {
        **decorators,
        "pkg/__init__.py": "import warnings\n\n"
        "def a(x, y=None):\n"
        + inline.format(p="y")
        + "def b(x, y=None):\n"
        + inline.format(p="y")
        + "def c(x, z=None):\n"
        + inline.format(p="z"),
    }
    assert warned(diff(tmp_path, old, new)) == {}


def test_a_message_is_filled_from_the_constants_it_is_given(tmp_path):
    """``"%s is deprecated" % "a"`` and ``"{name} ...".format(name="c")`` were quoted with
    their placeholders; a template in a module constant (``_MSG.format(...)``, ``_PCT % "e"``)
    gave no text, and ``msg += " Use `a` instead."`` was lost. A value nothing gives is
    ``{...}``, as in an f-string."""
    old = {"pkg/__init__.py": "".join(f"def {n}(x):\n    return x\n\n" for n in "acdefhk")}
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "_MSG = '{name} is deprecated; use {alt}'\n_PCT = '%s is deprecated'\n\n"
        "def a(x):\n"
        "    warnings.warn('%s is deprecated, use `new` instead' % 'a', DeprecationWarning)\n"
        "    return x\n\n"
        "def c(x):\n    warnings.warn(\n"
        "        '{name} is deprecated; use {alt} instead'.format(name='c', alt='new'),\n"
        "        DeprecationWarning,\n    )\n    return x\n\n"
        "def h(x):\n"
        "    warnings.warn('{} is deprecated, use {}'.format('h', 'a'), DeprecationWarning)\n"
        "    return x\n\n"
        "def d(x):\n"
        "    warnings.warn(_MSG.format(name='d', alt='b'), DeprecationWarning)\n    return x\n\n"
        "def e(x):\n    warnings.warn(_PCT % 'e', DeprecationWarning)\n    return x\n\n"
        "def f(x):\n    msg = 'f is deprecated.'\n    msg += ' Use `a` instead.'\n"
        "    warnings.warn(msg, DeprecationWarning)\n    return x\n\n"
        "def k(x):\n"
        "    warnings.warn('{} is deprecated'.format(type(x)), DeprecationWarning)\n"
        "    return x\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k[0]: v["text"] for k, v in found.items()} == {
        "pkg.a": "a is deprecated, use `new` instead",
        "pkg.c": "c is deprecated; use new instead",
        "pkg.h": "h is deprecated, use a",
        "pkg.d": "d is deprecated; use b",
        "pkg.e": "e is deprecated",
        "pkg.f": "f is deprecated. Use `a` instead.",
        "pkg.k": "{...} is deprecated",
    }


def test_more_ways_a_warning_is_written_are_read(tmp_path):
    """A package's own ``AstroDeprecationWarning(AstroWarning)``, which derives from no
    deprecation category (astropy's), counts by its name, as another package's does; a
    message in ``textwrap.dedent(...)`` or in a constant made with ``.replace("\\n", " ")``
    (scipy 1.18's ``anderson``) is read; a warning built before the call (``w =
    SymDeprecationWarning(...); warnings.warn(w)``, sympy's helper) counts; one in ``p is not
    None and warn(...)`` or ``warn(...) if p is not None else None`` is under that test."""
    old = {
        "pkg/__init__.py": "".join(f"def {n}(x, p=None):\n    return x\n\n" for n in "efgs")
        + "def p1(x, p=None):\n    return x\n\ndef p2(x, p=None):\n    return x\n"
    }
    new = {
        "pkg/__init__.py": "import textwrap\nimport warnings\n\n"
        "from other import OtherDeprecationWarning\n\n"
        "class AstroWarning(Warning):\n    pass\n\n"
        "class AstroDeprecationWarning(AstroWarning):\n    pass\n\n"
        "class SymDeprecationWarning(DeprecationWarning):\n"
        "    def __init__(self, message, *, since):\n        super().__init__(message)\n\n"
        "def sym_deprecation_warning(message, *, since):\n"
        "    w = SymDeprecationWarning(message, since=since)\n"
        "    warnings.warn(w, stacklevel=2)\n\n"
        '_G = """g is\ndeprecated""".replace("\\n", " ")\n\n'
        "def e(x, p=None):\n    warnings.warn('e is deprecated', AstroDeprecationWarning)\n"
        "    return x\n\n"
        "def f(x, p=None):\n    warnings.warn(\n        textwrap.dedent(\n"
        '            """\n            f is deprecated, use h.\n            """\n'
        "        ),\n        OtherDeprecationWarning,\n    )\n    return x\n\n"
        "def g(x, p=None):\n    warnings.warn(_G, DeprecationWarning)\n    return x\n\n"
        "def s(x, p=None):\n    sym_deprecation_warning('s is deprecated', since='1.14')\n"
        "    return x\n\n"
        "def p1(x, p=None):\n"
        "    p is not None and warnings.warn('p is deprecated', DeprecationWarning)\n"
        "    return x\n\n"
        "def p2(x, p=None):\n"
        "    warnings.warn('p is deprecated', DeprecationWarning) if p is not None else None\n"
        "    return x\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v["category"], v["text"]) for k, v in found.items()} == {
        ("pkg.e", None): (WARNS_ALWAYS, "AstroDeprecationWarning", "e is deprecated"),
        ("pkg.f", None): (WARNS_ALWAYS, "OtherDeprecationWarning", "f is deprecated, use h."),
        ("pkg.g", None): (WARNS_ALWAYS, "DeprecationWarning", "g is deprecated"),
        ("pkg.s", None): (WARNS_ALWAYS, "SymDeprecationWarning", "s is deprecated"),
        ("pkg.p1", "p"): (WARNS_PARAMETER, "DeprecationWarning", "p is deprecated"),
        ("pkg.p2", "p"): (WARNS_PARAMETER, "DeprecationWarning", "p is deprecated"),
    }


def test_a_method_that_hands_its_call_to_its_base_and_chained_helpers(tmp_path):
    """scikit-learn 1.7's ``LassoCV.fit`` is ``return super().fit(X, ...)``, and the private
    base's ``fit`` warns of the constructor's ``n_alphas``: ``LassoCV(n_alphas=...)`` is
    deprecated. sqlalchemy's ``warn_deprecated(msg, version)`` calls
    ``_warn_with_version(msg, version, SADeprecationWarning)``, which builds ``type_(msg)`` and
    hands it to ``_warnings_warn(message, category=None)``; that one warns under ``if category
    is not None:``, a test of its own category, which is no guard. Three helpers deep is read;
    four are not."""
    old = {
        "pkg/__init__.py": "from pkg._base import _LinearModelCV\n"
        "from pkg.util import SADeprecationWarning\n\n"
        "class LassoCV(_LinearModelCV):\n    def __init__(self, n_alphas=100):\n"
        "        super().__init__(n_alphas)\n\n    def fit(self, X):\n"
        '        """Fit."""\n        return super().fit(X)\n\n'
        "def aliased(x):\n    return x\n\ndef direct(x):\n    return x\n\ndef deep(x):\n"
        "    return x\n",
        "pkg/_base.py": "class _LinearModelCV:\n    def __init__(self, n_alphas=100):\n"
        "        self.n_alphas = n_alphas\n\n    def fit(self, X):\n        return self\n",
        "pkg/util.py": "class SADeprecationWarning(DeprecationWarning):\n    pass\n",
    }
    new = {
        "pkg/__init__.py": "from pkg._base import _LinearModelCV\n"
        "from pkg.util import SADeprecationWarning, _warnings_warn, warn_deprecated, warn_1\n\n"
        "class LassoCV(_LinearModelCV):\n    def __init__(self, n_alphas='deprecated'):\n"
        "        super().__init__(n_alphas)\n\n    def fit(self, X):\n"
        '        """Fit."""\n        return super().fit(X)\n\n'
        "def aliased(x):\n"
        "    warn_deprecated('aliased() on a select is deprecated', '2.0.52')\n    return x\n\n"
        "def direct(x):\n"
        "    _warnings_warn('direct is deprecated', SADeprecationWarning)\n    return x\n\n"
        "def deep(x):\n    warn_1('deep is deprecated')\n    return x\n",
        "pkg/_base.py": "import warnings\n\nclass _LinearModelCV:\n"
        "    def __init__(self, n_alphas='deprecated'):\n        self.n_alphas = n_alphas\n\n"
        "    def fit(self, X):\n        if self.n_alphas != 'deprecated':\n"
        "            warnings.warn(\"'n_alphas' was deprecated in 1.7\", FutureWarning)\n"
        "        return self\n",
        "pkg/util.py": "import warnings\n\n"
        "class SADeprecationWarning(DeprecationWarning):\n    pass\n\n"
        "def _warnings_warn(message, category=None, stacklevel=2):\n"
        "    if category is not None:\n"
        "        warnings.warn(message, category, stacklevel=stacklevel)\n"
        "    else:\n        warnings.warn(message, stacklevel=stacklevel)\n\n"
        "def _warn_with_version(msg, version, type_, stacklevel):\n"
        "    warn = type_(msg)\n    warn.deprecated_since = version\n"
        "    _warnings_warn(warn, stacklevel=stacklevel + 1)\n\n"
        "def warn_deprecated(msg, version, stacklevel=3):\n"
        "    _warn_with_version(msg, version, SADeprecationWarning, stacklevel)\n\n"
        "def warn_4(msg):\n    warnings.warn(msg, DeprecationWarning)\n\n"
        "def warn_3(msg):\n    warn_4(msg)\n\n"
        "def warn_2(msg):\n    warn_3(msg)\n\n"
        "def warn_1(msg):\n    warn_2(msg)\n",
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v["category"], v["text"]) for k, v in found.items()} == {
        ("pkg.LassoCV.__init__", "n_alphas"): (
            WARNS_PARAMETER,
            "FutureWarning",
            "'n_alphas' was deprecated in 1.7",
        ),
        ("pkg.aliased", None): (
            WARNS_ALWAYS,
            "SADeprecationWarning",
            "aliased() on a select is deprecated",
        ),
        ("pkg.direct", None): (WARNS_ALWAYS, "SADeprecationWarning", "direct is deprecated"),
    }


def test_the_guards_on_what_is_new_hold(tmp_path):
    """The same text under another condition, with only its version changed, is not new; a
    function a decorator deprecates and whose body warns too is one change; a parameter a
    decorator deprecates and the body warns of is one; a warning's names of the API itself
    are no replacement (APIChange.library_names)."""
    decorators = (
        "def deprecated_function(message):\n    return lambda f: f\n\n"
        "def deprecate_kwarg(*args, **kwargs):\n    return lambda f: f\n"
    )
    old = {
        "pkg/__init__.py": "import warnings\n\n"
        "def f(a=0):\n    if a > 1:\n"
        "        warnings.warn('f with a large a is deprecated, removed in 3.0', FutureWarning)\n"
        "    return a\n\ndef g(x):\n    return x\n\ndef h(x, y=None):\n    return x\n\n"
        "def k(x):\n    return x\n\ndef m(x):\n    return x\n",
        "pkg/_decorators.py": decorators,
    }
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "from pkg._decorators import deprecate_kwarg, deprecated_function\n\n"
        "def f(a=0):\n    if a > 2:\n"
        "        warnings.warn('f with a large a is deprecated, removed in 4.0', FutureWarning)\n"
        "    return a\n\n"
        "@deprecated_function('use k')\ndef g(x):\n"
        "    warnings.warn('g is deprecated', FutureWarning)\n    return x\n\n"
        "@deprecate_kwarg('y')\ndef h(x, y=None):\n    if y is not None:\n"
        "        warnings.warn('y is deprecated', FutureWarning)\n    return x\n\n"
        "def k(x):\n    return x\n\n"
        "def m(x):\n    warnings.warn('`pkg.m` is deprecated, use `k` instead', FutureWarning)\n"
        "    return x\n",
        "pkg/_decorators.py": decorators,
    }
    changes = diff(tmp_path, old, new)
    assert found(changes) == {
        (DEPRECATED, "pkg.g", None),
        (DEPRECATED, "pkg.h", "y"),
        (DEPRECATED, "pkg.m", None),
    }
    assert {c.path: c.deprecated_by for c in changes if c.path in ("pkg.g", "pkg.h")} == {
        "pkg.g": "deprecated_function",
        "pkg.h": "deprecate_kwarg",
    }
    m = next(c for c in changes if c.path == "pkg.m")
    assert m.library_names == ["pkg.k"]


def test_a_method_whose_docstring_has_a_less_indented_line_is_read(tmp_path):
    """matplotlib 3.10's ``Axes.bxp`` has a docstring line at the start of its line: the
    method's source could not be dedented, so it was not parsed at all, and its ``vert``
    deprecation (``else: _api.warn_deprecated(...)``) was missed."""
    old = {"pkg/__init__.py": "class Axes:\n    def bxp(self, x, vert=None):\n        return x\n"}
    new = {
        "pkg/__init__.py": "import warnings\n\n"
        "class Axes:\n    @staticmethod\n    def _check(x):\n        return x\n\n"
        "    @_check\n    def bxp(self, x, vert=None):\n"
        '        """Draw a box plot.\n\nmeanprops : dict, optional\n        """\n'
        "        if vert is None:\n            vert = True\n        else:\n"
        "            warnings.warn('vert is deprecated', DeprecationWarning)\n"
        "        return x\n"
    }
    found = warned(diff(tmp_path, old, new))
    assert {k: (v["when"], v["at"]) for k, v in found.items()} == {
        ("pkg.Axes.bxp", "vert"): (WARNS_PARAMETER, "pkg/__init__.py:17")
    }
