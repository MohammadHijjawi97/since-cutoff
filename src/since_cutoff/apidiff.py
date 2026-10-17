"""Static API diff between two versions of a package, built on griffe.

The diff answers one question: *which code that was correct for the old version is wrong for
the new one?* So it reports removed objects and parameters, parameters that became required
or keyword-only, objects that changed kind, and objects newly marked deprecated (PEP 702
``@deprecated``, a library's own decorator whose name contains "deprecat", or a deprecation
``warnings.warn`` in the body: _Differ._warning_deprecations). Cosmetic changes
(defaults, attribute values, return annotations) are ignored.

Only the public API is compared. Besides ``_private`` names, test suites, benchmarks and
examples shipped inside a package are skipped (``pkg.testing`` and ``pkg.test`` directly under
the top-level package stay, since libraries such as pandas and numpy document them).

Packages are loaded statically (``allow_inspection=False``): no package code is imported. What a
static view cannot see is not reported rather than reported wrongly: a name the new version still
lists in ``__all__`` or hands out through a module ``__getattr__``, a member a class may now
inherit from a base outside the package, a module-level name bound only on some paths (inside
``if``/``try`` blocks, or under ``if __name__ == "__main__":``).
"""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import keyword
import logging
import re
import sys
import textwrap
import warnings
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from functools import cache
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from since_cutoff.cache import stable_hash

log = logging.getLogger(__name__)

# 12: import_paths, hint_source, move_evidence and still_handled_at; parameter hints from the
# parameter's own docstring entry and from the old code's warnings.warn text.
# 13: library_names, hint_file, deprecation_file and renamed (the evidence the notes from the
# diff show: notes.diff_note).
# 14: still_handled_text (what the pinned version's code that still handles a removed parameter
# says of it: "deprecated without replacement"), and a removed name's import paths no longer
# include the other names of the same object (``DeprecatedIn37 = CryptographyDeprecationWarning``
# and ``DeprecatedIn45 = ...`` are two names, each removed on its own).
# 15: a removed module-level class or function that came back under another name in the same
# module (or the module it moved to), keeping its public members, is a move whose
# ``move_evidence`` says ``"renamed": true`` (mcp 2's ``FastMCP`` -> ``MCPServer``,
# ``McpError`` -> ``MCPError``): _Differ.find_renamed.
# 16: a rename counts only the members the candidate defines itself, or inherits from a base
# the old class did not have (a new sibling subclass of the same base is not the rename), and
# needs 5 of them unless the names are related; a module's move records how many public names
# its old parent package still has (``move_evidence["left_behind"]``, 0 for a package that is
# gone or a shim without API: selection.package_moves groups only those); a module-level name
# bound to a method of an instance the module makes (huggingface_hub's ``duplicate_space =
# api.duplicate_space``) says which method (``alias_of``) and takes its deprecation text.
# 17: request_extras (a removed parameter's callable still takes ``extra_body`` /
# ``extra_query``, read from all its parameters: the recorded signature is cut at 400
# characters, and anthropic 1.8's ``Messages.create`` is longer than that).
# 18: DEPENDENCY_SWITCHED and ``dependency``: a distribution the older release required and the
# pinned one no longer does, where the public API that named its types names another required
# distribution's types instead (openai 3, anthropic 1.8, huggingface-hub 2 and mcp 2.2 require
# ``httpx2`` instead of ``httpx``): diff_sources reads both releases' Requires-Dist.
# 19 (18 was never released): a switch inside the extras both releases define (fastmcp-slim 4's
# ``client``, ``mcp`` and ``server`` extras list ``httpx2`` where 3.4 listed ``httpx``), to a
# copy the package ships (typer 0.27's ``typer._click`` for ``click``; not a shim that imports
# it, like langsmith 0.14's ``_openapi_client._httpx``), and to a distribution the
# older release already required (gradio 4: ``httpx``); a switched parameter counted by the type
# it names now (``parameter_types``: 8 ``http_client`` of openai 3 take ``httpx2.Client``, 6
# ``httpx2.AsyncClient``); a constructor's calls under its class's path, and with each
# switched parameter's position (``hf_raise_for_status(response)`` is called positionally),
# classmethods and staticmethods called on their class included (fastmcp's
# ``FastMCP.from_openapi``); and "still names" leaves out the names a module only exports
# (``__all__``, ``__getattr__``, ``__dir__``) and a package name in a list of requirements or a
# documentation example (``examples=``, ``description=``).
# 20: PEP 702's ``@deprecated`` imported through a library's compatibility module (``from
# pkg._compat import deprecated``) is PEP 702's, not the library's own decorator
# (``deprecated_by`` is None, so ``run`` probes it), and is found on an ``@overload`` (issue #44).
# 21 (20 was never released): a module the pinned release ships compiled, without a source or
# a stub, is not removed, nor is what it defines or a name a readable module still imports from
# it (issue #52; the scan warns instead: diff_sources_with_unread, Engine._unread_key). Also, a
# package's sibling modules load in the order of their names, not the file system's, so an
# object that several of them import from a private module has one public path on every machine
# (mcp 1.28's ``McpHttpClientFactory``: its switch's sites match across releases, 23 places not
# 20); ``warn(message)`` reads the message last assigned to ``message`` before the call
# (``hint``, ``still_handled_text``: httpx 0.27's ``Client(proxies=...)``); a replacement stated
# in quotes, or with alternatives ("Use 'proxy' or 'mounts' instead."), is a stated name
# (``library_names``, and whether find_renamed runs); and a parameter in the place of a removed
# one is no rename (``renamed``, ``suggestions``) when a version note in the new docstring says
# the one was added or the other removed (_documented_apart: click 8.2's ``CliRunner``). Also
# ``call_form_only``: the keyword-passable parameters that only a deprecated ``@overload`` takes
# (mcp 2.2's ``Server.__init__``), so that a note can name the deprecated call form instead of
# its signature and a call site is in the old form only when it passes one of them; [] when the
# form differs by a positional-only parameter or a ``*args`` / ``**kwargs`` (cachetools'
# ``cached(cache, key, lock, info, /)``), which no keyword names. A
# ``**kwargs: Unpack[SomeTypedDict]`` takes the TypedDict's keys (its bases' included) and no
# other keyword, so a key the release at the cutoff had and the pinned one has not is a removed
# parameter (issue #60: pandas 3's ``read_csv`` overloads take ``**kwds: Unpack[_read_shared]``
# and lost ``delim_whitespace``), and so is a key of the old TypedDict the new signature does
# not name; a bare ``**kwargs``, or a TypedDict open to other keys (PEP 728), still takes
# anything, and ``self``/``cls`` are no keywords (_Differ.keyword_names,
# _typed_kwargs_removals). A change to a method or attribute carries the paths of the public
# classes that inherit it without overriding it (``import_paths``, ``also``:
# ``pandas.DataFrame.fillna`` for ``NDFrame.fillna``), so the scan matches calls on them
# (_Differ.inheritors), and a change to a method of a private class is reported under the
# shortest public inheritor (_Differ._public_inheritor). An attribute the old class set in a
# method and the new constructor hands to a base outside the package (fastapi 0.143's
# ``Param(FieldInfo)`` passes ``kwargs["deprecated"]`` on) is unknown, not removed, and the
# unread entries say which class inherits from which base (UNREAD_BASE; unread_text); one the
# new class sets in a tuple target or through a function of its module is there
# (_Differ._attributes_set). ``tier``: whether the changed API is public (reached from the
# top-level package, a wildcard-copied name included, named in an ``__all__``, re-exported as
# ``X as X``, or documented) or internal (_Differ.tier_of); the reports count public changes
# and list internal ones on request. Also, a
# module that takes its names from another distribution with ``from x import *`` (mcp 2.3's
# ``mcp/types/__init__.py`` is ``from mcp_types import *``, and mcp requires mcp-types) is
# compared with that distribution read next to the release (engine.Sibling: load_api's
# ``extra_roots``), under the paths that re-export the names (``mcp.types.Tool``); and an
# object the new release only re-exports from elsewhere is compared too, where griffe stops at
# the alias (_Differ._realiased, _pair_breakages: a class's members, inherited ones included, a
# function's parameters, a change of kind), under the re-exporting path, so ``Tool.inputSchema`` ->
# ``input_schema`` is a removal under ``mcp.types.Tool`` and a parameter ``mcp_types.Tool``'s
# method lost is a ``param_removed`` of ``mcp.types.Tool``. Only a star import that runs makes
# a sibling (engine._star_imported: not one in a docstring, a function or an ``if
# TYPE_CHECKING:`` block). The diff's cache key has the distributions read next to each release
# (Engine._diff_key), which the cache keeps per release (Engine._siblings_key).
# 22: a deprecation announced by a ``warnings.warn`` in a public function's or method's body
# (issue #53: pandas 2.3.1 -> 3.0.6 had 0 deprecations; pandas warns in its methods), with a
# DeprecationWarning, PendingDeprecationWarning, FutureWarning or a subclass the package defines
# (pandas 3's ``Pandas4Warning``), called directly or through a helper of the package whose
# name says "deprecat" or "warn" and that warns unconditionally (``_deprecate(msg)``), which the
# release at the cutoff did not issue: ``deprecated_by`` "warnings.warn" and ``warning`` (when
# it warns: at entry, the function or class is deprecated; under ``if p is not <default>``,
# ``"p" in kwargs`` and the like, the parameter is; under another condition, the function is,
# under that condition), the message read from a literal, an f-string's literal parts or a
# constant of the module (_Differ._warning_deprecations).
DIFF_SCHEMA = 22

# Above this many removals in one package the release is a rewrite. Looking for similarly
# named replacements (difflib over every owner's members) then costs minutes and adds little.
FUZZY_LIMIT = 2000

# The parameters through which an SDK method sends request fields it has no parameter for.
REQUEST_EXTRAS = ("extra_body", "extra_query")

# The longest deprecated call form (an ``@overload``'s signature, APIChange.call_form) that a
# note or a report quotes as it is; a longer one is named by the parameters only it takes
# (``call_form_only``), or not at all.
CALL_FORM_LIMIT = 120

REMOVED = "removed"
MOVED = "moved"
PARAM_REMOVED = "param_removed"
PARAM_REQUIRED = "param_required"
PARAM_KEYWORD_ONLY = "param_keyword_only"
PARAM_POSITIONAL_ONLY = "param_positional_only"
KIND_CHANGED = "kind_changed"
DEPRECATED = "deprecated"
# The package requires another distribution instead of one its older release required, and its
# public API names the new one's types where it named the old one's (``_dependency_switches``).
DEPENDENCY_SWITCHED = "dependency_switched"

# APIChange.tier: whether the changed API is one the package presents (TIER_PUBLIC) or an
# internal one its public API does not lead to (TIER_INTERNAL). The reports count the public
# changes and show how many internal ones there are.
TIER_PUBLIC = "public"
TIER_INTERNAL = "internal"
# An entry of PackageScan.unread (diff_sources_with_unread) for a class with a base outside the
# package whose attributes were not compared: ``"fastapi.routing.APIRoute inherits from
# starlette.routing.Route"``. The other entries name compiled modules.
UNREAD_BASE = " inherits from "

# Where APIChange.hint comes from (APIChange.hint_source).
HINT_DECORATOR = "decorator"  # the old version's deprecation decorator
HINT_DOCSTRING = "docstring"  # a line of the old docstring that says "deprecated" (and names it)
HINT_PARAM_DOC = "param_doc"  # the removed parameter's own entry in the old docstring
HINT_WARNING = "warnings.warn"  # the old code's warnings.warn text when the parameter is passed

# APIChange.warning["when"]: when the pinned release's ``warnings.warn`` that deprecates the API
# is issued (_Differ._warning_deprecations).
WARNS_ALWAYS = "always"  # at every call: the function (a class, for its __init__ or __new__)
WARNS_PARAMETER = "parameter"  # when the caller passes the parameter (APIChange.parameter)
# When the caller leaves the parameter out (APIChange.parameter): its default is deprecated, or
# changes (``if n_init == "warn":``).
WARNS_DEFAULT = "default"
WARNS_CONDITION = "condition"  # under another condition, which ``warning["condition"]`` gives
# When the name is read (a property, a name a module's ``__getattr__`` serves) or set (a
# property's setter): ``warning["access"]`` says which.
WARNS_ACCESS = "access"
# The warning categories that announce a deprecation, and the module-level ``warn`` functions.
_DEPRECATION_CATEGORIES = ("DeprecationWarning", "PendingDeprecationWarning", "FutureWarning")
_WARN_FUNCTIONS = ("warnings.warn", "warnings.warn_explicit")

KIND_PRIORITY = {
    DEPENDENCY_SWITCHED: 0,
    REMOVED: 0,
    MOVED: 0,
    PARAM_REMOVED: 1,
    PARAM_REQUIRED: 2,
    KIND_CHANGED: 3,
    DEPRECATED: 4,
    PARAM_KEYWORD_ONLY: 5,
    PARAM_POSITIONAL_ONLY: 5,
}

_PRIVATE_EXEMPT = {
    "__init__",
    "__call__",
    "__enter__",
    "__exit__",
    "__aenter__",
    "__aexit__",
    "__iter__",
}
_SKIP_SEGMENTS = {
    "tests",
    "_vendor",
    "vendor",
    "vendored",
    "_vendored",
    "extern",
    "conftest",
}
# Modules that are never API. Only module segments are checked (``FieldInfo.examples`` is API).
_NON_API_MODULES = {"benchmark", "benchmarks", "example", "examples"}
# Stand-ins used when an optional backend is missing (transformers' ``utils.dummy_pt_objects``).
_PLACEHOLDER_MODULE = re.compile(r"dummy_\w*objects")
# Test helpers are API only directly under the top-level package (``pandas.testing``).
_TEST_MODULES = {"test", "testing"}
# A code generator's template, when the module generated from it is beside it (_is_template).
_TEMPLATE_MODULE = re.compile(r"modular_(\w+)")
_OWNER_NORMALIZE = re.compile(r"^(Async)|(With(Raw|Streaming)Response)$")
# A module segment that holds an SDK's beta mirror of its API (``resources.beta``, ``v1beta1``).
_BETA_SEGMENT = re.compile(r"(?:v\d+)?beta\d*")
_PEP702 = ("typing_extensions.deprecated", "warnings.deprecated", "typing.deprecated")
# Name parts of decorators that deprecate a parameter rather than the whole function
# (``deprecate_renamed_parameter``, ``deprecate_kwarg``, ``deprecate_nonkeyword_arguments``).
_PARAMETER_WORDS = {"param", "params", "parameter", "parameters", "arg", "args", "kwarg"}
_PARAMETER_WORDS |= {"kwargs", "argument", "arguments", "keyword", "nonkeyword", "positional"}
_VERSION = re.compile(r"v?\d+(\.\d+)*\w*")
_OLD_NAME_KEYS = ("old_name", "old_arg_name", "old_param", "old")
_NEW_NAME_KEYS = ("new_name", "new_arg_name", "new_param", "new")
# Module-level names that are never API: the typing flag, and values built by calls that give
# every module its own object (a logger per module, a type variable per module).
_NEVER_API = {"TYPE_CHECKING"}
_PER_MODULE_CALLS = {"getLogger", "get_logger", "TypeVar", "ParamSpec", "TypeVarTuple"}
# Standard-library modules whose import has side effects; their classes are never looked up.
_NO_IMPORT = {"antigravity", "this", "idlelib", "turtle", "turtledemo", "tkinter", "__main__"}
_POSITIONAL = ("positional_only", "positional_or_keyword")
_VARIADIC = ("var_positional", "var_keyword")
# Annotations of plain data: an attribute declared with one of these is not callable.
_DATA_TYPES = {"int", "float", "complex", "str", "bytes", "bool", "None", "list", "dict", "set"}
_DATA_TYPES |= {"tuple", "frozenset", "bytearray", "Literal", "List", "Dict", "Set", "Tuple"}
# Subscripted forms that make a type, not something to call.
_TYPE_FORMS = {"Union", "Optional", "Literal", "Annotated", "Callable", "Type", "ClassVar"}
_TYPE_FORMS |= {"Final", "Required", "NotRequired", "ReadOnly"}
# ``**kwargs: Unpack[SomeTypedDict]`` (PEP 692): the keys of the TypedDict are the keywords.
_UNPACK = {"typing.Unpack", "typing_extensions.Unpack"}
# Bases of a TypedDict that add no keys: ``TypedDict`` itself, ``Generic[T]``, ``Protocol``.
_TYPING_MODULES = ("typing.", "typing_extensions.")


@dataclass
class APIChange:
    package: str
    from_version: str
    to_version: str
    kind: str
    path: str
    name: str
    owner: str | None = None
    parameter: str | None = None
    old_signature: str | None = None
    new_signature: str | None = None
    old_doc: str | None = None
    new_doc: str | None = None
    moved_to: str | None = None
    deprecation: str | None = None
    hint: str | None = None
    suggestions: list[str] = field(default_factory=list)
    occurrences: int = 1
    also: list[str] = field(default_factory=list)
    # KIND_CHANGED: griffe kinds ("class", "function", "attribute", "type alias", "module"),
    # with "property" for attribute-like functions.
    old_kind: str | None = None
    new_kind: str | None = None
    # REMOVED: public path of an unrelated object that has the same name in the new version.
    namesake: str | None = None
    # DEPRECATED: the library's own decorator that marks it (not PEP 702, so type checkers
    # do not report uses of it).
    deprecated_by: str | None = None
    # DEPRECATED: the one call form (an ``@overload``) that is deprecated, when the function's
    # other overloads are not: pydantic's ``with_config(*, config: ConfigDict)``.
    call_form: str | None = None
    # DEPRECATED with ``call_form``: the parameters that only the deprecated overload takes,
    # none of the other overloads having them (mcp 2.2's ``Server.__init__``:
    # ``on_set_logging_level``, ``on_roots_list_changed`` and ``on_progress``), in the order of
    # its signature, when every one of them can be passed by keyword. A call is in the
    # deprecated form when it passes one of them (the notes name them; selection.form). []
    # when the overloads differ otherwise: by a type, or by a positional-only parameter, an
    # ``*args`` or a ``**kwargs`` (cachetools' ``cached(cache, key, lock, info, /)``), which no
    # keyword names; None without ``call_form``, and in a diff made before DIFF_SCHEMA 21.
    call_form_only: list[str] | None = None
    # Every public path that leads to the changed object, in either version: where it is
    # defined and where it is re-exported (``pkg.fetch`` for ``pkg.dl.fetch``); for a class
    # member, each path of its class with the member's name. ``path`` and ``also`` included,
    # and none left out, unlike ``also``. For a removal, only the paths that no longer lead to
    # it (a re-export dropped while the object stays elsewhere). None in a diff made before
    # DIFF_SCHEMA 12, which did not record them (selection then matches by name).
    import_paths: list[str] | None = None
    # Where ``hint`` comes from (HINT_*: its first part when it joins several); None without.
    hint_source: str | None = None
    # MOVED: what find_moved compared to take the object at ``moved_to`` for the same one:
    # ``{"compared": "class" | "module" | "function" | "value" | "message", "kept": k, "of": n}``,
    # k of the n public names (a class's or module's) or public parameters (a function's) still
    # there; the value is the same literal (1 of 1); ``of`` is 0 when there was nothing to count.
    # With ``"renamed": true`` (DIFF_SCHEMA 15), the object at ``moved_to`` has another name
    # (find_renamed). For a module (DIFF_SCHEMA 16), ``"left_behind"`` is how many public names
    # its old parent package still has in the new version: 0 when the package is gone, or a
    # module with no API (mcp 2's ``mcp/server/fastmcp.py``). ``"compared": "package"`` is not
    # the diff's: selection.collapse makes it for a subpackage whose modules all moved to the
    # same new parent, leaving nothing behind (``"of"`` counts them).
    move_evidence: dict[str, Any] | None = None
    # REMOVED or MOVED, for a module-level name bound to a method of an instance the module
    # makes (huggingface_hub 1's ``api = HfApi()`` and ``duplicate_space = api.duplicate_space``
    # in ``hf_api.py``): the public path of that method (``huggingface_hub.hf_api.HfApi
    # .duplicate_space``). The change takes the method's deprecation text (``hint``,
    # ``library_names``) when it has none of its own, and selection.collapse lists it with the
    # method's own removal as one API. None otherwise, and in a diff made before DIFF_SCHEMA 16.
    alias_of: str | None = None
    # PARAM_REMOVED: where the new version's source still reads the parameter by name
    # (``kwargs.pop("x"``, ``.get("x"``, ``"x" in kwargs``) in a decorator of the callable, as
    # ``pkg/module.py:line``. Calls passing it may then still run, with a warning, although
    # the signature (and a type checker) rejects it. Nothing of the library is run to know.
    still_handled_at: str | None = None
    # PARAM_REMOVED: what that code says of the parameter, in the new version: its entry in the
    # docstring of the function that handles it, else its warnings.warn text (huggingface_hub
    # 2.0's ``resume_download``: "deprecated without replacement. ..."). None without.
    still_handled_text: str | None = None
    # The names that the library's own deprecation text for this change (``hint``, from the
    # old version, or ``deprecation``, from the new one) gives and that exist in the new
    # version: a parameter of the new callable (for a parameter change: ``stop`` for
    # huggingface_hub's ``text_generation(stop_sequences=...)``), or the public path of a
    # function or class of the new API. Code-like names first, then in the text's order. []
    # when the text names nothing that exists, or there is no text; None in a diff made before
    # DIFF_SCHEMA 13. Resolved statically, from the sources: nothing is imported or run.
    library_names: list[str] | None = None
    # The file ``hint`` was read from, in the old version
    # (``huggingface_hub/inference/_client.py``), and the file ``deprecation`` was read from, in
    # the new one: where a note's replacement is named (notes.Replacement.source).
    hint_file: str | None = None
    deprecation_file: str | None = None
    # PARAM_REMOVED: ``suggestions`` holds the parameter's new name, taken from a positional
    # parameter renamed in place (the same position, and the same annotation where both have
    # one). Without it, ``suggestions`` are names that merely look similar (not confirmed as
    # replacements). False in a diff made before DIFF_SCHEMA 13.
    renamed: bool = False
    # PARAM_REMOVED: which of ``extra_body`` and ``extra_query`` the new callable takes (an
    # SDK method that sends a request, as every Stainless-generated client's do: fields it
    # has no parameter for go there). [] when neither; None in a diff made before
    # DIFF_SCHEMA 17. Whether the API itself still takes the removed field is not known.
    request_extras: list[str] | None = None
    # DEPENDENCY_SWITCHED (DIFF_SCHEMA 18; ``name`` is the distribution no longer required,
    # ``path`` the package's import name): the evidence, from both releases' Requires-Dist
    # and public APIs. ``old`` / ``new`` (canonical distribution names), ``old_modules`` /
    # ``new_modules`` (the module names looked for), ``old_requirement`` / ``new_requirement``
    # (as Requires-Dist lists them, without markers; ``new_requirement`` None for a copy the
    # package ships), ``old_in_extras`` (the pinned release's extras that still list ``old``),
    # ``extras`` (DIFF_SCHEMA 19: the extras whose requirements switched, both releases
    # defining them; [] for the base requirements), ``new_added`` (``new`` is not among the
    # older release's requirements it is compared with: gradio 3.45 already required
    # ``httpx``), ``vendored`` (``new`` is a module the package ships, ``typer._click``) with
    # ``public_names`` (the package's top-level names that are switched names:
    # ``typer.Context``), ``sites`` (places of the public API, a parameter, a return, an
    # attribute, a base class or its type argument, or a re-export, that named ``old`` types
    # and name ``new`` types), ``sites_same_name`` (of them, those where a type kept its name:
    # ``httpx.Client`` -> ``httpx2.Client``), ``names`` (those names), ``parameters``
    # (switched parameters and how many signatures each is in) and ``parameter_types`` (by
    # parameter, ``{"new", "direct", "count"}`` for each type those signatures name now,
    # ``direct`` when it is the annotation or a member of its union), ``examples``, ``bases``
    # and ``reexports`` (a few of the sites; ``bases_count`` all the base classes), ``calls``
    # (``[path, parameter, position]`` of each switched parameter of a constructor, a function,
    # or a classmethod or staticmethod on its class, under every public path, to match the
    # project's code; ``position`` counts from 0 after ``self`` / ``cls`` and is None for a
    # keyword-only parameter; a diff made earlier in schema 19 has ``[path, parameter]``),
    # ``still_named_at`` (where the pinned release's source still imports ``old`` or has a
    # string that is its name, other than a name it only exports, ``pkg/module.py:line``;
    # nothing is run) and ``still_named_count``. None for every other kind.
    dependency: dict[str, Any] | None = None
    # TIER_PUBLIC when the API the change is to (the object; for a member or a parameter, its
    # class or callable) is one the package presents: reached from the top-level package
    # (``pkg.Thing``), named in the ``__all__`` of a module it is reached through, or
    # documented (a docstring, at a path with no private part) in either version; for a
    # member, when a public class inherits it. TIER_INTERNAL otherwise (fastapi's
    # ``dependencies.utils.get_flat_dependant``, uvicorn's ``config.LOOP_SETUPS``): the reports
    # count the public changes and keep the internal ones apart (``scan --internal``, the MCP
    # tools' ``include_internal``); results.json has both. None in a diff made before
    # DIFF_SCHEMA 21.
    tier: str | None = None
    # DEPRECATED by a ``warnings.warn`` the pinned release's code issues and the release at the
    # cutoff did not (``deprecated_by`` is "warnings.warn"; DIFF_SCHEMA 22): ``{"when":
    # WARNS_ALWAYS | WARNS_PARAMETER | WARNS_DEFAULT | WARNS_CONDITION | WARNS_ACCESS,
    # "category": "FutureWarning", "at": "pandas/core/frame.py:6950"}``. ``"access"`` ("read",
    # "set", "delete") for WARNS_ACCESS. ``"condition"`` when it is issued in some of those
    # calls only (always for WARNS_CONDITION): the tests it is issued under besides the one that
    # says the parameter is passed or left out, joined with ``and``, as code, at most 120
    # characters; None when they cannot be quoted (a loop, an ``except``, longer); with
    # ``"readable": True`` when they read nothing but the function's parameters (the notes and
    # describe() quote them then). ``"behavior": True`` when the message says a result or a
    # default changes, not that the API goes ("The behavior of ... is deprecated, in a future
    # version this will return a Series"): a call of it is "uses this API". ``"also"``: the
    # other warnings of the same API and parameter (each a dict like this one, without
    # ``also``), whose messages ``deprecation`` gives too. A deprecated name a module's
    # ``__getattr__`` serves (deprecated_by "__getattr__") has one too (WARNS_ACCESS, "read").
    # A type checker sees none of them: the call warns at run time. None for every other change.
    warning: dict[str, Any] | None = None

    @property
    def id(self) -> str:
        if self.kind == DEPENDENCY_SWITCHED:
            return stable_hash(self.package, self.kind, self.name, self.switched_to)
        return stable_hash(self.package, self.kind, self.path, self.parameter, self.moved_to)

    @property
    def switched_to(self) -> str | None:
        """DEPENDENCY_SWITCHED: the distribution the pinned release requires instead of
        ``name`` (``httpx2`` for ``httpx``); None for another kind."""
        if self.kind != DEPENDENCY_SWITCHED:
            return None
        return str((self.dependency or {}).get("new") or "") or None

    @property
    def fingerprint(self) -> str:
        """Identity of the change independent of the exact version pair (used to reuse tasks)."""
        return stable_hash(self.package, self.kind, self.group_key, self.parameter)

    @property
    def module(self) -> str:
        return self.path.rsplit(".", 2 if self.owner else 1)[0]

    @property
    def group_key(self) -> str:
        """Identity used to merge sync/async twins and response wrappers in the same module."""
        if self.kind == DEPENDENCY_SWITCHED:
            return f"{self.kind}:{self.name}:{self.switched_to or ''}"
        owner = _OWNER_NORMALIZE.sub("", self.owner or "")
        return f"{self.kind}:{self.module}:{owner}.{self.name}:{self.parameter or ''}"

    @property
    def concept_key(self) -> str:
        """Coarser identity used to avoid probing near-duplicates (e.g. beta mirrors of an API)."""
        if self.kind == DEPENDENCY_SWITCHED:
            return f"{self.package}:{self.group_key}"
        kind = REMOVED if self.kind == MOVED else self.kind
        owner = _OWNER_NORMALIZE.sub("", self.owner or "")
        return f"{self.package}:{kind}:{owner}.{self.name}:{self.parameter or ''}"

    @property
    def api_key(self) -> str:
        """Identity of the API the change is to: one callable, class, attribute or module, by
        package, module, normalised owner (sync/async twins and response wrappers, which live
        in the same module, are one) and name. ``Messages.create`` losing three parameters is
        one API with three changes; ``anthropic.resources.beta.messages.messages.Messages.create``
        is another API than ``anthropic.resources.messages.messages.Messages.create``. A
        dependency switch is an API of its own: ``openai:dependency:httpx->httpx2``."""
        if self.kind == DEPENDENCY_SWITCHED:
            return f"{self.package}:dependency:{self.name}->{self.switched_to or ''}"
        owner = _OWNER_NORMALIZE.sub("", self.owner or "")
        return f"{self.package}:{self.module}:{owner}.{self.name}"

    @property
    def in_beta(self) -> bool:
        """Whether the change is to an API under a ``beta`` module (``client.beta.messages``
        in anthropic's and openai's SDKs): a mirror of an API with the same class and name,
        which code reaches only through ``beta``."""
        return any(is_beta_segment(s) for s in self.module.split(".")[1:])

    @property
    def display(self) -> str:
        if self.kind == DEPENDENCY_SWITCHED:
            return f"{self.name} -> {self.switched_to or '?'}"
        target = f"{self.owner}.{self.name}" if self.owner else self.path
        if self.parameter and self.kind in (
            PARAM_REMOVED,
            PARAM_REQUIRED,
            PARAM_KEYWORD_ONLY,
            PARAM_POSITIONAL_ONLY,
            DEPRECATED,
        ):
            return f"{target}({_argument(self.parameter)})"
        return target

    @property
    def short_path(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else self.path

    @property
    def renamed_to(self) -> str | None:
        """MOVED: the new name of an object that came back under another name (``MCPServer``
        for ``FastMCP``), from find_renamed's evidence; None for a move that keeps the name."""
        if self.kind != MOVED or not self.moved_to or not (self.move_evidence or {}).get("renamed"):
            return None
        return self.moved_to.rsplit(".", 1)[-1]

    @property
    def is_package_move(self) -> bool:
        """MOVED: a whole subpackage moved to a new parent (selection.collapse groups the moves
        of its modules into one change), not one object."""
        return self.kind == MOVED and (self.move_evidence or {}).get("compared") == "package"

    def describe(self, *, short: bool = False, versioned: bool = True) -> str:
        """One human sentence describing the change (``versioned=False`` drops "(pkg 1.2)")."""
        pkg = f" ({self.package} {self.to_version})" if versioned else ""
        if self.kind == DEPENDENCY_SWITCHED:
            return self._describe_switch(versioned)
        path = self.short_path if short else self.path
        call = f"{path}({_argument(self.parameter)})" if self.parameter else path
        if self.kind == PARAM_REMOVED and self.parameter and self.parameter.startswith("*"):
            what = "keyword" if self.parameter.startswith("**") else "positional"
            return (
                f"`{call}`: `{self.parameter}` was removed, so extra {what} arguments are no "
                f"longer accepted{pkg}"
            )
        if self.kind == MOVED:
            if self.is_package_move:
                count = (self.move_evidence or {}).get("of")
                what = f" ({count} modules and classes)" if count else ""
                return f"the package `{path}` moved to `{self.moved_to}`{what}{pkg}"
            renamed = self.renamed_to
            new_name = f" (`{self.name}` is now `{renamed}`)" if renamed else ""
            return f"`{path}` moved to `{self.moved_to}`{new_name}{pkg}"
        if self.kind == REMOVED:
            other = (
                f"; a different `{self.name}` now exists at `{self.namesake}`"
                if self.namesake
                else ""
            )
            return f"`{path}` was removed{pkg}{other}"
        if self.kind == PARAM_REMOVED:
            return f"`{call}`: parameter `{self.parameter}` was removed{pkg}"
        if self.kind == PARAM_REQUIRED:
            return f"`{call}`: parameter `{self.parameter}` is now required{pkg}"
        if self.kind == PARAM_KEYWORD_ONLY:
            return f"`{call}`: `{self.parameter}` is now keyword-only{pkg}"
        if self.kind == PARAM_POSITIONAL_ONLY:
            return f"`{path}`: `{self.parameter}` is now positional-only{pkg}"
        if self.kind == KIND_CHANGED:
            if self.old_kind and self.new_kind and self.old_kind != self.new_kind:
                return f"`{path}` changed from {self.old_kind} to {self.new_kind}{pkg}"
            return f"`{path}` changed kind{pkg}"
        if self.kind == DEPRECATED:
            meaningful = self.deprecation and self.deprecation.strip(" .").lower() != "deprecated"
            extra = f": {self.deprecation}" if meaningful else ""
            if self.warning:
                return self._describe_warning(path, call, pkg, extra)
            if self.parameter:
                return f"`{call}`: parameter `{self.parameter}` is deprecated{pkg}{extra}"
            if self.call_form_only:
                named = [f"`{p}=`" for p in self.call_form_only]
                keywords = f"{', '.join(named[:-1])} or {named[-1]}" if named[1:] else named[0]
                return f"`{path}` called with {keywords} is deprecated{pkg}{extra}"
            if self.call_form and len(self.call_form) <= CALL_FORM_LIMIT:
                return f"`{path}` called as `{self.call_form}` is deprecated{pkg}{extra}"
            if self.call_form:
                return f"`{path}`: one call form (an overload) is deprecated{pkg}{extra}"
            return f"`{path}` is deprecated{pkg}{extra}"
        return f"`{path}` changed{pkg}"

    def _describe_warning(self, path: str, call: str, pkg: str, extra: str) -> str:
        """DEPRECATED by a ``warnings.warn`` (APIChange.warning): ``` `pkg.f(x)`: parameter `x`
        is deprecated, with a FutureWarning at run time (pkg 2): ...```. A condition is quoted
        only when it reads nothing but the function's parameters (``warning["readable"]``):
        the library's other names mean nothing to the reader."""
        w = self.warning or {}
        category = w.get("category") or "warning"
        runtime = f", with {_a(category)} {category} at run time"
        when = w.get("when")
        condition = w.get("condition") if w.get("readable") else None
        some = f" when `{condition}`" if condition else " in some calls"
        partial = "condition" in w
        behavior = bool(w.get("behavior"))
        p = self.parameter
        if when == WARNS_DEFAULT and p:
            changes = "will change" if behavior else "is deprecated"
            only = some if partial else ""
            return (
                f"`{call}`: the default of `{p}` {changes}{only}, with {_a(category)} {category} "
                f"at run time when it is left out{pkg}{extra}"
            )
        if p:
            what = (
                f"passing `{p}` will change behavior"
                if behavior
                else f"parameter `{p}` is deprecated"
            )
            return f"`{call}`: {what}{some if partial else ''}{runtime}{pkg}{extra}"
        verb = "will change behavior" if behavior else "is deprecated"
        if when == WARNS_ACCESS:
            access = {"set": "setting ", "delete": "deleting "}.get(str(w.get("access")), "")
            only = (some if condition else " in some cases") if partial else ""
            return f"{access}`{path}` {verb}{only}{runtime}{pkg}{extra}"
        if when == WARNS_CONDITION:
            return f"`{path}` {verb}{some}{runtime}{pkg}{extra}"
        return f"`{path}` {verb}{runtime}{pkg}{extra}"

    def _describe_switch(self, versioned: bool) -> str:
        """DEPENDENCY_SWITCHED: ``openai 3.19.2 requires `httpx2` instead of `httpx```; for
        extras only, ``fastmcp-slim 4.0.10's `client` and `server` extras require ...``; for a
        distribution the older release already required, ``gradio 4.28.3 no longer requires
        `requests`; its API names `httpx` types instead``; for a copy the package ships, ``typer
        0.27.2 no longer requires `click` and ships `typer._click` instead``."""
        d = self.dependency or {}
        old, new = self.name, self.switched_to or "?"
        who = f"{self.package} {self.to_version}" if versioned else self.package
        extras = [f"`{e}`" for e in d.get("extras") or ()]
        many = len(extras) > 1
        if extras:
            listed = ", ".join(extras[:-1]) + f" and {extras[-1]}" if many else extras[0]
            who = f"{who}'s {listed} extra{'s' if many else ''}"
        s = "" if many else "s"
        if d.get("vendored"):
            ships = f", and {self.package} ships" if extras else " and ships"
            return f"{who} no longer require{s} `{old}`{ships} `{new}` instead"
        if d.get("new_added", True):
            return f"{who} require{s} `{new}` instead of `{old}`"
        return f"{who} no longer require{s} `{old}`; its API names `{new}` types instead"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> APIChange:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def is_beta_segment(segment: str) -> bool:
    """A module (or attribute) name that holds an SDK's beta mirror of its API: ``beta``,
    ``v1beta1``."""
    return _BETA_SEGMENT.fullmatch(segment) is not None


def _a(word: str) -> str:
    """The article for a warning category's name: ``a FutureWarning``, ``an
    ArrowDeprecationWarning``."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def _argument(parameter: str | None) -> str:
    """How a call passes ``parameter``: ``name=...``, or ``*args`` / ``**kwargs`` as written."""
    return parameter if parameter and parameter.startswith("*") else f"{parameter}=..."


# --------------------------------------------------------------------- loading
@cache
def griffe_version() -> str:
    """The version of the griffe that reads the sources. A diff is griffe's reading as much as
    this module's, so cached diffs are keyed on it too. griffe 2 ships its code in the
    ``griffelib`` distribution (``griffe`` is a meta-package that requires it)."""
    for distribution in ("griffelib", "griffe"):
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return "unknown"


def load_api(import_name: str, root: Path, extra_roots: Sequence[Path] = ()) -> Any:
    """Load one top-level package statically. ``foo-stubs`` directories load as ``foo``.

    ``extra_roots`` are the source trees of the distributions the package takes names from with
    ``from x import *`` (engine.Sibling): griffe expands such an import only from a package it
    can find, so with them ``mcp.types`` has mcp-types' names, and without, a placeholder."""

    # griffe warns about every annotation it cannot resolve in third-party code; that is noise
    # for our purpose (and would spill into the user's terminal from worker processes).
    logging.getLogger("griffe").setLevel(logging.CRITICAL + 1)

    stubs = import_name.endswith("-stubs")
    name = import_name[: -len("-stubs")] if stubs else import_name
    if not stubs and _stubs_only(root, name):  # pandas-stubs' import name is pandas
        stubs = True
    with _quiet():
        loader = _loader(root, extra_roots)
        module = loader.load(name, try_relative_path=False, find_stubs_package=stubs)
        if extra_roots:
            try:
                loader.expand_wildcards(module, external=True)
            except Exception as exc:  # a sibling griffe cannot read leaves the placeholder
                log.debug("cannot expand the star imports of %s: %s", name, exc)
    _alias_class_assignments(module)
    _mark_reexports(module)
    return module


def _loader(root: Path, extra_roots: Sequence[Path] = ()) -> Any:
    """griffe's loader, keeping what it drops when it merges a ``.pyi`` stub into its module.

    Functions declared only through ``@overload`` (the usual case in a stub, which has no
    implementation) become members. Names that only a stub declares stay available at runtime:
    griffe marks them otherwise, which leaves them out of ``from x import *`` (qdrant-client's
    ``grpc`` package, whose modules build their classes dynamically and declare them in stubs,
    would look empty).

    A package's modules load by depth and then by name: griffe sorts them by depth only and
    leaves the rest to the file system (``os.walk``), so an object that two sibling modules
    import from a private one took its public path from the machine's directory order. mcp 1.28's ``McpHttpClientFactory`` was ``mcp.client.sse.McpHttpClientFactory`` on
    one machine and ``mcp.client.streamable_http.McpHttpClientFactory`` on another, and only
    ``sse`` still imports it in 2.2, so its switch to ``httpx2`` had 23 places or 20.
    """
    import griffe

    class Finder(griffe.ModuleFinder):
        def submodules(self, module: Any) -> list[Any]:
            found = super().submodules(module)
            return sorted(found, key=lambda sub: (len(sub[0]), sub[0], str(sub[1])))

    declared: list[Any] = []

    class Stubs(griffe.Extension):
        def on_module_members(self, *, mod: Any, **kwargs: Any) -> None:
            _promote_overloads(mod)
            if str(getattr(mod, "filepath", "")).endswith(".pyi"):
                declared.extend(
                    m
                    for m in mod.members.values()
                    if not m.is_alias and getattr(m, "runtime", True)
                )

        def on_class_members(self, *, cls: Any, **kwargs: Any) -> None:
            _promote_overloads(cls)

    class Loader(griffe.GriffeLoader):
        def expand_wildcards(self, obj: Any, **kwargs: Any) -> None:
            if kwargs.get("seen") is None:  # the first call, for the whole package
                for member in declared:
                    member.runtime = True
            super().expand_wildcards(obj, **kwargs)

    search_paths = [str(root), *(str(r) for r in extra_roots)]
    loader = Loader(
        extensions=griffe.load_extensions(Stubs()),
        search_paths=search_paths,
        allow_inspection=False,
        store_source=True,
    )
    loader.finder = Finder(search_paths)  # what griffe makes from the same search paths
    return loader


def _promote_overloads(container: Any) -> None:
    """Make functions that exist only as ``@overload`` signatures members, like any other.

    griffe keeps overload signatures aside until it meets the implementation. A ``.pyi`` stub has
    none, so ``ndarray.partition`` or ``QuerySet.defer`` would look removed. The first signature
    stands for the function, with all of them as its ``overloads``.
    """
    try:
        for name, signatures in list((getattr(container, "overloads", None) or {}).items()):
            if signatures and name not in container.members:
                signatures[0].overloads = list(signatures)
                container.set_member(name, signatures[0])
    except Exception as exc:
        log.debug("cannot keep the overloads of %s: %s", getattr(container, "path", "?"), exc)


@contextmanager
def _quiet() -> Iterator[None]:
    """Silence what parsing old sources makes Python say ("invalid escape sequence").

    Those warnings are about the analysed package, not about this run, and would otherwise
    land on the user's terminal.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


def _alias_class_assignments(root: Any) -> None:
    """Turn module-level ``Name = OtherClass`` assignments into griffe aliases.

    griffe models ``PreTrainedTokenizer = PythonBackend`` as an attribute, so every class that
    inherits from ``PreTrainedTokenizer`` gets an empty MRO and all its inherited members look
    removed. As an alias, the base resolves to the class it names.
    """
    import griffe

    for _ in range(3):  # ``A = B`` after ``B = C`` resolves on the next pass
        changed = False
        for module in _walk_modules(root):
            for name, member in list(module.members.items()):
                if getattr(member, "is_alias", False) or not getattr(member, "is_attribute", False):
                    continue
                value = getattr(member, "value", None)
                if not isinstance(value, (griffe.ExprName, griffe.ExprAttribute)):
                    continue
                try:
                    target = module.modules_collection.get_member(value.canonical_path)
                    if getattr(target, "is_alias", False):
                        target = target.final_target
                    public = member.is_public
                except Exception:
                    continue
                if not getattr(target, "is_class", False) or target.path == member.path:
                    continue
                alias = griffe.Alias(
                    name, target, lineno=member.lineno, endlineno=member.endlineno, parent=module
                )
                alias.public = public
                module.set_member(name, alias)
                changed = True
        if not changed:
            return


def _mark_reexports(root: Any) -> None:
    """Treat names re-exported by a public ``__init__`` without ``__all__`` as public.

    griffe follows the convention that imported names are private unless listed in
    ``__all__``, but ``from pkg._client import Client`` in ``pkg/__init__.py`` is how most
    libraries define their public API.
    """
    package = root.path.split(".")[0]
    for module in _walk_modules(root):
        if _has_private_segment(module.path) or not getattr(module, "is_init_module", False):
            continue
        if getattr(module, "exports", None) is not None:
            continue  # an explicit __all__ is authoritative
        for member in list(module.members.values()):
            if (
                getattr(member, "is_alias", False)
                and not member.name.startswith("_")
                and getattr(member, "public", None) is None
                and str(member.target_path).startswith(package + ".")
            ):
                member.public = True


def _module_exists(root: Path, import_name: str) -> bool:
    base = root.joinpath(*import_name.split("."))
    exists = base.is_dir() or base.with_suffix(".py").exists() or base.with_suffix(".pyi").exists()
    return exists or _stubs_only(root, import_name)


def _stubs_only(root: Path, import_name: str) -> bool:
    """Is ``import_name`` a stub-only package (``foo-stubs/``, PEP 561) in ``root``?"""
    top, _, rest = import_name.partition(".")
    base = root / f"{top}-stubs"
    if rest:
        base = base.joinpath(*rest.split("."))
    return not (root / top).exists() and (base.is_dir() or base.with_suffix(".pyi").exists())


def _compiled_owner(path: str, compiled: frozenset[str], *, module: bool = False) -> str | None:
    """The entry of ``compiled`` (SourceTree.compiled) that hides ``path`` from a static
    reading, or None. A compiled module hides itself and everything in it. A package whose
    ``__init__`` is compiled (``pkg.__init__``) hides the package and the names it defines
    (its direct members, unless ``module``), not its submodules, which have files of their
    own."""
    for entry in compiled:
        package = entry.removesuffix(".__init__")
        if package != entry:
            if path == package or (not module and path.rpartition(".")[0] == package):
                return entry
        elif path == entry or path.startswith(entry + "."):
            return entry
    return None


def _star_from_compiled(member: Any, compiled: frozenset[str]) -> bool:
    """griffe's placeholder for a ``from x import *`` it could not expand because x is now
    compiled (:func:`_star_from_outside` is the one for another package)."""
    if not (getattr(member, "is_alias", False) and member.name.endswith("/*")):
        return False
    return _compiled_owner(str(member.target_path), compiled, module=True) is not None


def diff_sources(
    package: str,
    old_version: str,
    old_root: Path,
    new_version: str,
    new_root: Path,
    import_names: list[str],
    *,
    old_requires: Sequence[str] = (),
    new_requires: Sequence[str] = (),
    new_compiled: Sequence[str] = (),
    old_roots: Sequence[Path] = (),
    new_roots: Sequence[Path] = (),
) -> list[dict[str, Any]]:
    """Diff two extracted source trees. Returns plain dicts (picklable across processes).

    ``old_requires`` / ``new_requires`` are the releases' Requires-Dist (SourceTree.requires):
    with them, a distribution the pinned release requires instead of one the older release
    required is a change of its own when the public API switched to its types
    (DEPENDENCY_SWITCHED).

    ``new_compiled`` are the modules the pinned release ships compiled, without a source or a
    stub (SourceTree.compiled): what a static reading cannot see there is not reported as
    removed (:func:`diff_sources_with_unread` also says which of them hid something).

    ``old_roots`` / ``new_roots`` are the source trees of the distributions each release takes
    names from with ``from x import *`` (engine.Sibling: mcp 2.3's ``mcp.types`` is mcp-types'
    names), so that those names are compared under the paths that re-export them.

    Never raises for problems in the analysed package: griffe can fail on unusual code, and one
    bad top-level module must not hide the changes found in the others.
    """
    return diff_sources_with_unread(
        package,
        old_version,
        old_root,
        new_version,
        new_root,
        import_names,
        old_requires=old_requires,
        new_requires=new_requires,
        new_compiled=new_compiled,
        old_roots=old_roots,
        new_roots=new_roots,
    )[0]


def diff_sources_with_unread(
    package: str,
    old_version: str,
    old_root: Path,
    new_version: str,
    new_root: Path,
    import_names: list[str],
    *,
    old_requires: Sequence[str] = (),
    new_requires: Sequence[str] = (),
    new_compiled: Sequence[str] = (),
    old_roots: Sequence[Path] = (),
    new_roots: Sequence[Path] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """:func:`diff_sources`, and the entries of ``new_compiled`` that hid something the older
    release had, so that its removal was not reported: the scan warns that changes to them,
    and to the names taken from them, are not reported (PackageScan.unread)."""
    unread: set[str] = set()
    with _quiet():
        changes = _diff_imports(
            package,
            old_version,
            old_root,
            new_version,
            new_root,
            import_names,
            old_requires=old_requires,
            new_requires=new_requires,
            new_compiled=new_compiled,
            unread=unread,
            old_roots=old_roots,
            new_roots=new_roots,
        )
    return [c.to_dict() for c in _group(changes)], sorted(unread)


def unread_text(package: str, version: str, cutoff_version: str, entries: Sequence[str]) -> str:
    """The scan's warning for a package whose diff could not compare everything: the entries
    of :func:`diff_sources_with_unread` (PackageScan.unread), which are the modules the pinned
    release ships compiled, without a source or a stub, and the classes whose attributes a base
    outside the package may set (UNREAD_BASE: ``"pkg.Route inherits from starlette.routing
    .Route"``). One warning per package, in one line."""
    modules = [e for e in entries if UNREAD_BASE not in e]
    bases = [e for e in entries if UNREAD_BASE in e]
    parts: list[str] = []
    if modules:
        one = len(modules) == 1
        what = "is a compiled module" if one else "are compiled modules"
        it = "it" if one else "them"
        parts.append(
            f"{', '.join(modules)} {what} without a .py source or a .pyi stub, unlike in "
            f"{cutoff_version}; since-cutoff does not run code, so changes to {it} and to the "
            f"names taken from {it} are not reported"
        )
    if bases:
        by_base: dict[str, list[str]] = {}
        for entry in bases:
            cls, _, base = entry.partition(UNREAD_BASE)
            by_base.setdefault(base, []).append(cls)
        clauses = [
            f"{_and(classes)} inherit{'s' if len(classes) == 1 else ''} from {base}"
            for base, classes in by_base.items()
        ]
        one = len(bases) == 1
        parts.append(
            f"{', and '.join(clauses)}, which since-cutoff did not read; the attributes "
            f"{'it sets' if one else 'they set'} are not compared"
        )
    return f"{package} {version}: " + ". ".join(parts)


def _and(items: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _diff_imports(
    package: str,
    old_version: str,
    old_root: Path,
    new_version: str,
    new_root: Path,
    import_names: list[str],
    *,
    old_requires: Sequence[str] = (),
    new_requires: Sequence[str] = (),
    new_compiled: Sequence[str] = (),
    unread: set[str] | None = None,
    old_roots: Sequence[Path] = (),
    new_roots: Sequence[Path] = (),
) -> list[APIChange]:
    """The changes of each import name; ``unread`` collects the entries of ``new_compiled``
    that hid a removal (see diff_sources_with_unread)."""
    changes: list[APIChange] = []
    compiled = frozenset(new_compiled)
    hidden: set[str] = set() if unread is None else unread
    # The trees are kept for the dependency pass only when a requirement went (a base one, or
    # one of an extra both releases define): in most release pairs none did (87% of 1991
    # measured for the base requirements), and the pass is skipped.
    dropped = _dropped_requirements(old_requires, new_requires, import_names, new_root)
    loaded: list[tuple[Any, Any]] = []
    for import_name in import_names:
        try:
            old = load_api(import_name, old_root, old_roots)
        except Exception as exc:
            log.debug("cannot load %s %s from %s: %s", package, old_version, import_name, exc)
            continue
        try:
            new = load_api(import_name, new_root, new_roots)
        except Exception as exc:
            owner = _compiled_owner(import_name, compiled, module=True)
            if owner is not None:
                hidden.add(owner)
            elif not _module_exists(new_root, import_name):
                changes.append(
                    APIChange(
                        package=package,
                        from_version=old_version,
                        to_version=new_version,
                        kind=REMOVED,
                        path=import_name,
                        name=import_name.rsplit(".", 1)[-1],
                        old_signature=f"module {import_name}",
                        old_doc=doc_summary(old),
                        import_paths=[import_name],
                        library_names=[],
                        tier=TIER_PUBLIC,
                    )
                )
            else:
                log.debug("cannot load %s %s from %s: %s", package, new_version, import_name, exc)
            continue
        if dropped:
            loaded.append((old, new))
        try:
            differ = _Differ(package, old_version, new_version, old, new, compiled=compiled)
            changes.extend(differ.run())
            hidden |= differ.unread
        except Exception as exc:
            log.debug("diff of %s failed: %s", import_name, exc)
    if dropped and loaded:
        try:
            changes.extend(
                _dependency_switches(
                    package,
                    old_version,
                    new_version,
                    loaded,
                    dropped,
                    old_requires,
                    new_requires,
                    new_root,
                )
            )
        except Exception as exc:
            log.debug("dependency pass of %s failed: %s", package, exc)
    return changes


class _Differ:
    def __init__(
        self,
        package: str,
        old_version: str,
        new_version: str,
        old: Any,
        new: Any,
        *,
        compiled: frozenset[str] = frozenset(),
    ) -> None:
        self.package = package
        self.old_version = old_version
        self.new_version = new_version
        self.old = old
        self.new = new
        # Modules the new release ships compiled, without a source or a stub (see diff_sources),
        # and those of them that hid a removal (_unreadable).
        self.compiled = compiled
        self.unread: set[str] = set()
        self._public_map: dict[str, str] | None = None
        self._new_index: dict[str, list[str]] | None = None
        self._old_names: set[str] | None = None  # see old_module_level_names
        # Every public path of each module-level object, per version (see _path_index).
        self._path_indexes: dict[int, dict[str, list[str]]] = {}
        # The names in the ``__all__`` of the modules below a module (see _listed_below).
        self._exported_below: dict[int, set[str]] = {}
        self._lost_bases: dict[str, bool] = {}
        # Removed inherited members, by the member they were inherited from; those of a
        # code generator's template (_in_template), which fold but are never shown.
        self._inherited: dict[str, list[Any]] = {}
        self._from_templates: set[int] = set()
        self._sources: dict[int, _Source] = {}
        # The parsed __getattr__ of the lazy module each module swaps in (see _served_lazily).
        self._lazy_hooks: dict[int, tuple[Any, ast.AST] | None] = {}
        self._tables: dict[tuple[int, str], set[str]] = {}
        # Per version, the public classes below each class (by the class's canonical path):
        # those whose MRO has it (see inheritors).
        self._inheritors: dict[int, dict[str, list[Any]]] = {}
        # Per version and TypedDict path, its keys, or None when they cannot be read
        # (see typed_dict_keys).
        self._typed_dicts: dict[tuple[int, str], set[str] | None] = {}
        # The parsed ``class`` statement of each class looked at (see _class_def).
        self._class_defs: dict[int, ast.ClassDef | None] = {}
        # The target of each re-export the new release put where the old one defined an object,
        # by the target's path, to the old object's path (see _realiased, _reexported_as).
        self._realiased_paths: dict[str, str] = {}
        # Where the new release serves a removed name with a warning: the module and the code
        # (see _served, _served_warning).
        self._served_scopes: dict[str, tuple[Any, ast.AST]] = {}
        self.fuzzy = True

    def run(self) -> list[APIChange]:
        out: list[APIChange] = []
        breakages = self._breakages()
        removals = sum(
            1
            for b in breakages
            if b.kind.name == "OBJECT_REMOVED" and not getattr(b.obj, "inherited", False)
        )
        self.fuzzy = removals <= FUZZY_LIMIT
        if not self.fuzzy:
            log.debug("%s: %d removals, skipping similar-name suggestions", self.package, removals)
        for b in breakages:
            try:
                change = self._from_breakage(b)
            except Exception as exc:
                log.debug("skipping breakage %s: %s", getattr(b, "obj", b), exc)
                continue
            if change is not None:
                out.append(change)
        out.extend(self._fold_inherited(out))
        try:
            out.extend(self._typed_kwargs_removals(out))
        except Exception as exc:
            log.debug("typed **kwargs scan failed for %s: %s", self.package, exc)
        try:
            out.extend(self._deprecations())
        except Exception as exc:
            log.debug("deprecation scan failed for %s: %s", self.package, exc)
        try:
            out.extend(self._warning_deprecations(out))
        except Exception as exc:
            log.debug("warnings.warn scan failed for %s: %s", self.package, exc)
        self.unread |= self._compiled_since()
        return out

    def _compiled_since(self) -> set[str]:
        """The compiled modules of the new release (``self.compiled``) that this import name
        had as source or a stub in the old one, private ones included: nothing in them, and
        nothing a readable module takes from them, can be compared any more. griffe reports no
        removal of a private module, so they are not all among the removals _unreadable hid.
        """
        top = str(self.old.path)
        found: set[str] = set()
        for entry in self.compiled:
            path = entry.removesuffix(".__init__")
            if path != top and not path.startswith(top + "."):
                continue
            if _kind_of(_get(self.old, _rel(path, top))) == "module":
                found.add(entry)
        return found

    def _breakages(self) -> list[Any]:
        """Collect griffe's breakages, keeping everything found before any internal error, and
        those inside the objects the new release only re-exports (:meth:`_realiased`)."""
        import griffe

        found: list[Any] = []
        complete = False
        try:
            gen = griffe.find_breaking_changes(self.old, self.new)
            while True:
                try:
                    found.append(next(gen))
                except StopIteration:
                    complete = True
                    break
        except Exception as exc:
            log.debug("breakage detection stopped early for %s: %s", self.package, exc)
        if not complete:
            # Fall back to diffing each public submodule on its own, so one bad alias costs one
            # module.
            for module in _walk_modules(self.old):
                if module is self.old or _has_private_segment(module.path):
                    continue
                new_module = self.new_object(module.path)
                if new_module is None:
                    continue
                try:
                    found.extend(griffe.find_breaking_changes(module, new_module))
                except Exception:
                    continue
        found.extend(self._realiased())
        seen: set[tuple[str, str, str]] = set()
        unique = []
        for b in found:
            k = (b.kind.name, b.obj.path, str(getattr(b.old_value, "name", b.old_value)))
            if k not in seen:
                seen.add(k)
                unique.append(b)
        return unique

    def _realiased(self) -> list[Any]:
        """The breakages inside an object that the old release defined in a module and the new
        release only imports there: an alias to an object of another module, or of another
        distribution read next to it (mcp 2.3's ``mcp.types.Tool`` is ``mcp_types.Tool``, from
        ``from mcp_types import *``). griffe compares an object with an alias by resolving the
        alias, then stops: the old object's path is already among the paths it has seen, so a
        field the target renamed (``Tool.inputSchema`` -> ``input_schema``) went unreported.
        Each such pair is compared here on its own (:func:`_pair_breakages`: the members of a
        class, the signature of a function, a change of kind); what the pair's module gained or
        lost is griffe's as before. The breakages on the target's side carry the target's path
        (``mcp_types.Tool.model_validate``), which :meth:`_reexported_as` maps back to the name
        the old release defined (``mcp.types.Tool.model_validate``)."""
        found: list[Any] = []
        for module in _walk_modules(self.old):
            new_module = self.new_object(module.path)
            if not getattr(new_module, "is_module", False):
                continue
            for name, old_member in list(module.members.items()):
                if name.startswith("_") or getattr(old_member, "is_alias", False):
                    continue
                if _kind_of(old_member) not in ("class", "function"):
                    continue
                new_member = new_module.members.get(name)
                if new_member is None or not getattr(new_member, "is_alias", False):
                    continue
                try:
                    target = new_member.final_target
                except Exception:
                    continue  # unresolved: griffe skipped it, and so does this
                if _kind_of(target) not in ("class", "function"):
                    continue  # now a value or a module: not an API of the same shape
                self._realiased_paths.setdefault(str(target.path), f"{module.path}.{name}")
                try:
                    found.extend(_pair_breakages(old_member, target))
                except Exception as exc:
                    log.debug("cannot compare %s with %s: %s", old_member.path, target.path, exc)
        return found

    def _reexported_as(self, path: str) -> str | None:
        """The old release's path of an object griffe reports under the path of the target of
        a re-export (:meth:`_realiased`): ``pkg_types.Client.send`` is ``pkg.types.Client.send``
        when ``pkg.types`` defined ``Client`` and now star-imports it. None for any other path,
        and for a path the old release has an object at too (the old ``pkg.impl.Client``, when
        both releases have one): that object is compared on its own, and the breakage is its."""
        parts = path.split(".")
        for i in range(len(parts), 0, -1):
            public = self._realiased_paths.get(".".join(parts[:i]))
            if public is not None:
                return None if find_object(self.old, path) else ".".join([public, *parts[i:]])
        return None

    def _old_of(self, obj: Any) -> Any:
        """The old release's object at the path of ``obj``, the new release's object griffe
        reports a parameter or kind breakage on; ``obj`` itself when there is none."""
        path = self._reexported_as(str(obj.path)) or str(obj.path)
        return _get(self.old, _rel(path, self.old.path)) or obj

    def _new_of(self, obj: Any) -> Any:
        """The new release's object at the path of ``obj`` (griffe's object for a parameter or
        kind breakage): by its path, or ``obj`` itself when the path leads into another
        distribution read next to the release (:meth:`_realiased`), where no lookup reaches."""
        found = self.new_object(obj.path)
        if found is None and self._reexported_as(str(obj.path)) is not None:
            return obj
        return found

    # ---------------------------------------------------------------- helpers
    def _change(
        self,
        kind: str,
        obj: Any,
        *,
        old_obj: Any = None,
        new_obj: Any = None,
        path: str | None = None,
        **kw: Any,
    ) -> APIChange:
        """The change to ``obj``, which is ``old_obj`` (the old version's object) or ``new_obj``
        (the new version's); the paths of both make ``import_paths``. ``path``: where to report
        it instead of under its own public path (a public class that inherits a private class's
        method: ``pkg.DataFrame.fillna`` for ``pkg._base._Base.fillna``), its owner the class
        on that path."""
        parent = getattr(obj, "parent", None)
        owner = parent.name if parent is not None and getattr(parent, "is_class", False) else None
        if path is not None and owner is not None:
            owner = path.rsplit(".", 2)[-2]
        path = path or self.public_path(obj.path) or obj.path
        change = APIChange(
            package=self.package,
            from_version=self.old_version,
            to_version=self.new_version,
            kind=kind,
            path=path,
            name=obj.name,
            owner=owner,
            **kw,
        )
        change.import_paths = self.import_paths(change, old_obj, new_obj)
        change.tier = self.tier_of(old_obj, new_obj)
        if owner is not None and kind not in (REMOVED, MOVED):
            self._add_inheritors(change, old_obj, new_obj)
        return change

    def _add_inheritors(self, change: APIChange, old_obj: Any, new_obj: Any) -> None:
        """Give a change to a method or attribute the paths of the public classes that inherit
        it without overriding it, in either version (``pkg.DataFrame.fillna`` for a change to
        ``NDFrame.fillna``): code calls it on them, so they are among its ``import_paths``,
        ``also`` shows a few, and each class is an occurrence. A public one among them makes
        the change public (APIChange.tier). Removals are folded over the classes that lost
        the member instead (_fold_inherited)."""
        classes: dict[str, Any] = {}
        paths: list[str] = []
        for tree, obj in ((self.old, old_obj), (self.new, new_obj)):
            parent = getattr(obj, "parent", None)
            if obj is None or not getattr(parent, "is_class", False):
                continue
            for sub in self.inheritors(tree, parent, obj.name):
                classes.setdefault(str(sub.path), (tree, sub))
                paths += [f"{p}.{obj.name}" for p in self.paths_in(tree, sub)]
        if not classes:
            return
        others = [p for p in _ordered(paths) if p != change.path and p not in change.also]
        change.also = [*change.also, *others][:5]
        change.occurrences += len(classes)
        change.import_paths = _ordered([*(change.import_paths or ()), *paths])
        if change.tier != TIER_PUBLIC and any(
            self._public_api(tree, sub) for tree, sub in classes.values()
        ):
            change.tier = TIER_PUBLIC

    def inheritors(self, tree: Any, cls: Any, name: str) -> list[Any]:
        """The public classes of ``tree`` (a version) that inherit ``name`` from ``cls``: their
        own members have no ``name``, and the one they get through their bases is ``cls``'s
        (not another base's, which would come first in their MRO)."""
        index = self._inheritor_index(tree)
        target = f"{cls.path}.{name}"
        found = []
        for sub in index.get(str(cls.path), ()):
            try:
                if name in sub.members:
                    continue
                member = sub.all_members.get(name)
                if member is None:
                    continue
                if getattr(member, "is_alias", False):
                    member = member.final_target
                if str(member.path) == target:
                    found.append(sub)
            except Exception:
                continue
        return found

    def _public_inheritor(self, obj: Any) -> str | None:
        """For a method of a private class (the new version's ``obj``): the shortest public
        path of a class that inherits it without overriding it, in both versions, with the
        method's name (``pkg.DataFrame.fillna`` for ``pkg._base._Base.fillna``), or None when
        no public class does. A change to the method is reported there: code calls it on
        the public class, as it does the folded removals of _fold_inherited."""
        parent: Any = getattr(obj, "parent", None)
        if not getattr(parent, "is_class", False):
            return None
        old_parent = _get(self.old, _rel(parent.path, self.old.path))
        if not getattr(old_parent, "is_class", False):
            return None
        before = {str(sub.path) for sub in self.inheritors(self.old, old_parent, obj.name)}
        paths = [
            p
            for sub in self.inheritors(self.new, parent, obj.name)
            if str(sub.path) in before
            for p in self.paths_in(self.new, sub)
        ]
        return f"{_ordered(paths)[0]}.{obj.name}" if paths else None

    def _inheritor_index(self, tree: Any) -> dict[str, list[Any]]:
        key = id(tree)
        if key not in self._inheritors:
            index: dict[str, list[Any]] = {}
            for obj, _public in iter_public_objects(tree):
                if not getattr(obj, "is_class", False):
                    continue
                try:
                    bases = list(obj.mro())
                except Exception:
                    continue
                for base in bases:
                    index.setdefault(str(base.path), []).append(obj)
            self._inheritors[key] = index
        return self._inheritors[key]

    # ---------------------------------------------------------------- tiers
    def tier_of(self, *objs: Any) -> str:
        """APIChange.tier of a change to the old version's object and the new version's
        (``objs``, either None): TIER_PUBLIC when the API it belongs to (the object, or for a
        member or a parameter its class or callable) is one the package presents in either
        version (:meth:`_public_api`), else TIER_INTERNAL."""
        for tree, obj in zip((self.old, self.new), objs, strict=False):
            anchor = _api_anchor(obj)
            if anchor is not None and self._public_api(tree, anchor):
                return TIER_PUBLIC
        return TIER_INTERNAL

    def _public_api(self, tree: Any, obj: Any) -> bool:
        """Is this module-level object (or module) one the package presents, in ``tree``: a
        name of the top-level package (``pkg.Thing``, ``pkg.sub``; one a ``from .m import *``
        there copies from a module that re-exports it on purpose too: ``anthropic
        .AnthropicBedrock``), a name a module it is reached through lists in ``__all__`` or
        re-exports as ``from .m import X as X`` (``anthropic.types.CompletionCreateParams``),
        or a documented one (a value made of a function, ``convert = deprecated(...)(_convert)``,
        with the function's docstring)? Its paths have no private part already (only public
        paths are reported)."""
        top = str(tree.path).count(".") + 1
        paths = self.paths_in(tree, obj)
        if any(p.count(".") == top for p in paths) or self._copied_to_top(tree, obj):
            return True
        for p in paths:
            module_path, _, name = p.rpartition(".")
            module = find_object(tree, module_path)
            if getattr(module, "is_module", False) and (
                self._listed(module, name) or name in self._source(module).reexported
            ):
                return True
        return bool(_api_docstring(obj))

    def _copied_to_top(self, tree: Any, obj: Any) -> bool:
        """Did a ``from .m import *`` of the top-level package copy ``obj`` there, from a module
        that re-exports it on purpose (not :meth:`_leaked`)? griffe does not count the copy
        as public when the package has an ``__all__`` that leaves it out (anthropic's
        ``from .lib.bedrock import *``, with ``AnthropicBedrock as AnthropicBedrock`` there),
        but ``from anthropic import AnthropicBedrock`` is how it is used."""
        if getattr(obj, "is_module", False):
            return False
        member = tree.members.get(obj.name)
        if not getattr(member, "is_alias", False) or not getattr(
            member, "wildcard_imported", False
        ):
            return False
        try:
            return str(member.final_target.path) == str(obj.path) and not self._leaked(member)
        except Exception:
            return False

    def _from_breakage(self, b: Any) -> APIChange | None:
        kind = b.kind.name
        obj = b.obj
        if kind == "OBJECT_REMOVED" and self.compiled:
            # Before the public filter: a private module that is compiled now still hides the
            # public names taken from it, which the scan warns about.
            owner = self._unreadable(obj)
            if owner is not None:
                self.unread.add(owner)
                return None
        via = None  # the public path a change to a private class's method is reported under
        if (
            kind == "OBJECT_REMOVED"
            and getattr(obj, "inherited", False)
            and self._in_template(str(obj.path))
            and getattr(obj, "parent", None) is not None
            and not self.lost_bases(obj.parent.path)
        ):
            # A template's class (transformers' modular_*.py) is no API, but what it lost
            # from a public base may be reported nowhere else: folded, never shown.
            self._inherited.setdefault(_origin(obj), []).append(obj)
            self._from_templates.add(id(obj))
            return None
        if not self.is_public(obj.path, module=_kind_of(obj) == "module"):
            if not kind.startswith("PARAMETER_"):
                return None
            via = self._public_inheritor(obj)
            if via is None:
                return None
        if kind == "OBJECT_REMOVED":
            parent = getattr(obj, "parent", None)
            if (
                obj.name == "__init__"
                and parent is not None
                and self.new_object(parent.path) is not None
            ):
                return None  # the class is still constructible (inherited or synthesized __init__)
            if getattr(obj, "inherited", False) and parent is not None:
                if not self.lost_bases(parent.path):
                    # Folded with the other classes that inherited it (see _fold_inherited).
                    self._inherited.setdefault(_origin(obj), []).append(obj)
                return None  # when the new bases cannot be followed, it may well still be there
            if self._not_removed(obj):
                return None
            served, warning = self._served(obj)
            if served:
                if warning is None:
                    return None  # still handed out (lazily), without a warning
                change = self._change(
                    DEPRECATED,
                    obj,
                    old_obj=obj,
                    deprecation=warning or None,
                    deprecated_by="__getattr__",
                    warning=self._served_warning(obj),
                    old_signature=signature_of(obj),
                    old_doc=doc_summary(obj),
                )
                if warning and parent is not None:
                    change.deprecation_file = _file_of(self.new_object(parent.path))
                change.library_names = self.library_names(change, None)
                return change
            if self._still_exported(obj):
                return None
            return self._removal(obj)
        # For parameter and kind breakages griffe reports the NEW object; look up the old one.
        old_obj = self._old_of(obj)
        if kind == "OBJECT_CHANGED_KIND":
            before = str(getattr(b.old_value, "value", b.old_value))
            if _kind_of(old_obj) != before:
                # Reported through an alias: griffe gives the target's path, not the name that
                # changed, so the old object cannot be located reliably.
                return None
            new_obj = self._new_of(obj)
            if self._conditional(old_obj) or self._conditional(new_obj):
                return None  # defined per Python version or platform: the kind depends on it
            if _compatible_kind_change(old_obj, new_obj, self):
                return None
            return self._change(
                KIND_CHANGED,
                old_obj,
                old_obj=old_obj,
                new_obj=new_obj,
                old_kind=_kind_label(old_obj),
                new_kind=_kind_label(new_obj) or str(getattr(b.new_value, "value", b.new_value)),
                old_signature=signature_of(old_obj),
                old_doc=doc_summary(old_obj),
                new_signature=signature_of(new_obj),
                library_names=[],
            )
        if kind not in {
            "PARAMETER_REMOVED",
            "PARAMETER_ADDED_REQUIRED",
            "PARAMETER_CHANGED_REQUIRED",
            "PARAMETER_CHANGED_KIND",
        }:
            return None
        new_fn = self._new_of(obj)
        if new_fn is None:
            return None
        old_fn = old_obj
        if _special_form(old_fn) or _special_form(new_fn):
            return None  # wrapped by a class: callers call that object, not this signature
        if _fixture(old_fn) or _fixture(new_fn):
            return None  # pytest calls it, injecting its parameters (``request``): not a call API
        param_obj = b.old_value if kind == "PARAMETER_REMOVED" else (b.new_value or b.old_value)
        param = param_obj.name
        if param in ("self", "cls") or param.startswith("_"):
            return None  # ``__x`` is positional-only by convention (PEP 484): its name is not API
        if kind == "PARAMETER_REMOVED" and param in deprecated_parameters(new_fn):
            return None  # still accepted under its old name, with a warning: a deprecation
        old_overloads = list(getattr(old_fn, "overloads", None) or [])
        if (
            old_overloads
            and kind != "PARAMETER_REMOVED"
            and (
                kind == "PARAMETER_CHANGED_KIND" or all(_requires(f, param) for f in old_overloads)
            )
        ):
            # Several old signatures, of which griffe compared one: a parameter is newly
            # required only for callers of a signature that did not require it already.
            return None
        overloads = list(getattr(new_fn, "overloads", None) or [])
        if overloads:
            # Overloads describe the real call signatures (every SDK ``create`` is overloaded on
            # ``stream``). A removal is real only if no overload still accepts the parameter.
            if kind != "PARAMETER_REMOVED":
                return None
            # Still accepted by one of them, by name or through its ``**kwargs`` (a bare one,
            # or a TypedDict with the key).
            for f in [new_fn, *overloads]:
                names = self.keyword_names(self.new, f)
                if names is None or param in names:
                    return None
        suggestions: list[str] = []
        rename = False  # suggestions is the new name of the parameter, renamed in place
        if kind == "PARAMETER_REMOVED":
            pkind = _pkind(b.old_value)
            renamed = _renamed(old_fn, new_fn, param)
            if pkind in _VARIADIC:
                if _absorbed(old_fn, new_fn, pkind):
                    return None  # what *args or **kwargs took, the new signature names
                if pkind == "var_keyword" and self.keyword_names(self.old, old_fn) is not None:
                    # ``**kwargs: Unpack[SomeTypedDict]`` took the TypedDict's keys, which are
                    # compared one by one (_typed_kwargs_removals).
                    return None
                param = ("**" if pkind == "var_keyword" else "*") + param
            elif pkind == "positional_only" and renamed is not None:
                return None  # a positional-only parameter renamed: its name is not API
            elif _accepts_var_keyword(new_fn) and pkind != "positional_only":
                names = self.keyword_names(self.new, new_fn)
                # A bare ``**kwargs`` takes it; ``**kwargs: Unpack[SomeTypedDict]`` only
                # when the TypedDict has the key.
                taken = names is None or param in names
                if taken and (renamed is None or not _required(renamed)):
                    return None  # still accepted through **kwargs
                if renamed is not None:
                    # The new name is then missing (required), or in the old one's place.
                    suggestions = _rename_hint(b.old_value, renamed, new_fn)
                    rename = bool(suggestions)
                elif names is not None:
                    known = self.keyword_names(self.old, old_fn) or _parameter_names(old_fn)
                    suggestions = _close(sorted(names - known), param)
            elif renamed is not None:
                suggestions = _rename_hint(b.old_value, renamed, new_fn)
                rename = bool(suggestions)
            else:
                known = _parameter_names(old_fn)
                fresh = [p.name for p in new_fn.parameters if p.name not in known]
                suggestions = _close(fresh, param)
            ckind = PARAM_REMOVED
        elif kind == "PARAMETER_CHANGED_KIND":
            before, after = _pkind(b.old_value), _pkind(b.new_value)
            if after == "keyword_only" and before in ("positional_or_keyword", "positional_only"):
                ckind = PARAM_KEYWORD_ONLY
            elif after == "positional_only" and before in ("positional_or_keyword", "keyword_only"):
                ckind = PARAM_POSITIONAL_ONLY
            else:
                return None
        else:
            before_name = _renamed(new_fn, old_fn, param)
            if kind == "PARAMETER_ADDED_REQUIRED" and _required(before_name):
                # A required positional parameter under a new name: the same argument, not a new
                # one. Keyword callers of the old name see its removal, reported once.
                return None
            ckind = PARAM_REQUIRED
        return self._parameter_change(ckind, old_fn, new_fn, param, suggestions, rename, via)

    def _parameter_change(
        self,
        ckind: str,
        old_fn: Any,
        new_fn: Any,
        param: str,
        suggestions: list[str],
        rename: bool,
        via: str | None = None,
    ) -> APIChange:
        """The change of kind ``ckind`` to parameter ``param`` of ``old_fn`` (the old
        version's callable; ``new_fn`` is the new version's), with the library's hint for it,
        whether the new source still handles it and the request extras it has. ``via``: the
        public path to report a private class's method under (:meth:`_public_inheritor`)."""
        if ckind == PARAM_REMOVED:
            hint, source = parameter_hint(old_fn, param.lstrip("*"))
        else:
            hint, source = deprecation_hint_and_source(old_fn, param.lstrip("*"))
        handled, handled_text = None, None
        extras: list[str] | None = None
        if ckind == PARAM_REMOVED and not param.startswith("*"):
            handled, handled_text = self.still_handled(new_fn, param)
            names = _parameter_names(new_fn)
            extras = [n for n in REQUEST_EXTRAS if n in names]
        # Under the new path, which public_path knows; under the old one when the new release
        # only re-exports the callable from elsewhere (_realiased: its own path may be in
        # another distribution, which is no path of this package).
        subject = old_fn if self._reexported_as(str(new_fn.path)) else new_fn
        change = self._change(
            ckind,
            subject,
            old_obj=old_fn,
            new_obj=new_fn,
            path=via,
            parameter=param,
            hint=hint,
            hint_source=source,
            hint_file=_file_of(old_fn) if hint else None,
            still_handled_at=handled,
            still_handled_text=handled_text,
            suggestions=suggestions,
            renamed=rename,
            request_extras=extras,
            old_signature=signature_of(old_fn),
            new_signature=signature_of(new_fn),
            old_doc=doc_summary(old_fn),
            new_doc=doc_summary(new_fn),
        )
        change.library_names = self.library_names(change, new_fn)
        return change

    # ------------------------------------------------ **kwargs: Unpack[TypedDict]
    def _typed_kwargs_removals(self, found: list[APIChange]) -> list[APIChange]:
        """The keywords a callable took at the cutoff and no longer takes, where the pinned
        version takes its keywords through ``**kwargs: Unpack[SomeTypedDict]`` (PEP 692).

        griffe compares the signatures' names, and a ``**kwargs`` swallows every keyword for
        it (pandas 3.0's ``read_csv`` has ``**kwds: Unpack[_read_shared]`` in its implementation
        and its four overloads, and ``delim_whitespace``, which 2.3 took, is no key of
        ``_read_shared``: issue #60). So the names the old signatures took by keyword, their
        own TypedDict's keys included, are compared with the new ones' here, whichever side
        has the TypedDict: a key of the old one that the new signature does not name is gone
        too. A bare ``**kwargs`` on either side takes anything, and nothing is compared.
        ``found``: the changes the breakages gave, not reported twice.
        """
        reported = {(c.path, c.parameter) for c in found}
        out: list[APIChange] = []
        for new_fn, _public in iter_public_objects(self.new):
            if not getattr(new_fn, "is_function", False) or not self.is_public(new_fn.path):
                continue
            old_fn = _get(self.old, _rel(new_fn.path, self.old.path))
            if _kind_of(old_fn) != "function" or _special_form(old_fn) or _fixture(old_fn):
                continue
            if not (self._typed_kwargs(new_fn) or self._typed_kwargs(old_fn)):
                continue
            new_names = self._keyword_names_of_all(self.new, new_fn)
            old_names = self._keyword_names_of_all(self.old, old_fn)
            if new_names is None or old_names is None:
                continue
            path = self.public_path(new_fn.path) or new_fn.path
            still = deprecated_parameters(new_fn)
            for param in sorted(old_names - new_names):
                if param.startswith("_") or param in still or (path, param) in reported:
                    continue
                known = old_names | _parameter_names(old_fn)
                suggestions = _close(sorted(new_names - known), param)
                out.append(
                    self._parameter_change(PARAM_REMOVED, old_fn, new_fn, param, suggestions, False)
                )
        return out

    def _typed_kwargs(self, fn: Any) -> bool:
        """Does ``fn``, or one of its overloads, take ``**kwargs: Unpack[...]``?"""
        return any(_unpacked(f) is not None for f in (fn, *(getattr(fn, "overloads", None) or [])))

    def _keyword_names_of_all(self, tree: Any, fn: Any) -> set[str] | None:
        """The keywords ``fn`` (of ``tree``) takes through any of its signatures (the
        implementation and its overloads), or None when one of them takes any keyword."""
        names: set[str] = set()
        for f in (fn, *(getattr(fn, "overloads", None) or [])):
            found = self.keyword_names(tree, f)
            if found is None:
                return None
            names |= found
        return names

    def keyword_names(self, tree: Any, fn: Any) -> set[str] | None:
        """The names a callable of ``tree`` (a version) takes by keyword: its parameters other
        than positional-only and variadic ones, and the keys of the TypedDict its
        ``**kwargs`` unpacks (``**kwds: Unpack[_read_shared[HashableT]]``). None when it takes
        any keyword: a bare ``**kwargs``, or one whose TypedDict cannot be read (not a class
        of the package, or with a base outside it)."""
        names: set[str] = set()
        try:
            parameters = list(fn.parameters)
        except Exception:
            return None
        bound = _bound_parameter(fn)  # ``self``, ``cls``: not a keyword
        for p in parameters:
            kind = _pkind(p)
            if kind == "var_keyword":
                path = _unpacked(fn)
                if path is None:
                    return None
                keys = self.typed_dict_keys(tree, path)
                if keys is None:
                    return None
                names |= keys
            elif kind not in ("positional_only", "var_positional") and p is not bound:
                names.add(p.name)
        return names

    def typed_dict_keys(self, tree: Any, path: str) -> set[str] | None:
        """The keys of the TypedDict class at ``path`` in ``tree``: its annotated attributes
        and its bases' (a key that moved to a base is still a key). None when the class is not
        in the package or has a base outside it other than typing's (``TypedDict``,
        ``Generic[T]``), whose keys cannot be read, or allows other keys (PEP 728's
        ``extra_items=`` or ``closed=False``); ``total=`` changes nothing here."""
        key = (id(tree), path)
        if key not in self._typed_dicts:
            self._typed_dicts[key] = self._typed_dict_keys(tree, path, frozenset())
        return self._typed_dicts[key]

    def _typed_dict_keys(self, tree: Any, path: str, seen: frozenset[str]) -> set[str] | None:
        if path in seen:
            return set()
        cls = find_object(tree, path)
        if not getattr(cls, "is_class", False):
            return None
        if _open_typed_dict(self._class_def(cls)):
            return None  # PEP 728: keys it does not name are allowed
        keys: set[str] = set()
        try:
            for name, member in cls.members.items():
                if _kind_of(member) == "attribute" and not getattr(member, "is_alias", False):
                    keys.add(name)
            bases = list(getattr(cls, "bases", None) or [])
        except Exception:
            return None
        package = str(tree.path).split(".")[0]
        for base in bases:
            base_path = _unsubscripted(base)
            if base_path.startswith(_TYPING_MODULES) or base_path == "TypedDict":
                continue
            if base_path.split(".", 1)[0] != package:
                return None
            inherited = self._typed_dict_keys(tree, base_path, seen | {path})
            if inherited is None:
                return None
            keys |= inherited
        return keys

    def _removal(self, obj: Any) -> APIChange:
        moved_to, namesake, evidence = self.find_moved(obj)
        hint, source = deprecation_hint_and_source(obj)
        if moved_to is None and not (hint and stated_names(hint)):
            # Not found under its own name, and the library's own text names no replacement:
            # the same object under another name is the next best evidence.
            moved_to, evidence = self.find_renamed(obj)
            if moved_to:
                namesake = None
        if moved_to and evidence is not None and _kind_of(obj) == "module":
            evidence["left_behind"] = self.left_behind(obj)
        method = self.aliased_method(obj)
        hinted = obj
        if method is not None and not hint:
            # ``duplicate_space = api.duplicate_space``: what the library says of the method,
            # it says of this name for it.
            hint, source = deprecation_hint_and_source(method)
            hinted = method
        change = self._change(
            MOVED if moved_to else REMOVED,
            obj,
            old_obj=obj,
            moved_to=moved_to,
            move_evidence=evidence,
            namesake=namesake,
            hint=hint,
            hint_source=source,
            hint_file=_file_of(hinted) if hint else None,
            suggestions=[] if moved_to or not self.fuzzy else self.similar_names(obj),
            old_signature=signature_of(obj),
            old_doc=doc_summary(obj),
            new_doc=doc_summary(self.new_object(moved_to)) if moved_to else None,
            new_signature=signature_of(self.new_object(moved_to)) if moved_to else None,
        )
        if method is not None:
            change.alias_of = self.public_path(str(method.path)) or str(method.path)
        if moved_to:
            change.library_names = []
        elif method is not None and hinted is method:
            # The names the method's text gives, looked up as for the method (in its class).
            like = replace(change, path=change.alias_of or "", owner=method.parent.name)
            change.library_names = self.library_names(like, None)
        else:
            change.library_names = self.library_names(change, None)
        return change

    def left_behind(self, module: Any) -> int:
        """How many public names the parent package of ``module`` (an old module that moved)
        still has in the new version (APIChange.move_evidence ``"left_behind"``): the modules,
        classes, functions and values it defines or re-exports, plain imports and private
        names aside. 0 when the package is gone, or is a module with no API (mcp 2's
        ``mcp/server/fastmcp.py``, a shim that only points at ``mcp.server.mcpserver``), so
        that a package whose modules moved counts as moved whole only then."""
        parent = getattr(module, "parent", None)
        if parent is None or not getattr(parent, "is_module", False):
            return 0
        new_parent = self.new_object(parent.path)
        if new_parent is None or not getattr(new_parent, "is_module", False):
            return 0
        package = self.new.path.split(".")[0]
        try:
            members = list(new_parent.members.items())
        except Exception:
            return 0
        count = 0
        for name, member in members:
            if _private(name) or "/" in name:
                continue
            if getattr(member, "is_alias", False):
                if not _exported(member):
                    continue
                try:
                    if not str(member.final_target.path).startswith(package + "."):
                        continue
                except Exception:
                    continue
            count += 1
        return count

    def aliased_method(self, obj: Any) -> Any:
        """The method a module-level attribute of the old version is bound to, when it is one
        (APIChange.alias_of): ``duplicate_space = api.duplicate_space`` where ``api`` is an
        instance the same module makes (``api = HfApi()``) of a class that has that method.
        None for anything else; nothing is run to know."""
        if not getattr(getattr(obj, "parent", None), "is_module", False):
            return None
        try:
            if getattr(obj, "is_alias", False):
                obj = obj.final_target  # the re-export in ``__init__``: the same binding
        except Exception:
            return None
        parent: Any = getattr(obj, "parent", None)  # the module that makes the instance
        if _kind_of(obj) != "attribute" or not getattr(parent, "is_module", False):
            return None
        try:
            value = str(obj.value) if obj.value is not None else ""
            base, _, attr = value.rpartition(".")
            if attr != obj.name or not base.isidentifier():
                return None
            instance = parent.members.get(base)
            if instance is None or _kind_of(instance) != "attribute" or instance.value is None:
                return None
            call = re.match(r"([A-Za-z_][\w.]*)\(", str(instance.value))
            if call is None:
                return None
            cls = _get(parent, call.group(1))
            if not getattr(cls, "is_class", False):
                return None
            method = cls.all_members.get(attr)
            if getattr(method, "is_alias", False):
                method = method.final_target
        except Exception:
            return None
        if not getattr(method, "is_function", False):
            return None
        if not str(method.path).startswith(self.old.path.split(".")[0] + "."):
            return None
        return method

    def _fold_inherited(self, changes: list[APIChange]) -> list[APIChange]:
        """One change per removed base-class member, not one per class that inherited it.

        A method dropped from a base class disappears from every subclass (1,600 model classes
        in transformers). It is reported once, on the public base class that defined it when
        that class lost it too, otherwise on the shortest subclass path, with the other classes
        counted as similar occurrences.
        """
        own = {c.path: c for c in changes if c.kind in (REMOVED, MOVED)}
        extra: list[APIChange] = []
        for origin, every in self._inherited.items():
            objs = [o for o in every if id(o) not in self._from_templates]
            if not objs:  # only templates inherited it: the public base's own removal
                base_member = self._removed_from_base(origin)
                if base_member is None or self._maybe_inherited(base_member):
                    continue
                if (self.public_path(origin) or origin) not in own:
                    extra.append(self._removal(base_member))
                continue
            by_path = {self.public_path(o.path) or o.path: o for o in objs}
            paths = sorted(by_path, key=lambda p: (len(p), p))
            rep = own.get(self.public_path(origin) or origin)
            if rep is None:
                base_member = self._removed_from_base(origin)
                if base_member is not None and self._maybe_inherited(base_member):
                    continue  # the base class still inherits it, and so do its subclasses
                rep = self._removal(base_member if base_member is not None else by_path[paths[0]])
                extra.append(rep)
            rest = [p for p in paths if p != rep.path]
            rep.occurrences += len(rest)
            rep.also = [*rep.also, *rest][:5]
            # Every subclass that lost it, under each of its paths, for "your code uses".
            lost = [p for o in objs for p in self.paths_in(self.old, o)]
            rep.import_paths = _ordered([*(rep.import_paths or ()), *rest, *lost])
            if rep.tier != TIER_PUBLIC and any(
                self._public_api(self.old, o.parent) for o in objs if o.parent is not None
            ):
                rep.tier = TIER_PUBLIC  # a public class had it
        return extra

    def _removed_from_base(self, origin: str) -> Any:
        """The old member at ``origin`` if its (public) class still exists but lost it."""
        member = _get(self.old, _rel(origin, self.old.path))
        parent = getattr(member, "parent", None)
        if parent is None or not parent.is_class or not self.is_public(origin):
            return None
        new_parent = self.new_object(parent.path)
        try:
            if not getattr(new_parent, "is_class", False) or member.name in new_parent.all_members:
                return None
        except Exception:
            return None
        return member

    def _deprecations(self) -> Iterator[APIChange]:
        for obj, _public in iter_public_objects(self.new):
            if not (getattr(obj, "is_function", False) or getattr(obj, "is_class", False)):
                continue
            message = pep702_message(obj)
            marks: list[tuple[str | None, str, str | None]] = []
            if message is None:
                marks.extend(decorator_deprecations(obj))
            if message is None and not marks:
                continue
            old_obj = _get(self.old, _rel(obj.path, self.old.path))
            if old_obj is None:
                continue  # a new object: the model cannot know it anyway
            old_marks = decorator_deprecations(old_obj)
            if pep702_message(old_obj) is not None or any(m[0] is None for m in old_marks):
                continue  # already deprecated
            form, only = None, None
            if message is not None:
                marks = [(None, message, None)]
                form, only = _deprecated_form(obj)
            old_params = _parameter_names(old_obj)
            for parameter, text, decorator in marks:
                if parameter is not None and (
                    parameter in {m[0] for m in old_marks} or parameter not in old_params
                ):
                    continue  # deprecated before, or never accepted by the old version
                change = self._change(
                    DEPRECATED,
                    obj,
                    old_obj=old_obj,
                    new_obj=obj,
                    parameter=parameter,
                    deprecation=text or None,
                    deprecation_file=_file_of(obj) if text else None,
                    deprecated_by=decorator,
                    call_form=form,
                    call_form_only=only,
                    old_signature=signature_of(old_obj),
                    new_signature=signature_of(obj),
                    old_doc=doc_summary(old_obj),
                    new_doc=doc_summary(obj),
                )
                change.library_names = self.library_names(change, obj if parameter else None)
                yield change

    def _warning_deprecations(self, found: list[APIChange]) -> Iterator[APIChange]:
        """Deprecations a public function, method or property announces with
        ``warnings.warn`` in its body in the pinned release, and did not in the release at the
        cutoff (issue #53): pandas 3's ``DataFrame.set_index`` warns ``"The 'verify_integrity'
        keyword in DataFrame.set_index is deprecated ..."`` with a ``Pandas4Warning`` when it is
        passed.

        Read statically (:class:`_WarnReader`): nothing is imported or run. A warning issued
        at every call deprecates the function, or the class for its ``__init__`` or
        ``__new__``; one issued only when the caller passes a parameter (``if p is not
        <default>:``, ``if "p" in kwargs:``, ``self.p != "deprecated"`` for a constructor's)
        deprecates the parameter, of the constructor for ``self.p``; one issued only while
        the caller leaves a parameter out deprecates its default (WARNS_DEFAULT); one under
        another condition deprecates the function under that condition; a property's getter
        or setter, and a module's ``__getattr__`` for a name it serves, warn when the name is
        read or set (WARNS_ACCESS). An API already deprecated in the release at the cutoff (a
        warning at every call, PEP 702, the library's decorator), a warning that release
        issued already (:meth:`_Issuance.covers`), a parameter it did not take and a change ``found``
        has (a decorator's deprecation, a removed parameter the code still reads) are left
        out. The warnings of one API and parameter are one change, which keeps each one's
        message and when it is issued (APIChange.warning["also"])."""
        known = {(c.path, c.parameter) for c in found if c.kind in (DEPRECATED, PARAM_REMOVED)}
        new = _WarnReader(self.new, self._source)
        old = _WarnReader(self.old, self._source)
        groups: dict[tuple[str, str | None], list[_Candidate]] = {}
        for obj, _public in iter_public_objects(self.new, test_methods=True):
            try:
                candidates = list(self._warned(obj, new, old))
            except Exception as exc:
                log.debug("cannot read the warnings of %s: %s", obj.path, exc)
                continue
            for c in candidates:
                groups.setdefault((c.path, c.warned.parameter), []).append(c)
        try:
            served = list(self._getattr_warned(new, old))
        except Exception as exc:
            log.debug("cannot read the __getattr__ warnings of %s: %s", self.package, exc)
            served = []
        for c in served:
            groups.setdefault((c.path, None), []).append(c)
        for (path, parameter), candidates in groups.items():
            if (path, parameter) in known or (path, None) in known:
                continue  # deprecated by a decorator, or already reported
            try:
                yield self._warned_change(candidates)
            except Exception as exc:
                log.debug("cannot report the warnings of %s: %s", path, exc)

    def _warned(self, obj: Any, new: _WarnReader, old: _WarnReader) -> Iterator[_Candidate]:
        """The warnings :meth:`_warning_deprecations` reports for one public function, method,
        property or class (its ``__new__``)."""
        readers: list[tuple[Any, str | None]] = []  # a function, and the access it is for
        if "property" in _labels(obj):  # griffe loads one as an attribute with the label
            readers.append((obj, "read"))
            for done, attr in (("set", "setter"), ("delete", "deleter")):
                fn = getattr(obj, attr, None)
                if getattr(fn, "is_function", False):
                    readers.append((fn, done))
        elif getattr(obj, "is_function", False):
            readers.append((obj, None))
        elif getattr(obj, "is_class", False):
            ctor = obj.members.get("__new__")
            if getattr(ctor, "is_function", False) and not getattr(ctor, "is_alias", False):
                readers.append((ctor, None))
        if not readers or _deprecated_whole(obj):
            return
        old_obj = self._old_counterpart(obj)
        if old_obj is None or _deprecated_whole(old_obj):
            return  # a new API (the model cannot know it), or deprecated before
        for fn, access in readers:
            warned = new.deprecations(fn)
            if not warned:
                continue
            old_fn, same_kind = self._old_function(obj, old_obj, fn, access, old)
            before = old.deprecations(old_fn, every=True) if old_fn is not None else []
            if access is not None:
                before = [_as_access(w, access) for w in before]
            if any(
                w.deprecation and w.when in (WARNS_ALWAYS, WARNS_ACCESS) and not w.conditional
                for w in before
            ):
                continue  # deprecated as a whole already
            issued = _Issuance.of(before, old_fn, old)
            old_params = _parameter_names(old_fn) if old_fn is not None else set()
            old_kwargs = _accepts_var_keyword(old_fn) if old_fn is not None else False
            for w in warned:
                if access is not None:
                    w = _as_access(w, access)
                if issued.covers(w, str(getattr(fn, "name", ""))):
                    continue  # the release at the cutoff warned already
                if not same_kind and w.when != WARNS_ALWAYS:
                    continue  # the name was another kind of object: only a deprecation of it
                candidate = self._candidate(obj, old_obj, fn, w, old_params, old_kwargs)
                if candidate is not None:
                    yield candidate

    def _candidate(
        self,
        obj: Any,
        old_obj: Any,
        fn: Any,
        w: _Warned,
        old_params: set[str],
        old_kwargs: bool,
    ) -> _Candidate | None:
        """What a warning of ``fn`` (``obj``'s, or its ``__new__``) deprecates: the function,
        its class (a constructor that warns at every call), a parameter of its own or of the
        class's constructor (``self.p``); None for a parameter the release at the cutoff did
        not take. A default warns calls that leave the parameter out, which code written then
        does, though the parameter is new (statsmodels 0.15's ``result_object``)."""
        target, old_target = obj, old_obj
        parent, old_parent = getattr(fn, "parent", None), getattr(old_obj, "parent", None)
        dunder_new = fn is not obj and getattr(obj, "is_class", False)  # the class's __new__
        if dunder_new:
            parent, old_parent = obj, old_obj
        if w.on_init:
            target = _member(parent, "__init__") if getattr(parent, "is_class", False) else None
            old_target = (
                _member(old_parent, "__init__") if getattr(old_parent, "is_class", False) else None
            )
            if target is None or old_target is None:
                return None
            old_params = _parameter_names(old_target)
            old_kwargs = _accepts_var_keyword(old_target)
        if (
            w.parameter is not None
            and w.when != WARNS_DEFAULT
            and w.parameter not in old_params
            and not (w.from_kwargs and old_kwargs)
        ):
            return None  # never accepted by the release at the cutoff
        constructor = (
            fn.name in ("__init__", "__new__")
            and getattr(parent, "is_class", False)
            and getattr(old_parent, "is_class", False)
        )
        if w.when == WARNS_ALWAYS and constructor:
            if _deprecated_whole(parent) or _deprecated_whole(old_parent):
                return None
            target, old_target = parent, old_parent  # the class is deprecated
        elif dunder_new:
            return None  # a __new__ that warns in some calls only: not read
        path = self.public_path(target.path) or target.path
        return _Candidate(path, target, old_target, fn if not w.on_init else target, w)

    def _old_counterpart(self, obj: Any) -> Any:
        """The release at the cutoff's object for ``obj``: at its path, else at its public
        path or another path it is reached by (statsmodels 0.15 moved ``seasonal_decompose``
        into ``tsa/seasonal/_seasonal.py`` and re-exports it where it was); None when there is
        none."""
        found = _get(self.old, _rel(obj.path, self.old.path))
        if found is not None:
            return found
        paths = [self.public_path(obj.path), *self.paths_in(self.new, obj)]
        for path in dict.fromkeys(p for p in paths if p):
            found = find_object(self.old, path)
            if found is not None:
                return found
        return None

    def _old_function(
        self, obj: Any, old_obj: Any, fn: Any, access: str | None, old: _WarnReader
    ) -> tuple[Any, bool]:
        """The release at the cutoff's function to compare ``fn`` (``obj``'s, or its getter,
        setter or ``__new__``) with, and whether ``old_obj`` is the same kind of object. An
        alias the old release bound (``graph_could_be_isomorphic = could_be_isomorphic`` in
        networkx 3.4) is the function it names, and another kind of object: only a warning at
        every call deprecates it."""
        if access is not None:
            if "property" not in _labels(old_obj):
                return None, False
            if access == "read":
                return old_obj, True
            other = getattr(old_obj, "setter" if access == "set" else "deleter", None)
            return (other if getattr(other, "is_function", False) else None), True
        if fn is not obj and getattr(obj, "is_class", False):  # __new__
            if not getattr(old_obj, "is_class", False):
                return None, False
            other = old_obj.members.get("__new__")
            return (other if getattr(other, "is_function", False) else None), True
        if getattr(old_obj, "is_function", False):
            return old_obj, True
        if getattr(old_obj, "is_attribute", False):
            value = getattr(old_obj, "value", None)
            parent = getattr(old_obj, "parent", None)
            if value is not None and getattr(parent, "is_module", False):
                named = old.resolve(parent, str(value))
                if getattr(named, "is_function", False):
                    return named, False
        return None, False

    def _warned_change(self, candidates: list[_Candidate]) -> APIChange:
        """The change for the warnings of one API and parameter: the first by
        :func:`_warned_order` says when, the others are ``also`` (each when it is issued),
        and the message is each one's, in that order."""
        ordered = sorted(candidates, key=lambda c: _warned_order(c.warned))
        main = ordered[0]
        w = main.warned
        messages = list(dict.fromkeys(c.warned.message for c in ordered if c.warned.message))
        text = " ".join(messages)
        evidence = w.evidence()
        also = []
        for c in ordered[1:]:
            more = c.warned.evidence()
            if more != evidence and more not in also:
                also.append(more)
        if also:
            evidence["also"] = also[:4]
        target, old_target = main.target, main.old_target
        change = self._change(
            DEPRECATED,
            target,
            old_obj=old_target,
            new_obj=target,
            path=main.path if main.path != (self.public_path(target.path) or target.path) else None,
            parameter=w.parameter,
            deprecation=text[: 300 * len(messages)] or None,
            deprecation_file=_file_of(target) if text else None,
            deprecated_by=HINT_WARNING,
            warning=evidence,
            old_signature=signature_of(old_target),
            new_signature=signature_of(target),
            old_doc=doc_summary(old_target),
            new_doc=doc_summary(target),
        )
        if isinstance(target, _ServedName):  # a name a module's __getattr__ serves
            module, old_module = target.parent, getattr(old_target, "parent", None)
            paths = [*self.paths_in(self.old, old_module), *self.paths_in(self.new, module)]
            change.import_paths = _ordered([change.path, *(f"{p}.{target.name}" for p in paths)])
            change.tier = self.tier_of(old_module, module)
        itself = {change.path, *(change.import_paths or ())}
        change.library_names = [  # "... in DataFrame.set_index is deprecated" names itself
            n
            for n in self.library_names(change, main.fn if w.parameter else None)
            if n not in itself
        ]
        return change

    def _getattr_warned(self, new: _WarnReader, old: _WarnReader) -> Iterator[_Candidate]:
        """Names a public module's ``__getattr__`` (PEP 562) serves in both releases and
        warns of in the pinned one only: ``if name == "Old": warnings.warn("Old is
        deprecated, use New", DeprecationWarning); return New``, or a table it looks the name
        up in. A name the release at the cutoff defined itself is the removal path's
        (:meth:`_served`)."""
        for module in _walk_modules(self.new):
            path = str(module.path)
            if _has_private_segment(path) or _non_api_path(path, module=True):
                continue
            hook = module.members.get("__getattr__")
            if not getattr(hook, "is_function", False) or getattr(hook, "is_alias", False):
                continue
            warned = _getattr_warnings(new, hook, self._source(module).tree)
            if not warned:
                continue
            old_module = _get(self.old, _rel(path, self.old.path))
            if not getattr(old_module, "is_module", False):
                continue
            old_hook = old_module.members.get("__getattr__")
            old_tree = self._source(old_module).tree
            before: dict[str, list[_Warned]] = {}
            served: set[str] = set()
            if getattr(old_hook, "is_function", False) and not getattr(old_hook, "is_alias", False):
                before = _getattr_warnings(old, old_hook, old_tree, every=True)
                served = _getattr_names(old_hook, old_tree)
            for name, warnings_of in warned.items():
                if name in old_module.members or name not in served:
                    continue  # the old release's own name, or a new one
                if any(w.deprecation for w in before.get(name, ())):
                    continue
                for w in warnings_of:
                    yield _Candidate(
                        f"{self.public_path(path) or path}.{name}",
                        _ServedName(name, module),
                        _ServedName(name, old_module),
                        None,
                        _as_access(w, "read"),
                    )

    # ------------------------------------------- what a static reading misses
    def _not_removed(self, obj: Any) -> bool:
        """Is an object griffe finds missing still there, or was it never API?

        For module-level names: names only bound under ``if __name__ == "__main__":``, values
        bound only on some paths of an ``if``/``try`` block (temporaries such as a path being
        searched), per-module values (loggers, type variables), names a module got from
        ``from x import *`` for its own use, and functions that were attached to a class which
        now defines the method itself. For class members: a member the class may still inherit
        from a base outside the package.
        """
        name = obj.name
        if "/" in name or name in _NEVER_API:
            return True  # griffe's placeholder for an unresolved ``from x import *``
        parent: Any = getattr(obj, "parent", None)
        if getattr(parent, "is_class", False):
            return self._maybe_inherited(obj)
        if not getattr(parent, "is_module", False):
            return False
        binding = self._source(parent).bindings.get(name)
        if binding == _MAIN:
            return True
        if _kind_of(obj) == "function" and self._now_a_method(obj):
            return True
        if getattr(obj, "is_alias", False):
            # An import, even under ``if TYPE_CHECKING:``, re-exports on purpose; unless it came
            # with a star import.
            return self._leaked(obj) or self._incidental(obj)
        if _per_module_value(obj):
            return True
        return binding == _SOMETIMES and obj.is_attribute and not self._listed(parent, name)

    def _still_exported(self, obj: Any) -> bool:
        """Does the new module still list the name in ``__all__`` and bind names in a way
        griffe cannot follow (``globals().update(...)``, say), or re-export names from outside
        the package through a ``from x import *`` that griffe could not follow?

        A name ``__all__`` lists but nothing binds is gone: ``from m import name`` fails (black
        26.5.1 kept ``generate_tokens`` in ``blib2to3.pgen2.tokenize.__all__``).
        """
        parent: Any = getattr(obj, "parent", None)
        new_module: Any = self.new_object(parent.path) if parent is not None else None
        if not getattr(new_module, "is_module", False):
            return False
        if self._listed(new_module, obj.name) and self._source(new_module).dynamic:
            return True
        try:
            package = self.new.path.split(".")[0]
            return any(_star_from_outside(m, package) for m in new_module.members.values())
        except Exception:
            return False

    def _unreadable(self, obj: Any) -> str | None:
        """The compiled module (SourceTree.compiled) that hides a removed object from the
        static view, which is not the same as its being gone (issue #52); None when the
        removal is real.

        That is the object itself or what it is defined in, for a compiled module, and the
        package and the names it defines, for a compiled package ``__init__``. A name that a
        readable module imports from a compiled one is hidden only while the new module still
        binds it, or star-imports a compiled module the static view cannot expand: dropped
        from the import, ``from pkg import name`` fails, compiled or not.
        """
        owner = _compiled_owner(str(obj.path), self.compiled, module=_kind_of(obj) == "module")
        if owner is not None or not getattr(obj, "is_alias", False):
            return owner
        targets = [str(getattr(obj, "target_path", ""))]
        with suppress(Exception):
            targets.append(str(obj.final_target.path))
        owner = next(filter(None, (_compiled_owner(t, self.compiled) for t in targets)), None)
        parent: Any = getattr(obj, "parent", None)
        if owner is None or parent is None:
            return None
        members = getattr(self.new_object(parent.path), "members", None) or {}
        if obj.name in members or any(
            _star_from_compiled(m, self.compiled) for m in members.values()
        ):
            return owner
        return None

    def _source(self, module: Any) -> _Source:
        key = id(module)
        if key not in self._sources:
            self._sources[key] = _Source.parse(module)
        return self._sources[key]

    def _listed(self, module: Any, name: str) -> bool:
        """Is ``name`` in the module's ``__all__``, including names added to it on some paths
        (``__all__.append("x")`` under an ``if``), which griffe does not read?"""
        exports = getattr(module, "exports", None) or ()
        names = {e if isinstance(e, str) else getattr(e, "name", str(e)) for e in exports}
        return name in names or name in self._source(module).exported

    def _conditional(self, obj: Any) -> bool:
        """Is this module-level object defined on some paths only (per Python version,
        platform or ``TYPE_CHECKING``)?"""
        parent = getattr(obj, "parent", None)
        if obj is None or not getattr(parent, "is_module", False):
            return False
        return self._source(parent).bindings.get(obj.name) == _SOMETIMES

    def _leaked(self, obj: Any, depth: int = 0) -> bool:
        """Did this name reach its module only as another module's import, via ``import *``?

        Without ``__all__``, ``from x import *`` copies every public name of x, including the
        modules and helpers x imported (``os``, ``typing.Any``): they were never x's API. An
        import meant as a re-export (``import y as y``, or listed in ``__all__``) counts.
        """
        if depth > 5 or not getattr(obj, "wildcard_imported", False):
            return False
        source_path, _, name = str(obj.target_path).rpartition(".")
        try:
            source = obj.modules_collection.get_member(source_path)
            member = source.members[name]
        except Exception:
            return False
        if not getattr(member, "is_alias", False):
            return False  # defined there
        if getattr(member, "wildcard_imported", False):
            return self._leaked(member, depth + 1)
        if getattr(member, "public", None) or self._listed(source, name):
            return False
        return name not in self._source(source).reexported

    def _incidental(self, obj: Any) -> bool:
        """Did a plain module (not a package ``__init__``) get this name from ``import *`` of a
        public module, for its own use? The name was never API there, as an explicit import
        would not be either: the object is still where it is defined."""
        parent = obj.parent
        return (
            getattr(obj, "wildcard_imported", False)
            and not getattr(parent, "is_init_module", False)
            and not self._listed(parent, obj.name)
            and not _has_private_segment(str(obj.target_path))
        )

    def _maybe_inherited(self, obj: Any) -> bool:
        """Could the new class still have ``obj`` through a base from outside the package?

        A class that drops its own ``keys`` but still derives from ``UserDict`` keeps ``keys``.
        Bases from the standard library are looked up; for any other outside base, a method
        that called the same method of its base (``super().get(...)``) was an override of it,
        and an attribute the old class set in a method (``self.deprecated = deprecated``) that
        the new constructor hands to the base's (``super().__init__(deprecated=...)``, a
        ``**kwargs`` with the key, or no constructor of its own, so that the base's runs) may
        now be set by the base, which since-cutoff did not read (fastapi 0.143's ``Param
        (pydantic.fields.FieldInfo)`` passes ``kwargs["deprecated"]`` on): it is unknown, not
        removed, and the class is an unread entry (UNREAD_BASE), so that the scan says its
        attributes were not compared. A field or constant of the class body (a pydantic
        model's ``usage: int``), and an attribute the constructor neither sets nor hands on
        (fastapi 0.143's ``APIRoute.secure_cloned_response_field``), are removed.

        First, though, an attribute the new class still sets where griffe does not look: in a
        tuple target (``self.dependant, _ = build(...)``), or in a function of its module the
        constructor gives ``self`` to (fastapi 0.143's ``_populate_api_route_state(self,
        path, ...)`` sets ``route.path``). It is there, whatever the bases.
        """
        cls = self.new_object(obj.parent.path)
        if not getattr(cls, "is_class", False):
            return False
        attribute = _kind_of(obj) == "attribute"
        if attribute and obj.name in self._attributes_set(cls):
            return True
        package = self.new.path.split(".")[0]
        for base in getattr(cls, "bases", None) or []:
            path = str(base if isinstance(base, str) else getattr(base, "canonical_path", "") or "")
            if not path or path.split(".", 1)[0] == package:
                continue  # griffe follows the package's own bases
            found = _outside_attribute(_typing_path(path), obj.name)
            if found is None:
                found = self._vendored_attribute(path, obj.name)
            if found is None and path.startswith(_TYPING_MODULES):
                found = False  # typing's constructs (TypedDict, Generic) set no attributes
            if found or (found is None and _calls_super(obj)):
                return True
            if (
                found is None
                and attribute
                and obj.name not in _class_body_names(self._class_def(obj.parent))
                and _handed_to_base(self._init_def(cls), obj.name)
            ):
                owner = self.public_path(str(cls.path)) or str(cls.path)
                self.unread.add(f"{owner}{UNREAD_BASE}{path}")
                return True
        return False

    def _attributes_set(self, cls: Any) -> set[str]:
        """The attributes the (new) class's own methods refer to on ``self``, read or set, a
        tuple target included, and those a function of its module that a method gives
        ``self`` to assigns on it. griffe lists an attribute only from a plain ``self.x = ...``
        in a method."""
        node = self._class_def(cls)
        if node is None:
            return set()
        module = getattr(cls, "module", None)
        tree = self._source(module).tree if getattr(module, "is_module", False) else None
        return _self_attributes(node) | _set_by_helpers(node, tree)

    def _class_def(self, cls: Any) -> ast.ClassDef | None:
        """The parsed ``class`` statement of a griffe class (None when its source cannot be
        read), kept per class."""
        key = id(cls)
        if key not in self._class_defs:
            self._class_defs[key] = _class_def(cls)
        return self._class_defs[key]

    def _init_def(self, cls: Any) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """The parsed constructor the (new) class runs: its own ``__init__``, or one from a
        base of the package; None when it has none the package defines (the outside base's
        runs) or its source cannot be read."""
        try:
            init = cls.all_members.get("__init__")
            if getattr(init, "is_alias", False):
                init = init.final_target
        except Exception:
            return None
        if not getattr(init, "is_function", False):
            return None
        return _function_def(init)

    def _vendored_attribute(self, path: str, name: str) -> bool | None:
        """Whether the package's own copy of the outside class at ``path`` has ``name``: from
        Python 3.12, setuptools serves ``distutils`` from ``setuptools._distutils`` (pip vendors
        packages in ``pip._vendor``). None when the package has no copy of it."""
        package = self.new.path.split(".")[0]
        for prefix in ("_", "_vendor.", "extern."):
            cls = self.new_object(f"{package}.{prefix}{path}")
            if getattr(cls, "is_class", False):
                try:
                    return name in cls.all_members
                except Exception:
                    return None
        return None

    def _now_a_method(self, obj: Any) -> bool:
        """Was this function attached to a class (``Document.new_page = utils.new_page``) that
        now defines the method itself? Callers use the method, which is still there."""
        modules = [obj.parent, getattr(obj.parent, "parent", None), self.old]
        for module in dict.fromkeys(m for m in modules if getattr(m, "is_module", False)):
            tree = self._source(module).tree
            for node in tree.body if tree is not None else []:
                if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                    continue
                target, value = node.targets[0], node.value
                if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
                    continue
                if (value.attr if isinstance(value, ast.Attribute) else _name(value)) != obj.name:
                    continue
                cls = _get(module, target.value.id)
                new_cls: Any = (
                    self.new_object(cls.path) if getattr(cls, "is_class", False) else None
                )
                if getattr(new_cls, "is_class", False) and target.attr in new_cls.members:
                    return True
        return False

    def _served(self, obj: Any) -> tuple[bool, str | None]:
        """Does the new version still hand out this removed name, and with which warning?

        Modules serve names through ``__getattr__`` (click, mypy_extensions) or a table of
        deprecated aliases (anyio.abc); classes through a metaclass property (urllib3's
        ``Retry.BACKOFF_MAX``). Returns ``(served, warning)``: the warning text, "" when the code
        warns without a readable text, None when it serves the name without a warning.
        """
        parent: Any = getattr(obj, "parent", None)
        if getattr(parent, "is_class", False):
            return self._served_by_metaclass(obj)
        module = self.new_object(parent.path) if getattr(parent, "is_module", False) else None
        tree = self._source(module).tree if getattr(module, "is_module", False) else None
        if tree is None:
            return False, None
        statements = list(_module_level(tree.body))
        hook = next(
            (s for s in statements if isinstance(s, _FUNCTIONS) and s.name == "__getattr__"),
            None,
        )
        found: tuple[bool, str | None] | None = None
        for node in statements:
            if isinstance(node, _FUNCTIONS):
                if hook is None:
                    continue
            elif isinstance(node, (ast.Expr, ast.Assign)) and isinstance(node.value, ast.Call):
                if not re.search(r"deprecat|lazy|alias", _name(node.value.func), re.IGNORECASE):
                    continue
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                # A table the hook reads: {"BadHeaderError": "BadHeaderError is deprecated..."}
                if hook is None or not any(k and _is_string(k, obj.name) for k in node.value.keys):
                    continue
            else:
                continue
            if not any(_is_string(n, obj.name) for n in ast.walk(node)):
                continue
            scope = _branch(node, obj.name)
            warning = _warning_text(scope, obj.name)
            if warning:
                self._served_scopes[str(obj.path)] = (module, scope)
                return True, warning
            if found is None:  # keep looking for a message: the hook may name it elsewhere
                found = (True, "" if "deprecat" in ast.unparse(scope).lower() else None)
                if found[1] is not None:
                    self._served_scopes[str(obj.path)] = (module, scope)
        return found or self._served_lazily(obj, module, statements)

    def _served_warning(self, obj: Any) -> dict[str, Any]:
        """APIChange.warning of a removed name the new release still serves with a warning
        (:meth:`_served`): issued when the name is read (WARNS_ACCESS), with the category and
        the line of the code that serves it, when they can be read ("" and None else)."""
        module, scope = self._served_scopes.get(str(obj.path), (None, None))
        category, at = "", None
        if scope is not None:
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call) or not _HELPER_NAME.search(_name(node.func)):
                    continue
                kind = node.args[1] if len(node.args) > 1 else None
                kind = next((k.value for k in node.keywords if k.arg == "category"), kind)
                if kind is None and node.args and isinstance(node.args[0], ast.Call):
                    kind = node.args[0].func
                name = _name(kind) if kind is not None else ""
                if name.endswith("Warning"):
                    category = name
                    break
            file = _source_file(module) if module is not None else None
            at = f"{file}:{scope.lineno}" if file and hasattr(scope, "lineno") else None
        return {"when": WARNS_ACCESS, "category": category, "at": at, "access": "read"}

    def _served_lazily(
        self, obj: Any, module: Any, statements: list[ast.stmt]
    ) -> tuple[bool, str | None]:
        """Does the module replace itself with a module object whose class's ``__getattr__``
        serves the name by its suffix? transformers 5 (``sys.modules[__name__] =
        _LazyModule(...)``) hands out ``T5TokenizerFast`` as ``T5Tokenizer``: after ``if
        name.endswith("TokenizerFast")``, the name without its last 4 letters.

        Such a module also serves what the ``__all__`` of the modules below it lists (its
        import structure): ``transformers.BartTokenizerFast`` from ``models/bart``; and, under
        the guard, a name that is a key of a table the hook tests it against
        (``ElectraTokenizer in SLOW_TO_FAST_CONVERTERS``).

        Returns ``(served, warning)`` as :meth:`_served` does.
        """
        key = id(module)
        if key not in self._lazy_hooks:
            self._lazy_hooks[key] = _lazy_hook(module, statements)
        found = self._lazy_hooks[key]
        if found is None:
            return False, None
        hook, tree = found
        if self._listed_below(module, obj.name):
            return True, None
        for guard in ast.walk(tree):
            if not any(obj.name.endswith(s) for s in _suffix_tests(guard)):
                continue
            tables = set().union(
                *(self._table_keys(hook, tree, t) for t in _membership_tables(guard))
            )
            for cut in _cuts(guard):
                fallback = obj.name[:-cut]
                if fallback and (
                    _get(module, fallback) is not None
                    or self._listed_below(module, fallback)
                    or fallback in tables
                ):
                    if "deprecat" not in ast.unparse(guard).lower():
                        return True, None
                    self._served_scopes[str(obj.path)] = (None, guard)
                    return True, _warning_text(guard, obj.name)
        return False, None

    def _table_keys(self, hook: Any, tree: ast.AST, name: str) -> set[str]:
        """The string keys of the dict literal ``name`` stands for in the module of ``hook``
        (parsed as ``tree``), or in the module the hook imports it from (``from
        ..convert_slow_tokenizer import X``)."""
        key = (id(hook), name)
        if key not in self._tables:
            self._tables[key] = self._find_table_keys(hook, tree, name)
        return self._tables[key]

    def _find_table_keys(self, hook: Any, tree: ast.AST, name: str) -> set[str]:
        module = hook.parent
        while module is not None and not getattr(module, "is_module", False):
            module = module.parent
        if module is None:
            return set()
        path = f"{module.path}.{name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names):
                package = module.path.split(".")
                base = package[: len(package) - node.level] if node.level else []
                path = ".".join([*base, *([node.module] if node.module else []), name])
        table = self.new_object(path)
        try:
            value = ast.parse(str(getattr(table, "value", "") or ""), mode="eval").body
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return set()
        if not isinstance(value, ast.Dict):
            return set()
        return {
            k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }

    def _listed_below(self, module: Any, name: str) -> bool:
        """Does a module below ``module`` list ``name`` in its ``__all__``?"""
        key = id(module)
        if key not in self._exported_below:
            names: set[str] = set()
            stack = [module]
            while stack:
                for member in list(stack.pop().members.values()):
                    if not getattr(member, "is_alias", False) and member.is_module:
                        stack.append(member)
                        exports = getattr(member, "exports", None) or ()
                        names.update(
                            e if isinstance(e, str) else getattr(e, "name", str(e)) for e in exports
                        )
            self._exported_below[key] = names
        return name in self._exported_below[key]

    def _served_by_metaclass(self, obj: Any) -> tuple[bool, str | None]:
        cls = self.new_object(obj.parent.path)
        if not getattr(cls, "is_class", False):
            return False, None
        expressions = [(getattr(cls, "keywords", None) or {}).get("metaclass")]
        for dec in getattr(cls, "decorators", None) or []:
            if str(getattr(dec, "callable_path", "")).endswith("add_metaclass"):
                expressions.extend(getattr(dec.value, "arguments", None) or [])
        for expr in expressions:
            path = getattr(expr, "canonical_path", None)
            meta: Any = find_object(self.new, str(path)) if path else None
            member = meta.members.get(obj.name) if getattr(meta, "is_class", False) else None
            if member is None:
                continue
            try:
                with _quiet():
                    tree: ast.AST = ast.parse(textwrap.dedent(member.source))
            except Exception:
                return True, None
            if "deprecat" not in ast.unparse(tree).lower():
                return True, None
            self._served_scopes[str(obj.path)] = (None, tree)
            return True, _warning_text(tree, obj.name)
        return False, None

    # ------------------------------------------------------- import paths
    def import_paths(self, change: APIChange, old_obj: Any, new_obj: Any) -> list[str]:
        """Every public path that leads to the changed object (APIChange.import_paths).

        The old version's paths and the new version's: code written for either imports it
        under one of them. For a module-level removal, only the old paths that the new version
        no longer has: when only a re-export was dropped (``pkg.fetch``), the object's own
        module still serves it (``pkg.helpers.fetch``), and code importing it from there is
        not affected. And only those under the removed name: what goes is a name, and another
        name of the same object (``DeprecatedIn45`` for ``DeprecatedIn37``, both
        ``= CryptographyDeprecationWarning``) is removed, or kept, on its own.
        """
        old_paths = self.paths_in(self.old, old_obj)
        if change.kind in (REMOVED, MOVED) and not getattr(
            getattr(old_obj, "parent", None), "is_class", False
        ):
            names = {change.name, change.path.rsplit(".", 1)[-1]}
            old_paths = [
                p
                for p in old_paths
                if p.rsplit(".", 1)[-1] in names and find_object(self.new, p) is None
            ]
        return _ordered([change.path, *old_paths, *self.paths_in(self.new, new_obj)])

    def paths_in(self, tree: Any, obj: Any, depth: int = 0) -> list[str]:
        """The public paths of ``obj`` in ``tree`` (the old or the new version): a
        module-level object's (see :meth:`_path_index`), or, for a class member, each path of
        its class with the member's name (an inherited member under its subclass)."""
        if obj is None or depth > 10:
            return []
        parent = getattr(obj, "parent", None)
        if getattr(parent, "is_class", False):
            return [f"{p}.{obj.name}" for p in self.paths_in(tree, parent, depth + 1)]
        try:
            if getattr(obj, "is_alias", False):
                obj = obj.final_target
            return list(self._path_index(tree).get(str(obj.path), ()))
        except Exception:
            return []

    def _path_index(self, tree: Any) -> dict[str, list[str]]:
        key = id(tree)
        if key not in self._path_indexes:
            self._path_indexes[key] = path_index(tree)
        return self._path_indexes[key]

    def still_handled(self, fn: Any, parameter: str) -> tuple[str | None, str | None]:
        """Where the new version's source still reads a removed parameter by name, as
        ``pkg/module.py:line``, and what that code says of it (APIChange.still_handled_at and
        still_handled_text); (None, None) when it does not.

        Looked for in the callable's decorators from the package itself, and in the functions
        of their module that they call: a decorator is what can take a keyword the signature
        no longer has, and drop it with a warning (huggingface_hub 2.0's
        ``@validate_hf_hub_args`` calls ``smoothly_deprecate_legacy_arguments``, which pops
        ``resume_download``, and whose docstring says it is "deprecated without replacement").
        """
        package = self.new.path.split(".")[0]
        for dec in getattr(fn, "decorators", None) or []:
            path = str(getattr(dec, "callable_path", "") or "")
            if not path.startswith(package + "."):
                continue
            try:
                target = fn.modules_collection.get_member(path)
                if getattr(target, "is_alias", False):
                    target = target.final_target
            except Exception:
                continue
            module = getattr(target, "parent", None)
            if not getattr(module, "is_module", False):
                continue  # a method used as a decorator: not followed
            tree = self._source(module).tree
            defined = {
                n.name: n
                for n in (tree.body if tree is not None else ())
                if isinstance(n, (*_FUNCTIONS, ast.ClassDef))
            }
            decorator = defined.get(target.name)
            if decorator is None:
                continue
            called = {_name(n.func) for n in ast.walk(decorator) if isinstance(n, ast.Call)}
            scope = [decorator, *(defined[n] for n in sorted(called) if n in defined)]
            lines = [line for node in scope if (line := _reads_key(node, parameter))]
            if lines:
                return f"{_source_file(module)}:{min(lines)}", _handled_text(scope, parameter)
        return None, None

    def library_names(self, change: APIChange, fn: Any) -> list[str]:
        """APIChange.library_names: the names the library's deprecation text for ``change``
        gives that exist in the new version.

        For a parameter change (``fn`` is the new callable), the parameters of ``fn`` come
        first; then functions and classes of the new API, under their public paths. Only names
        written as code count (in backticks, called, dotted, snake_case or CamelCase): a plain
        word of the text that happens to name a function ("login") is not evidence.
        """
        text = " ".join(t for t in (change.hint, change.deprecation) if t)
        if not text:
            return []
        found: list[str] = []
        if fn is not None and change.parameter:
            try:
                parameters = {p.name for p in fn.parameters}
            except Exception:
                parameters = set()
            found += parameter_replacements(text, parameters, change.parameter)
        found += object_replacements(change, text, self.new)
        return list(dict.fromkeys(found))

    # ------------------------------------------------------- public-ness
    def public_map(self) -> dict[str, str]:
        """Map canonical paths inside private modules to the public paths that expose them.

        Built from both versions (old wins), following re-export chains to their final target,
        so paths reported by griffe for either version can be mapped.
        """
        if self._public_map is None:
            mapping: dict[str, str] = {}
            for tree in (self.new, self.old):
                for obj, public in iter_public_objects(tree):
                    if not _has_private_segment(obj.path):
                        continue
                    current = mapping.get(obj.path)
                    if current is None or tree is self.old or len(public) < len(current):
                        mapping[obj.path] = public
            self._public_map = mapping
        return self._public_map

    def public_path(self, path: str) -> str | None:
        if not _has_private_segment(path):
            return path
        mapping = self.public_map()
        parts = path.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in mapping:
                rest = parts[i:]
                if any(_private(p) for p in rest):
                    return None
                return ".".join([mapping[prefix], *rest])
        return None

    def is_public(self, path: str, *, module: bool = False) -> bool:
        if any(_skipped_segment(p) for p in path.split(".")) or self._in_template(path):
            return False
        public = self.public_path(path)
        return public is not None and not _non_api_path(public, module=module)

    def _in_template(self, path: str) -> bool:
        """Is ``path`` in a code generator's template (:func:`_is_template`: transformers'
        ``modular_*.py``), in either release?"""
        parts = path.split(".")
        for i, part in enumerate(parts[1:], start=2):
            if _TEMPLATE_MODULE.fullmatch(part):
                prefix = ".".join(parts[:i])
                found = self.new_object(prefix) or _get(self.old, _rel(prefix, self.old.path))
                if getattr(found, "is_module", False) and _is_template(found):
                    return True
        return False

    # ------------------------------------------------------------- lookups
    def new_object(self, path: str | None) -> Any:
        return _get(self.new, _rel(path, self.new.path)) if path else None

    def similar_names(self, obj: Any) -> list[str]:
        """Public names in the same (new) owner that look like replacements for ``obj``.

        Only names the old owner did not have (one that existed next to the removed name is not
        its replacement), of the same sort (callables for callables, values for values, modules
        for modules), and no imports of other objects (``operator``, ``typing.List``).
        """
        parent: Any = getattr(obj, "parent", None)
        owner = self.new_object(parent.path) if parent is not None else None
        if owner is None:
            return []
        sort = _sort_of(obj)
        try:
            before = set(parent.all_members if parent.is_class else parent.members)
            members = list(owner.members.items())
        except Exception:
            return []
        names = []
        for name, member in members:
            if name.startswith("_") or name in before:
                continue
            if getattr(member, "is_alias", False):
                if not _exported(member):
                    continue
                try:
                    member = member.final_target
                except Exception:
                    continue
            if _sort_of(member) == sort:
                names.append(name)
        return _close(names, obj.name)

    def lost_bases(self, class_path: str) -> bool:
        """Does the new version of this class have bases that cannot be followed to a class?"""
        if class_path not in self._lost_bases:
            new_cls = self.new_object(class_path)
            lost = False
            if getattr(new_cls, "is_class", False):
                lost = _unresolved_ancestry(new_cls, self.new.path.split(".")[0], frozenset())
            self._lost_bases[class_path] = lost
        return self._lost_bases[class_path]

    def new_index(self) -> dict[str, list[str]]:
        """The public paths of the new version's objects, by name."""
        if self._new_index is None:
            index: dict[str, list[str]] = {}
            for o, public in iter_public_objects(self.new):
                index.setdefault(o.name, []).append(public)
            self._new_index = index
        return self._new_index

    def find_moved(self, obj: Any) -> tuple[str | None, str | None, dict[str, Any] | None]:
        """If a removed module-level object reappears elsewhere in the public API, report it.

        Returns ``(moved_to, namesake, evidence)``. The name alone proves nothing (``LOGGER``
        or ``T`` is defined in dozens of modules), so the candidate must look like the same
        object: a class or module keeps at least half of its public names (for a module, the
        one keeping the most; a tie is no move), a function keeps its parameters, a value is
        the same literal or type expression. A class that fails is only a namesake (an
        unrelated class with the same name). ``evidence`` is what was counted for the move
        (APIChange.move_evidence).
        """
        name = obj.name
        if name.startswith("_") or name in _PRIVATE_EXEMPT:
            return None, None, None
        parent = getattr(obj, "parent", None)
        if parent is None or not getattr(parent, "is_module", False):
            # Class members: a same-named method elsewhere is almost never a move.
            return None, None, None
        old_kind = _kind_of(obj)
        # Protobuf < 4 built each message class at import; newer generated code declares it in
        # a .pyi stub, so the same message is now a class (qdrant-client's grpc modules).
        message = _generated_message(obj)

        def plausible(p: str) -> bool:
            target = self.new_object(p)
            if p == obj.path or target is None:
                return False
            if _kind_of(target) != old_kind and not (message and _kind_of(target) == "class"):
                return False
            if not getattr(getattr(target, "parent", None), "is_module", False):
                return False
            # A different object that already existed under that path is not a move.
            previous = _get(self.old, _rel(p, self.old.path))
            return previous is None or previous.path == obj.path

        candidates = [p for p in self.new_index().get(name, []) if plausible(p)]
        if not candidates:
            return None, None, None
        old_parts = obj.path.split(".")

        def score(p: str) -> tuple[int, int]:
            parts = p.split(".")
            common = 0
            for a, c in zip(old_parts, parts, strict=False):
                if a != c:
                    break
                common += 1
            return (-common, len(parts))

        candidates.sort(key=score)
        if message:
            classes = [p for p in candidates if _kind_of(self.new_object(p)) == "class"]
            if not classes:
                return None, None, None
            return classes[0], None, _evidence("message", (0, 0))
        if old_kind == "class":
            for p in candidates:
                counts = _class_kept(obj, self.new_object(p))
                if counts is not None and 2 * counts[0] >= counts[1]:
                    return p, None, _evidence("class", counts)
            return None, candidates[0], None
        if old_kind == "module":
            kept = {p: _module_kept(obj, self.new_object(p)) for p in candidates}
            share = {p: k / n if n else 0.0 for p, (k, n) in kept.items()}
            ranked = sorted((p for p in candidates if share[p] >= 0.5), key=lambda p: -share[p])
            if len(ranked) > 1 and share[ranked[0]] == share[ranked[1]]:
                return None, None, None  # several look alike: no way to tell which one it became
            if not ranked:
                return None, None, None
            return ranked[0], None, _evidence("module", kept[ranked[0]])
        for p in candidates:
            counts = _object_kept(obj, self.new_object(p))
            if counts is not None:
                return p, None, _evidence("function" if old_kind == "function" else "value", counts)
        return None, None, None

    def old_module_level_names(self) -> set[str]:
        """The names of the old version's public module-level objects (modules, classes,
        functions, values), whatever their module."""
        if self._old_names is None:
            self._old_names = {
                o.name
                for o, _ in iter_public_objects(self.old)
                if getattr(getattr(o, "parent", None), "is_module", False)
            }
        return self._old_names

    def find_renamed(self, obj: Any) -> tuple[str | None, dict[str, Any] | None]:
        """A removed module-level class or function that came back under another name:
        ``(moved_to, evidence)``, or ``(None, None)``.

        Looked for in the module the object was in, in the new version, or in the module that
        one moved to (mcp 2's ``mcp.server.fastmcp.server`` is ``mcp.server.mcpserver.server``),
        among the objects of the same kind that did not exist anywhere in the old version's
        public API. The candidate must keep the old one's public members: a class's own public
        names, a function's public parameters, at least 80% of them and at least 5 (or all, of
        fewer). Never for a tiny object (fewer than 3 members) or when two candidates qualify,
        and a function's or a value type's (a class of fields alone) new name must also contain
        the old one, or be contained in it: too many of those share their members. The one
        exception is a name that differs in case or underscores only (``McpError`` ->
        ``MCPError``), taken with every member kept, however few. The evidence is find_moved's,
        with ``"renamed": True``.
        """
        parent = getattr(obj, "parent", None)
        kind = _kind_of(obj)
        if parent is None or not getattr(parent, "is_module", False):
            return None, None
        if kind not in ("class", "function") or obj.name.startswith("_"):
            return None, None
        module = self.new_object(parent.path)
        if module is None or not getattr(module, "is_module", False):
            moved_module, _, _ = self.find_moved(parent)
            module = self.new_object(moved_module) if moved_module else None
        if module is None:
            return None, None
        try:
            members = list(module.members.items())
        except Exception:
            return None, None
        package = self.new.path.split(".")[0]
        old_names = self.old_module_level_names()
        found: list[tuple[str, Any, tuple[int, int]]] = []
        for name, member in members:
            if name == obj.name or _private(name) or name in old_names:
                continue
            target = member
            if getattr(member, "is_alias", False):
                if not _exported(member):
                    continue
                try:
                    target = member.final_target
                except Exception:
                    continue
                if not str(target.path).startswith(package + "."):
                    continue
            if _kind_of(target) != kind:
                continue
            if _get(self.old, _rel(str(target.path), self.old.path)) is not None:
                continue  # it existed before: not what the removed one became
            counts = _rename_kept(obj, target)
            if counts is not None:
                found.append((name, target, counts))
        if len(found) != 1:
            return None, None  # nothing, or several look alike: no way to tell
        name, target, counts = found[0]
        public = [
            p
            for p in self.new_index().get(name, [])
            if getattr(self.new_object(p), "path", None) == target.path
        ]
        public.sort(key=lambda p: (p.count("."), len(p), p))
        moved_to = public[0] if public else self.public_path(str(target.path)) or str(target.path)
        evidence = _evidence(kind, counts)
        evidence["renamed"] = True
        return moved_to, evidence


def _rename_kept(old: Any, new: Any) -> tuple[int, int] | None:
    """How many of the public members of ``old`` (a class's own public names, a function's
    public parameters) ``new`` keeps, of how many, when that is enough for ``new`` to be
    ``old`` under another name (find_renamed); None otherwise.

    A class keeps a name when it defines it itself, or inherits it from a base the old class
    did not have (:func:`_names_of_its_own`): the methods a new sibling subclass inherits from
    the base both share say nothing about it. Names unrelated to the old one need five such
    members (``Task`` -> ``Worker`` on ``run``, ``close`` and ``start`` is no rename); related
    names (``FastThing`` -> ``Thing``) three, or all of fewer than five.
    """
    kind = _kind_of(old)
    try:
        if kind == "class":
            names = _public_names(old, inherited=False)
            kept = len(names & _names_of_its_own(old, new))
            value_type = all(
                _kind_of(m) == "attribute" for n, m in old.members.items() if n in names
            )
        elif kind == "function":
            names = {p.name for p in old.parameters if _public_parameter(p.name)}
            kept = len(names & {p.name for p in new.parameters if _public_parameter(p.name)})
            value_type = True  # parameters are shared as widely as fields: the name must help
        else:
            return None
    except Exception:
        return None
    total = len(names)
    same_name = _same_name_spelt(old.name, new.name)
    if same_name:
        return (kept, total) if total and kept == total else None
    related = _names_related(old.name, new.name)
    if total < (3 if related else 5) or kept < min(5, total) or 5 * kept < 4 * total:
        return None
    if value_type and not related:
        return None
    return kept, total


def _names_of_its_own(old: Any, new: Any) -> set[str]:
    """The public names of the class ``new`` that count towards its being ``old`` under
    another name: those it defines itself, and those it inherits from a class that was not
    among ``old``'s bases (a base new to it, holding what ``old`` used to define). What it
    inherits from a base ``old`` also had is the base's, not evidence."""
    own = _public_names(new, inherited=False)
    try:
        ancestors = {str(c.path) for c in old.mro()}
        inherited = list(new.inherited_members.items())
    except Exception:
        return own
    for name, member in inherited:
        if name.startswith("_") or name in own:
            continue
        try:
            origin = str(member.final_target.parent.path)
        except Exception:
            continue
        if origin not in ancestors:
            own.add(name)
    return own


def _same_name_spelt(a: str, b: str) -> bool:
    """``McpError`` and ``MCPError``, ``get_json`` and ``getJSON``: the same name but for case
    and underscores."""
    return a.lower().replace("_", "") == b.lower().replace("_", "")


def _names_related(a: str, b: str) -> bool:
    """Whether one name contains the other at its start or end (``RequestContext`` and
    ``ServerRequestContext``), case and underscores aside; the shorter must have at least four
    letters, or ``Base`` and ``Error`` would relate everything."""
    x, y = a.lower().replace("_", ""), b.lower().replace("_", "")
    if x == y:
        return True
    short, long = sorted((x, y), key=len)
    return len(short) >= 4 and (long.startswith(short) or long.endswith(short))


def _origin(obj: Any) -> str:
    """Canonical path of the member an inherited alias stands for."""
    try:
        return str(obj.final_target.path)
    except Exception:
        return str(obj.path)


def _rel(path: str | None, root: str) -> str:
    """``path`` relative to the loaded module ``root``.

    ``root`` is usually the top-level package, but for a namespace distribution it is deeper
    (``google.genai``): stripping only the first segment would miss every object in it.
    """
    if not path or path == root:
        return ""
    if path.startswith(root + "."):
        return path[len(root) + 1 :]
    return path.split(".", 1)[1] if "." in path else ""


def _pkind(param: Any) -> str:
    kind = getattr(param, "kind", None)
    return str(getattr(kind, "name", kind or ""))


def _renamed(fn: Any, other: Any, param: str) -> Any:
    """The parameter of ``other`` that ``param`` of ``fn`` became (or came from), or None.

    That is a positional rename: ``other`` has a positional parameter of another name at the
    same position, which ``fn`` does not have, and ``param`` is gone from ``other``.
    """
    try:
        mine = [p for p in fn.parameters if _pkind(p) in _POSITIONAL]
        theirs = [p for p in other.parameters if _pkind(p) in _POSITIONAL]
        names = {p.name for p in fn.parameters}
        other_names = {p.name for p in other.parameters}
    except Exception:
        return None
    index = next((i for i, p in enumerate(mine) if p.name == param), None)
    if index is None or index >= len(theirs) or param in other_names:
        return None
    return theirs[index] if theirs[index].name not in names else None


def _rename_hint(old: Any, new: Any, fn: Any) -> list[str]:
    """The new name of a positional parameter renamed in place, when it looks like the same
    argument (a public name, the same annotation where both have one) and the docstring of
    ``fn``, the new callable, does not say that one went and the other came."""
    before, after = getattr(old, "annotation", None), getattr(new, "annotation", None)
    if new.name.startswith("_") or (before and after and str(before) != str(after)):
        return []
    if _documented_apart(fn, old.name, new.name):
        return []
    return [new.name]


def _documented_apart(fn: Any, removed: str, added: str) -> bool:
    """Does a version note in the new version's docstring (the callable's, and its class's for
    ``__init__``) say that ``removed`` was removed, or that ``added`` was added, without naming
    the other? Then the parameter in the same place is a new one, not a rename: click 8.2's
    ``CliRunner`` took ``catch_exceptions: bool = True`` where 8.1 took ``mix_stderr: bool =
    True``, and notes ".. versionchanged:: 8.2 Added the ``catch_exceptions`` parameter." and
    ".. versionchanged:: 8.2 ``mix_stderr`` parameter has been removed.". Only version notes
    count: "New ``start`` values ..." describing a parameter says nothing about its name."""
    owners = [fn, getattr(fn, "parent", None)] if getattr(fn, "name", "") == "__init__" else [fn]
    for owner in owners:
        lines = _doc_lines(owner)
        for i, line in enumerate(lines):
            note = _VERSION_NOTE.match(line.strip())
            if note is None:
                continue
            text = _sentence(lines, i) or ""
            if _mentions(text, removed) == _mentions(text, added):
                continue  # neither, or a replacement ("removed in favour of ``new``")
            if _mentions(text, removed) and re.search(r"\bremoved\b", text, re.IGNORECASE):
                return True
            said = text[len(note.group()) :].lstrip()
            new = note.group(1).lower() == "added" or re.match(r"(?:added|new)\b", said, re.I)
            if _mentions(text, added) and new:
                return True
    return False


# A Sphinx version note: ".. versionadded:: 2.0", ".. versionchanged:: 2.0".
_VERSION_NOTE = re.compile(r"\.\.\s*version(added|changed)::\s*\S*", re.IGNORECASE)


def _required(param: Any) -> bool:
    if param is None:
        return False
    return getattr(param, "default", None) is None and _pkind(param) not in _VARIADIC


def _requires(fn: Any, name: str) -> bool:
    try:
        return any(p.name == name and _required(p) for p in fn.parameters)
    except Exception:
        return False


def _absorbed(old: Any, new: Any, kind: str) -> bool:
    """Does the new signature take what the old one's ``*args`` or ``**kwargs`` took?

    Under another name (``**kw``), or as parameters it names now (``timeout=`` instead of a
    ``**kwargs`` passed through): the calls that worked still do.
    """
    usable = {kind, "positional_or_keyword"}
    usable.add("keyword_only" if kind == "var_keyword" else "positional_only")
    try:
        known = {p.name for p in old.parameters}
        return any(_pkind(p) in usable for p in new.parameters if p.name not in known)
    except Exception:
        return False


def _fixture(fn: Any) -> bool:
    """Is this function a pytest fixture (``@pytest.fixture``, ``@pytest.fixture(...)``)?"""
    for dec in getattr(fn, "decorators", None) or []:
        path = str(getattr(dec, "callable_path", "") or "")
        if path in ("pytest.fixture", "_pytest.fixtures.fixture", "pytest_asyncio.fixture"):
            return True
    return False


def _special_form(fn: Any) -> bool:
    """Is this function wrapped by a class of its package (``@_TypedDictSpecialForm``)?

    A bare class decorator turns the function into an instance of that class, which callers
    call through the class's ``__call__``: the function's own signature is not the call's.
    """
    for dec in getattr(fn, "decorators", None) or []:
        path = str(getattr(dec, "callable_path", "") or "")
        if "." not in path or "(" in str(dec.value):
            continue  # a call such as ``@deprecated("...")`` returns a wrapper of the function
        try:
            target = fn.modules_collection.get_member(path)
            if getattr(target, "is_alias", False):
                target = target.final_target
        except Exception:
            continue
        if getattr(target, "is_class", False):
            return True
    return False


def _is_attribute_like(obj: Any) -> bool:
    """Plain attributes and property-like functions are read the same way.

    Any decorator whose name ends in "property" counts: ``property``, ``cached_property``,
    and descriptors such as pydantic's ``deprecated_instance_property`` that make a
    classmethod readable as an attribute.
    """
    if getattr(obj, "is_attribute", False):
        return True
    if "property" in (getattr(obj, "labels", None) or set()):
        return True
    for dec in getattr(obj, "decorators", None) or []:
        # The written name counts too: ``property = sphinx_accessor`` (polars) resolves elsewhere.
        for path in (str(getattr(dec, "callable_path", "") or ""), str(dec.value)):
            if path.split("(")[0].split(".")[-1].lower().endswith("property"):
                return True
    return False


def _compatible_kind_change(old: Any, new: Any, differ: _Differ) -> bool:
    """A kind change that code written for the old version does not notice.

    A function that became a class is still callable; an attribute and a (cached_)property are
    read the same way; an attribute and a type alias both work in annotations. A name bound to
    a value (``write_pack_index = write_pack_index_v2``, ``Scanned = namedtuple(...)``,
    ``BadHeaderError = ValueError``) that becomes a function or class, or the reverse, is
    compatible unless the value is plain data (a literal, ``x: int``): it may well be called
    the same way.
    """
    if new is None:
        return False
    before, after = _kind_of(old), _kind_of(new)
    if before == "function" and after == "class":
        return True  # functions that became classes are still callable
    if _is_attribute_like(old) and _is_attribute_like(new):
        return True  # attribute <-> (cached_)property: ``obj.name`` reads the same
    if {before, after} == {"attribute", "type alias"}:
        return True
    if before == "attribute" and after in ("function", "class"):
        return _maybe_callable(old, differ.old)
    if after == "attribute" and before in ("function", "class"):
        target = _value_target(new, differ.new)
        if target is not None and _kind_of(target) != "attribute":
            kind = _kind_of(target)
            return kind == before or (before == "function" and kind == "class")
        return _maybe_callable(new, differ.new)
    return False


def _value_target(attribute: Any, root: Any) -> Any:
    """The object of the package that an attribute names (``X = Y``, ``X = mod.Y``), if any.

    Only a name: for ``X = make()`` griffe's path is the callee's, not the value's.
    """
    value = getattr(attribute, "value", None)
    if type(value).__name__ not in ("ExprName", "ExprAttribute"):
        return None
    path = getattr(value, "canonical_path", None)
    return find_object(root, str(path)) if path else None


def _maybe_callable(attribute: Any, root: Any, depth: int = 0) -> bool:
    """Could this attribute's value be called? False only for plain data."""
    value = getattr(attribute, "value", None)
    if value is None:  # declared only (in a stub): judge by the annotation
        return not _data_annotation(str(getattr(attribute, "annotation", None) or ""))
    if _literal(str(value)) or _type_form(str(value)):
        return False
    target = _value_target(attribute, root)
    if target is None:
        return True  # built by a call, or named from another package: it may well be callable
    if _kind_of(target) == "attribute" and depth < 3:
        return _maybe_callable(target, root, depth + 1)
    return _kind_of(target) in ("function", "class")


def _type_form(text: str) -> bool:
    """``Union[A, B]``, ``A | B``, ``Literal["a"]``: a type that cannot be called."""
    try:
        node = ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return True
    return isinstance(node, ast.Subscript) and _name(node.value) in _TYPE_FORMS


def _literal(text: str) -> bool:
    try:
        ast.literal_eval(text)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _data_annotation(text: str) -> bool:
    """Does this annotation declare plain data (``int``, ``Final[str]``, ``list[str] | None``)?"""
    text = text.strip()
    while True:
        wrapped = re.fullmatch(r"(?:[\w.]+\.)?(?:Final|ClassVar)\[(.*)\]", text)
        if not wrapped:
            break
        text = wrapped.group(1).strip()
    head = re.split(r"[\[|\s]", text, maxsplit=1)[0]
    return head.rsplit(".", 1)[-1] in _DATA_TYPES


# ------------------------------------------------------------------ utilities
def _private(segment: str) -> bool:
    return segment.startswith("_") and segment not in _PRIVATE_EXEMPT


def _skipped_segment(segment: str) -> bool:
    return segment in _SKIP_SEGMENTS or segment.startswith("test_") or segment.endswith("_test")


def _non_api_path(path: str, *, module: bool = False) -> bool:
    """Is this inside (or, with ``module``, is it) a test suite, benchmark, example module, or
    a module of placeholders for a missing optional backend (``utils.dummy_pt_objects``)?

    Only module segments are checked, so a member named ``examples`` stays API. ``test`` and
    ``testing`` directly under the top-level package (``pandas.testing``) stay API too.
    """
    parts = path.split(".")
    for i, part in enumerate(parts if module else parts[:-1]):
        if (part in _NON_API_MODULES and i > 0) or (part in _TEST_MODULES and i > 1):
            return True
        if i > 0 and _PLACEHOLDER_MODULE.fullmatch(part):
            return True
    return False


def _is_template(module: Any) -> bool:
    """Is this module a code generator's template, next to the module generated from it
    (transformers' ``models/bert/modular_bert.py`` beside ``modeling_bert.py``, whose header
    says it is generated from it)? Nothing imports a template: it is no API."""
    found = _TEMPLATE_MODULE.fullmatch(str(getattr(module, "name", "")))
    if found is None:
        return False
    try:
        return f"modeling_{found.group(1)}" in module.parent.members
    except Exception:
        return False


def _has_private_segment(path: str) -> bool:
    return any(_private(p) for p in path.split(".")[1:])


def _kind_of(obj: Any) -> str | None:
    if obj is None:
        return None
    try:
        return str(obj.kind.value)
    except Exception:
        return None


def _kind_label(obj: Any) -> str | None:
    """griffe's kind, with "property" for functions that are read like attributes."""
    kind = _kind_of(obj)
    return "property" if kind == "function" and _is_attribute_like(obj) else kind


_BUILTIN_NAMES = frozenset(dir(builtins))


def _unresolved_ancestry(cls: Any, package: str, seen: frozenset[str]) -> bool:
    """True if a base class from this package, at any depth, cannot be followed to a class.

    griffe leaves such bases out of the MRO, so everything inherited through them looks
    removed. Bases from other packages are never loaded and do not count.
    """
    if cls.path in seen:
        return False
    seen = seen | {cls.path}
    for base in getattr(cls, "bases", None) or []:
        path = str(base if isinstance(base, str) else getattr(base, "canonical_path", "") or "")
        if "." not in path:
            if path in _BUILTIN_NAMES:
                continue
            return True  # a name griffe could not resolve (star import, computed base)
        if path.split(".", 1)[0] != package:
            continue
        try:
            target = cls.modules_collection.get_member(path)
            if getattr(target, "is_alias", False):
                target = target.final_target
        except Exception:
            return True
        if not getattr(target, "is_class", False) or _unresolved_ancestry(target, package, seen):
            return True
    return False


def _public_names(cls: Any, *, inherited: bool) -> set[str]:
    try:
        members = cls.all_members if inherited else cls.members
        return {n for n in members if not n.startswith("_")}
    except Exception:
        return set()


def _class_kept(old: Any, new: Any) -> tuple[int, int] | None:
    """How many of old's public members ``new`` still has, of how many: ``new`` could be
    ``old`` moved elsewhere if it keeps half of them (or there is nothing to compare, as in
    ``class Error(Base): pass``). None if ``new`` is not a class."""
    if not getattr(new, "is_class", False):
        return None
    names = _public_names(old, inherited=False) or _public_names(old, inherited=True)
    return len(names & _public_names(new, inherited=True)), len(names)


def _module_kept(old: Any, new: Any) -> tuple[int, int]:
    """How many of the public names a module defines (or, if it only imports, imports)
    ``new`` (a module) still has, of how many; ``(0, 0)`` if there is nothing to compare."""
    if not getattr(new, "is_module", False):
        return 0, 0
    try:
        names = {n for n, m in old.members.items() if not n.startswith("_") and not m.is_alias}
    except Exception:
        return 0, 0
    names = names or _public_names(old, inherited=False)
    return len(names & _public_names(new, inherited=False)), len(names)


def _evidence(compared: str, counts: tuple[int, int]) -> dict[str, Any]:
    """APIChange.move_evidence."""
    return {"compared": compared, "kept": counts[0], "of": counts[1]}


def _generated_message(obj: Any) -> bool:
    """``Filter = _reflection.GeneratedProtocolMessageType('Filter', ...)``: a protobuf
    message class, as protobuf's code generator wrote it before protobuf 4."""
    if _kind_of(obj) != "attribute":
        return False
    value = str(getattr(obj, "value", "") or "")
    name = re.escape(obj.name)
    return re.match(rf"[\w.]*GeneratedProtocolMessageType\(\s*['\"]{name}['\"]", value) is not None


def _object_kept(old: Any, new: Any) -> tuple[int, int] | None:
    """Could ``new`` be the function or value ``old``, moved? A function must take all of old's
    public parameters (none, if old took none); a value must be the same literal or type
    expression. Returns how many of old's public parameters ``new`` takes, of how many (a
    value: ``(1, 1)``), or None if it is not the same."""
    kind = _kind_of(old)
    if new is None or _kind_of(new) != kind:
        return None
    try:
        if kind == "function":
            before = [p.name for p in old.parameters if _public_parameter(p.name)]
            after = [p.name for p in new.parameters if _public_parameter(p.name)]
            if all(n in after for n in before) and (bool(before) or not after):
                return len(before), len(before)
            return None
        if kind in ("attribute", "type alias"):
            # The same literal or type expression. Not the same call: ``Console()`` or
            # ``getLogger(__name__)`` in two modules makes two different objects.
            value = str(old.value) if old.value is not None else ""
            same = bool(value) and value == str(new.value) and not _is_call(value)
            return (1, 1) if same else None
    except Exception:
        return None
    return None


def _is_call(text: str) -> bool:
    try:
        return isinstance(ast.parse(text, mode="eval").body, ast.Call)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return True


def _public_parameter(name: str) -> bool:
    return name not in ("self", "cls") and not name.startswith("_")


def _sort_of(obj: Any) -> str | None:
    """What a replacement must be to stand in for ``obj``: a callable, a value or a module."""
    kind = _kind_of(obj)
    if kind in ("function", "class"):
        return "value" if _is_attribute_like(obj) else "callable"
    return {"attribute": "value", "type alias": "value", "module": "module"}.get(kind or "")


def _exported(alias: Any) -> bool:
    """Is this import a re-export (``__all__``, an ``__init__`` re-export), not a plain import?"""
    try:
        return bool(alias.is_public)
    except Exception:
        return False


def _star_from_outside(member: Any, package: str) -> bool:
    """griffe's placeholder for a ``from x import *`` it could not follow, x being another
    package (not a module of this one that no longer exists)."""
    if not (getattr(member, "is_alias", False) and member.name.endswith("/*")):
        return False
    return not str(member.target_path).startswith(package + ".")


def _per_module_value(obj: Any) -> bool:
    """``logger = logging.getLogger(__name__)``, ``T = TypeVar("T")``: each module's own."""
    if not getattr(obj, "is_attribute", False):
        return False
    call = re.match(r"^[\w.]*?(\w+)\(", str(getattr(obj, "value", "") or ""))
    return call is not None and call.group(1) in _PER_MODULE_CALLS


def _outside_attribute(path: str, name: str) -> bool | None:
    """Whether the class at ``path`` (outside the package) has ``name``.

    Answered for builtins and the standard library, which are imported to look; None (unknown)
    for classes of other packages, which are never imported.
    """
    if "." not in path:
        cls = getattr(builtins, path, None)
        return hasattr(cls, name) if isinstance(cls, type) else None
    module, _, attr = path.rpartition(".")
    top = module.split(".", 1)[0]
    if top not in getattr(sys, "stdlib_module_names", ()) or top in _NO_IMPORT:
        return None
    try:
        with _quiet():
            cls = getattr(importlib.import_module(module), attr)
    except Exception:
        return None
    return hasattr(cls, name) if isinstance(cls, type) else None


def _calls_super(obj: Any) -> bool:
    """Does this method call the same method of its base (``super().get(...)``)?"""
    try:
        source = obj.source
    except Exception:
        return False
    pattern = rf"super\([^)]*\)\s*\.\s*{re.escape(obj.name)}\s*\("
    return bool(re.search(pattern, source or ""))


# ------------------------------------------------------ a class's own source
def _class_def(cls: Any) -> ast.ClassDef | None:
    """The parsed ``class`` statement of a griffe class, or None when its source cannot be
    read or parsed."""
    node = _parsed(cls)
    return node if isinstance(node, ast.ClassDef) else None


def _function_def(fn: Any) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The parsed ``def`` of a griffe function, or None."""
    node = _parsed(fn)
    return node if isinstance(node, _FUNCTIONS) else None


def _parsed(obj: Any) -> ast.stmt | None:
    """The parsed ``def`` or ``class`` statement of a griffe object, or None. A source whose
    docstring has a line less indented than the statement (matplotlib 3.10's ``Axes.bxp``)
    cannot be dedented as a whole: it is parsed as the body of an ``if``, which a string's
    lines do not need to line up with."""
    try:
        raw = obj.source or ""
        with _quiet():
            try:
                body = ast.parse(textwrap.dedent(raw)).body
            except SyntaxError:
                if not raw[:1].isspace():
                    return None
                wrapped = ast.increment_lineno(ast.parse("if True:\n" + raw).body[0], -1)
                body = wrapped.body if isinstance(wrapped, ast.If) else []
    except Exception:
        return None
    return next((n for n in body if isinstance(n, (ast.ClassDef, *_FUNCTIONS))), None)


def _class_body_names(node: ast.ClassDef | None) -> set[str]:
    """The names a class binds in its body: fields (``usage: int``), constants
    (``DEFAULT_TIMEOUT = 10``), methods and nested classes."""
    if node is None:
        return set()
    names: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, (ast.ClassDef, *_FUNCTIONS)):
            names.add(stmt.name)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            names |= {
                n.id
                for n in ast.walk(stmt)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            }
    return names


def _self_attributes(node: ast.ClassDef) -> set[str]:
    """The attributes the class's methods refer to on their first parameter (``self.x``, read
    or set, in a tuple target too)."""
    names: set[str] = set()
    for fn in node.body:
        me = _first_parameter(fn)
        if me is None:
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == me:
                names.add(n.attr)
    return names


def _set_by_helpers(node: ast.ClassDef, module: ast.Module | None) -> set[str]:
    """The attributes that functions of the class's module assign on the instance a method
    gives them (fastapi 0.143's ``APIRoute.__init__`` calls ``_populate_api_route_state(self,
    path, ...)``, which sets ``route.path``)."""
    if module is None:
        return set()
    functions = {n.name: n for n in module.body if isinstance(n, _FUNCTIONS)}
    names: set[str] = set()
    for fn in node.body:
        me = _first_parameter(fn)
        if me is None:
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            helper = functions.get(call.func.id)
            if helper is None:
                continue
            params = [a.arg for a in (*helper.args.posonlyargs, *helper.args.args)]
            given = {
                params[i] for i, a in enumerate(call.args) if _hands(a, me) and i < len(params)
            }
            given |= {kw.arg for kw in call.keywords if kw.arg and _hands(kw.value, me)}
            for n in ast.walk(helper):
                if (
                    isinstance(n, ast.Attribute)
                    and isinstance(n.ctx, ast.Store)
                    and isinstance(n.value, ast.Name)
                    and n.value.id in given
                ):
                    names.add(n.attr)
    return names


def _first_parameter(fn: ast.stmt) -> str | None:
    """The name a method's first parameter (``self``, ``cls``), or None for a static method
    or anything that is not a method."""
    if not isinstance(fn, _FUNCTIONS) or any(_name(d) == "staticmethod" for d in fn.decorator_list):
        return None
    first = [*fn.args.posonlyargs, *fn.args.args]
    return first[0].arg if first else None


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _hands(node: ast.AST, name: str) -> bool:
    """Is the argument ``name`` itself, or ``cast(SomeType, name)`` (fastapi 0.143 passes
    ``cast(_APIRouteLike, self)``)?"""
    if isinstance(node, ast.Call) and _name(node.func) == "cast" and len(node.args) == 2:
        node = node.args[1]
    return _is_name(node, name)


def _handed_to_base(init: ast.FunctionDef | ast.AsyncFunctionDef | None, name: str) -> bool:
    """Does the constructor give ``name`` to the base's: ``super().__init__(path, deprecated=
    ...)`` (``Base.__init__(self, ...)`` too), by name or as a positional of that name, or
    through a ``**kwargs`` whose dict its body gives the key (``kwargs["deprecated"] = ...``),
    or its own ``**kwargs`` passed on, which takes the name when no parameter does? Without a
    constructor (``init`` is None) the base's runs with the arguments: True."""
    if init is None:
        return True
    var_keyword = init.args.kwarg.arg if init.args.kwarg is not None else None
    params = {a.arg for a in (*init.args.posonlyargs, *init.args.args, *init.args.kwonlyargs)}
    for call in ast.walk(init):
        if not isinstance(call, ast.Call) or not _is_base_init(call.func):
            continue
        handed = {kw.arg for kw in call.keywords if kw.arg}
        handed |= {a.id for a in call.args if isinstance(a, ast.Name)}
        starred = [kw.value for kw in call.keywords if kw.arg is None]
        if starred:
            handed |= _dict_keys(init)
            if name not in params and any(_is_name(s, var_keyword or "") for s in starred):
                return True
        if name in handed:
            return True
    return False


def _is_base_init(func: ast.expr) -> bool:
    """``super().__init__``, ``super(Cls, self).__init__`` or ``Base.__init__``."""
    if not isinstance(func, ast.Attribute) or func.attr != "__init__":
        return False
    value = func.value
    if isinstance(value, ast.Call):
        return _is_name(value.func, "super")
    return isinstance(value, (ast.Name, ast.Attribute))


def _dict_keys(node: ast.AST) -> set[str]:
    """The string keys the code puts in dicts: ``d["k"] = v``, ``{"k": v}``, ``d.setdefault("k",
    v)`` and the keywords of ``dict(k=v)`` and ``d.update(k=v)`` (not those of any other call:
    ``log.info(..., extra=...)`` puts no ``extra`` in a dict)."""
    keys: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store):
            if isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                keys.add(n.slice.value)
        elif isinstance(n, ast.Dict):
            keys |= {
                k.value for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        elif isinstance(n, ast.Call):
            if _name(n.func) in ("dict", "update"):
                keys |= {kw.arg for kw in n.keywords if kw.arg}
            if _name(n.func) == "setdefault" and n.args:
                first = n.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.add(first.value)
    return keys


# --------------------------------------------------------------- module sources
_ALWAYS, _SOMETIMES, _MAIN = "always", "sometimes", "main"
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class _Source:
    """What a module's own source says beyond griffe's tree (see :func:`_bindings`)."""

    tree: ast.Module | None = None
    bindings: dict[str, str] = field(default_factory=dict)
    # Names imported as ``import x as x`` / ``from m import x as x``: re-exports in a stub.
    reexported: set[str] = field(default_factory=set)
    # Names put in ``__all__`` anywhere at module level (``__all__.append("x")`` included).
    exported: set[str] = field(default_factory=set)
    # Whether the module binds names griffe cannot see: ``globals().update(...)``,
    # ``globals()[name] = ...``, ``vars()``, a module ``__getattr__`` or a module object
    # swapped into ``sys.modules``.
    dynamic: bool = False

    @classmethod
    def parse(cls, module: Any) -> _Source:
        path = getattr(module, "filepath", None)
        if not isinstance(path, Path):
            return cls()
        try:
            with _quiet():
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            return cls()
        reexported = {
            a.name
            for node in _module_level(tree.body)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for a in node.names
            if a.asname == a.name
        }
        exported = {
            n.value
            for node in _module_level(tree.body)
            if _changes_all(node)
            for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        return cls(tree, _bindings(tree.body), reexported, exported, _binds_dynamically(tree))


def _binds_dynamically(tree: ast.Module) -> bool:
    """Can the module bind names at import that its source does not spell out (see
    :attr:`_Source.dynamic`)?"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func) in ("globals", "vars", "exec"):
            return True
        if isinstance(node, _FUNCTIONS) and node.name == "__getattr__" and node in tree.body:
            return True
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and ast.unparse(node.value).endswith("sys.modules")
        ):
            return True
    return False


def _bindings(body: list[ast.stmt]) -> dict[str, str]:
    """How each module-level name is bound: "always", "sometimes" or "main".

    "sometimes": only on some paths through ``if``/``try``/loop blocks (an ``if`` with the name
    bound in both branches, or a ``try`` whose handlers all bind it too, counts as always).
    "main": only under ``if __name__ == "__main__":``, which never runs on import.
    """
    main: set[str] = set()

    def targets(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Tuple, ast.List)):
            return set().union(*(targets(e) for e in node.elts))
        if isinstance(node, ast.Starred):
            return targets(node.value)
        return set()

    def scan(stmts: list[ast.stmt]) -> tuple[set[str], set[str]]:
        always: set[str] = set()
        sometimes: set[str] = set()
        for s in stmts:
            if isinstance(s, (*_FUNCTIONS, ast.ClassDef)):
                always.add(s.name)
            elif isinstance(s, ast.Assign):
                always.update(*(targets(t) for t in s.targets))
            elif isinstance(s, (ast.AnnAssign, ast.AugAssign)):
                always.update(targets(s.target))
            elif isinstance(s, (ast.Import, ast.ImportFrom)):
                always.update(a.asname or a.name.split(".")[0] for a in s.names if a.name != "*")
            elif isinstance(s, ast.If):
                body, orelse = scan(s.body), scan(s.orelse)
                if _is_main_guard(s.test):
                    main.update(*body)
                    sometimes.update(*orelse)
                    continue
                both = body[0] & orelse[0]
                always |= both
                sometimes |= (body[0] | body[1] | orelse[0] | orelse[1]) - both
            elif _is_try(s):
                t: Any = s
                tried, final = scan(t.body + t.orelse), scan(t.finalbody)
                handled = [scan(h.body) for h in t.handlers]
                certain = set(tried[0]).intersection(*(h[0] for h in handled)) | final[0]
                every = set().union(*tried, *final, *(h[0] | h[1] for h in handled))
                always |= certain
                sometimes |= every - certain
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                inner = scan(s.body)
                always |= inner[0]
                sometimes |= inner[1]
            elif isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
                sometimes.update(*scan(s.body + s.orelse))
                if not isinstance(s, ast.While):
                    sometimes |= targets(s.target)
            elif type(s).__name__ == "Match":
                for case in getattr(s, "cases", ()):
                    sometimes.update(*scan(case.body))
        return always, sometimes - always

    always, sometimes = scan(body)
    out = dict.fromkeys(main - always - sometimes, _MAIN)
    out.update(dict.fromkeys(sometimes, _SOMETIMES))
    out.update(dict.fromkeys(always, _ALWAYS))
    return out


def _is_main_guard(test: ast.expr) -> bool:
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, *test.comparators]
    return any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides) and any(
        _is_string(s, "__main__") for s in sides
    )


def _module_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements that run at import, including inside ``if``/``try``/``with`` blocks."""
    for s in body:
        yield s
        if isinstance(s, ast.If) and not _is_main_guard(s.test):
            yield from _module_level(s.body + s.orelse)
        elif _is_try(s):
            t: Any = s
            handlers = [stmt for h in t.handlers for stmt in h.body]
            yield from _module_level(t.body + handlers + t.orelse + t.finalbody)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            yield from _module_level(s.body)


def _changes_all(node: ast.stmt) -> bool:
    """``__all__ = [...]``, ``__all__ += [...]``, ``__all__.append(...)`` or ``.extend(...)``."""
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)
    call = node.value if isinstance(node, ast.Expr) else None
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "__all__"
        and call.func.attr in ("append", "extend", "insert")
    )


def _is_try(node: ast.AST) -> bool:
    return type(node).__name__ in ("Try", "TryStar")


def _is_string(node: ast.AST, text: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == text


def _name(node: ast.AST) -> str:
    """The last name of ``f`` or ``mod.f`` ("" for anything else)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _branch(node: ast.AST, name: str) -> ast.AST:
    """The innermost ``if`` of ``node`` that tests for the string ``name`` (else ``node``)."""
    scope = node
    for branch in ast.walk(node):  # breadth first: later matches are deeper
        if isinstance(branch, ast.If) and any(_is_string(n, name) for n in ast.walk(branch.test)):
            scope = branch
    return scope


def _lazy_hook(module: Any, statements: list[ast.stmt]) -> tuple[Any, ast.AST] | None:
    """The ``__getattr__`` (and its parsed source) of the class of a module object that
    ``module`` swaps into ``sys.modules`` (``sys.modules[__name__] = _LazyModule(...)``)."""
    for node in statements:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        if not any(
            isinstance(t, ast.Subscript) and ast.unparse(t.value).endswith("sys.modules")
            for t in node.targets
        ):
            continue
        cls: Any = _get(module, _name(node.value.func))
        hook: Any = cls.members.get("__getattr__") if getattr(cls, "is_class", False) else None
        try:
            with _quiet():
                return hook, ast.parse(textwrap.dedent(hook.source))
        except Exception:  # no such hook, or no source for it
            continue
    return None


def _membership_tables(node: ast.AST) -> set[str]:
    """``SLOW_TO_FAST_CONVERTERS`` for ``if lookup_name in SLOW_TO_FAST_CONVERTERS:``."""
    return {
        n.comparators[0].id
        for n in ast.walk(node)
        if isinstance(n, ast.Compare)
        and len(n.ops) == 1
        and isinstance(n.ops[0], ast.In)
        and isinstance(n.comparators[0], ast.Name)
    }


def _suffix_tests(node: ast.AST) -> set[str]:
    """``{"Tokenizer", "TokenizerFast"}`` for ``if name.endswith("Tokenizer") or
    name.endswith("TokenizerFast"):``; empty for anything else."""
    if not isinstance(node, ast.If):
        return set()
    return {
        call.args[0].value
        for call in ast.walk(node.test)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "endswith"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }


def _cuts(node: ast.AST) -> list[int]:
    """How many letters ``name[:-4]`` cuts off, for each such slice in ``node``."""
    out = []
    for n in ast.walk(node):
        if not (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice)):
            continue
        lower, upper = n.slice.lower, n.slice.upper
        if (
            lower is None
            and isinstance(upper, ast.UnaryOp)
            and isinstance(upper.op, ast.USub)
            and isinstance(upper.operand, ast.Constant)
            and isinstance(upper.operand.value, int)
        ):
            out.append(upper.operand.value)
    return out


def _warning_text(scope: ast.AST, name: str) -> str:
    """The deprecation message code gives for ``name``: its entry in a table of deprecated
    names (a new path such as ``{"Lock": "anyio.Lock"}``, or a message), else a string that
    says "deprecat" ("" if none)."""
    for node in ast.walk(scope):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                text = (
                    _string_value(value, name) if key is not None and _is_string(key, name) else ""
                )
                if text and re.fullmatch(r"[\w.]+", text):
                    return f"use `{text}` instead"
                if text and "deprecat" in text.lower():
                    return " ".join(text.split())
    body = scope.body if isinstance(scope, ast.If) else [scope]
    for node in (n for stmt in body for n in ast.walk(stmt)):
        text = _string_value(node, name)
        if text and "deprecat" in text.lower():
            return " ".join(text.split())
    return ""


def _string_value(node: ast.AST, name: str) -> str | None:
    """A string literal's text; in an f-string, a bare ``{variable}`` stands for ``name``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            parts.append(str(part.value))
        else:
            inner = getattr(part, "value", None)
            parts.append(name if isinstance(inner, ast.Name) else "...")
    return "".join(parts)


def _parameter_names(obj: Any) -> set[str]:
    """Parameter names of a function, or of a class's own ``__init__``."""
    try:
        fn = obj.members.get("__init__") if getattr(obj, "is_class", False) else obj
        return {p.name for p in fn.parameters} if fn is not None else set()
    except Exception:
        return set()


def _get(root: Any, rel: str) -> Any:
    """Object at ``rel`` below ``root`` (aliases followed), or None.

    Declared members are tried first: computing a class's inherited members builds the MRO
    and wraps every inherited member, which is slow on large class hierarchies.
    """
    if not rel:
        return root
    obj = root
    try:
        for part in rel.split("."):
            if getattr(obj, "is_alias", False):
                obj = obj.final_target
            member = obj.members.get(part)
            obj = member if member is not None else obj.all_members[part]
        if getattr(obj, "is_alias", False):
            obj = obj.final_target
        return obj
    except Exception:
        return None


def find_object(root: Any, path: str) -> Any:
    """The object at the dotted ``path`` in a package loaded with :func:`load_api`, or None.

    Re-exports are followed, so ``pkg.Client`` finds ``pkg._client.Client``. A path outside
    ``root`` (another package, a sibling namespace package) is None.
    """
    if path != root.path and not path.startswith(root.path + "."):
        return None
    return _get(root, _rel(path, root.path))


def _walk_modules(root: Any) -> Iterator[Any]:
    stack = [root]
    seen: set[str] = set()
    while stack:
        mod = stack.pop()
        if mod.path in seen:
            continue
        seen.add(mod.path)
        yield mod
        for member in list(mod.members.values()):
            if not getattr(member, "is_alias", False) and getattr(member, "is_module", False):
                stack.append(member)


def _pair_breakages(old: Any, new: Any) -> list[Any]:
    """griffe's breakages between ``old`` and ``new``, two versions of one class or function
    that griffe's own walk does not compare (:meth:`_Differ._realiased`).

    ``find_breaking_changes(old, new)`` compares the *members* of the two objects: for a class
    that is its methods and fields, without a base it lost or a change of kind, and for a
    function it is nothing, so a parameter the new one dropped went unreported. The two are
    compared here as the one member of a throwaway module each, under the old object's name
    (the target of the re-export may have another: mcp 2.2's ``RegistrationRequest =
    OAuthClientMetadata``), which is how griffe meets the members of a package: a kind change, a
    lost base and a function's parameters included. The modules are not told about their member
    (``set_member`` would make them its parent).
    """
    import griffe

    holders = []
    for obj in (old, new):
        holder = griffe.Module("_since_cutoff")
        holder.members[old.name] = obj
        holders.append(holder)
    return list(griffe.find_breaking_changes(*holders))


def iter_public_objects(root: Any, *, test_methods: bool = False) -> Iterator[tuple[Any, str]]:
    """Publicly reachable objects of a loaded package, as ``(object, public_path)`` pairs.

    Objects defined in public modules are yielded under their own path or a shorter re-export
    (``pkg.Session`` for ``pkg.sessions.Session``); objects defined in private modules under the
    shortest public path that re-exports them (``pkg.Client`` for ``pkg._client.Client``). A plain
    import (``from pkg.protocol import X`` in ``pkg.server``) is a path only to an object of a
    private module that nothing re-exports. Each object is yielded once. A code generator's
    template (:func:`_is_template`) is no API. With ``test_methods``, a class's methods named
    like tests (statsmodels' ``DescStatUV.test_mean``, ``OLSResults.el_test``) are yielded
    too: only modules and module-level names are test code by their name.
    """
    package = root.path.split(".")[0]
    seen: set[str] = set()
    queue: deque[tuple[Any, str]] = deque([(root, root.path)])
    imports: deque[tuple[Any, str]] = deque()  # plain imports of private objects: last resort
    while queue or imports:  # breadth first, so that the shortest path comes first
        if not queue:
            target, path = imports.popleft()
            if target.path not in seen:
                seen.add(target.path)
                yield target, path
                if getattr(target, "is_module", False) or getattr(target, "is_class", False):
                    queue.append((target, path))
            continue
        container, public = queue.popleft()
        try:
            members = list(container.members.values())
        except Exception:
            continue
        in_class = test_methods and getattr(container, "is_class", False)
        for member in members:
            if _private(member.name):
                continue
            if member.name in _SKIP_SEGMENTS if in_class else _skipped_segment(member.name):
                continue
            target = member
            path = f"{public}.{member.name}"
            if getattr(member, "is_alias", False):
                try:
                    target = member.final_target
                except Exception:
                    continue  # re-export of another package, or unresolvable
                if not str(target.path).startswith(package + "."):
                    continue
                if not _exported(member):
                    if _has_private_segment(target.path):
                        imports.append((target, path))
                    continue  # an import: the object is reached where it is defined
            if target.path in seen:
                continue
            if getattr(target, "is_module", False) and (
                _non_api_path(path, module=True) or _is_template(target)
            ):
                continue
            seen.add(target.path)
            yield target, path
            if getattr(target, "is_module", False) or getattr(target, "is_class", False):
                queue.append((target, path))


# At most this many public paths are kept per object (a module re-exported in a loop of
# packages would otherwise give it paths without end).
_MAX_PATHS = 64


def path_index(root: Any) -> dict[str, list[str]]:
    """Every public path of each module-level object of a loaded package, by the object's
    canonical path, shortest first.

    The paths :func:`iter_public_objects` walks, but all of them instead of the shortest:
    ``pkg.dl.fetch`` where it is defined, ``pkg.fetch`` where a package re-exports it
    (``__all__``, an ``__init__`` re-export, an ``if TYPE_CHECKING:`` import listed in
    ``__all__``). A plain import is not a path to the object. Class members are not listed:
    they are reached through their class (see :meth:`_Differ.paths_in`).
    """
    package = root.path.split(".")[0]
    index: dict[str, list[str]] = {str(root.path): [str(root.path)]}
    queue: deque[tuple[Any, str]] = deque([(root, str(root.path))])
    while queue:  # breadth first, so that shorter paths come first
        module, public = queue.popleft()
        try:
            members = list(module.members.items())
        except Exception:
            continue
        for name, member in members:
            if _private(name) or _skipped_segment(name) or "/" in name:
                continue
            target = member
            if getattr(member, "is_alias", False):
                if not _exported(member):
                    continue  # an import: the object is reached where it is defined
                try:
                    target = member.final_target
                except Exception:
                    continue  # re-export of another package, or unresolvable
                if not str(target.path).startswith(package + "."):
                    continue
            path = f"{public}.{name}"
            is_module = getattr(target, "is_module", False)
            if is_module and (_non_api_path(path, module=True) or _is_template(target)):
                continue
            paths = index.setdefault(str(target.path), [])
            if path in paths or len(paths) >= _MAX_PATHS:
                continue
            paths.append(path)
            if is_module:
                queue.append((target, path))
    return index


def _ordered(paths: Iterable[str]) -> list[str]:
    """Distinct paths, shortest first."""
    return sorted(set(paths), key=lambda p: (p.count("."), p))


# ------------------------------------------------- receiver types (selection.type_project)
# What receiver_map records of a pinned release; bumped when that changes, so that the copies
# the scan caches per release (Engine.store, "receivers") are made again. Not part of a diff.
# 2: ``members``, the names each class declares itself.
RECEIVERS_SCHEMA = 2
# Where an annotation that names no package of the project leads: the standard library and the
# typing helpers (``typing_extensions.Self``). Not recorded.
_STDLIB_MODULES = frozenset(getattr(sys, "stdlib_module_names", ())) | {
    "builtins",
    "typing_extensions",
}
# A method annotated to return its own class (PEP 673): the receiver's class, whatever it is.
SELF_TYPE = "Self"


def receiver_map(root: Any) -> dict[str, dict[str, Any]]:
    """What a package's own annotations say about the values the project's code holds, for
    :func:`since_cutoff.selection.type_project` (read statically; nothing is run).

    ``classes`` maps each public path of a class to its canonical path (``pandas.DataFrame`` ->
    ``pandas.core.frame.DataFrame``), and a class the package re-exports from another package
    (``langchain.chat_models.BaseChatModel``) to the path it imports it from. ``bases`` maps
    each class (canonical path) to its base classes: the package's own by canonical path,
    another package's as imported; the standard library's are left out. ``returns`` maps a
    public function, under each of its public paths, and a method, as ``<class>.<name>`` with
    the class's canonical path, to the classes its return annotation names (``X``; ``X |
    None``; ``X | Y`` and the returns of its ``@overload`` forms as ``X|Y``, in the order
    written: pandas' ``read_csv`` gives a ``DataFrame`` or a ``TextFileReader``), or to
    :data:`SELF_TYPE`; a return of a container (``list[X]``), of a type variable or of a type
    outside every package is not recorded. ``attrs`` is the same for properties and annotated
    attributes, which are read without a call. ``members`` maps each class (canonical path) to
    the public names it declares itself, not those it inherits: where a member read on an
    instance is found, so that a subclass's own ``sum`` is not its base's.
    """
    import griffe

    scope = _Scope(root)
    index = path_index(root)
    classes: dict[str, str] = {}
    bases: dict[str, list[str]] = {}
    returns: dict[str, str] = {}
    attrs: dict[str, str] = {}
    defines: dict[str, list[str]] = {}

    def class_set(expr: Any) -> list[str]:
        """The canonical paths of the classes an annotation names (its top, or the members of
        its union), or ``[SELF_TYPE]``; nothing for a container or an unresolvable name."""
        if expr is None or isinstance(expr, str):
            return []
        found: list[str] = []
        for leaf, top in _annotation_names(expr):
            name = str(getattr(leaf, "name", ""))
            if not top or name in _NOT_A_TYPE:
                continue
            if name == SELF_TYPE:
                return [SELF_TYPE]
            try:
                path = _class_path(scope, str(leaf.canonical_path))
            except Exception:
                return []
            if path is None:
                return []  # ``list[X]``, a type variable, a type alias: not a class
            if path not in found:
                found.append(path)
        return found

    def returned(fn: Any) -> str | None:
        """What a function's return annotation, and those of its ``@overload`` forms, name."""
        found = class_set(getattr(fn, "returns", None))
        for overload in getattr(fn, "overloads", None) or ():
            found += [c for c in class_set(getattr(overload, "returns", None)) if c not in found]
        if SELF_TYPE in found:
            return SELF_TYPE
        return "|".join(found[:4]) if found else None

    def one_base(base: Any) -> str | None:
        inner = base.left if isinstance(base, griffe.ExprSubscript) else base
        leaves = [leaf for leaf, top in _annotation_names(inner) if top]
        if len(leaves) != 1:
            return None
        try:
            return _class_path(scope, str(leaves[0].canonical_path))
        except Exception:
            return None

    for module in _walk_modules(root):
        if _non_api_path(str(module.path), module=True):
            continue
        try:
            members = list(module.members.items())
        except Exception:
            continue
        module_paths = index.get(str(module.path), [])
        for name, member in members:
            if _private(name) or _skipped_segment(name):
                continue
            if getattr(member, "is_alias", False):
                # A class another package defines, re-exported here (``from langchain_core
                # .language_models import BaseChatModel`` in ``langchain.chat_models``).
                if not _exported(member):
                    continue
                target = str(getattr(member, "target_path", "") or "")
                if not target or _under(target, str(root.path)) or _stdlib(target):
                    continue
                for module_path in module_paths:
                    classes[f"{module_path}.{name}"] = target
                continue
            if getattr(member, "is_function", False):
                found = returned(member)
                if found is not None:
                    for public in index.get(str(member.path), ()):
                        returns[public] = found
                continue
            if not getattr(member, "is_class", False):
                continue
            canonical = str(member.path)
            for public in index.get(canonical, ()):
                classes[public] = canonical
            found_bases = [one_base(b) for b in getattr(member, "bases", None) or ()]
            kept = [b for b in found_bases if b is not None and b != canonical]
            if kept:
                bases[canonical] = list(dict.fromkeys(kept))
            try:
                items = list(member.members.items())
            except Exception:
                continue
            declared = [n for n, _ in items if not _private(n)]
            if declared:
                defines[canonical] = declared
            for attr_name, attr in items:
                if _private(attr_name) or getattr(attr, "is_alias", False):
                    continue
                if getattr(attr, "is_function", False):
                    found = returned(attr)
                    where = (
                        attrs if "property" in (getattr(attr, "labels", None) or ()) else returns
                    )
                elif getattr(attr, "is_attribute", False):
                    found = "|".join(class_set(getattr(attr, "annotation", None))) or None
                    where = attrs
                else:
                    continue
                if found is not None:
                    where[f"{canonical}.{attr_name}"] = found
    return {
        "classes": classes,
        "bases": bases,
        "returns": returns,
        "attrs": attrs,
        "members": defines,
    }


# Names in an annotation that are not the class it names: the forms of a union and the way of
# writing "no value".
_NOT_A_TYPE = frozenset({"None", "Optional", "Union"})


def _class_path(scope: _Scope, path: str) -> str | None:
    """The canonical path of the class an annotation's name leads to (:meth:`_Scope.chase`):
    inside the package, only a class counts (not a type alias); outside it, any path but the
    standard library's, as written (the other package's map canonicalises it)."""
    final = scope.chase(path)
    if scope.inside(final):
        obj = scope.lookup(final)
        return final if obj is not None and getattr(obj, "is_class", False) else None
    if "." not in final or _stdlib(final):
        return None
    return final


def _stdlib(path: str) -> bool:
    return path.split(".", 1)[0] in _STDLIB_MODULES


def _reads_key(tree: ast.AST | None, name: str) -> int | None:
    """The first line of ``tree`` that reads ``name`` as a key: ``.pop("name"``,
    ``.get("name"`` or ``"name" in ...``; None if none does."""
    lines = []
    for node in ast.walk(tree) if tree is not None else ():
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("pop", "get")
            and node.args
            and _is_string(node.args[0], name)
        ) or (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and _is_string(node.left, name)
        ):
            lines.append(node.lineno)
    return min(lines, default=None)


_DOC_ENTRY = re.compile(r"^(?:[-*]\s+)?`*(?P<name>\w+)`*\s*(?:\([^)]*\))?\s*:\s*(?P<text>.*)$")


def _handled_text(scope: Sequence[ast.AST], name: str) -> str | None:
    """What the code that still reads a removed parameter says of it (``scope``: the parsed
    decorator and the functions it calls): the parameter's entry in one of their docstrings
    ("- `resume_download`: deprecated without replacement. ..."), else a ``warnings.warn``
    text that names it; None when neither does. At most 300 characters, on one line."""
    for node in scope:
        doc = (
            ast.get_docstring(node)
            if isinstance(node, (*_FUNCTIONS, ast.ClassDef, ast.Module))
            else None
        )
        entry = _docstring_entry(doc or "", name)
        if entry:
            return entry[:300]
    for node in scope:
        for text in _warn_messages(node):
            if _mentions(text, name):
                return text[:300]
    return None


def _docstring_entry(doc: str, name: str) -> str | None:
    """The text of ``name``'s entry in a docstring's list (``- `name`: text``, ``name: text``,
    ``name (type): text``), with the lines indented below it up to a blank line; None."""
    lines = doc.splitlines()
    for i, line in enumerate(lines):
        m = _DOC_ENTRY.match(line.strip())
        if m is None or m.group("name") != name:
            continue
        indent = len(line) - len(line.lstrip())
        parts = [m.group("text")]
        for follow in lines[i + 1 :]:
            if not follow.strip():
                if any(p.strip() for p in parts):
                    break
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break
            parts.append(follow.strip())
        text = " ".join(" ".join(parts).split())
        return text or None
    return None


def _source_file(module: Any) -> str:
    """The file of a loaded module, relative to the folder its package is in
    (``huggingface_hub/utils/_validators.py``)."""
    filepath = Path(str(getattr(module, "filepath", "") or ""))
    depth = len(str(module.path).split(".")) + (filepath.stem == "__init__")
    return "/".join(filepath.parts[-depth:])


def _file_of(obj: Any) -> str | None:
    """The file ``obj`` is defined in (its module's, for a class, function or method), as
    :func:`_source_file` writes it, or None when it cannot be told."""
    try:
        if getattr(obj, "is_alias", False):
            obj = obj.final_target
        module = obj if getattr(obj, "is_module", False) else obj.module
        return _source_file(module) or None
    except Exception:
        return None


# -------------------------------------------------- names a deprecation text gives
_CODE_SPAN = re.compile(r"`([^`]+)`")
_ROLE = re.compile(r":(?:\w+:)+(?=`)")  # Sphinx roles: :func:`x`, :py:meth:`x`
_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
# Words of deprecation notes that are never the replacement's name.
_PROSE = {
    "use",
    "using",
    "prefer",
    "instead",
    "replace",
    "replacement",
    "alternative",
    "deprecated",
    "deprecation",
    "removed",
    "will",
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "since",
    "version",
    "release",
    "future",
    "please",
    "favor",
    "favour",
    "replaced",
    "now",
    "new",
    "old",
    "call",
    "see",
    "not",
    "has",
    "been",
    "was",
    "are",
    "its",
    "method",
    "function",
    "class",
    "argument",
    "parameter",
    "attribute",
    "module",
    "warning",
}
# Literals a code span may hold next to a name (``force_download=True``): never a replacement.
_LITERALS = {"True", "False", "None"}


def replacement_candidates(text: str, *, words: bool = True) -> list[str]:
    """Names a deprecation note may give as the replacement, the code-like ones first: in
    backticks, called, dotted, snake_case or CamelCase. With ``words``, plain words of the
    prose come last (the signatures baseline tries them; the notes do not)."""
    text = _ROLE.sub(" ", text)
    code: list[str] = []
    for span in _CODE_SPAN.findall(text):
        code += _NAME.findall(span.lstrip("~"))  # Sphinx's :func:`~pkg.name`
    found: list[str] = []
    prose = _CODE_SPAN.sub(" ", text)
    for m in _NAME.finditer(prose):
        name = m.group()
        called = prose[m.end() : m.end() + 1] == "("
        if called or "." in name or "_" in name or any(c.isupper() for c in name[1:]):
            code.append(name)
        elif words and len(name) > 2 and name.lower() not in _PROSE and not keyword.iskeyword(name):
            found.append(name)
    return [n for n in dict.fromkeys(code + found) if n not in _LITERALS]


def candidate_paths(change: APIChange, text: str, root: str, *, words: bool = True) -> list[str]:
    """Full paths to try for each candidate name of ``text`` (:func:`replacement_candidates`):
    absolute, then in the change's owner, module and package. Never the change itself."""
    return list(
        dict.fromkeys(
            p
            for name in replacement_candidates(text, words=words)
            for p in _scoped(change, name, root)
        )
    )


def _scoped(change: APIChange, name: str, root: str) -> list[str]:
    """The paths ``name`` (as a deprecation text writes it) may stand for, in the order tried."""
    if name in (change.name, change.owner) or any(p.startswith("_") for p in name.split(".")):
        return []
    if name == root or name.startswith(root + "."):
        paths = [name]
    else:
        scopes = [f"{change.module}.{change.owner}"] if change.owner else []
        paths = [f"{scope}.{name}" for scope in [*scopes, change.module, root]]
    return [p for p in dict.fromkeys(paths) if p != change.path]


def is_callable_api(obj: Any, root: str) -> bool:
    """A function or class defined in the package itself (not a re-exported dependency)."""
    if obj is None or not (getattr(obj, "is_function", False) or getattr(obj, "is_class", False)):
        return False
    top = root.split(".")[0]
    return str(getattr(obj, "path", "")).split(".")[0] == top


# A name as a deprecation text writes it: bare, in backticks or quotes, called, or with a value.
_WRITTEN = r"[`'\"]?{}(?:\(\))?(?:=[^\s`'\",]*)?[`'\"]?"
# How a deprecation text states its replacement: "Use `stop` instead", "Use 'proxy' or
# 'mounts' instead" (httpx 0.27: the others are ``alt``), "replaced by X", "renamed to X", "in
# favour of X", "Deprecated: use X".
STATED_REPLACEMENT = (
    r"\buse\s+"
    + _WRITTEN.format(r"(?P<n>[\w.]+)")
    + r"(?P<alt>(?:\s*(?:,(?:\s*or\b)?|\bor\b)\s*"
    + _WRITTEN.format(r"[\w.]+")
    + r")*)\s+(?:instead|in\s+its\s+place)\b",
    r"\b(?:replaced|superseded)\s+(?:by|with)\s+[`'\"]?(?P<n>[\w.]+)",
    r"\brenamed\s+(?:to|as)\s+[`'\"]?(?P<n>[\w.]+)",
    r"\bin\s+favou?r\s+of\s+[`'\"]?(?P<n>[\w.]+)",
    r"\bdeprecated\W+(?:please\s+)?use\s+[`'\"]?(?P<n>[\w.]+)",
)


def stated_alternatives(match: re.Match[str]) -> list[str]:
    """The names a STATED_REPLACEMENT match gives after its first: ``mounts`` in "Use 'proxy'
    or 'mounts' instead", ``b`` and ``c`` in "Use a, b, or c instead", in the text's order."""
    alt = re.sub(r"=[^\s`'\",]*", "", match.groupdict().get("alt") or "")
    return [n.strip(".") for n in re.findall(r"[A-Za-z_][\w.]*", alt) if n.lower() != "or"]


def stated_names(text: str) -> list[str]:
    """The names ``text`` states outright as the replacement ("Use fetch instead."), even as
    plain words, in the order it gives them."""
    found: list[tuple[int, int, str]] = []
    for pattern in STATED_REPLACEMENT:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            for i, name in enumerate((m.group("n").strip("."), *stated_alternatives(m))):
                if name and name not in _LITERALS and name.lower() not in _PROSE:
                    found.append((m.start("n"), i, name))
    return list(dict.fromkeys(name for _, _, name in sorted(found)))


def _named(text: str) -> list[str]:
    """What the notes accept as a name the text gives: names written as code, and names it
    states as the replacement (a plain word elsewhere, "login", is not evidence)."""
    return list(dict.fromkeys(replacement_candidates(text, words=False) + stated_names(text)))


def parameter_replacements(text: str, parameters: Iterable[str], removed: str) -> list[str]:
    """The names ``text`` gives (:func:`_named`) that are among ``parameters`` (the new
    callable's), other than the parameter ``removed`` itself: ``stop`` in "Deprecated argument.
    Use `stop` instead." for ``text_generation(stop_sequences=...)``."""
    names = set(parameters) - {removed.lstrip("*"), "self", "cls"}
    return [n for n in _named(text) if n in names and not n.startswith("_")]


def object_replacements(change: APIChange, text: str, root: Any) -> list[str]:
    """The public paths of the functions and classes of the loaded API ``root`` that ``text``
    gives (:func:`_named`): for each name, the first of its :func:`_scoped` paths that
    resolves."""
    found: list[str] = []
    for name in _named(text):
        for path in _scoped(change, name, str(root.path)):
            if is_callable_api(find_object(root, path), str(root.path)):
                found.append(path)
                break
    return found


def signature_parameters(signature: str | None) -> list[str]:
    """The parameter names in a :func:`signature_of` text (which may be cut short: then only
    those before the cut)."""
    if not signature or "(" not in signature:
        return []
    inner = signature.split("(", 1)[1]
    names: list[str] = []
    depth = 0
    start = True
    for token in re.finditer(r"[A-Za-z_]\w*|[\[\](){},=:]", inner):
        t = token.group()
        if t in "([{":
            depth += 1
        elif t in ")]}":
            if depth == 0:
                break  # the end of the parameters
            depth -= 1
        elif depth == 0 and t == ",":
            start = True
        elif depth == 0 and t in "=:":
            start = False
        elif start and depth == 0:
            names.append(t)
            start = False
    return names


def keyword_parameters(signature: str | None) -> list[str] | None:
    """The parameters of a :func:`signature_of` text that a call may pass by keyword: not the
    positional-only ones (before ``/``) nor ``*args``. None when the callable takes
    ``**kwargs`` (any keyword), or when the text is missing or cut short."""
    if not signature or "(" not in signature:
        return None
    inner = signature.split("(", 1)[1]
    names: list[str] = []
    depth = 0
    start = True  # at the start of a parameter
    star = False  # after a ``*``: the next name is ``*args``
    for token in re.finditer(r"\*\*?|/|[A-Za-z_]\w*|[\[\](){},=:]", inner):
        t = token.group()
        if t in ("(", "[", "{"):
            depth += 1
        elif t in (")", "]", "}"):
            if depth == 0:
                return names  # the end of the parameters
            depth -= 1
        elif depth == 0 and t == ",":
            start, star = True, False
        elif depth == 0 and t in ("=", ":"):
            start = False
        elif depth == 0 and start:
            if t == "**":
                return None
            if t == "*":
                star = True
            elif t == "/":
                names.clear()
            else:
                if not star:
                    names.append(t)
                start, star = False, False
    return None  # cut short: the parameters after the cut are unknown


def _accepts_var_keyword(fn: Any) -> bool:
    # By the kind's name: griffe 2.3.1 made str(kind) its value ("variadic keyword").
    try:
        return any(_pkind(p) == "var_keyword" for p in fn.parameters)
    except Exception:
        return False


def _unpacked(fn: Any) -> str | None:
    """The canonical path of the TypedDict a callable's ``**kwargs`` unpacks (``pkg.io
    ._read_shared`` for ``**kwds: Unpack[_read_shared[HashableT]]``, PEP 692), or None for a
    bare ``**kwargs`` or none at all."""
    try:
        kwargs = next((p for p in fn.parameters if _pkind(p) == "var_keyword"), None)
        annotation = getattr(kwargs, "annotation", None)
        if annotation is None or isinstance(annotation, str):
            return None
        left = getattr(annotation, "left", None)
        inner = getattr(annotation, "slice", None)
        if left is None or inner is None or str(getattr(left, "canonical_path", "")) not in _UNPACK:
            return None
        return _unsubscripted(inner) or None
    except Exception:
        return None


def _unsubscripted(expr: Any) -> str:
    """The canonical path of a type expression without its subscript (``pkg.io._read_shared``
    for ``_read_shared[HashableT]``), or of a bare name; "" for anything else."""
    for _ in range(8):  # ``A[B][C]``
        inner = getattr(expr, "left", None)
        if inner is None or getattr(expr, "slice", None) is None:
            break
        expr = inner
    if isinstance(expr, str):
        return expr
    return str(getattr(expr, "canonical_path", "") or "")


def _open_typed_dict(node: ast.ClassDef | None) -> bool:
    """Does the TypedDict class (its parsed ``class`` statement) take keys it does not name:
    PEP 728's ``extra_items=`` or ``closed=False``? griffe keeps no class keywords."""
    for kw in node.keywords if node is not None else ():
        if kw.arg == "extra_items":
            return True
        if kw.arg == "closed" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
            return True
    return False


def _bound_parameter(fn: Any) -> Any:
    """The parameter a method is bound on (``self``, ``cls``): its first, unless it is a static
    method; None for a function."""
    parent = getattr(fn, "parent", None)
    if not getattr(parent, "is_class", False) or "staticmethod" in (
        getattr(fn, "labels", None) or ()
    ):
        return None
    try:
        return next(iter(fn.parameters), None)
    except Exception:
        return None


def _typing_path(path: str) -> str:
    """``typing_extensions.X`` as ``typing.X``, which the standard library has (and
    :func:`_outside_attribute` can look at); any other path as it is."""
    module, _, name = path.rpartition(".")
    return f"typing.{name}" if module == "typing_extensions" else path


def _api_anchor(obj: Any) -> Any:
    """The module-level object a change to ``obj`` is a change to: ``obj`` itself, or for a
    member (a method, an attribute, a nested class, at any depth) the class at the top."""
    while obj is not None and getattr(getattr(obj, "parent", None), "is_class", False):
        obj = obj.parent
    return obj


def _api_docstring(obj: Any) -> str:
    """The object's docstring, or, for a value made of a function (``convert = deprecated(
    "0.1")(_convert)``), the function's; "" when there is none or it cannot be read."""
    try:
        doc = obj.docstring
        text = str(getattr(doc, "value", "")).strip() if doc is not None else ""
    except Exception:
        return ""
    if text or not getattr(obj, "is_attribute", False):
        return text
    wrapped = _wrapped_function(obj)
    return _api_docstring(wrapped) if wrapped is not None else ""


def _wrapped_function(obj: Any) -> Any:
    """The function of the attribute's own module that its value is a call on (``deprecated(
    "0.1.16", alternative=...)(_impl)`` is ``_impl`` with a warning), or None."""
    value = getattr(obj, "value", None)
    parent = getattr(obj, "parent", None)
    arguments = getattr(value, "arguments", None)
    if type(value).__name__ != "ExprCall" or parent is None or arguments is None:
        return None
    for arg in arguments:
        name = getattr(arg, "name", None) if type(arg).__name__ == "ExprName" else None
        try:
            member = parent.members.get(name) if name else None
        except Exception:
            return None
        if getattr(member, "is_function", False):
            return member
    return None


def _close(names: list[str], name: str) -> list[str]:
    import difflib

    return [n for n in difflib.get_close_matches(name, names, n=3, cutoff=0.7) if n != name]


def _decorator_call(dec: Any) -> tuple[list[Any], dict[str, Any]]:
    """Literal positional and keyword arguments of a decorator call (None where not literal)."""
    import ast

    try:
        call = ast.parse(str(dec.value), mode="eval").body
    except SyntaxError:
        return [], {}
    if not isinstance(call, ast.Call):
        return [], {}

    def literal(node: ast.expr) -> Any:
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return None

    return (
        [literal(a) for a in call.args],
        {k.arg: literal(k.value) for k in call.keywords if k.arg},
    )


def _text(value: Any) -> str | None:
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


def _decorator_message(name: str, args: list[Any], kwargs: dict[str, Any]) -> str:
    """The advice in a deprecation decorator's arguments, or ''."""
    alternative = _text(kwargs.get("alternative")) or _text(kwargs.get("alternative_import"))
    if alternative:
        return alternative if " " in alternative else f"use {alternative} instead"
    for key in ("message", "reason", "msg"):
        if _text(kwargs.get(key)):
            return _text(kwargs.get(key)) or ""
    first = _text(args[0]) if args else None
    if not first or _VERSION.fullmatch(first):
        return ""  # nothing, or only the version it was deprecated in
    if re.search(r"renamed|moved", name, re.IGNORECASE) and re.fullmatch(r"[A-Za-z_][\w.]*", first):
        return f"use `{first}` instead"  # deprecate_renamed_function("new_name")
    return first


def decorator_deprecations(obj: Any) -> list[tuple[str | None, str, str]]:
    """Deprecations declared by a library's own decorators rather than PEP 702 ``@deprecated``.

    Every decorator whose name contains "deprecat" counts. Returns ``(parameter, message,
    decorator name)`` tuples: ``parameter`` is None when the function or class itself is
    deprecated, or the old parameter name for decorators such as polars'
    ``@deprecate_renamed_parameter("old", "new")`` or pandas' ``@deprecate_kwarg``. A parameter
    decorator whose parameter cannot be read is ignored rather than reported as deprecating
    the whole function.
    """
    try:
        if getattr(obj, "is_alias", False):
            obj = obj.final_target
        decorators = list(getattr(obj, "decorators", None) or [])
    except Exception:
        return []
    out: list[tuple[str | None, str, str]] = []
    for dec in decorators:
        path = str(getattr(dec, "callable_path", "") or "")
        # The resolved name, or the written one when resolution led elsewhere.
        names = (path.rsplit(".", 1)[-1], str(dec.value).split("(")[0].rsplit(".", 1)[-1])
        name = next((n for n in names if "deprecat" in n.lower()), "")
        if not name or path in _PEP702 or _resolves_to_pep702(obj, path):
            continue
        if "property" in name.lower():
            continue  # a descriptor such as deprecated_instance_property: only some access is
        args, kwargs = _decorator_call(dec)
        if not _is_parameter_decorator(name):
            out.append((None, _decorator_message(name, args, kwargs), name))
            continue
        # The names are the string arguments: pandas 3's deprecate_kwarg(Pandas4Warning,
        # "method", new_arg_name=None) passes the warning's category before them.
        strings = [a for a in args if isinstance(a, str)]
        old = next((kwargs[k] for k in _OLD_NAME_KEYS if isinstance(kwargs.get(k), str)), None)
        new = next((kwargs[k] for k in _NEW_NAME_KEYS if isinstance(kwargs.get(k), str)), None)
        old = old or (strings[0] if strings else None)
        new = new or (strings[1] if len(strings) > 1 else None)
        if old and old.isidentifier():
            # transformers' deprecate_kwarg("old", "4.50") passes a version second, not a name.
            is_name = new and new.isidentifier() and not _VERSION.fullmatch(new)
            renamed = f"renamed to `{new}`" if is_name else ""
            out.append((old, renamed, name))
    return out


def _is_parameter_decorator(name: str) -> bool:
    return any(word in _PARAMETER_WORDS for word in name.lower().split("_"))


def deprecated_parameters(fn: Any) -> set[str]:
    """Parameters that a deprecation decorator still accepts under an old name."""
    return {p for p, _, _ in decorator_deprecations(fn) if p}


_PARAM_HEAD = re.compile(r"^\s*\**[A-Za-z_]\w*\**\s*(\([^)]*\))?\s*:(\s|$)")
_PARAM_SECTIONS = {
    "args", "arguments", "parameters", "params", "keyword args", "keyword arguments",
    "other parameters", "attributes",
}  # fmt: skip
_OTHER_SECTIONS = {
    "returns", "return", "yields", "raises", "examples", "example", "notes", "note",
    "see also", "warnings", "warning", "references",
}  # fmt: skip
_NOT_PARAMS = _PARAM_SECTIONS | _OTHER_SECTIONS | {"deprecated", "todo", "usage", "tip"}


def _is_param_head(line: str) -> bool:
    """``"proxies (dict, *optional*):"`` starts a parameter entry; ``"Deprecated: use X"`` doesn't."""
    if not _PARAM_HEAD.match(line):
        return False
    word = line.strip().lstrip("*").split("(")[0].split(":")[0].strip("* ").lower()
    return word not in _NOT_PARAMS


def _doc_section(lines: list[str], i: int) -> bool | None:
    """If ``lines[i]`` starts a docstring section: whether it lists parameters; else None.

    Handles Google style (``Args:``) and numpydoc (``Parameters`` over a ``----`` line).
    """
    text = lines[i].strip().rstrip(":").lower()
    google = lines[i].strip().endswith(":")
    numpy = i + 1 < len(lines) and set(lines[i + 1].strip()) == {"-"}
    if not (google or numpy):
        return None
    if text in _PARAM_SECTIONS:
        return True
    if text in _OTHER_SECTIONS:
        return False
    return None


def deprecation_hint(obj: Any, param: str | None = None) -> str | None:
    """What the *old* version said about this object's deprecation, if anything.

    Libraries often announce a removal one release earlier (``@deprecated(alternative="invoke")``,
    ``.. deprecated::`` docstrings, "Deprecated: use X" parameter docs). That text is the best
    source for a fix note.
    """
    return deprecation_hint_and_source(obj, param)[0]


def deprecation_hint_and_source(
    obj: Any, param: str | None = None
) -> tuple[str | None, str | None]:
    """:func:`deprecation_hint` and where it comes from (HINT_DECORATOR or HINT_DOCSTRING:
    that of its first part, when it joins several)."""
    try:
        if getattr(obj, "is_alias", False):
            obj = obj.final_target
    except Exception:
        return None, None
    parts: list[tuple[str, str]] = []
    if param is None:
        for dec in getattr(obj, "decorators", None) or []:
            path = str(getattr(dec, "callable_path", "") or "")
            name = path.rsplit(".", 1)[-1]
            if "deprecat" not in path.lower() or _is_parameter_decorator(name):
                continue  # parameter decorators say nothing about the object itself
            if "property" in name.lower():
                continue  # descriptors deprecate a way of reading it, not the object
            args, kwargs = _decorator_call(dec)
            parts.append((_decorator_message(name, args, kwargs), HINT_DECORATOR))
    lines = _doc_lines(obj)
    in_params = False
    for i, line in enumerate(lines):
        header = _doc_section(lines, i)
        if header is not None:
            in_params = header
            continue
        if "deprecat" not in line.lower():
            continue
        if param is None and (in_params or _is_param_head(line)):
            continue  # a parameter's deprecation, not the object's
        if param is not None and not _mentions(line, param):
            continue
        text = _sentence(lines, i)
        if text is None:
            continue  # cut off before what it announces
        parts.append((text, HINT_DOCSTRING))
        break
    kept = [(t.strip(), source) for t, source in parts if t and t.strip()]
    text = "; ".join(t for t, _ in kept)[:300]
    return (text, kept[0][1]) if text else (None, None)


def parameter_hint(fn: Any, param: str) -> tuple[str | None, str | None]:
    """What the old version said about the deprecation of a parameter it had, and where it
    said it (APIChange.hint and hint_source).

    As :func:`deprecation_hint`, else the parameter's own docstring entry when it says
    "deprecated" (huggingface_hub 1.21's ``stop_sequences``: "Deprecated argument. Use `stop`
    instead."), else the text of a ``warnings.warn`` in the function that names the
    parameter and says "deprecated".
    """
    text, source = deprecation_hint_and_source(fn, param)
    if text:
        return text, source
    try:
        if getattr(fn, "is_alias", False):
            fn = fn.final_target
    except Exception:
        return None, None
    for find, source in ((_parameter_doc_hint, HINT_PARAM_DOC), (_warning_hint, HINT_WARNING)):
        text = find(fn, param)
        if text:
            return text[:300], source
    return None, None


def _doc_lines(obj: Any) -> list[str]:
    try:
        doc = obj.docstring.value if obj.docstring else ""
    except Exception:
        doc = ""
    return (doc or "").splitlines()


def _mentions(text: str, name: str) -> bool:
    """Does ``text`` name ``name`` as a whole word (``stop`` is not in ``stop_sequences``)?"""
    return re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", text) is not None


def _sentence(lines: list[str], i: int) -> str | None:
    """Line ``i`` of a docstring and the lines that continue its sentence (docstrings wrap
    long sentences, and ".. deprecated:: 1.0" is usually followed by the advice); None when
    it ends with a colon, cut off before what it announces."""
    chunk = [lines[i].strip()]
    for j, follow in enumerate(lines[i + 1 : i + 4], start=i + 1):
        if not follow.strip() or _is_param_head(follow) or _doc_section(lines, j) is not None:
            break
        chunk.append(follow.strip())
    text = " ".join(" ".join(chunk).split())
    return None if text.endswith(":") else text


def _parameter_doc_hint(fn: Any, param: str) -> str | None:
    """The sentence of ``param``'s own docstring entry that says "deprecated", or None.

    The entry is the parameter's line (``stop_sequences (`list[str]`, *optional*):`` in
    Google style, ``stop_sequences : list`` in numpydoc) and the lines indented below it.
    """
    lines = _doc_lines(fn)
    for i, line in enumerate(lines):
        if not _is_param_head(line):
            continue
        name = line.strip().lstrip("*").split("(")[0].split(":")[0].strip("* ")
        if name != param:
            continue
        indent = len(line) - len(line.lstrip())
        head = line.split(":", 1)[1] if ":" in line else ""
        entry = [head, *lines[i + 1 :]]
        for j, text in enumerate(entry):
            if j and text.strip() and len(text) - len(text.lstrip()) <= indent:
                break  # the next parameter, or the end of the section
            if "deprecat" in text.lower():
                return _sentence(entry, j)
        return None
    return None


def _warning_hint(fn: Any, param: str) -> str | None:
    """The message of a ``warnings.warn`` in the function's own code that names ``param`` and
    says "deprecated" (huggingface_hub 0.36's ``hf_hub_download``: "`resume_download` is
    deprecated and will be removed in version 1.0.0. ..."), or None."""
    try:
        with _quiet():
            tree = ast.parse(textwrap.dedent(fn.source))
    except Exception:  # no source (a stub), or code this Python cannot parse
        return None
    for text in _warn_messages(tree):
        if "deprecat" in text.lower() and _mentions(text, param):
            return text
    return None


def _warn_messages(tree: ast.AST) -> Iterator[str]:
    """The text of each ``warn(...)`` call in ``tree``, on one line: its message, or for a
    name, the message last assigned to that name before the call (httpx 0.27's
    ``Client.__init__``: ``message = ("The 'proxies' argument is now deprecated." " Use 'proxy'
    or 'mounts' instead.")``, then ``warnings.warn(message, DeprecationWarning)``)."""
    assigned: dict[str, list[tuple[int, ast.expr]]] = {}
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(target := node.targets[0], ast.Name):
                assigned.setdefault(target.id, []).append((node.lineno, node.value))
        elif isinstance(node, ast.Call) and _name(node.func) == "warn" and node.args:
            calls.append(node)
    for call in calls:
        message = call.args[0]
        if isinstance(message, ast.Name):
            earlier = [a for a in assigned.get(message.id, ()) if a[0] <= call.lineno]
            if earlier:
                message = max(earlier, key=lambda a: a[0])[1]
        yield " ".join(_message(message).split())


def _message(node: ast.AST, value: str = "...") -> str:
    """The text of a message expression: a string, an f-string (its values as ``value``),
    strings joined with ``+``, or the template of ``"...".format(...)`` or ``"..." % x``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else value for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _message(node.left, value) + _message(node.right, value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _message(node.left, value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _message(node.func.value, value)
    return ""


# ------------------------------------------- deprecations announced with warnings.warn
# A function whose source has neither word issues no warning (a quick test before parsing it).
_MAY_WARN = re.compile(r"warn|deprecat", re.IGNORECASE)
# A helper that warns for its callers (``_deprecate(msg)``, ``warn_deprecated(...)``): a
# function of the package whose name says so, and that warns at every call. A flag of a
# function whose name says so (``deprecated=False``) asks it to warn: a warning only when the
# caller sets it is opt-in (statsmodels' internal ``check_random_state``), not a deprecation.
_HELPER_NAME = re.compile(r"deprecat|warn", re.IGNORECASE)
# A string default that stands for "not passed" (scikit-learn's ``n_init="warn"`` and
# ``y_pred="deprecated"``): a caller that gives anything else passes the parameter.
_SENTINEL_STRING = re.compile(r"deprecated?|warn|default|no_?default|unset|<[^<>]*>", re.I)
# The parameter of a helper its message comes from when the helper builds the message from
# several (matplotlib's ``warning = _generate_deprecation_warning(since, message, name, ...)``).
_MESSAGE_PARAMETER = re.compile(r"msg|message|text|warning|reason", re.IGNORECASE)
# What a warning's message shows for a value an f-string or a format puts in it ("Passing {...}
# to {...}.join is deprecated"): not "...", which reads as the end of a sentence.
_VALUE = "{...}"
# The longest condition APIChange.warning["condition"] quotes.
_CONDITION_LIMIT = 120
# A condition a warning is issued under (:func:`_calls_under`): the ``if`` test (or the left
# operand of an ``and``/``or``, the test of ``x if c else y``) and the branch (False for its
# ``else``), or ``(None, True)`` for a loop, a comprehension, an ``except`` or a ``match`` case.
_Condition = tuple[ast.expr | None, bool]


class _Guard(tuple):  # type: ignore[type-arg]
    """A :data:`_Condition` that an ``if <test>: return`` at the top of a function puts
    what follows it under (:func:`_calls_under`)."""

    __slots__ = ()


# A message that says a result or a default will change rather than that the API goes away
# (pandas 3's "The behavior of X.to_pytimedelta is deprecated, in a future version this will
# return a Series", scikit-learn's "The default value of `n_init` will change from 4 to 1"):
# the function is not deprecated, the behaviour is (APIChange.warning["behavior"]). It says so
# in words (:data:`_BEHAVIOR`) and does not say the API is deprecated or goes (_DEPRECATES),
# unless it names the behaviour or the default; or it is a FutureWarning that says neither.
_BEHAVIOR = re.compile(
    r"\bbehaviou?r\b"
    r"|\bdefault\b[^.]*\b(?:will|is going to|is changing|is switching)\b"
    r"|\bin (?:a |the )?future(?: versions?| releases?)?(?: of \w+)?,?\s+(?:\S+\s+){0,10}?"
    r"will\s+(?!raise|be\s+removed|stop|no\s+longer|not\b|error|fail)"
    r"|\b(?:beginning|starting) (?:in|with|from)\b[^.]*\bwill\s+"
    r"(?!raise|be\s+removed|stop|no\s+longer|not\b|error|fail)"
    r"|\bwill (?:return|change|become|switch|now)\b|\bis (?:changing|switching) to\b",
    re.IGNORECASE,
)
_DEPRECATES = re.compile(
    r"deprecat|\bremov|\bwill (?:raise|error|fail|stop)\b|\braise an? \w*(?:error|exception)"
    r"|\b(?:error|exception) will be raised|\bno longer\b|\bnot be (?:supported|allowed|accepted)"
    r"|\bunsupported\b|\brenamed\b|\bdisallow",
    re.IGNORECASE,
)
_BEHAVIOR_WORD = re.compile(r"\bbehaviou?r\b|\bdefault\b", re.IGNORECASE)
# What the text of a warning of another category says when it announces a deprecation all the
# same: scikit-learn 1.6 warned "The `cv='prefit'` option is deprecated in 1.6 ..." as a
# UserWarning, and 1.7 as a FutureWarning (no new deprecation).
_DEPRECATION_TEXT = re.compile(
    r"deprecat|will be removed|removed in|will raise|in the future|future versions?|no longer",
    re.IGNORECASE,
)
# A version in a message ("will be removed in 3.0", "in version 4", "v5.19"), which two releases'
# texts of the same warning may differ in.
_VERSION_TOKEN = re.compile(r"\bv?\d+(?:\.\d+)+\w*|\b(?:version|release|v)\s*\d+\b", re.IGNORECASE)
# The words a message names a parameter with: "the 'copy' keyword", "the inv argument".
_PARAMETER_NOUNS = r"(?:keyword|parameter|argument|kwarg|option)s?"
# The builtin types an ``isinstance`` test is evaluated with (:func:`_evaluate`).
_TYPES: dict[str, type] = {
    t.__name__: t
    for t in (str, int, float, bool, complex, bytes, list, tuple, dict, set, frozenset, type)
}
_TYPES["object"] = object
# A printf-style placeholder (``%s``, ``%(name)s``, ``%%``).
_PRINTF = re.compile(
    r"%(?:\((\w+)\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?([diouxXeEfFgGcrsa%])"
)


@dataclass
class _Warned:
    """One deprecation warning a function issues (:meth:`_WarnReader.deprecations`)."""

    when: str  # WARNS_*
    category: str  # as the code names it: "FutureWarning", "Pandas4Warning"
    message: str  # on one line; "" when it cannot be read statically
    at: str | None  # "pandas/core/frame.py:6950"
    parameter: str | None = None  # WARNS_PARAMETER, WARNS_DEFAULT
    # The tests it is issued under, as code: for WARNS_CONDITION all of them; for a parameter
    # those besides the one that says it is passed (or left out). None when there is none to
    # quote: a loop, an ``except``, a ``return`` in a block before it, or longer than
    # _CONDITION_LIMIT.
    condition: str | None = None
    from_kwargs: bool = False  # the parameter is a key of ``**kwargs`` ("p" in kwargs)
    # Issued only in some of the calls ``when`` says (WARNS_CONDITION always is).
    conditional: bool = False
    readable: bool = False  # the condition reads nothing but the function's parameters
    behavior: bool = False  # the message says the behaviour or the default changes
    on_init: bool = False  # the parameter is the class constructor's (``self.p`` in a method)
    access: str | None = None  # WARNS_ACCESS: "read", "set" or "delete"
    deprecation: bool = True  # its category announces a deprecation (not a UserWarning)

    @property
    def key(self) -> tuple[Any, ...]:
        """What is deprecated: the function, a parameter, an access, or the function under a
        condition. Two releases' warnings with one key are one deprecation."""
        if self.parameter is not None:
            return self.when, self.parameter, self.on_init
        if self.when == WARNS_ACCESS:
            return self.when, self.access, self.condition
        return self.when, self.condition

    def evidence(self) -> dict[str, Any]:
        """APIChange.warning."""
        out: dict[str, Any] = {"when": self.when, "category": self.category, "at": self.at}
        if self.access is not None:
            out["access"] = self.access
        if self.when == WARNS_CONDITION or self.conditional:
            out["condition"] = self.condition
            if self.condition and self.readable:
                out["readable"] = True
        if self.behavior:
            out["behavior"] = True
        return out


@dataclass
class _Reading:
    """A function whose warnings are read: its parsed ``def`` and what its names stand for."""

    node: ast.FunctionDef | ast.AsyncFunctionDef
    module: Any
    owner: Any  # the class of a method, else None
    names: set[str]  # every parameter's name, ``self`` and ``cls`` included
    defaults: dict[str, str | None]  # each parameter but ``self``/``cls``: its default, as code
    var_keyword: str | None  # the name of its ``**kwargs``
    me: str | None  # a method's first parameter (``self``)
    init: dict[str, str | None]  # its class's constructor's parameters, read as ``self.p``
    aliases: dict[str, list[tuple[int, ast.expr | None, bool]]]  # where locals are bound (_aliases)
    imports: dict[str, str]  # a name the function imports itself: the path it stands for


@dataclass(frozen=True)
class _Read:
    """A parameter a test reads (:func:`_parameter_read`): its name, whether it is a key of
    ``**kwargs``, its default as code (None for a required one), and whether it is the class
    constructor's (``self.n_init`` in ``MDS.fit_transform``)."""

    name: str
    from_kwargs: bool
    default: str | None
    on_init: bool = False


@dataclass(frozen=True)
class _Passes:
    """A helper's call that warns while one of the caller's parameters is not a sentinel
    (``("copy", "lib.no_default")``), or while ``**kwargs`` has a key (``("all", None, True)``):
    the parameter, and the sentinel."""

    name: str
    sentinel: str | None
    from_kwargs: bool = False


@dataclass
class _Helper:
    """A function or method of the package that warns of a deprecation for its callers, and
    what it takes from them: the message and the category may be its parameters (or a
    template of its parameters, filled with what a call gives), and it may warn only when one
    of its parameters is not a sentinel (pandas 3's ``NDFrame._check_copy_deprecation(copy)``:
    ``if copy is not lib.no_default: warn``) or when its ``**kwargs`` has a key (Django 5.2's
    ``_check_deprecated_find_param(**kwargs)``: ``if "all" in kwargs: warn``)."""

    parameters: list[str]  # in the order a call passes them positionally
    defaults: dict[str, ast.expr]
    reading: _Reading  # its own: where its names (a default category, a template) are read
    message: str  # its own message, when it does not take one
    message_from: str | None  # the parameter that is the message
    message_node: ast.expr | None  # a template of its parameters (f"{name} is deprecated")
    message_line: int
    category: str | None  # its own category, when it does not take one
    category_from: str | None  # the parameter that is the category
    # The parameter it warns of, when it warns only while that one is not ``guard_default``
    # (as code): a caller that hands it a parameter of its own with that default warns when
    # that parameter is passed.
    guard: str | None = None
    guard_default: str | None = None
    guard_key: str | None = None  # it warns while its ``**kwargs`` has this key

    def argument(self, call: ast.Call, name: str) -> ast.expr | None:
        """What ``call`` passes for the parameter ``name``, or its default."""
        for k in call.keywords:
            if k.arg == name:
                return k.value
        i = self.parameters.index(name) if name in self.parameters else -1
        if 0 <= i < len(call.args) and not any(
            isinstance(a, ast.Starred) for a in call.args[: i + 1]
        ):
            return call.args[i]
        return self.defaults.get(name)


@dataclass
class _Issued:
    """What a call issues (:meth:`_WarnReader.warning_of`): the category (its name, "" when
    the call does not say) and whether it announces a deprecation, the message, and for a
    helper that warns while a parameter is not a sentinel, the caller's parameter handed to
    it (:class:`_Passes`). Read inside a helper, what of the warning its own parameters give:
    the message, a template of them, the category."""

    category: str
    deprecation: bool
    message: str
    passes: _Passes | None = None
    message_from: str | None = None
    message_node: ast.expr | None = None
    category_from: str | None = None


class _WarnReader:
    """The deprecation warnings that the functions of one loaded release issue, read from
    their sources (nothing is imported or run).

    A warning is a call of ``warnings.warn`` (or ``warn`` imported from it, or
    ``warnings.warn_explicit``), resolved through the module's imports and the function's own
    (``from . import warn_external`` in matplotlib's ``warn_deprecated``), whose category is a
    DeprecationWarning, PendingDeprecationWarning or FutureWarning, or a class that derives
    from one (pandas 3's ``Pandas4Warning(PandasDeprecationWarning)``), or a class whose name
    says "Deprecat" (astropy's ``AstropyDeprecationWarning(AstropyWarning)``): given as the
    second argument, as ``category=``, or as the warning itself (``warn(FutureWarning("..."))``,
    ``w = SymPyDeprecationWarning(...); warn(w)``). A call of a helper of the package
    (:meth:`helper`) counts as the warning it issues. The message is a literal, an f-string (a
    constant its value is, else ``{...}``), strings joined with ``+``, a ``%`` or ``.format``
    template filled with the constants given (``{...}`` for the others), ``textwrap.dedent``,
    ``.strip()`` or ``.replace()`` of one, a name assigned one of them earlier in the function
    (``msg += "..."`` included), or a constant of the module (or one it imports from the
    package)."""

    def __init__(self, root: Any, source: Any) -> None:
        self.root = root
        self.package = str(root.path).split(".")[0]
        self._source = source  # _Differ._source: a module's parsed source, cached
        self._helpers: dict[tuple[str, int], _Helper | None] = {}
        self._categories: dict[str, tuple[str, bool]] = {}
        self._module_texts: dict[str, set[str]] = {}

    # ------------------------------------------------------------ a function's warnings
    def deprecations(self, fn: Any, *, every: bool = False, depth: int = 0) -> list[_Warned]:
        """The deprecation warnings ``fn`` issues, in the order of its source, each with when
        it is issued (:func:`_classify`); with ``every``, its warnings of any category too
        (the release at the cutoff's, which :meth:`_Differ._warned` compares with). [] for a
        function without a source (a stub). A method whose body only returns
        ``super().<its name>(...)`` issues what the base's method does (scikit-learn's
        ``LassoCV.fit``)."""
        try:
            if getattr(fn, "is_alias", False):
                fn = fn.final_target
            source = fn.source or ""
        except Exception:
            return []
        node = _function_def(fn)
        if node is None:
            return []
        delegated = self._delegated(fn, node)
        if delegated is not None:
            return self.deprecations(delegated, every=every, depth=depth + 1) if depth < 2 else []
        if not _MAY_WARN.search(source):
            return []
        r = self.reading(fn, node)
        file = _file_of(fn)
        found: list[_Warned] = []
        for call, conditions, exited in _calls_under(node):
            got = self.warning_of(call, r, every=every)
            if got is None or not (got.deprecation or every):
                continue
            passes = got.passes
            if (
                passes is not None
                and not passes.from_kwargs
                and not _same_code(r.defaults.get(passes.name) or "", passes.sentinel or "")
            ):
                continue  # hands the helper something else: not read
            behavior = _behavior(got.message, got.category)
            found_class = _classify(conditions, exited, r, got.message, passes, behavior)
            if found_class is None:
                continue  # opt-in: the caller asks for the warning
            when, parameter, condition, from_kwargs, conditional, readable, on_init = found_class
            line = int(getattr(fn, "lineno", 1) or 1) + call.lineno - 1
            found.append(
                _Warned(
                    when,
                    got.category or "UserWarning",
                    got.message,
                    f"{file}:{line}" if file else None,
                    parameter,
                    condition,
                    from_kwargs,
                    conditional or when == WARNS_CONDITION,
                    readable,
                    behavior,
                    on_init,
                    None,
                    got.deprecation,
                )
            )
        return found

    def reading(self, fn: Any, node: ast.FunctionDef | ast.AsyncFunctionDef) -> _Reading:
        """``fn`` (loaded, and parsed as ``node``) as :class:`_Reading` reads it."""
        module = fn.module
        parent = getattr(fn, "parent", None)
        owner = parent if getattr(parent, "is_class", False) else None
        args = node.args
        names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        names |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
        defaults, var_keyword = _signature_defaults(node)
        me = _first_parameter(node) if owner is not None else None
        init: dict[str, str | None] = {}
        if owner is not None and me is not None and fn.name != "__init__":
            ctor = _member(owner, "__init__")
            ctor_node = _function_def(ctor) if getattr(ctor, "is_function", False) else None
            if ctor_node is not None:
                init = _signature_defaults(ctor_node)[0]
        return _Reading(
            node,
            module,
            owner,
            names,
            defaults,
            var_keyword,
            me,
            init,
            _aliases(node),
            _local_imports(node, module),
        )

    def _delegated(self, fn: Any, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Any:
        """The base's method a method hands its call to, when its body (its docstring aside)
        is only ``return super().<its name>(...)``; else None."""
        body = [s for s in node.body if not _is_docstring_statement(s)]
        if len(body) != 1 or not isinstance(body[0], (ast.Return, ast.Expr)):
            return None
        value = body[0].value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == fn.name
            and isinstance(value.func.value, ast.Call)
            and _is_name(value.func.value.func, "super")
        ):
            return None
        owner = getattr(fn, "parent", None)
        try:
            bases = owner.mro() if owner is not None and getattr(owner, "is_class", False) else []
        except Exception:
            return None
        for base in bases:
            member = _member(base, fn.name) if fn.name in base.members else None
            if member is not None:
                return member if getattr(member, "is_function", False) else None
        return None

    def warning_of(
        self, call: ast.Call, r: _Reading, depth: int = 0, *, every: bool = False
    ) -> _Issued | None:
        """What warning ``call`` (in the function ``r`` reads) issues, or None when it issues
        no deprecation warning (another call, another category, or none given: a
        UserWarning; with ``every``, any warning). ``self.helper(...)`` and
        ``cls.helper(...)`` are looked up on the method's class."""
        dotted = _dotted(call.func)
        if dotted is None:
            return None
        head, _, name = dotted.partition(".")
        bound = False
        if r.owner is not None and head in ("self", "cls") and name and "." not in name:
            resolved = _member(r.owner, name)
            bound = resolved is not None and "staticmethod" not in _labels(resolved)
        else:
            resolved = self.resolve(r.module, dotted, r.imports)
        if isinstance(resolved, str):
            if resolved not in _WARN_FUNCTIONS and dotted != "warn":
                return None
            return self._direct(call, r, every)
        if not getattr(resolved, "is_function", False) or depth > 2:
            return None
        helper = self.helper(resolved, depth)
        if helper is None:
            return None
        if bound and helper.parameters:
            helper = replace(helper, parameters=helper.parameters[1:])  # self, cls
        return self._through(helper, call, r, every)

    def _direct(self, call: ast.Call, r: _Reading, every: bool) -> _Issued | None:
        """:meth:`warning_of` for a call of ``warnings.warn`` itself."""
        keywords = {k.arg: k.value for k in call.keywords if k.arg}
        message = call.args[0] if call.args else keywords.get("message")
        kind = call.args[1] if len(call.args) > 1 else keywords.get("category")
        if kind is None:
            instance = self._instance(message, r, call.lineno)
            if instance is not None:  # warn(FutureWarning("...")), or one built before
                kind = instance.func
                message = _first_argument(instance)
        category_from = _name_in(kind, r.names)
        if kind is None:
            category, deprecation = "UserWarning", False
        else:
            category, deprecation = self.category_of(r.module, kind, r.imports)
        if category_from is None and not deprecation and not every:
            return None
        message_from, message_node = self._from_parameters(message, r, call.lineno)
        return _Issued(
            category,
            deprecation,
            self.text(message, r, call.lineno),
            None,
            message_from,
            message_node,
            category_from,
        )

    def _through(self, helper: _Helper, call: ast.Call, r: _Reading, every: bool) -> _Issued | None:
        """:meth:`warning_of` for a call of a helper: the category and the message the call
        gives it (or the helper's own), and when it warns."""
        category = helper.category or ""
        deprecation = helper.category is not None
        category_from = None
        if helper.category_from is not None:
            given = helper.argument(call, helper.category_from)
            if given is not None and given is helper.defaults.get(helper.category_from):
                category, deprecation = self.category_of(
                    helper.reading.module, given, helper.reading.imports
                )
            elif given is not None:
                category_from = _name_in(given, r.names)
                category, deprecation = self.category_of(r.module, given, r.imports)
        message_from: str | None = None
        message_node: ast.expr | None = None
        if helper.message_from is not None:
            given = helper.argument(call, helper.message_from)
            instance = self._instance(given, r, call.lineno)
            if instance is not None and not deprecation and category_from is None:
                # _warnings_warn(warn) of a warning built from its class (sqlalchemy)
                category, deprecation = self.category_of(r.module, instance.func, r.imports)
                category_from = _name_in(instance.func, r.names)
                given = _first_argument(instance)
            message = self.text(given, r, call.lineno, from_helper=True)
            message_from, message_node = self._from_parameters(given, r, call.lineno)
        elif helper.message_node is not None:
            message = self._filled(helper, call, r)
        else:
            message = helper.message
        if not deprecation and category_from is None and not (every and category):
            return None
        passes = None
        if helper.guard is not None:
            given = helper.argument(call, helper.guard)
            if not isinstance(given, ast.Name):
                return None  # not a parameter of the caller's: when it warns is not read
            passes = _Passes(given.id, helper.guard_default)
        elif helper.guard_key is not None:
            passed = {k.arg for k in call.keywords if k.arg}
            if helper.guard_key in passed or not _forwards(call, r.var_keyword):
                return None  # passes the key itself, or not the caller's **kwargs
            passes = _Passes(helper.guard_key, None, True)
        return _Issued(
            category, deprecation, message, passes, message_from, message_node, category_from
        )

    def _filled(self, helper: _Helper, call: ast.Call, r: _Reading) -> str:
        """A helper's template message (``f"The {name!r} keyword is deprecated"``) with what
        ``call`` gives its parameters: a constant, or a name of the caller's with a constant
        value; ``{...}`` for the others."""
        bindings: dict[str, str] = {}
        for name in helper.parameters:
            given = helper.argument(call, name)
            if given is None or given is helper.defaults.get(name):
                continue
            value = self._render(given, r, call.lineno, {}, 0)
            if value is not None:
                bindings[name] = value
        out = self._render(helper.message_node, helper.reading, helper.message_line, bindings, 0)
        return " ".join((out or "").split())

    def _instance(self, node: ast.expr | None, r: _Reading, line: int) -> ast.Call | None:
        """The call that builds the warning ``node`` is, when it is one: ``FutureWarning(
        "...")``, a name assigned one before (``w = SymPyDeprecationWarning(...)``), or a call
        of the function's own parameter (``type_(msg)``); else None."""
        if isinstance(node, ast.Name) and node.id not in r.names:
            node = _assigned_value(r.node, node.id, line)
        if not isinstance(node, ast.Call):
            return None
        dotted = _dotted(node.func)
        if dotted is None:
            return None
        if _name_in(node.func, r.names) or dotted.rsplit(".", 1)[-1].endswith("Warning"):
            return node
        resolved = self.resolve(r.module, dotted, r.imports)
        return node if getattr(resolved, "is_class", False) else None

    def _from_parameters(
        self, node: ast.expr | None, r: _Reading, line: int
    ) -> tuple[str | None, ast.expr | None]:
        """What of a message ``node`` comes from the parameters of the function ``r`` reads:
        the parameter it is (or the message-like one a value built from several is: polars'
        ``message = f"{message.strip()} ..."``, matplotlib's ``warning =
        _generate_deprecation_warning(since, message, ...)``), or a template of them
        (``f"{name} is deprecated"``); ``(None, None)`` when nothing does."""
        if node is None:
            return None, None
        if isinstance(node, ast.Name):
            if node.id in r.names:
                return node.id, None
            value = _assigned_value(r.node, node.id, line)
            if value is None:
                return None, None
            if not isinstance(value, (ast.JoinedStr, ast.BinOp, ast.Constant)):
                read = _loaded_names(value) & r.names
                message = sorted(n for n in read if _MESSAGE_PARAMETER.fullmatch(n))
                return (message[0], None) if message else (None, None)
            node = value
        if _loaded_names(node) & r.names:
            return None, node
        return None, None

    # ------------------------------------------------------------------- helpers
    def helper(self, fn: Any, depth: int = 0) -> _Helper | None:
        """``fn`` as a helper that warns of a deprecation for its callers, or None: a function
        of the package, or a method of one of its classes, whose name says "deprecat" or
        "warn" (``_deprecate``, ``warn_deprecated``, ``_check_copy_deprecation``) and that
        issues a deprecation warning at every call, directly or through another such helper,
        or whenever one of its parameters is not a sentinel (``if copy is not
        lib.no_default:``), or whenever its ``**kwargs`` has a key (``if "all" in kwargs:``).
        A helper's own category and message parameters are no such guard: sqlalchemy's
        ``_warnings_warn(message, category=None)`` warns at every call."""
        path = str(fn.path)
        key = (path, depth)
        if key in self._helpers:
            return self._helpers[key]
        self._helpers[key] = None  # a helper that calls itself is none
        found: _Helper | None = None
        parent = getattr(fn, "parent", None)
        if (
            _HELPER_NAME.search(fn.name)
            and (getattr(parent, "is_module", False) or getattr(parent, "is_class", False))
            and path.split(".")[0] == self.package
        ):
            node = _function_def(fn)
            if node is not None:
                found = self._helper_of(fn, node, depth)
        self._helpers[key] = found
        return found

    def _helper_of(
        self, fn: Any, node: ast.FunctionDef | ast.AsyncFunctionDef, depth: int
    ) -> _Helper | None:
        """:meth:`helper` for the parsed ``def`` of a function or method."""
        r = self.reading(fn, node)
        args = node.args
        positional = [a.arg for a in (*args.posonlyargs, *args.args)]
        parameters = positional + [a.arg for a in args.kwonlyargs]
        defaults: dict[str, ast.expr] = dict(
            zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True)
        )
        defaults.update(
            {a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d}
        )
        for call, conditions, exited in _calls_under(node):
            got = self.warning_of(call, r, depth + 1, every=True)
            if got is None:
                continue
            instance_later = (
                got.message_from is not None and got.category == "UserWarning"
            )  # warn(message): a warning instance its callers build
            if not (got.deprecation or got.category_from is not None or instance_later):
                continue
            guard: tuple[str, str] | None = None
            key: str | None = None
            if conditions or exited:
                own = {n for n in (got.message_from, got.category_from) if n is not None}
                guard = _sentinel_guard(conditions, r.names - own)
                key = _kwargs_guard(conditions, r.var_keyword) if guard is None else None
                if guard is None and key is None and not (own and _sentinel_guard(conditions, own)):
                    continue  # a helper warns at every call, or while a parameter is passed
            if got.passes is not None:
                if guard is not None or key is not None:
                    continue
                if got.passes.from_kwargs:
                    key = got.passes.name  # through this helper's own **kwargs
                elif got.passes.name in r.names and got.passes.sentinel is not None:
                    guard = (got.passes.name, got.passes.sentinel)
                else:
                    continue
            own_text = not (got.message_from or got.message_node is not None)
            return _Helper(
                parameters,
                defaults,
                r,
                message=got.message if own_text else "",
                message_from=got.message_from,
                message_node=got.message_node,
                message_line=call.lineno,
                category=got.category if got.deprecation and not got.category_from else None,
                category_from=got.category_from,
                guard=guard[0] if guard else None,
                guard_default=guard[1] if guard else None,
                guard_key=key,
            )
        return None

    # ------------------------------------------------------------------- names
    def resolve(self, module: Any, dotted: str, imports: dict[str, str] | None = None) -> Any:
        """What ``dotted`` (``warnings.warn``, ``errors.Pandas4Warning``) is in ``module``:
        the loaded object, or the path it stands for when that is outside the package
        (``"warnings.warn"``), or the name itself when nothing binds it (a builtin).
        ``imports`` are the function's own imports, which come first."""
        head, *rest = dotted.split(".")
        if imports and head in imports:
            target = ".".join([imports[head], *rest])
            if target.split(".")[0] != self.package:
                return target
            found = find_object(self.root, target)
            return found if found is not None else target
        try:
            obj = module.members.get(head)
        except Exception:
            obj = None
        if obj is None:
            return dotted
        for i, part in enumerate([*rest, None]):
            if getattr(obj, "is_alias", False):
                try:
                    obj = obj.final_target
                except Exception:
                    return ".".join([str(getattr(obj, "target_path", head)), *rest[i:]])
            if part is None:
                return obj
            try:
                member = obj.members.get(part)
            except Exception:
                member = None
            if member is None:
                return ".".join([str(obj.path), *rest[i:]])
            obj = member
        return obj

    def category_of(
        self, module: Any, node: ast.expr | None, imports: dict[str, str] | None = None
    ) -> tuple[str, bool]:
        """The name of the warning category ``node`` names in ``module`` ("" for none it can
        read), and whether it announces a deprecation (:meth:`announces`)."""
        dotted = _dotted(node) if node is not None else None
        if dotted is None:
            return "", False
        resolved = self.resolve(module, dotted, imports)
        found = self.announces(resolved)
        if found:
            return found, True
        if isinstance(resolved, str):
            return resolved.rsplit(".", 1)[-1], False
        return str(getattr(resolved, "name", dotted.rsplit(".", 1)[-1])), False

    def category(self, module: Any, node: ast.expr | None) -> str | None:
        """The name of a deprecation category ``node`` names in ``module``, else None."""
        name, deprecation = self.category_of(module, node)
        return name if deprecation else None

    def announces(self, resolved: Any, depth: int = 0) -> str | None:
        """The name of a category (:meth:`resolve`'s result) that is a deprecation warning, or
        None: one of the three built-in ones, a class that derives from one (``Pandas4Warning``),
        a name bound to one (``_DEPRECATION = FutureWarning``), or a class whose name says
        "Deprecat" (astropy's ``AstropyDeprecationWarning``, which derives from a
        ``UserWarning``; another package's, which is not loaded)."""
        if depth > 8:
            return None
        if isinstance(resolved, str):
            name = resolved.removeprefix("builtins.")
            last = name.rsplit(".", 1)[-1]
            return last if name in _DEPRECATION_CATEGORIES or "Deprecat" in last else None
        path = str(getattr(resolved, "path", ""))
        if path in self._categories:
            name, deprecation = self._categories[path]
            return name if deprecation else None
        self._categories[path] = ("", False)  # a class among its own bases is none
        found: str | None = None
        if getattr(resolved, "is_attribute", False):
            value = getattr(resolved, "value", None)
            parent = getattr(resolved, "parent", None)
            if value is not None and getattr(parent, "is_module", False):
                found = self.announces(self.resolve(parent, str(value)), depth + 1)
        elif getattr(resolved, "is_class", False):
            if "Deprecat" in str(resolved.name):
                found = str(resolved.name)
            for base in getattr(resolved, "bases", None) or ():
                if found:
                    break
                where = str(getattr(base, "canonical_path", None) or base)
                target = find_object(self.root, where) if "." in where else None
                if self.announces(target if target is not None else where, depth + 1):
                    found = str(resolved.name)
        self._categories[path] = (found or "", found is not None)
        return found

    # ------------------------------------------------------------------- messages
    def text(
        self, node: ast.expr | None, r: _Reading, line: int, *, from_helper: bool = False
    ) -> str:
        """The message ``node`` gives, on one line (:class:`_WarnReader`); "" when it cannot be
        read statically. A message a caller hands a helper may hold the helper's own
        ``%(since)s`` placeholders (matplotlib): ``{...}``."""
        out = self._render(node, r, line, {}, 0)
        if not out:
            return ""
        if from_helper:
            out = re.sub(r"%\(\w+\)[a-z]", _VALUE, out)
        return " ".join(out.split())

    def _render(
        self,
        node: ast.AST | None,
        r: _Reading | None,
        line: int,
        bindings: dict[str, str],
        depth: int,
        module: Any = None,
    ) -> str | None:
        """The text of the expression ``node`` in the function ``r`` reads (or at the top of
        ``module``), with ``bindings`` for its names; None when it is not a string this can
        read."""
        if node is None or depth > 8:
            return None
        module = r.module if r is not None else module

        def again(n: ast.AST | None) -> str | None:
            return self._render(n, r, line, bindings, depth + 1, module)

        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                    continue
                inner = getattr(v, "value", None)
                text = None
                if isinstance(inner, ast.Constant):
                    text = str(inner.value)
                elif isinstance(inner, ast.Name) and inner.id in bindings:
                    text = bindings[inner.id]
                if text is not None and getattr(v, "conversion", -1) == ord("r"):
                    text = repr(text)
                parts.append(_VALUE if text is None else text)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = again(node.left), again(node.right)
            if left is None and right is None:
                return None
            return (_VALUE if left is None else left) + (_VALUE if right is None else right)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            template = again(node.left)
            if template is None:
                return None
            given = node.right
            if isinstance(given, ast.Dict):
                named = {
                    str(k.value): again(v) or _VALUE
                    for k, v in zip(given.keys, given.values, strict=True)
                    if isinstance(k, ast.Constant)
                }
                return _printf(template, [], named)
            values = given.elts if isinstance(given, ast.Tuple) else [given]
            return _printf(template, [again(v) or _VALUE for v in values], {})
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr == "format":
                template = again(node.func.value)
                if template is None:
                    return None
                return _format(
                    template,
                    [again(a) or _VALUE for a in node.args],
                    {k.arg: again(k.value) or _VALUE for k in node.keywords if k.arg},
                )
            if attr in ("strip", "lstrip", "rstrip") and not node.args:
                text = again(node.func.value)
                return getattr(text, attr)() if text is not None else None
            if attr == "replace" and len(node.args) == 2:
                text, old, new = again(node.func.value), again(node.args[0]), again(node.args[1])
                if text is not None and old is not None and new is not None:
                    return text.replace(old, new)
                return text
        if isinstance(node, ast.Call) and node.args:
            dotted = _dotted(node.func) or ""
            if dotted in ("textwrap.dedent", "dedent"):
                text = again(node.args[0])
                return textwrap.dedent(text) if text is not None else None
            if dotted in ("inspect.cleandoc", "cleandoc"):
                text = again(node.args[0])
                return inspect.cleandoc(text) if text is not None else None
            return None
        if isinstance(node, ast.Name):
            if node.id in bindings:
                return bindings[node.id]
            if r is not None:
                value = _assigned_text(self, r, node.id, line, depth)
                if value is not None:
                    return value
                if node.id in r.names:
                    return None  # a parameter: what the caller gives
            return self.constant(module, node.id, imports=r.imports if r else None) or None
        if isinstance(node, ast.Attribute):
            chain = _dotted(node)
            if chain is None:
                return None
            return self.constant(module, chain, imports=r.imports if r else None) or None
        return None

    def constant(
        self, module: Any, dotted: str, depth: int = 0, imports: dict[str, str] | None = None
    ) -> str:
        """The text of a module-level string constant (``_DEPRECATION_MSG = "..."``) that
        ``dotted`` names in ``module``, imported from elsewhere in the package or its own; ""
        when there is none."""
        if module is None or depth > 4:
            return ""
        if "." not in dotted and not (imports and dotted in imports):
            tree = self._source(module).tree if getattr(module, "is_module", False) else None
            for stmt in reversed(tree.body if tree is not None else []):
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and _is_name(stmt.targets[0], dotted)
                ) or (isinstance(stmt, ast.AnnAssign) and _is_name(stmt.target, dotted)):
                    out = self._render(stmt.value, None, 0, {}, depth + 1, module)
                    return " ".join(out.split()) if out else ""
        resolved = self.resolve(module, dotted, imports)
        if not getattr(resolved, "is_attribute", False):
            return ""
        parent = getattr(resolved, "parent", None)
        if parent is module or not getattr(parent, "is_module", False):
            return ""
        return self.constant(parent, resolved.name, depth + 1)

    def module_texts(self, module: Any) -> set[str]:
        """The messages (:func:`_comparable`) of every call in ``module`` whose name says
        "warn" or "deprecat", whatever its category, read where they are literal: the
        release at the cutoff warned of them, if not in the function that warns now."""
        path = str(getattr(module, "path", ""))
        if path in self._module_texts:
            return self._module_texts[path]
        texts: set[str] = set()
        tree = self._source(module).tree if getattr(module, "is_module", False) else None
        for node in ast.walk(tree) if tree is not None else ():
            if not isinstance(node, ast.Call) or not _HELPER_NAME.search(_name(node.func)):
                continue
            text = self._render(_first_argument(node), None, 0, {}, 0, module)
            if text and _VALUE not in text and (comparable := _comparable(text)):
                texts.add(comparable)
        self._module_texts[path] = texts
        return texts


def _assigned_value(scope: ast.AST, name: str, line: int) -> ast.expr | None:
    """The value last assigned to the local ``name`` at or before ``line`` in ``scope``, or
    None."""
    earlier = [
        (n.lineno, n.value)
        for n in _own_nodes(scope)
        if (isinstance(n, ast.Assign) and len(n.targets) == 1 and _is_name(n.targets[0], name))
        or (isinstance(n, ast.AnnAssign) and _is_name(n.target, name) and n.value is not None)
    ]
    earlier = [(at, v) for at, v in earlier if at <= line and v is not None]
    return max(earlier, key=lambda a: a[0])[1] if earlier else None


def _assigned_text(
    reader: _WarnReader, r: _Reading, name: str, line: int, depth: int
) -> str | None:
    """The text of the local ``name`` at ``line``: the value last assigned to it, with the
    strings added to it since (``msg += " Use `a` instead."``); None when it has none."""
    statements = sorted(
        (
            n
            for n in _own_nodes(r.node)
            if (isinstance(n, ast.Assign) and len(n.targets) == 1 and _is_name(n.targets[0], name))
            or (isinstance(n, ast.AnnAssign) and _is_name(n.target, name) and n.value is not None)
            or (
                isinstance(n, ast.AugAssign)
                and isinstance(n.op, ast.Add)
                and _is_name(n.target, name)
            )
        ),
        key=lambda n: n.lineno,
    )
    statements = [n for n in statements if n.lineno <= line]
    start = max(
        (i for i, n in enumerate(statements) if not isinstance(n, ast.AugAssign)), default=None
    )
    if start is None:
        return None
    text = reader._render(statements[start].value, r, statements[start].lineno - 1, {}, depth + 1)
    if text is None:
        return None
    for n in statements[start + 1 :]:
        more = reader._render(n.value, r, n.lineno - 1, {}, depth + 1)
        text += _VALUE if more is None else more
    return text


def _printf(template: str, values: list[str], named: dict[str, str]) -> str:
    """``template % values`` (or ``% named``), each placeholder filled, ``{...}`` for one
    without a value."""
    left = iter(values)

    def fill(m: re.Match[str]) -> str:
        if m.group(2) == "%":
            return "%"
        if m.group(1):
            return named.get(m.group(1), _VALUE)
        return next(left, _VALUE)

    return _PRINTF.sub(fill, template)


def _format(template: str, values: list[str], named: dict[str, str]) -> str:
    """``template.format(*values, **named)``, each field filled, ``{...}`` for one without a
    value."""
    import string

    out: list[str] = []
    auto = iter(values)
    try:
        for literal, field_name, _spec, conversion in string.Formatter().parse(template):
            out.append(literal)
            if field_name is None:
                continue
            base = re.split(r"[.\[]", field_name, maxsplit=1)[0]
            if base == "":
                value = next(auto, _VALUE)
            elif base.isdigit():
                index = int(base)
                value = values[index] if index < len(values) else _VALUE
            else:
                value = named.get(base, _VALUE)
            if conversion == "r" and value != _VALUE:
                value = repr(value)
            out.append(value)
    except ValueError:
        return re.sub(r"\{[^{}]*\}", _VALUE, template)
    return "".join(out)


def _first_argument(call: ast.Call) -> ast.expr | None:
    """The first argument of a call: its first positional, else its ``message=``."""
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg in ("message", "msg")), None)


def _loaded_names(node: ast.AST) -> set[str]:
    """The names ``node`` reads."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function's body, not those of a function, class or lambda it defines."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (*_FUNCTIONS, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(current))


def _aliases(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, list[tuple[int, ast.expr | None, bool]]]:
    """Each local a function binds, with every place it binds it, in order: the line, what
    was assigned (None for any other binding: ``+=``, a loop's target, ``with ... as``), and
    whether the assignment is a statement of the body itself, run on every path
    (``tp_plan = kwargs.pop("tp_plan", None)``; scikit-learn's ``quantile_method =
    self.quantile_method``, assigned again under an ``if`` later)."""
    top = {id(s) for s in node.body}
    found: dict[str, list[tuple[int, ast.expr | None, bool]]] = {}
    assigned: set[int] = set()
    for n in _own_nodes(node):
        value: ast.expr | None = None
        target: ast.expr | None = None
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            target, value = n.targets[0], n.value
        elif isinstance(n, ast.AnnAssign) and n.value:
            target, value = n.target, n.value
        name = _bound_name(target) if target is not None else None
        if name is not None and target is not None:
            assigned.add(id(target))
            found.setdefault(name, []).append((getattr(n, "lineno", 0), value, id(n) in top))
    for n in _own_nodes(node):
        if isinstance(n, (ast.Name, ast.Attribute)) and isinstance(n.ctx, (ast.Store, ast.Del)):
            name = _bound_name(n)
            if name is not None and id(n) not in assigned:
                found.setdefault(name, []).append((n.lineno, None, False))
        elif isinstance(n, ast.arg) and n not in (*node.args.args, *node.args.kwonlyargs):
            found.setdefault(n.arg, []).append((n.lineno, None, False))  # a lambda's
    for places in found.values():
        places.sort(key=lambda p: p[0])
    return found


def _bound_name(target: ast.expr) -> str | None:
    """The name an assignment binds: a local's (``x``), or an attribute of a name's
    (``self.x``, transformers 5's ``self.continuous_batching_config = kwargs.pop(...)`` in a
    constructor); None for any other target."""
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return f"{target.value.id}.{target.attr}"
    return None


def _alias(r: _Reading, name: str, line: int | None) -> ast.expr | None:
    """What the local ``name`` holds where it is read at ``line``: the value of its one
    assignment, or of the last assignment before ``line`` when that one runs on every path
    and nothing binds the name again before the read; else None."""
    places = r.aliases.get(name)
    if not places:
        return None
    if len(places) == 1:
        return places[0][1]
    before = [p for p in places if line is not None and p[0] < line]
    if not before:
        return None
    _at, value, top = before[-1]
    return value if top else None


def _local_imports(node: ast.AST, module: Any) -> dict[str, str]:
    """The names a function's own ``import`` and ``from ... import`` bind, with the paths
    they stand for (a relative one against ``module``'s package)."""
    found: dict[str, str] = {}
    package = str(getattr(module, "path", "")).split(".")
    try:
        is_package = bool(module.is_init_module)
    except Exception:
        is_package = False
    for n in _own_nodes(node):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    found[a.asname] = a.name
                else:
                    head = a.name.split(".")[0]
                    found[head] = head
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = package if is_package else package[:-1]
                base = base[: len(base) - (n.level - 1)]
                prefix = ".".join([*base, *([n.module] if n.module else [])])
            else:
                prefix = n.module or ""
            for a in n.names:
                if a.name != "*":
                    found[a.asname or a.name] = f"{prefix}.{a.name}" if prefix else a.name
    return found


def _is_docstring_statement(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _forwards(call: ast.Call, var_keyword: str | None) -> bool:
    """Does ``call`` pass on the caller's own ``**kwargs``?"""
    return var_keyword is not None and any(
        k.arg is None and _is_name(k.value, var_keyword) for k in call.keywords
    )


def _dotted(node: ast.AST | None) -> str | None:
    """``a.b.c`` for a name or a chain of attributes of one, else None (``self.x()``'s
    ``self.x`` is ``"self.x"``, which resolves to nothing)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _name_in(node: ast.AST | None, names: set[str]) -> str | None:
    """The name ``node`` is when it is one of ``names``, else None."""
    return node.id if isinstance(node, ast.Name) and node.id in names else None


def _member(cls: Any, name: str) -> Any:
    """The member ``name`` of a loaded class, inherited ones included (aliases followed), or
    None."""
    try:
        member = cls.members.get(name)
        if member is None:
            member = cls.all_members.get(name)
        if getattr(member, "is_alias", False):
            member = member.final_target
        return member
    except Exception:
        return None


def _labels(obj: Any) -> set[str]:
    """griffe's labels of an object (``staticmethod``, ``classmethod``, ``property``)."""
    try:
        return {str(label) for label in obj.labels}
    except Exception:
        return set()


def _sentinel_guard(conditions: list[_Condition], names: set[str]) -> tuple[str, str] | None:
    """For a helper's warning under ``conditions``: the helper's parameter it warns of and
    the sentinel, as code, when the one condition is that the parameter is not the sentinel
    (``if copy is not lib.no_default:``, or the ``else`` of ``if copy is lib.no_default:``);
    else None."""
    if len(conditions) != 1 or conditions[0][0] is None:
        return None
    test, branch = conditions[0]
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, branch = test.operand, not branch
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    op = test.ops[0]
    if not isinstance(op, (ast.IsNot, ast.NotEq, ast.Is, ast.Eq)):
        return None
    for a, b in ((test.left, test.comparators[0]), (test.comparators[0], test.left)):
        if (
            isinstance(a, ast.Name)
            and a.id in names
            and isinstance(op, (ast.IsNot, ast.NotEq)) == branch
        ):
            return a.id, ast.unparse(b)
    return None


def _kwargs_guard(conditions: list[_Condition], var_keyword: str | None) -> str | None:
    """For a helper's warning under ``conditions``: the key of its ``**kwargs`` it warns of,
    when the one condition is that the key is there (``if "all" in kwargs:``); else None."""
    if len(conditions) != 1 or conditions[0][0] is None or var_keyword is None:
        return None
    test, branch = conditions[0]
    found = _kwargs_key(test, branch, var_keyword) if test is not None else None
    return found[0] if found is not None and found[1] else None


def _kwargs_key(test: ast.expr, branch: bool, var_keyword: str | None) -> tuple[str, bool] | None:
    """For ``"p" in kwargs`` (and ``not in``) under ``branch``: the key, and whether the test
    holds when the key is there; else None."""
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, branch = test.operand, not branch
    if (
        var_keyword
        and isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], (ast.In, ast.NotIn))
        and _is_name(test.comparators[0], var_keyword)
        and isinstance(test.left, ast.Constant)
        and isinstance(test.left.value, str)
        and test.left.value.isidentifier()
    ):
        return test.left.value, isinstance(test.ops[0], ast.In) == branch
    return None


def _deprecated_whole(obj: Any) -> bool:
    """Whether ``obj`` is deprecated as a whole by PEP 702 or by the library's decorator."""
    try:
        return pep702_message(obj) is not None or any(
            m[0] is None for m in decorator_deprecations(obj)
        )
    except Exception:
        return False


def _comparable(text: str) -> str:
    """A warning's text as two releases are compared: without versions ("will be removed in
    3.0" in one, "in 4.0" in the other), case, punctuation or spacing; other numbers stay
    ("axis=0" is not "axis=2")."""
    return " ".join(re.sub(r"\W+", " ", _VERSION_TOKEN.sub(" ", text.lower())).split())


def _behavior(message: str, category: str) -> bool:
    """Does a warning's message say that a behaviour or a default changes, not that the API
    goes away (:data:`_BEHAVIOR`)?"""
    if not message:
        return False
    deprecates = _DEPRECATES.search(message)
    if not _BEHAVIOR.search(message):
        return category == "FutureWarning" and deprecates is None
    if deprecates is None:
        return True
    # It says something is deprecated: a behaviour or a default when that is what it says
    # is ("The behavior of X is deprecated"; not "'n_alphas' was deprecated ..., the
    # default value of 'alphas' will change").
    start = message.rfind(". ", 0, deprecates.start()) + 1
    return _BEHAVIOR_WORD.search(message, start, deprecates.start()) is not None


def _named_parameters(message: str, names: Iterable[str]) -> set[str]:
    """The parameters among ``names`` that a message names as what it is about: in quotes or
    backticks (``'copy'``, `` `p=...` ``), as ``p=``, or with a word that says so ("the copy
    keyword", "argument p", "passing p"); not one it names as the replacement ("use `name`",
    "`name` instead")."""
    found: set[str] = set()
    if not message:
        return found
    for p in names:
        e = re.escape(p)
        mention = re.compile(
            rf"[`'\"]{e}(?:=[^`'\"\s]*)?[`'\"]|(?<![\w.]){e}="
            rf"|(?<![\w.`'\"]){e}[`'\"]?\s+{_PARAMETER_NOUNS}\b"
            rf"|\b{_PARAMETER_NOUNS}\s+[`'\"]?{e}(?!\w)|\bpassing\s+[`'\"]?{e}(?!\w)",
            re.IGNORECASE,
        )
        for m in mention.finditer(message):
            before = message[max(0, m.start() - 24) : m.start()]
            after = message[m.end() : m.end() + 12]
            if re.search(
                r"(?:\buse|\bin favou?r of|\brenamed to|\breplaced (?:by|with)|\bswitch to"
                r"|\bpass)\s*$",
                before,
                re.IGNORECASE,
            ) or re.match(r"\s+instead\b", after, re.IGNORECASE):
                continue  # the replacement, not what is deprecated
            found.add(p)
            break
    return found


def _calls_under(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[tuple[ast.Call, list[_Condition], bool]]:
    """Each call in the body of ``fn`` (not in a function or class it defines), with the
    conditions it is under, outermost first, and whether a ``return`` at the top level of the
    body comes before it (then it is issued on some paths only). An ``if <test>: ...;
    return`` at the top level puts what follows under ``not <test>``. In an expression, the
    right of ``and``/``or`` is under the left, and ``x if c else y`` under ``c``. A ``raise``
    before a call does not count: the call that goes on is the one that warns."""
    returned = False
    guards: list[_Condition] = []  # the early returns' tests, for what follows them

    def calls(
        node: ast.AST, conditions: list[_Condition]
    ) -> Iterator[tuple[ast.Call, list[_Condition]]]:
        stack: list[tuple[ast.AST, list[_Condition]]] = [(node, conditions)]
        while stack:
            current, under = stack.pop()
            if isinstance(current, (*_FUNCTIONS, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(current, ast.Call):
                yield current, under
            children: list[tuple[ast.AST, list[_Condition]]]
            if isinstance(current, ast.BoolOp):
                children, before = [], cast(list[_Condition], [])
                for value in current.values:
                    children.append((value, [*under, *before]))
                    before.append((value, isinstance(current.op, ast.And)))
            elif isinstance(current, ast.IfExp):
                children = [
                    (current.test, under),
                    (current.body, [*under, (current.test, True)]),
                    (current.orelse, [*under, (current.test, False)]),
                ]
            elif isinstance(current, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                children = [(c, [*under, (None, True)]) for c in ast.iter_child_nodes(current)]
            else:
                children = [(c, under) for c in ast.iter_child_nodes(current)]
            stack.extend(reversed(children))

    def walk(
        stmts: list[ast.stmt], conditions: list[_Condition], top: bool
    ) -> Iterator[tuple[ast.Call, list[_Condition], bool]]:
        nonlocal returned
        for stmt in stmts:
            here = [*conditions, *guards] if top else conditions
            if isinstance(stmt, (*_FUNCTIONS, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.If):
                yield from walk(stmt.body, [*here, (stmt.test, True)], False)
                yield from walk(stmt.orelse, [*here, (stmt.test, False)], False)
                ends = _returns(stmt.body), bool(stmt.orelse) and _returns(stmt.orelse)
                if top and ends[0] != ends[1]:
                    guards.append(_Guard((stmt.test, ends[1])))  # what follows runs in the other
                    continue
            elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                yield from walk([*stmt.body, *stmt.orelse], [*here, (None, True)], False)
            elif _is_try(stmt):
                block: Any = stmt  # a try, or a try with except* (3.11)
                yield from walk(block.body, here if not top else conditions, top)
                for handler in block.handlers:
                    yield from walk(handler.body, [*here, (None, True)], False)
                yield from walk([*block.orelse, *block.finalbody], conditions, top)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                yield from walk(stmt.body, conditions, top)
            elif isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    yield from walk(case.body, [*here, (None, True)], False)
            else:
                for call, under in calls(stmt, here):
                    yield call, under, returned
            if top and any(isinstance(n, ast.Return) for n in calls_and_returns(stmt)):
                returned = True

    def calls_and_returns(node: ast.AST) -> Iterator[ast.AST]:
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, (*_FUNCTIONS, ast.ClassDef, ast.Lambda)):
                continue
            yield current
            stack.extend(ast.iter_child_nodes(current))

    yield from walk(fn.body, [], True)


def _returns(body: list[ast.stmt]) -> bool:
    """Does a block return (a ``return`` among its own statements)?"""
    return any(isinstance(s, ast.Return) for s in body)


def _signature_defaults(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[dict[str, str | None], str | None]:
    """The parameters of ``fn`` (``self`` and ``cls`` left out), each with its default as
    code (None without one), and the name of its ``**kwargs``."""
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    defaults: dict[str, str | None] = {a.arg: None for a in positional}
    for a, d in zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True):
        defaults[a.arg] = ast.unparse(d)
    for a, kd in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        defaults[a.arg] = ast.unparse(kd) if kd is not None else None
    for name in ("self", "cls"):
        defaults.pop(name, None)
    return defaults, args.kwarg.arg if args.kwarg else None


# ------------------------------------------- when a warning is issued (_classify)
@dataclass(frozen=True)
class _Lit:
    """A value :func:`_evaluate` knows: a literal."""

    value: Any


@dataclass(frozen=True)
class _Sentinel:
    """A default that is no literal (``lib.no_default``): equal to itself only."""

    code: str


@dataclass
class _Fact:
    """What one test a warning is under says (:func:`_fact`): "passed" (it holds only when
    the caller passes ``read``; ``exact`` when whenever it is passed), "value" (only for some
    values passed), "default" (only when ``read`` is left at its default), "kwargs" (``if
    kwargs:``) or "other"; and the test as it holds (``atom``)."""

    kind: str
    read: _Read | None
    atom: ast.expr | None
    exact: bool = False
    equals: Any = None  # a value fact ``p == "epoch"``: the value
    types: tuple[type, ...] | None = None  # a value fact ``isinstance(p, str)``: the types
    guard: bool = False  # from an ``if <test>: return`` before the warning (_Guard)


def _classify(
    conditions: list[_Condition],
    returned: bool,
    r: _Reading,
    message: str,
    passes: _Passes | None,
    behavior: bool,
) -> tuple[str, str | None, str | None, bool, bool, bool, bool] | None:
    """When a warning under ``conditions`` is issued: ``(when, parameter, condition,
    from_kwargs, conditional, readable, on_init)``; None for one the caller asks for itself
    (a flag named so, ``deprecated=False``, set: opt-in).

    Every test counts, the outer ones too, each conjunct of an ``and`` on its own. WARNS_ALWAYS
    under none (and no ``return`` before it). WARNS_PARAMETER when one holds only when the
    caller passes a parameter (:func:`_fact`: ``if p is not <default>:``, ``"p" in kwargs``,
    ``p = kwargs.pop("p", None)`` then ``if p is not None:``, ``self.p != "deprecated"`` for a
    constructor's), or only for some of its values (``errors != "raise"``, ``if axis:``:
    conditional, the test kept); WARNS_DEFAULT when one holds only while the caller leaves it
    out (``if n_init == "warn":``, the default). When several parameters qualify, the one the
    message names (:func:`_named_parameters`) is it, or none (WARNS_CONDITION); so is one the
    message does not name when it names another. For a default that a message about behaviour
    names, that one first. The other tests are the condition (those the parameter's own value
    makes true left out: ``date_format == "epoch"`` implies ``not (date_format is None and
    ...)``, ``isinstance(verify, str)`` implies ``verify is not False``); a ``return`` on
    another path does not make a parameter's warning conditional. Else WARNS_CONDITION, with
    all the tests."""
    facts: list[_Fact] = []
    marker = False
    for condition in conditions:
        test, branch = condition
        if test is None:
            marker = True
            continue
        for t, b in _atoms(test, branch):
            fact = _fact(t, b, r)
            fact.guard = isinstance(condition, _Guard)
            facts.append(fact)
    if passes is not None:
        facts.append(_Fact("passed", _Read(passes.name, passes.from_kwargs, None), None, True))
    for f in facts:
        if (
            f.kind == "value"
            and f.read is not None
            and f.read.default in ("False", "0")
            and _HELPER_NAME.search(f.read.name)
            and not _mentions_word(message, f.read.name)
        ):
            return None
    required = [f for f in facts if f.kind in ("passed", "value") and f.read is not None]
    defaulted = [f for f in facts if f.kind == "default" and f.read is not None]
    named = _named_parameters(
        message, {f.read.name for f in [*required, *defaulted] if f.read} | set(r.defaults)
    )

    def pick(group: list[_Fact]) -> _Read | None:
        reads = list(dict.fromkeys(f.read for f in group if f.read is not None))
        keys = list(dict.fromkeys((rd.name, rd.on_init) for rd in reads))
        if len(keys) == 1:
            chosen = keys[0]
            if named and chosen[0] not in named:
                return None  # the message is about another parameter
        else:
            hits = [k for k in keys if k[0] in named]
            if len(hits) != 1:
                return None
            chosen = hits[0]
        return next(rd for rd in reads if (rd.name, rd.on_init) == chosen)

    chosen: _Read | None = None
    when = WARNS_CONDITION
    if behavior and defaulted and any(f.read and f.read.name in named for f in defaulted):
        chosen = pick(defaulted)
        when = WARNS_DEFAULT
    if chosen is None and required:
        chosen = pick(required)
        when = WARNS_PARAMETER if chosen is not None else WARNS_CONDITION
    elif chosen is None and defaulted:
        chosen = pick(defaulted)
        when = WARNS_DEFAULT if chosen is not None else WARNS_CONDITION
    if chosen is not None:
        key = (chosen.name, chosen.on_init)
        mine = [f for f in facts if f.read is not None and (f.read.name, f.read.on_init) == key]
        own = {id(f) for f in mine}
        exact_kind = "passed" if when == WARNS_PARAMETER else "default"
        rest = [
            f
            for f in facts
            if not (id(f) in own and f.kind == exact_kind and f.exact)
            and not (f.kind == "kwargs" and chosen.from_kwargs)
            and f.atom is not None
        ]
        value = next((f.equals for f in mine if f.equals is not None), None)
        if when == WARNS_DEFAULT and chosen.default is not None:
            value = _default_value(chosen.default)
        if value is not None:  # what the parameter's own value makes true says nothing more
            rest = [
                f
                for f in rest
                if id(f) in own or f.atom is None or not _implied(f.atom, chosen, value, r)
            ]
        # A return before the warning on another path (a fast path that does not read the
        # parameter) does not make it conditional, as a return in a block does not.
        rest = [f for f in rest if id(f) in own or not f.guard]
        types = next((f.types for f in mine if f.types is not None), None)
        if types is not None:  # isinstance(verify, str) implies verify is not False
            rest = [f for f in rest if id(f) in own or not _type_implied(f.atom, chosen, types, r)]
        # An early return on another path does not make it conditional: it is about the
        # parameter (httpx's create_ssl_context returns for a string verify before cert).
        conditional = bool(rest) or marker
        text, readable = (None, False)
        if rest and not marker:
            text, readable = _condition_text([f.atom for f in rest if f.atom is not None], r)
        from_kwargs = chosen.from_kwargs
        return when, chosen.name, text, from_kwargs, conditional, readable, chosen.on_init
    if not conditions and not returned and passes is None:
        return WARNS_ALWAYS, None, None, False, False, False, False
    atoms = [f.atom for f in facts if f.atom is not None]
    text, readable = (None, False)
    if atoms and not marker:
        text, readable = _condition_text(atoms, r)
    return WARNS_CONDITION, None, text, False, True, readable, False


def _mentions_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(word)}(?!\w)", text or "") is not None


def _atoms(test: ast.expr, branch: bool) -> list[tuple[ast.expr, bool]]:
    """``test`` under ``branch`` as tests that all hold: the conjuncts of an ``and`` that
    holds, of an ``or`` that does not, through ``not``."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _atoms(test.operand, not branch)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And if branch else ast.Or):
        return [a for v in test.values for a in _atoms(v, branch)]
    return [(test, branch)]


_NEGATED_OPS: dict[type, type] = {
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


def _negated(test: ast.expr) -> ast.expr:
    """``not test``, as code would say it: ``x is not None`` for ``x is None``."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return test.operand
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in _NEGATED_OPS:
        return ast.Compare(test.left, [_NEGATED_OPS[type(test.ops[0])]()], test.comparators)
    return ast.UnaryOp(ast.Not(), test)


def _fact(test: ast.expr, branch: bool, r: _Reading) -> _Fact:
    """What one test (an atom of :func:`_atoms`) under ``branch`` says of the parameters
    (:class:`_Fact`), worked out by evaluating it with its one parameter at its default."""
    atom = test if branch else _negated(test)
    if r.var_keyword and _is_name(test, r.var_keyword):
        return _Fact("kwargs", None, atom)
    key = _kwargs_key(test, branch, r.var_keyword)
    if key is not None:
        read = _Read(key[0], True, None)
        return _Fact("passed" if key[1] else "default", read, atom, True)
    reads = _reads_of(test, r)
    if not reads or len(set(reads)) != 1 or reads[0].default is None:
        return _Fact("other", None, atom)
    read = reads[0]
    value = _default_value(read.default or "None")
    at_default = _truth(_evaluate(test, read, value, r))
    if at_default is None:
        return _Fact("other", None, atom)
    if at_default != branch:  # does not hold at the default: the caller passes it
        exact = _exact_passed(test, branch, read, r)
        equals = _equality(test, branch, read, r)
        types = _instance_of(test, branch, read, r)
        return _Fact("passed" if exact else "value", read, atom, exact, equals, types)
    if _exact_default(test, branch, read, r):
        return _Fact("default", read, atom, True)
    return _Fact("other", None, atom)


def _parameter_read(node: ast.AST, r: _Reading, depth: int = 0) -> _Read | None:
    """The parameter ``node`` reads: a parameter by its name, a local that holds one there
    (:func:`_alias`: ``p = kwargs.pop("p", None)``, ``q = self.q``), ``kwargs.get("p")`` /
    ``kwargs.pop("p", d)`` (whose default is None, or the one given), or in a method
    ``self.p`` for a parameter of the class's constructor; None for anything else."""
    if isinstance(node, ast.Name):
        if node.id in r.defaults:
            return _Read(node.id, False, r.defaults[node.id])
        if depth < 3 and node.id not in r.names:
            alias = _alias(r, node.id, getattr(node, "lineno", None))
            return _parameter_read(alias, r, depth + 1) if alias is not None else None
        return None
    if isinstance(node, ast.Attribute) and r.me is not None and _is_name(node.value, r.me):
        alias = _alias(r, f"{r.me}.{node.attr}", getattr(node, "lineno", None))
        if alias is not None and depth < 3:  # set from a parameter in this very method
            return _parameter_read(alias, r, depth + 1)
        if node.attr in r.init:
            return _Read(node.attr, False, r.init[node.attr], True)
        return None
    if (
        r.var_keyword
        and isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("get", "pop")
        and _is_name(node.func.value, r.var_keyword)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.isidentifier()
    ):
        default = ast.unparse(node.args[1]) if len(node.args) > 1 else "None"
        return _Read(node.args[0].value, True, default)
    return None


def _reads_of(test: ast.expr, r: _Reading) -> list[_Read] | None:
    """The one parameter ``test`` reads (:func:`_parameter_read`), as often as it reads it;
    None when it reads anything else but constants, builtins and that parameter's default
    (``lib.no_default``): another parameter, a local, an attribute of another object."""
    reads: list[_Read] = []
    foreign: list[str] = []
    stack: list[ast.AST] = [test]
    while stack:
        node = stack.pop()
        read = (
            _parameter_read(node, r)
            if isinstance(node, (ast.Name, ast.Attribute, ast.Call))
            else None
        )
        if read is not None:
            reads.append(read)
            continue
        if isinstance(node, ast.Name) and node.id in _BUILTIN_NAMES:
            continue
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            dotted = _dotted(node)
            head = dotted.split(".")[0] if dotted else None
            if dotted is not None and head not in r.defaults and head not in r.aliases:
                foreign.append(dotted)  # a module's name, or another object's: unless a default
                continue
        stack.extend(ast.iter_child_nodes(node))
    if not reads or len(set(reads)) != 1:
        return None
    default = reads[0].default or ""
    if any(not _same_code(f, default) for f in foreign):
        return None
    return reads


def _default_value(code: str) -> _Lit | _Sentinel:
    """A default (as code) as :func:`_evaluate` takes it."""
    try:
        return _Lit(ast.literal_eval(code))
    except Exception:
        return _Sentinel(code)


def _evaluate(node: ast.AST, read: _Read, value: Any, r: _Reading) -> Any:
    """``node`` with the parameter ``read`` being ``value``: a :class:`_Lit`, the
    :class:`_Sentinel`, or None when it cannot tell. The test reads nothing else (_reads_of)."""
    if _parameter_read(node, r) == read:
        return value
    if isinstance(value, _Sentinel) and _same_code(ast.unparse(node), value.code):
        return value
    if isinstance(node, ast.Constant):
        return _Lit(node.value)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = [_evaluate(e, read, value, r) for e in node.elts]
        if all(isinstance(i, _Lit) for i in items):
            return _Lit(tuple(i.value for i in items))
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        truth = _truth(_evaluate(node.operand, read, value, r))
        return None if truth is None else _Lit(not truth)
    if isinstance(node, ast.BoolOp):
        truths = [_truth(_evaluate(v, read, value, r)) for v in node.values]
        stop = isinstance(node.op, ast.Or)  # True ends an or, False an and
        if stop in truths:
            return _Lit(stop)
        return _Lit(not stop) if all(t is (not stop) for t in truths) else None
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left = _evaluate(node.left, read, value, r)
        right = _evaluate(node.comparators[0], read, value, r)
        return _compare(node.ops[0], left, right)
    if isinstance(node, ast.Call) and _is_name(node.func, "isinstance") and len(node.args) == 2:
        item = _evaluate(node.args[0], read, value, r)
        types = _builtin_types(node.args[1])
        if item is None or types is None:
            return None
        if isinstance(item, _Sentinel):
            return _Lit(object in types)
        return _Lit(isinstance(item.value, types))
    return None


def _same_code(a: str, b: str) -> bool:
    """Do two sentinels' code name one object (``lib.no_default``, ``no_default``)?"""
    if a == b:
        return True
    dotted = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
    if not (dotted.fullmatch(a) and dotted.fullmatch(b)):
        return False
    return a.rsplit(".", 1)[-1] == b.rsplit(".", 1)[-1]


def _compare(op: ast.cmpop, left: Any, right: Any) -> Any:
    if left is None or right is None:
        return None
    if isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
        if isinstance(left, _Sentinel) and isinstance(right, _Sentinel):
            same = _same_code(left.code, right.code)
        elif isinstance(left, _Sentinel) or isinstance(right, _Sentinel):
            same = False
        else:
            try:
                same = bool(left.value == right.value)
            except Exception:
                return None
        return _Lit(same if isinstance(op, (ast.Is, ast.Eq)) else not same)
    if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, _Lit):
        if isinstance(left, _Sentinel):
            inside = False
        else:
            try:
                inside = left.value in right.value
            except Exception:
                return None
        return _Lit(inside if isinstance(op, ast.In) else not inside)
    if isinstance(left, _Lit) and isinstance(right, _Lit):
        try:
            if isinstance(op, ast.Lt):
                return _Lit(left.value < right.value)
            if isinstance(op, ast.LtE):
                return _Lit(left.value <= right.value)
            if isinstance(op, ast.Gt):
                return _Lit(left.value > right.value)
            if isinstance(op, ast.GtE):
                return _Lit(left.value >= right.value)
        except Exception:
            return None
    return None


def _truth(value: Any) -> bool | None:
    if isinstance(value, _Lit):
        try:
            return bool(value.value)
        except Exception:
            return None
    return None


def _builtin_types(node: ast.expr) -> tuple[type, ...] | None:
    """The builtin types an ``isinstance`` test names, or None for any other."""
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    found = [_TYPES.get(n.id) if isinstance(n, ast.Name) else None for n in names]
    return tuple(t for t in found if t is not None) if all(found) else None


def _is_sentinel(code: str | None) -> bool:
    """A default that stands for "not passed": None, no literal (``lib.no_default``), or a
    string that says so (``"deprecated"``, ``"warn"``)."""
    if code is None:
        return False
    value = _default_value(code)
    if isinstance(value, _Sentinel) or value.value is None:
        return True
    return isinstance(value.value, str) and _SENTINEL_STRING.fullmatch(value.value) is not None


def _compared(test: ast.expr, read: _Read, r: _Reading) -> tuple[ast.cmpop, ast.expr] | None:
    """For ``p <op> x`` (or ``x <op> p``) on the parameter ``read``: the operator and ``x``."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    left, right = test.left, test.comparators[0]
    if _parameter_read(left, r) == read:
        return test.ops[0], right
    if _parameter_read(right, r) == read:
        return test.ops[0], left
    return None


def _is_default(node: ast.expr, read: _Read) -> bool:
    code = ast.unparse(node)
    if read.default is None:
        return False
    if _same_code(code, read.default):
        return True
    try:
        return bool(ast.literal_eval(code) == ast.literal_eval(read.default))
    except Exception:
        return False


def _exact_passed(test: ast.expr, branch: bool, read: _Read, r: _Reading) -> bool:
    """Does ``test`` under ``branch`` hold whenever the caller passes ``read``: ``p is not
    <default>``, ``p != <sentinel>``, ``kwargs.get("p") is not None``, ``if p:`` for a
    None default, or ``not (isinstance(p, str) and p == "deprecated")``?"""
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, branch = test.operand, not branch
    compared = _compared(test, read, r)
    if compared is not None:
        op, other = compared
        if not _is_default(other, read):
            return False
        if isinstance(op, ast.IsNot) == branch and isinstance(op, (ast.Is, ast.IsNot)):
            return True
        return (
            isinstance(op, ast.NotEq) == branch
            and isinstance(op, (ast.Eq, ast.NotEq))
            and _is_sentinel(read.default)
        )
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) and not branch:
        return any(_exact_passed(v, False, read, r) for v in test.values)
    return branch and _parameter_read(test, r) == read and read.default == "None"


def _exact_default(test: ast.expr, branch: bool, read: _Read, r: _Reading) -> bool:
    """Does ``test`` under ``branch`` hold whenever the caller leaves ``read`` at its default
    and only then: ``p is <default>``, ``p == <sentinel>``, ``if not p:`` for a None default?"""
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, branch = test.operand, not branch
    compared = _compared(test, read, r)
    if compared is not None:
        op, other = compared
        if not _is_default(other, read):
            return False
        if isinstance(op, ast.Is) == branch and isinstance(op, (ast.Is, ast.IsNot)):
            return True
        return (
            isinstance(op, ast.Eq) == branch
            and isinstance(op, (ast.Eq, ast.NotEq))
            and _is_sentinel(read.default)
        )
    return not branch and _parameter_read(test, r) == read and read.default == "None"


def _equality(test: ast.expr, branch: bool, read: _Read, r: _Reading) -> _Lit | None:
    """For ``p == <literal>`` (or ``is``) that holds: the literal."""
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, branch = test.operand, not branch
    compared = _compared(test, read, r)
    if compared is None or not isinstance(compared[0], (ast.Eq, ast.Is)) or not branch:
        return None
    try:
        return _Lit(ast.literal_eval(compared[1]))
    except Exception:
        return None


def _instance_of(test: ast.expr, branch: bool, read: _Read, r: _Reading) -> tuple[type, ...] | None:
    """For ``isinstance(p, T)`` that holds, on the parameter ``read``: the builtin types."""
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, branch = test.operand, not branch
    if (
        branch
        and isinstance(test, ast.Call)
        and _is_name(test.func, "isinstance")
        and len(test.args) == 2
        and _parameter_read(test.args[0], r) == read
    ):
        return _builtin_types(test.args[1])
    return None


def _type_implied(atom: ast.expr | None, read: _Read, types: tuple[type, ...], r: _Reading) -> bool:
    """Does ``atom`` hold whenever the parameter ``read`` is one of ``types``: ``p is not
    False`` (or ``p != None``) for a constant that is none of them?"""
    if atom is None:
        return False
    compared = _compared(atom, read, r)
    if compared is None or not isinstance(compared[0], (ast.IsNot, ast.NotEq)):
        return False
    other = compared[1]
    return isinstance(other, ast.Constant) and not isinstance(other.value, types)


def _implied(atom: ast.expr, read: _Read, value: Any, r: _Reading) -> bool:
    """Does ``atom`` hold whenever the parameter ``read`` is ``value`` (``not (date_format is
    None and ...)`` when ``date_format == "epoch"``)? Unknown parts are unknown."""
    return _truth(_partial(atom, read, value, r)) is True


def _partial(node: ast.AST, read: _Read, value: Any, r: _Reading) -> Any:
    """:func:`_evaluate` for a test that may read other things too (unknown, None)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        truth = _truth(_partial(node.operand, read, value, r))
        return None if truth is None else _Lit(not truth)
    if isinstance(node, ast.BoolOp):
        truths = [_truth(_partial(v, read, value, r)) for v in node.values]
        stop = isinstance(node.op, ast.Or)
        if stop in truths:
            return _Lit(stop)
        return _Lit(not stop) if all(t is (not stop) for t in truths) else None
    reads = _reads_of(node, r) if isinstance(node, ast.expr) else None
    if reads and set(reads) == {read}:
        return _evaluate(node, read, value, r)
    return None


def _condition_text(atoms: list[ast.expr], r: _Reading) -> tuple[str | None, bool]:
    """The tests a warning is under, joined with ``and``, as code (None when longer than
    _CONDITION_LIMIT), and whether they read nothing but the function's parameters (and
    builtins)."""
    if not atoms:
        return None, False
    expr = atoms[0] if len(atoms) == 1 else ast.BoolOp(ast.And(), list(atoms))
    try:
        code = " ".join(ast.unparse(expr).split())
    except Exception:
        return None, False
    if len(code) > _CONDITION_LIMIT:
        return None, False
    allowed = set(r.defaults) | ({r.var_keyword} if r.var_keyword else set()) | _BUILTIN_NAMES
    readable = all(n.id in allowed for n in ast.walk(expr) if isinstance(n, ast.Name))
    return code, readable


# ------------------------------------------- what the release at the cutoff warned of
@dataclass
class _Candidate:
    """A warning of the pinned release that :meth:`_Differ._warned` reports: under ``path``
    (the public path of ``target``, the object the change is to), with the release at the
    cutoff's ``old_target``, and ``fn``, the function whose parameters it is about."""

    path: str
    target: Any
    old_target: Any
    fn: Any
    warned: _Warned


@dataclass
class _ServedName:
    """A name a module's ``__getattr__`` serves (:meth:`_Differ._getattr_warned`), as a change
    is made of it: an attribute of the module, which no static reading sees."""

    name: str
    parent: Any
    is_attribute: bool = True
    annotation: Any = None
    docstring: Any = None

    @property
    def path(self) -> str:
        return f"{self.parent.path}.{self.name}"

    @property
    def module(self) -> Any:
        return self.parent


@dataclass
class _Issuance:
    """What the release at the cutoff warned of in a function (:meth:`_Differ._warned`): the
    keys of its warnings (of a deprecation category, or of another whose text says it
    deprecates something: scikit-learn 1.6's UserWarning for ``cv='prefit'``), their texts by
    parameter (any category), the parameters its decorators deprecate, and the texts every
    warning of its module gives."""

    keys: set[tuple[Any, ...]]
    texts: dict[str | None, list[str]]
    decorated: set[str]
    module: set[str]

    @classmethod
    def of(cls, before: list[_Warned], old_fn: Any, reader: _WarnReader) -> _Issuance:
        keys = {w.key for w in before if w.deprecation or _DEPRECATION_TEXT.search(w.message)}
        texts: dict[str | None, list[str]] = {}
        for w in before:
            if comparable := _comparable(w.message):
                texts.setdefault(w.parameter, []).append(comparable)
        decorated: set[str] = set()
        module: set[str] = set()
        if old_fn is not None:
            try:
                decorated = {p for p, _text, _by in decorator_deprecations(old_fn) if p}
            except Exception:
                decorated = set()
            try:
                module = reader.module_texts(old_fn.module)
            except Exception:
                module = set()
        return cls(keys, texts, decorated, module)

    def covers(self, w: _Warned, name: str) -> bool:
        """Did the release at the cutoff issue ``w`` (a warning of the function ``name``)
        already: one with its key; for a parameter one of its decorators deprecates; one
        about the same parameter (or none) with its text once versions are left out, or whose
        text it goes on from (statsmodels 0.15's "Argument ... not used by MixedLM.fit. Future
        versions will raise ..."); or the same text, which names the parameter (or the
        function) and holds no value, anywhere in the function's module (a helper of another
        name, a function it delegates to)?"""
        if w.key in self.keys:
            return True
        if w.parameter is not None and w.parameter in self.decorated:
            return True
        mine = _comparable(w.message)
        if not mine:
            return False
        for text in self.texts.get(w.parameter, ()):
            grown = len(text) >= 20 and text.count(" ") >= 3 and f" {text} " in f" {mine} "
            if text == mine or grown:
                return True
        subject = w.parameter or name
        return (
            _VALUE not in w.message
            and mine in self.module
            and bool(subject)
            and _mentions_word(w.message, subject)
        )


def _as_access(w: _Warned, access: str) -> _Warned:
    """A warning of a property's getter or setter (or a module's ``__getattr__``) as one
    issued when the name is read or set (WARNS_ACCESS)."""
    if w.when == WARNS_ALWAYS:
        return replace(w, when=WARNS_ACCESS, access=access)
    keep = w.parameter is None
    return replace(
        w,
        when=WARNS_ACCESS,
        access=access,
        parameter=None,
        from_kwargs=False,
        on_init=False,
        conditional=True,
        condition=w.condition if keep else None,
        readable=w.readable and keep,
    )


def _warned_order(w: _Warned) -> tuple[int, bool]:
    """The order the warnings of one change are told in: at every call (or access) first,
    then a parameter, a default, a condition; each unconditional before conditional."""
    rank = {WARNS_ALWAYS: 0, WARNS_ACCESS: 0, WARNS_PARAMETER: 1, WARNS_DEFAULT: 2}
    return rank.get(w.when, 3), w.conditional


def _dict_tables(tree: ast.Module | None) -> dict[str, set[str]]:
    """A module's dicts of string keys, by name (``_DEPRECATED = {"Old": "New"}``)."""
    tables: dict[str, set[str]] = {}
    for stmt in tree.body if tree is not None else ():
        target, value = None, None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target, value = stmt.target, stmt.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Dict):
            tables[target.id] = {
                k.value
                for k in value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    return tables


def _selected_names(
    test: ast.expr, branch: bool, param: str, tables: dict[str, set[str]]
) -> set[str] | None:
    """The names a ``__getattr__``'s test picks when it holds: ``name == "Old"``, ``name in
    ("A", "B")``, ``name in _DEPRECATED`` (a table of the module), or several joined with
    ``or``; None for any other test."""
    if not branch:
        return None
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        parts = [_selected_names(v, True, param, tables) for v in test.values]
        if any(p is None for p in parts):
            return None
        return set().union(*(p for p in parts if p is not None))
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    op, left, right = test.ops[0], test.left, test.comparators[0]
    if isinstance(op, ast.Eq):
        for a, b in ((left, right), (right, left)):
            if _is_name(a, param) and isinstance(b, ast.Constant) and isinstance(b.value, str):
                return {b.value}
        return None
    if isinstance(op, ast.In) and _is_name(left, param):
        if isinstance(right, (ast.Tuple, ast.List, ast.Set)):
            found = {e.value for e in right.elts if isinstance(e, ast.Constant)}
            return {f for f in found if isinstance(f, str)} or None
        if isinstance(right, ast.Name) and right.id in tables:
            return set(tables[right.id])
    return None


def _getattr_names(hook: Any, tree: ast.Module | None) -> set[str]:
    """The names a module's ``__getattr__`` serves: those its tests pick
    (:func:`_selected_names`), and the keys of the tables it looks the name up in."""
    node = _function_def(hook)
    args = [a.arg for a in (*node.args.posonlyargs, *node.args.args)] if node else []
    if node is None or not args:
        return set()
    param, tables = args[0], _dict_tables(tree)
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.If):
            names |= _selected_names(n.test, True, param, tables) or set()
        elif (
            isinstance(n, ast.Subscript)
            and _is_name(n.slice, param)
            and isinstance(n.value, ast.Name)
            and n.value.id in tables
        ):
            names |= tables[n.value.id]
    return names


def _getattr_warnings(
    reader: _WarnReader, hook: Any, tree: ast.Module | None, *, every: bool = False
) -> dict[str, list[_Warned]]:
    """The deprecation warnings a module's ``__getattr__`` issues, by the name it issues them
    for (the tests that pick it: :func:`_selected_names`); WARNS_ALWAYS when nothing else
    decides, else WARNS_CONDITION. ``every``: of any category."""
    node = _function_def(hook)
    args = [a.arg for a in (*node.args.posonlyargs, *node.args.args)] if node else []
    if node is None or not args:
        return {}
    param, tables = args[0], _dict_tables(tree)
    r = reader.reading(hook, node)
    file = _file_of(hook)
    found: dict[str, list[_Warned]] = {}
    for call, conditions, exited in _calls_under(node):
        names: set[str] | None = None
        others = 0
        for test, branch in conditions:
            picked = _selected_names(test, branch, param, tables) if test is not None else None
            if picked is None:
                others += 1
            else:
                names = picked if names is None else names & picked
        if not names:
            continue
        got = reader.warning_of(call, r, every=every)
        if got is None or not (got.deprecation or every):
            continue
        line = int(getattr(hook, "lineno", 1) or 1) + call.lineno - 1
        conditional = bool(others) or exited
        for name in sorted(names):
            message = got.message
            if got.message_node is not None:
                filled = reader._render(got.message_node, r, call.lineno, {param: name}, 0)
                message = " ".join((filled or message).split())
            found.setdefault(name, []).append(
                _Warned(
                    WARNS_CONDITION if conditional else WARNS_ALWAYS,
                    got.category or "UserWarning",
                    message,
                    f"{file}:{line}" if file else None,
                    conditional=conditional,
                    behavior=_behavior(message, got.category),
                    deprecation=got.deprecation,
                )
            )
    return found


def pep702_message(obj: Any) -> str | None:
    """Return the deprecation message ('' if none) when decorated with a PEP 702 decorator.

    Looks at the object itself and at its ``@overload`` signatures, and follows compatibility
    shims (``from pkg._compat import deprecated``) that re-export the standard decorator.
    """
    for candidate in [obj, *(getattr(obj, "overloads", None) or [])]:
        dec = _pep702_decorator(candidate)
        if dec is not None:
            return _first_string_argument(dec.value) or ""
    return None


def _pep702_decorator(obj: Any) -> Any:
    """The PEP 702 ``@deprecated(...)`` decorator of this very object (not of its overloads)."""
    for dec in getattr(obj, "decorators", None) or []:
        path = getattr(dec, "callable_path", "") or ""
        if path in _PEP702 or _resolves_to_pep702(obj, path):
            return dec
    return None


def _deprecated_form(obj: Any) -> tuple[str | None, list[str] | None]:
    """The signature of the ``@overload`` of ``obj`` that PEP 702 deprecates, when the function
    itself and its other overloads are not (pydantic's ``with_config(*, config=...)``,
    cachetools' ``cached`` with a positional ``info``): only that call form is deprecated. With
    it, the parameters that only the deprecated overloads take (APIChange.call_form_only): []
    when the other overloads take every one of them too, and [] when one of them cannot be
    passed by keyword (positional-only, ``*args``, ``**kwargs``: cachetools' ``info``), since
    a call is then in the deprecated form without passing any keyword that names it.
    ``(None, None)`` for a function that is deprecated as a whole, or not at all."""
    overloads = list(getattr(obj, "overloads", None) or [])
    if obj not in overloads and _pep702_decorator(obj) is not None:
        return None, None
    marked = [o for o in overloads if _pep702_decorator(o) is not None]
    if not marked or len(marked) == len(overloads):
        return None, None
    signature = signature_of(marked[0]) or ""
    others = {name for o in overloads if o not in marked for name, _ in _overload_parameters(o)}
    only = [(n, k) for o in marked for n, k in _overload_parameters(o) if n not in others]
    if any(kind not in _BY_KEYWORD for _, kind in only):
        only = []
    return signature.split(" -> ")[0] or None, list(dict.fromkeys(n for n, _ in only))


# The kinds of parameter a call can pass by keyword.
_BY_KEYWORD = ("positional_or_keyword", "keyword_only")


def _overload_parameters(fn: Any) -> list[tuple[str, str]]:
    """The names and kinds (:func:`_pkind`) of ``fn``'s parameters, ``self`` and ``cls`` left
    out; [] when griffe has none for it."""
    try:
        return [(p.name, _pkind(p)) for p in fn.parameters if p.name not in ("self", "cls")]
    except Exception:
        return []


def _resolves_to_pep702(obj: Any, path: str) -> bool:
    """Whether the decorator at ``path`` is PEP 702's, imported through compatibility modules
    (``from pkg._compat import deprecated``, where ``_compat`` imports it from
    ``typing_extensions`` or, on 3.13 and later, ``warnings``).

    Follows each import by the path it names, one module at a time: griffe cannot resolve the
    last step (``final_target``) because ``typing_extensions`` is not loaded, which made the
    decorator look like the library's own (issue #44).
    """
    if not path.endswith(".deprecated"):
        return False
    seen: set[str] = set()
    while path not in seen:
        if path in _PEP702:
            return True
        seen.add(path)
        try:
            target = obj.modules_collection.get_member(path)
        except Exception:
            return False
        if not getattr(target, "is_alias", False):
            return False
        path = str(target.target_path)
    return False


def _first_string_argument(expr: Any) -> str | None:
    """The first positional string argument of a decorator call, e.g. ``deprecated("msg")``."""
    import ast

    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError:
        return None
    call = tree.body
    if not isinstance(call, ast.Call) or not call.args:
        return None
    try:
        value = ast.literal_eval(call.args[0])
    except (ValueError, SyntaxError):
        return None
    return " ".join(value.split()) if isinstance(value, str) else None


def signature_of(obj: Any, limit: int = 400) -> str | None:
    if obj is None:
        return None
    try:
        if getattr(obj, "is_alias", False):
            obj = obj.final_target
        if getattr(obj, "is_function", False):
            params: list[str] = []
            seen_kw_only = False
            parameters = list(obj.parameters)
            kinds = [_pkind(p) for p in parameters]
            last_positional_only = max(
                (i for i, k in enumerate(kinds) if k == "positional_only"), default=-1
            )
            for i, p in enumerate(parameters):
                kind = kinds[i]
                text = p.name
                if kind == "var_positional":
                    text = f"*{p.name}"
                    seen_kw_only = True
                elif kind == "var_keyword":
                    text = f"**{p.name}"
                elif kind == "keyword_only" and not seen_kw_only:
                    params.append("*")
                    seen_kw_only = True
                if p.annotation is not None and not text.startswith("*"):
                    text += f": {p.annotation}"
                if p.default is not None and kind not in _VARIADIC:
                    text += f" = {p.default}"
                params.append(text)
                if i == last_positional_only:
                    params.append("/")
            sig = f"{obj.name}({', '.join(params)})"
            if obj.returns is not None:
                sig += f" -> {obj.returns}"
        elif getattr(obj, "is_class", False):
            init = obj.members.get("__init__")
            inner = signature_of(init) if init is not None else None
            sig = f"class {obj.name}" + (inner[len("__init__") :] if inner else "")
        elif getattr(obj, "is_attribute", False):
            sig = f"{obj.name}: {obj.annotation}" if obj.annotation is not None else obj.name
        else:
            sig = f"{_kind_of(obj)} {obj.path}"
    except Exception:
        return None
    return sig if len(sig) <= limit else sig[: limit - 3] + "..."


def doc_summary(obj: Any, limit: int = 400) -> str | None:
    if obj is None:
        return None
    try:
        if getattr(obj, "is_alias", False):
            obj = obj.final_target
        doc = obj.docstring
    except Exception:
        return None
    if doc is None or not doc.value:
        return None
    first = doc.value.strip().split("\n\n", 1)[0]
    first = " ".join(first.split())
    return first if len(first) <= limit else first[: limit - 3] + "..."


# ----------------------------------------------------------- dependency switches
# Distributions a release drops for typing features its Python (``typing``) now has: its API
# then names ``typing`` instead, which is no switch to another distribution it requires.
_TYPING_HELPERS = frozenset(
    {"typing-extensions", "typing-inspection", "mypy-extensions", "eval-type-backport", "typed-ast"}
)
# Module segments where a switch is not counted: a package's command line (typed with its CLI
# framework), its test helpers, its experimental corner. A reference to the old distribution
# there still counts as the pinned release naming it.
_DEPENDENCY_SIDE = frozenset({"cli", "commands", "testing", "experimental"})
# Where a public API names another distribution's type (a site's context): what code hands it
# (a parameter), gets from it (a return, an attribute), subclasses or catches (a base class,
# and a base's type argument) or imports from it (a re-export). A parameter's default and an
# attribute's value only count as the API still naming a distribution.
SITE_PARAM = "param"
SITE_RETURN = "return"
SITE_ATTR = "attr"
SITE_BASE = "base"
SITE_TYPE_ARG = "type-arg"
SITE_REEXPORT = "re-export"
_SWITCH_SITES = (SITE_PARAM, SITE_RETURN, SITE_ATTR, SITE_BASE, SITE_TYPE_ARG, SITE_REEXPORT)
_EXTRA_MARKER = re.compile(r"""\bextra\s*==\s*['"]([^'"]+)['"]""")
# How much of the evidence a change keeps (APIChange.dependency): the counts are of everything.
_DEPENDENCY_EXAMPLES = 12
_DEPENDENCY_LISTED = 10
_DEPENDENCY_POINTERS = 5
_DEPENDENCY_CALLS = 128
_DEPENDENCY_PATHS = 64


@dataclass
class Requirements:
    """A release's Requires-Dist, by canonical distribution name: ``base`` what installing it
    installs (a marker on the Python version or the platform stays base), ``extras`` what only
    an extra asks for (``httpx2[http2]``: the extras that list it)."""

    base: dict[str, str] = field(default_factory=dict)  # name -> "httpx2<3,>=2.12.0"
    extras: dict[str, list[str]] = field(default_factory=dict)  # name -> ["http2"]
    # The text of a requirement listed only for extras (the first one), as ``base`` has it.
    extra_text: dict[str, str] = field(default_factory=dict)

    @property
    def extra_names(self) -> set[str]:
        """The extras the requirements mention."""
        return {e for listed in self.extras.values() for e in listed}

    def for_extra(self, extra: str) -> set[str]:
        """What installing the release with ``extra`` installs: the base requirements and that
        extra's."""
        return set(self.base) | {n for n, listed in self.extras.items() if extra in listed}

    def text(self, name: str) -> str:
        return self.base.get(name) or self.extra_text.get(name) or name


def requirements(lines: Iterable[str]) -> Requirements:
    """Read Requires-Dist lines. A requirement whose marker tests ``extra`` belongs to that
    extra; the text kept is the name, its extras and its version range, without the marker
    (the first one of a name listed several times, for several Pythons). Unreadable lines are
    skipped."""
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name

    out = Requirements()
    for line in lines:
        try:
            req = Requirement(str(line))
        except InvalidRequirement:
            continue
        name = canonicalize_name(req.name)
        extras = _EXTRA_MARKER.findall(str(req.marker) if req.marker else "")
        wanted = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
        text = f"{req.name}{wanted}{req.specifier}"
        if extras:
            listed = out.extras.setdefault(name, [])
            listed += [e for e in map(canonicalize_name, extras) if e not in listed]
            out.extra_text.setdefault(name, text)
            continue
        out.base.setdefault(name, text)
    return out


def dependency_module(name: str) -> str:
    """The module a distribution is looked for under: its normalised name (``httpx2``,
    ``huggingface_hub``). Only the name: an import name read from elsewhere (a cached tree)
    would make the same diff differ between machines. A distribution whose modules are named
    otherwise (Pillow's ``PIL``) is not found, which misses a switch and never makes one up."""
    from packaging.utils import canonicalize_name

    return canonicalize_name(name).replace("-", "_")


@dataclass(frozen=True)
class _Dropped:
    """A requirement of the older release that the pinned one no longer has: a base one, or
    one of the extras both releases define (``for_extras``)."""

    name: str  # canonical: "httpx"
    module: str  # "httpx"
    requirement: str  # as the older release lists it: "httpx<1,>=0.23.0"
    extras: tuple[str, ...]  # the pinned release's extras that still list it
    # The extras it was dropped from (fastmcp-slim 4.0's ``client``, ``mcp`` and ``server``);
    # () for a base requirement.
    for_extras: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SwitchTarget:
    """What a dropped requirement may have been switched to: another requirement of the pinned
    release (``httpx2``), or a copy of it the package ships (``typer._click``)."""

    module: str  # "httpx2", "typer._click"
    name: str  # the distribution's canonical name, or the module of a copy
    requirement: str | None  # as the pinned release lists it; None for a copy
    extras: tuple[str, ...] = ()  # the extras the switch is for; () for the base requirements
    added: bool = True  # not among the older release's requirements compared with
    vendored: bool = False


def _dropped_requirements(
    old_requires: Sequence[str],
    new_requires: Sequence[str],
    import_names: Sequence[str],
    new_root: Path,
) -> list[_Dropped]:
    """The requirements the pinned release dropped that may be switches: a base requirement it
    no longer has, or a requirement of an extra both releases define that installing the
    pinned release with that extra no longer installs; not a typing helper, not one of the
    package's own modules, and not a module the pinned release ships itself (pytest 7.2
    dropped ``py`` and has a ``py.py``). Nothing when either release lists no requirement: an
    sdist without Requires-Dist says nothing about what it dropped."""
    if not old_requires or not new_requires:
        return []
    old, new = requirements(old_requires), requirements(new_requires)
    own = {n.split(".")[0].removesuffix("-stubs") for n in import_names}

    def candidate(name: str) -> bool:
        module = dependency_module(name)
        return not (name in _TYPING_HELPERS or module in own or _module_exists(new_root, module))

    out = []
    for name in sorted(set(old.base) - set(new.base)):
        if candidate(name):
            extras = tuple(new.extras.get(name, ()))
            out.append(_Dropped(name, dependency_module(name), old.base[name], extras))
    # Dropped from an extra (not from the base requirements, above): fastmcp-slim 4.0 lists
    # ``httpx2`` where 3.4 listed ``httpx`` for its client, mcp and server extras (the
    # ``fastmcp`` distribution installs the client and server ones).
    for_extras: dict[str, list[str]] = {}
    for extra in sorted(old.extra_names & new.extra_names):
        for name in sorted(old.for_extra(extra) - new.for_extra(extra) - set(old.base)):
            for_extras.setdefault(name, []).append(extra)
    for name, dropped_from in sorted(for_extras.items()):
        if candidate(name):
            still = tuple(new.extras.get(name, ()))
            requirement = old.text(name)
            out.append(
                _Dropped(name, dependency_module(name), requirement, still, tuple(dropped_from))
            )
    return out


def _vendored_copy(roots: Sequence[Any], module: str, new_root: Path) -> str | None:
    """A module the pinned release ships that looks like a copy of ``module``: a private
    subpackage or module named after it (``typer._click`` for ``click``, ``pkg._vendor.click``),
    the shortest one; None when there is none. Not one whose source imports ``module`` itself:
    that is a shim over it (langsmith 0.14's ``_openapi_client._httpx`` imports ``httpx2``, or
    ``httpx`` where that is what is installed), not a copy."""
    found = []
    for root in roots:
        for mod in _walk_modules(root):
            path = str(mod.path)
            named = _has_private_segment(path) and path.rsplit(".", 1)[-1].lstrip("_") == module
            if named and not _imports_module(new_root, path, module):
                found.append(path)
    return min(found, key=lambda p: (p.count("."), p)) if found else None


def _imports_module(root: Path, dotted: str, module: str) -> bool:
    """Whether the source of the module or package ``dotted`` in ``root`` imports ``module``
    (an absolute import of it or of a module in it), anywhere in its files."""
    base = root.joinpath(*dotted.split("."))
    if base.is_dir():
        files = sorted(p for p in base.rglob("*") if p.suffix in (".py", ".pyi"))
    else:
        files = [p for p in (base.with_suffix(".py"), base.with_suffix(".pyi")) if p.is_file()]
    for f in files:
        try:
            with _quiet():
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            if any(_under(name, module) for name in names):
                return True
    return False


def _switch_targets(
    x: _Dropped,
    old_req: Requirements,
    new_req: Requirements,
    ys: dict[str, str],
    copy: str | None,
) -> list[_SwitchTarget]:
    """The targets a dropped requirement is compared with: for a base requirement, the pinned
    release's base requirements; for an extra's, those it installs with those extras (a target
    that only some of them list is for those only); and a copy the package ships of it."""
    out = [_SwitchTarget(copy, copy, None, x.for_extras, vendored=True)] if copy else []
    for y_module, y_name in sorted(ys.items()):
        if not x.for_extras:
            if y_name in new_req.base:
                added = y_name not in old_req.base
                out.append(_SwitchTarget(y_module, y_name, new_req.base[y_name], (), added))
            continue
        extras = tuple(
            e for e in x.for_extras if y_name in new_req.base or e in new_req.extras.get(y_name, ())
        )
        if extras:
            added = not any(y_name in old_req.for_extra(e) for e in extras)
            requirement = new_req.text(y_name)
            out.append(_SwitchTarget(y_module, y_name, requirement, extras, added))
    return out


def _dependency_switches(
    package: str,
    old_version: str,
    new_version: str,
    loaded: Sequence[tuple[Any, Any]],
    dropped: Sequence[_Dropped],
    old_requires: Sequence[str],
    new_requires: Sequence[str],
    new_root: Path,
) -> list[APIChange]:
    """DEPENDENCY_SWITCHED changes: for a dropped requirement X and a target Y (a requirement
    of the pinned release, or a copy of X it ships: :func:`_switch_targets`), the sites
    (public path, parameter, context: SITE_*) of the older release's API that named X types
    and whose same site in the pinned release names Y types.

    A switch needs at least one such site, and a type that kept its name at half of them or
    more (``httpx.Client`` -> ``httpx2.Client``): crewai's ``TokenCalcHandler`` deriving from
    pydantic's ``BaseModel`` instead of langchain's ``BaseCallbackHandler`` is a rewrite, not
    a switch. None when the pinned release's public API still names X anywhere, a default or a
    command-line module included (compatibility code, optional use: langsmith 0.14 still names
    ``httpx``). The trees are the ones the diff loaded (``loaded``, old and new per import
    name); only their public API is read, statically.
    """
    old_req, new_req = requirements(old_requires), requirements(new_requires)
    xs = {d.module for d in dropped}
    own = {str(new.path).split(".")[0] for _, new in loaded}
    ys = {
        dependency_module(name): name
        for name in {*new_req.base, *new_req.extras}
        if dependency_module(name) not in xs | own
    }
    new_roots = [new for _, new in loaded]
    copies = {x.module: _vendored_copy(new_roots, x.module, new_root) for x in dropped}
    vendored = {c for c in copies.values() if c}
    old_refs = _ApiRefs([old for old, _ in loaded], xs)
    new_refs = _ApiRefs(new_roots, xs | set(ys) | vendored, outside=vendored)
    out: list[APIChange] = []
    for x in dropped:
        if x.module in new_refs.named:
            continue
        old_sites = {
            key: {t: top for t, top in targets.items() if _under(t, x.module)}
            for key, targets in old_refs.sites.items()
            if any(_under(t, x.module) for t in targets)
        }
        if not old_sites:
            continue
        for y in _switch_targets(x, old_req, new_req, ys, copies[x.module]):
            switched = []
            for key, old_targets in old_sites.items():
                new_targets = {
                    t: top
                    for t, top in new_refs.sites.get(key, {}).items()
                    if new_refs.module_of(t) == y.module
                }
                if new_targets:
                    switched.append((key, old_targets, new_targets))
            # A subclass of a class whose own base switched says nothing more (every error of
            # huggingface_hub derives from ``HfHubHTTPError``, which derives from
            # ``httpx2.HTTPError``): only the class that switched counts.
            keys = {key for key, _, _ in switched}
            switched = [
                s
                for s in switched
                if not any((p, "", SITE_BASE) in keys for p in new_refs.through.get(s[0], ()))
            ]
            same = [s for s in switched if _last_names(s[1]) & _last_names(s[2])]
            if not switched or 2 * len(same) < len(switched):
                continue
            evidence = _switch_evidence(x, y, switched, same, new_refs, new_root)
            roots = [str(new.path) for _, new in loaded]
            path = max(roots, key=lambda r: sum(_under(key[0], r) for key, _, _ in switched))
            out.append(
                APIChange(
                    package=package,
                    from_version=old_version,
                    to_version=new_version,
                    kind=DEPENDENCY_SWITCHED,
                    path=path,
                    name=x.name,
                    import_paths=evidence.pop("import_paths"),
                    library_names=[],
                    dependency=evidence,
                    tier=TIER_PUBLIC,
                )
            )
    return out


def _under(path: str, prefix: str) -> bool:
    """``path`` is ``prefix`` or a dotted name inside it (``httpx.Client`` in ``httpx``)."""
    return path == prefix or path.startswith(prefix + ".")


def _last_names(targets: Iterable[str]) -> set[str]:
    """``{"Client"}`` for ``{"httpx.Client"}``: the names a switch keeps."""
    return {t.rsplit(".", 1)[-1] for t in targets if "." in t}


def _dependency_side(path: str) -> bool:
    return any(part in _DEPENDENCY_SIDE for part in path.split(".")[1:])


class _Scope:
    """Names in one loaded package, followed to what they stand for: re-exports and ``X =
    other.Name`` to the object they name (another package's, by its canonical path), and the
    package's own type aliases to the types in them (two levels deep). A copy of another
    distribution the package ships (``outside``: ``typer._click``) counts as another package."""

    def __init__(self, root: Any, outside: Iterable[str] = ()) -> None:
        self.root = root
        self.package = str(root.path)
        self.outside = tuple(outside)
        self._chased: dict[str, str] = {}

    def inside(self, path: str) -> bool:
        return _under(path, self.package) and not any(_under(path, o) for o in self.outside)

    def lookup(self, path: str) -> Any:
        if not self.inside(path):
            return None
        obj = self.root
        try:
            for part in path[len(self.package) + 1 :].split(".") if path != self.package else ():
                if getattr(obj, "is_alias", False):
                    obj = obj.final_target
                obj = obj.members.get(part)
                if obj is None:
                    return None
        except Exception:
            return None
        return obj

    def chase(self, path: str) -> str:
        """Where ``path`` leads: a re-export and ``X = other.Name`` followed, inside the
        package, to the object's own path (another package's canonical path, for a name the
        package takes from it)."""
        import griffe

        if path in self._chased:
            return self._chased[path]
        start, seen = path, set()
        while self.inside(path) and path not in seen:
            seen.add(path)
            obj = self.lookup(path)
            if obj is None:
                break
            if getattr(obj, "is_alias", False):
                path = str(obj.target_path)
                continue
            value = getattr(obj, "value", None) if getattr(obj, "is_attribute", False) else None
            if not isinstance(value, (griffe.ExprName, griffe.ExprAttribute)):
                break
            try:
                path = str(value.canonical_path)
            except Exception:
                break
        self._chased[start] = path
        return path

    def resolve(self, expr: Any, top: bool = True, depth: int = 0) -> dict[str, bool]:
        """The canonical paths outside the package that an annotation names, each with whether
        it is the annotation itself or a member of its union (``httpx.Client | None``: True;
        ``Callable[[], httpx.Client]``: False). ``Annotated`` counts only its type, and
        ``Literal`` values are not names."""
        import griffe

        out: dict[str, bool] = {}
        for leaf, is_top in _annotation_names(expr, top):
            try:
                path = leaf.canonical_path
            except Exception:
                continue
            if not isinstance(path, str) or not path:
                continue
            final = self.chase(path)
            if not self.inside(final):
                out[final] = out.get(final, False) or is_top
                continue
            if depth >= 2:
                continue
            obj = self.lookup(final)
            if obj is None or getattr(obj, "is_alias", False):
                continue
            value = getattr(obj, "value", None)
            if isinstance(value, griffe.Expr) and (
                getattr(obj, "is_attribute", False) or getattr(obj, "is_type_alias", False)
            ):
                for t, t_top in self.resolve(value, is_top, depth + 1).items():
                    out[t] = out.get(t, False) or t_top
        return out

    def top_types(self, expr: Any, depth: int = 0) -> set[str]:
        """What an annotation accepts: the canonical paths of the annotation itself or of the
        members of its union (``float | httpx2.Timeout | None``: ``float`` and
        ``httpx2.Timeout``), the package's own type aliases followed; ``None`` and the
        ``Optional`` / ``Union`` forms left out. A class of the package itself is its own
        path."""
        import griffe

        out: set[str] = set()
        for leaf, is_top in _annotation_names(expr):
            if not is_top or str(getattr(leaf, "name", "")) in ("None", "Optional", "Union"):
                continue
            try:
                path = leaf.canonical_path
            except Exception:
                continue
            if not isinstance(path, str) or not path:
                continue
            final = self.chase(path)
            obj = self.lookup(final) if self.inside(final) and depth < 2 else None
            value = getattr(obj, "value", None)
            if (
                obj is not None
                and not getattr(obj, "is_alias", False)
                and isinstance(value, griffe.Expr)
                and (getattr(obj, "is_attribute", False) or getattr(obj, "is_type_alias", False))
            ):
                out |= self.top_types(value, depth + 1)
                continue
            out.add(final)
        return out

    def ancestors(self, head: Any, seen: set[str], depth: int = 0) -> dict[str, bool]:
        """The outside base classes of the package's own classes that ``head`` names, through
        its own classes (mcp 2.2's ``OAuthClientProvider`` derives from its
        ``RedirectAwareAuth``, which derives from ``httpx2.Auth``); ``seen`` gets the
        canonical paths of the package's classes gone through."""
        import griffe

        out: dict[str, bool] = {}
        if depth > 5:
            return out
        for leaf, _ in _annotation_names(head):
            try:
                path = self.chase(str(leaf.canonical_path))
            except Exception:
                continue
            obj = self.lookup(path) if self.inside(path) else None
            if obj is None or path in seen or not getattr(obj, "is_class", False):
                continue
            seen.add(path)
            for base in getattr(obj, "bases", None) or ():
                inner = base.left if isinstance(base, griffe.ExprSubscript) else base
                out.update(self.resolve(inner))
                out.update(self.ancestors(inner, seen, depth + 1))
        return out


def _annotation_names(expr: Any, top: bool = True) -> Iterator[tuple[Any, bool]]:
    """The names in an annotation (the last name of each dotted one), each with whether it is
    the annotation itself or a member of its union (``X | None``, ``Optional[X]``,
    ``Union[X, Y]``). ``Annotated`` gives only its type, ``Literal`` nothing."""
    import griffe

    if isinstance(expr, griffe.ExprName):
        yield expr, top
    elif isinstance(expr, griffe.ExprAttribute):
        yield expr.last, top
    elif isinstance(expr, griffe.ExprSubscript):
        form = _form_name(expr.left)
        if form == "Literal":
            return
        inner = expr.slice
        elements = list(inner.elements) if isinstance(inner, griffe.ExprTuple) else [inner]
        if form == "Annotated":
            if elements:
                yield from _annotation_names(elements[0], top)
            return
        yield from _annotation_names(expr.left, top)
        union = top and form in ("Optional", "Union")
        for element in elements:
            yield from _annotation_names(element, union)
    elif isinstance(expr, griffe.ExprBinOp) and expr.operator == "|":
        yield from _annotation_names(expr.left, top)
        yield from _annotation_names(expr.right, top)
    elif isinstance(expr, griffe.Expr):
        for child in expr.iterate(flat=False):
            if isinstance(child, griffe.Expr):
                yield from _annotation_names(child, False)


def _form_name(expr: Any) -> str:
    import griffe

    if isinstance(expr, griffe.ExprName):
        return str(expr.name)
    if isinstance(expr, griffe.ExprAttribute):
        return str(expr.last.name)
    return ""


class _ApiRefs:
    """What one release's public API names under ``modules`` (other distributions'), read
    statically from the trees the diff loaded.

    ``sites``: by site ``(public path, parameter or "", context)`` (SITE_*, outside the side
    modules), the canonical paths named there, each with whether it is the annotation itself
    or a member of its union. ``named``: the modules named anywhere in the public API,
    defaults, values and side modules included. ``objects``: the object of each site.
    ``through``: for a base class site, the public paths of the package's own classes it
    derives from on the way (huggingface_hub's ``BadRequestError`` through
    ``HfHubHTTPError``). ``modules`` may name a module inside the package, a copy of another
    distribution it ships (``outside``: ``typer._click``), which then counts as another
    package's.
    """

    def __init__(
        self, roots: Sequence[Any], modules: set[str], outside: Iterable[str] = ()
    ) -> None:
        self.modules = modules
        self.outside = tuple(outside)
        self.sites: dict[tuple[str, str, str], dict[str, bool]] = {}
        self.objects: dict[tuple[str, str, str], Any] = {}
        self.named: set[str] = set()
        self.through: dict[tuple[str, str, str], set[str]] = {}
        # For a parameter site: whether its annotation accepts only types of ``modules``
        # (``None`` aside), in every overload.
        self.exclusive: dict[tuple[str, str, str], bool] = {}
        self.roots = {str(root.path): root for root in roots}
        self._indexes: dict[str, dict[str, list[str]]] = {}
        for root in roots:
            try:
                self._scan(root)
            except Exception as exc:
                log.debug("reference scan of %s stopped: %s", getattr(root, "path", "?"), exc)

    def _scan(self, root: Any) -> None:
        import griffe

        scope = _Scope(root, self.outside)
        found_objects = list(iter_public_objects(root))
        public_of = {str(obj.path): public for obj, public in found_objects}
        for obj, public in found_objects:
            side = _dependency_side(public)
            try:
                if getattr(obj, "is_function", False):
                    for fn in (obj, *(getattr(obj, "overloads", None) or ())):
                        for p in fn.parameters:
                            if p.name in ("self", "cls") or p.name.startswith("_"):
                                continue
                            key = (public, p.name, SITE_PARAM)
                            self._add(scope.resolve(p.annotation), key, obj, side)
                            if key in self.sites:
                                # Only the other distributions' types: ``http_client:
                                # httpx2.Client | None``, not ``timeout: float | Timeout``.
                                only = all(self.module_of(t) for t in scope.top_types(p.annotation))
                                self.exclusive[key] = self.exclusive.get(key, True) and only
                            self._add(scope.resolve(p.default), (public, p.name, "default"), obj)
                        self._add(scope.resolve(fn.returns), (public, "", SITE_RETURN), obj, side)
                elif getattr(obj, "is_class", False):
                    for base in obj.bases:
                        if isinstance(base, griffe.ExprSubscript):
                            head, args = base.left, base.slice
                            found = scope.resolve(args, top=False)
                            self._add(found, (public, "", SITE_TYPE_ARG), obj, side)
                        else:
                            head = base
                        seen: set[str] = set()
                        found = {**scope.resolve(head), **scope.ancestors(head, seen)}
                        key = (public, "", SITE_BASE)
                        self._add(found, key, obj, side)
                        if seen and key in self.sites:
                            self.through.setdefault(key, set()).update(
                                public_of[c] for c in seen if c in public_of
                            )
                elif getattr(obj, "is_attribute", False) or getattr(obj, "is_type_alias", False):
                    annotation = getattr(obj, "annotation", None)
                    self._add(scope.resolve(annotation), (public, "", SITE_ATTR), obj, side)
                    self._add(
                        scope.resolve(getattr(obj, "value", None)), (public, "", "value"), obj
                    )
            except Exception as exc:
                log.debug("reference scan skipped %s: %s", public, exc)
        for module in _walk_modules(root):
            path = str(module.path)
            if _has_private_segment(path) or _non_api_path(path, module=True):
                continue
            for name, member in list(module.members.items()):
                if _private(name) or not getattr(member, "is_alias", False):
                    continue
                if not _exported(member):
                    continue
                target = scope.chase(str(member.target_path))
                if not scope.inside(target):
                    key = (f"{path}.{name}", "", SITE_REEXPORT)
                    self._add({target: True}, key, member, _dependency_side(path))

    def _add(
        self, found: dict[str, bool], key: tuple[str, str, str], obj: Any, side: bool = True
    ) -> None:
        """Record what a site names under ``modules``; ``side`` (or a context other than
        SITE_*) records it only as named."""
        found = {t: top for t, top in found.items() if self.module_of(t)}
        if not found:
            return
        self.named.update(self.module_of(t) or "" for t in found)
        if side or key[2] not in _SWITCH_SITES:
            return
        site = self.sites.setdefault(key, {})
        for target, top in found.items():
            site[target] = site.get(target, False) or top
        self.objects.setdefault(key, obj)

    def module_of(self, path: str) -> str | None:
        """The module of ``modules`` that ``path`` is in (the longest), or None."""
        parts = path.split(".")
        for i in range(len(parts), 0, -1):
            if (head := ".".join(parts[:i])) in self.modules:
                return head
        return None

    def top_level(self, names: Iterable[str], copy: str) -> list[str]:
        """The package's public top-level classes that are a re-export of a class of the copy
        it ships (``typer.BadParameter``) or have a switched name and derive from a class of
        the copy (``typer.Context``), in alphabetical order. A class of the package's own that
        only shares a name with a switched type (an SDK's ``Client``) is none of them."""
        import griffe

        wanted = set(names)
        found = set()
        for package, root in self.roots.items():
            for name, member in list(root.members.items()):
                if _private(name):
                    continue
                target = member
                try:
                    if getattr(member, "is_alias", False):
                        if not _exported(member):
                            continue
                        target = member.final_target
                    if not getattr(target, "is_class", False):
                        continue
                    bases = [
                        b.left if isinstance(b, griffe.ExprSubscript) else b
                        for b in getattr(target, "bases", None) or ()
                    ]
                    derives = any(_under(str(getattr(b, "canonical_path", b)), copy) for b in bases)
                    if _under(str(target.path), copy) or (name in wanted and derives):
                        found.add(f"{package}.{name}")
                except Exception:
                    continue
        return sorted(found)

    def paths_of(self, obj: Any) -> list[str]:
        """Every public path of a module-level object (a class for a member), shortest first."""
        try:
            if getattr(obj, "is_alias", False):
                obj = obj.final_target
            path = str(obj.path)
        except Exception:
            return []
        for package, root in self.roots.items():
            if _under(path, package):
                if package not in self._indexes:
                    self._indexes[package] = path_index(root)
                return list(self._indexes[package].get(path, ()))
        return []


def _switch_evidence(
    x: _Dropped,
    y: _SwitchTarget,
    switched: list[tuple[tuple[str, str, str], dict[str, bool], dict[str, bool]]],
    same: list[tuple[tuple[str, str, str], dict[str, bool], dict[str, bool]]],
    refs: _ApiRefs,
    new_root: Path,
) -> dict[str, Any]:
    """APIChange.dependency for one switch (with ``import_paths``, which becomes the change's)."""
    names = sorted({n for _, old, new in same for n in _last_names(old) & _last_names(new)})
    counts: dict[str, int] = {}
    for (_, parameter, context), _, _ in switched:
        if context == SITE_PARAM:
            counts[parameter] = counts.get(parameter, 0) + 1
    sites = [_site(key, old, new, refs) for key, old, new in switched]
    # A parameter's signatures by the type they name now: openai 3's ``http_client`` is an
    # ``httpx2.Client`` in 8 of them and an ``httpx2.AsyncClient`` in 6.
    types: dict[str, dict[tuple[str, bool], int]] = {}
    for site in sites:
        if site["context"] == SITE_PARAM:
            kinds = types.setdefault(site["parameter"], {})
            kind = (str(site["new"]), bool(site["direct"]))
            kinds[kind] = kinds.get(kind, 0) + 1
    # One example per parameter and type it names now, at the signature code most likely
    # calls (the shortest public path; a function, then a constructor, then a method; sync
    # before async), and each return and attribute; then in the same order (an SDK's own
    # client first: ``OpenAI`` before ``Stream``), a parameter (what code hands over) before a
    # return or an attribute, one that takes nothing but the new distribution's types
    # (``http_client``) before one that takes a builtin too (``timeout: float | Timeout``), in
    # the most signatures first.
    by_parameter: dict[tuple[str, str, bool], dict[str, Any]] = {}
    others: list[dict[str, Any]] = []
    for site in sorted(sites, key=_site_order):
        if site["context"] == SITE_PARAM:
            key = (site["parameter"], str(site["new"]), bool(site["direct"]))
            by_parameter.setdefault(key, site)
        elif site["context"] in (SITE_RETURN, SITE_ATTR):
            others.append(site)

    def signatures(site: dict[str, Any]) -> int:
        kinds = types.get(site["parameter"] or "", {})
        return kinds.get((str(site["new"]), bool(site["direct"])), 1)

    examples = sorted(
        [*by_parameter.values(), *others],
        key=lambda s: (
            _site_order(s)[:3],
            s["context"] != SITE_PARAM,
            not s.get("exclusive", True),
            -signatures(s),
            _site_order(s),
            s["parameter"] or "",
        ),
    )
    bases = sorted((s for s in sites if s["context"] == SITE_BASE), key=_site_order)
    reexports = sorted(
        (s for s in sites if s["context"] == SITE_REEXPORT),
        key=lambda s: (s["path"].count("."), _secondary_name(s["path"]), s["path"]),
    )
    # What code calls by its path: a function, a class (its constructor), a classmethod or a
    # staticmethod on its class; not a method, whose instance a file may have from anywhere.
    positions: dict[tuple[str, str], int | None] = {}
    for s in sites:
        if s["context"] == SITE_PARAM and (
            s["callable"] in ("constructor", "function") or s["on_class"]
        ):
            for path in s["import_paths"]:
                positions.setdefault((path, str(s["parameter"])), s["position"])
    calls = sorted(
        ((path, parameter, position) for (path, parameter), position in positions.items()),
        key=lambda c: (c[0].count("."), c[0], c[1]),
    )
    paths = [
        p
        for s in sorted(sites, key=_site_order)
        if s["callable"] in ("constructor", "function", "class", "re-export")
        for p in s["import_paths"]
    ]
    skip = [y.module] if y.vendored else []
    pointers = _still_named(new_root, list(refs.roots), x.module, skip)
    return {
        "old": x.name,
        "new": y.name,
        "old_modules": [x.module],
        "new_modules": [y.module],
        "old_requirement": x.requirement,
        "new_requirement": y.requirement,
        "old_in_extras": list(x.extras),
        "extras": list(y.extras),
        "new_added": y.added,
        "vendored": y.vendored,
        "public_names": refs.top_level(names, y.module)[:_DEPENDENCY_LISTED] if y.vendored else [],
        "sites": len(switched),
        "sites_same_name": len(same),
        "names": names,
        "parameters": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "parameter_types": {
            parameter: [
                {"new": new, "direct": direct, "count": n}
                for (new, direct), n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
            for parameter, kinds in sorted(types.items())
        },
        "examples": [_short_site(s) for s in examples[:_DEPENDENCY_EXAMPLES]],
        "bases": [_short_site(s) for s in bases[:_DEPENDENCY_LISTED]],
        "bases_count": len(bases),
        "reexports": [_short_site(s) for s in reexports[:_DEPENDENCY_LISTED]],
        "calls": [list(c) for c in calls[:_DEPENDENCY_CALLS]],
        "still_named_at": pointers[:_DEPENDENCY_POINTERS],
        "still_named_count": len(pointers),
        "import_paths": _ordered(paths)[:_DEPENDENCY_PATHS],
    }


# At the same depth, what code is most likely to call: a module-level function, a class.
_CALLABLE_ORDER = {"function": 0, "constructor": 1, "method": 2}


def _secondary_name(path: str) -> bool:
    """An async twin or an alternative transport, listed after the plain one."""
    last = path.rsplit(".", 1)[-1].lower()
    return "async" in last or "aio" in last


def _site_order(site: dict[str, Any]) -> tuple[int, int, bool, bool, int, str]:
    """Sites in the order code most likely reaches them: by their shortest public path (a
    class's for its constructor), then kind (:data:`_CALLABLE_ORDER`), then an SDK's own
    client first (a class named like its package: ``openai.OpenAI``, ``anthropic.Anthropic``),
    sync before async, the shorter name first (``OpenAI`` before ``BedrockOpenAI``)."""
    shortest = (site.get("import_paths") or [site["path"]])[0]
    display = site["display"]
    package = site["path"].split(".")[0].replace("_", "").lower()
    return (
        shortest.count("."),
        _CALLABLE_ORDER.get(site["callable"], 3),
        display.split(".")[0].lower() != package,
        _secondary_name(display),
        len(display),
        site["path"],
    )


def _site(
    key: tuple[str, str, str], old: dict[str, bool], new: dict[str, bool], refs: _ApiRefs
) -> dict[str, Any]:
    """One switched site as the evidence shows it: which object, how code reaches it, and the
    types named there before and now (one that kept its name, when there is one)."""
    public, parameter, context = key
    obj: Any = refs.objects.get(key)
    parent: Any = getattr(obj, "parent", None)
    # A classmethod or a staticmethod, which code calls on the class itself
    # (``FastMCP.from_openapi(...)``), as it calls a constructor. A parameter's site is a
    # function's (a re-export's object may be an alias that does not resolve).
    labels = set(getattr(obj, "labels", None) or ()) if context == SITE_PARAM else set()
    on_class = False
    if context == SITE_REEXPORT:
        what, display, paths = "re-export", public, [public]
    elif obj is not None and getattr(parent, "is_class", False):
        class_paths = refs.paths_of(parent)
        name = str(obj.name)
        if name == "__init__":
            # Code calls the class: its path (the site's own, without ``.__init__``, when the
            # class is reached only through a module the index does not list).
            paths = class_paths or [public.removesuffix(".__init__")]
            what, display = "constructor", str(parent.name)
        else:
            what = "method" if getattr(obj, "is_function", False) else "attribute"
            display = f"{parent.name}.{name}"
            paths = [f"{p}.{name}" for p in class_paths]
            on_class = bool(labels & {"classmethod", "staticmethod"})
    else:
        paths = refs.paths_of(obj) if obj is not None else []
        if getattr(obj, "is_class", False):
            what = "class"
        else:
            what = "function" if getattr(obj, "is_function", False) else "attribute"
        display = str(getattr(obj, "name", "") or public.rsplit(".", 1)[-1])
        if what == "attribute":
            display = public
    paths = paths or [public]
    common = sorted(_last_names(old) & _last_names(new))
    before = sorted(old, key=lambda t: (t.rsplit(".", 1)[-1] not in common, t))[0]
    after = sorted(new, key=lambda t: (t.rsplit(".", 1)[-1] not in common, not new[t], t))[0]
    # Under the object's own name where a path has it (``openai.OpenAI``, not the
    # ``openai.Client`` alias that iter_public_objects reaches first).
    own = [p for p in paths if p == display or p.endswith("." + display)]
    shown = (own or paths)[0]
    bound = what in ("constructor", "method")  # a staticmethod has no self or cls
    return {
        "path": f"{shown}.__init__" if what == "constructor" else shown,
        "display": display,
        "parameter": parameter or None,
        "context": context,
        "old": before,
        "new": after,
        "direct": bool(new[after]),
        **({"exclusive": refs.exclusive.get(key, False)} if context == SITE_PARAM else {}),
        "callable": what,
        "on_class": on_class,
        "position": _position(obj, parameter, bound) if context == SITE_PARAM else None,
        "import_paths": paths[:_DEPENDENCY_LISTED],
    }


def _position(obj: Any, parameter: str, bound: bool) -> int | None:
    """Where a call passes ``parameter`` by position, from 0 (``self`` or ``cls`` left out
    for a constructor, a method or a classmethod: ``bound``); None for a parameter that is
    keyword-only or not found, or after one whose kind is not known. The first signature
    that has it counts, the function's own before its overloads."""
    import griffe

    positional = (griffe.ParameterKind.positional_only, griffe.ParameterKind.positional_or_keyword)
    for fn in (obj, *(getattr(obj, "overloads", None) or ())):
        params = list(getattr(fn, "parameters", None) or ())
        if bound and params and params[0].name in ("self", "cls"):
            params = params[1:]
        for i, p in enumerate(params):
            if p.kind not in positional:
                if p.name == parameter:
                    return None
                if p.kind in (griffe.ParameterKind.var_positional, None):
                    break  # what follows is keyword-only, or its position is not known
                continue
            if p.name == parameter:
                return i
    return None


def _short_site(site: dict[str, Any]) -> dict[str, Any]:
    """A site for the evidence: the import paths of a re-export or an attribute are its path."""
    out = dict(site)
    for key in ("callable", "on_class", "position"):
        out.pop(key, None)
    if out["context"] in (SITE_REEXPORT, SITE_ATTR, SITE_BASE, SITE_TYPE_ARG):
        out.pop("import_paths", None)
    return out


def _still_named(
    new_root: Path, packages: Sequence[str], module: str, skip: Sequence[str] = ()
) -> list[str]:
    """Where the pinned release's source still names ``module``: an import of it, or a string
    that is its name or a dotted name in it (``sys.modules.get("httpx")``), as
    ``pkg/module.py:line``, relative to the tree (never an absolute path: diffs are cached and
    shared). Comments are not code; a sentence that mentions it is not a name, and nor is a
    name the module only exports (:func:`_export_strings`: huggingface_hub 2.0's
    ``utils.httpx``, which returns ``httpx2``) or a package name in a list of requirements or
    a documentation example (:func:`_package_name_strings`: fastmcp 4's ``Field(examples=[[
    "fastmcp>=2.0,<3", "httpx", "pandas>=2.0"]])``). Nothing under the modules ``skip`` (a
    copy of ``module`` the package ships) is read."""
    places: set[tuple[str, int]] = set()
    # Only files where the name appears as a word are parsed (``httpx2`` is not ``httpx``).
    word = re.compile(rf"(?<![\w.]){re.escape(module)}(?!\w)")
    skipped = [new_root.joinpath(*s.split(".")) for s in skip]
    for package in packages:
        base = new_root.joinpath(*package.split("."))
        if base.is_dir():
            files = sorted(p for p in base.rglob("*") if p.suffix in (".py", ".pyi"))
        else:
            files = [p for p in (base.with_suffix(".py"), base.with_suffix(".pyi")) if p.is_file()]
        for f in files:
            rel = f.relative_to(new_root).as_posix()
            if any(_skipped_segment(part) for part in rel.split("/")[:-1]):
                continue
            if any(f.with_suffix("") == s or s in f.parents for s in skipped):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                if not word.search(text):
                    continue
                with _quiet():
                    tree = ast.parse(text)
            except (SyntaxError, ValueError, OSError):
                continue
            exports = _export_strings(tree) | _package_name_strings(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    hit = any(_under(a.name, module) for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    hit = node.level == 0 and _under(node.module or "", module)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    hit = id(node) not in exports and (
                        node.value == module
                        or (node.value.startswith(module + ".") and " " not in node.value)
                    )
                else:
                    continue
                if hit:
                    places.add((rel, node.lineno))
    return [f"{rel}:{line}" for rel, line in sorted(places)]


# A requirement with a version (``fastmcp>=2.0,<3``, ``pandas[excel]==2.2``).
_REQUIREMENT = re.compile(r"[A-Za-z0-9][\w.-]*\s*(?:\[[^\]]*\])?\s*(?:===|==|~=|!=|>=|<=|<|>)")
# Keyword arguments whose value documents something (pydantic's ``Field(examples=...)``).
_DOC_KEYWORDS = frozenset({"description", "example", "examples"})


def _package_name_strings(tree: ast.AST) -> set[int]:
    """The ``id`` of each string of a module that names a package to install or shows an
    example, not a module the code uses: in the value of a documentation keyword
    (``Field(examples=[["fastmcp>=2.0,<3", "httpx"]])``, ``description=``), or in a list,
    tuple or set with a requirement that has a version (``["httpx", "pandas>=2.0"]``)."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in _DOC_KEYWORDS:
            found.update(
                id(n)
                for n in ast.walk(node.value)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            )
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            strings = {
                id(e): e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
            if any(_REQUIREMENT.match(value.strip()) for value in strings.values()):
                found.update(strings)
    return found


def _export_strings(tree: ast.AST) -> set[int]:
    """The ``id`` of each string of a module that only names what it exports: in its
    ``__all__`` (assigned, extended or appended to), compared with the name its module-level
    ``__getattr__`` is asked for (``if name == "httpx":``), or returned by its module-level
    ``__dir__``."""
    found: set[int] = set()

    def strings(node: ast.AST | None) -> None:
        for n in ast.walk(node) if node is not None else ():
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                found.add(id(n))

    methods = {
        id(f)
        for c in ast.walk(tree)
        if isinstance(c, ast.ClassDef)
        for f in c.body
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                strings(node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("append", "extend")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "__all__"
        ):
            for arg in node.args:
                strings(arg)
        elif isinstance(node, ast.FunctionDef) and id(node) not in methods:
            if node.name == "__getattr__" and node.args.args:
                asked = node.args.args[0].arg
                for c in ast.walk(node):
                    operands = [c.left, *c.comparators] if isinstance(c, ast.Compare) else []
                    if any(isinstance(o, ast.Name) and o.id == asked for o in operands):
                        for o in operands:
                            strings(o)
            elif node.name == "__dir__":
                for r in ast.walk(node):
                    if isinstance(r, ast.Return):
                        strings(r.value)
    return found


def _group(changes: list[APIChange]) -> list[APIChange]:
    """Merge duplicates (sync/async twins, raw-response wrappers, re-exports) into one change."""
    groups: dict[str, list[APIChange]] = {}
    for c in changes:
        groups.setdefault(c.group_key, []).append(c)
    out: list[APIChange] = []
    for members in groups.values():
        members.sort(key=lambda c: (_is_secondary(c), len(c.path), c.path))
        rep = members[0]
        rep.occurrences = sum(m.occurrences for m in members)
        others = [m.path for m in members[1:]] + [a for m in members for a in m.also]
        # All of them for matching the project's code, the first 5 for display.
        rep.import_paths = _ordered(
            [rep.path, *others, *(p for m in members for p in m.import_paths or ())]
        )
        rep.also = list(dict.fromkeys(others))[:5]
        if any(m.tier == TIER_PUBLIC for m in members):
            rep.tier = TIER_PUBLIC  # the sync twin is public, the async one is not
        out.append(rep)
    out.sort(key=lambda c: (KIND_PRIORITY.get(c.kind, 9), c.path, c.parameter or ""))
    return out


def _is_secondary(c: APIChange) -> bool:
    text = f"{c.owner or ''}.{c.path}".lower()
    return any(s in text for s in ("async", "withraw", "withstreaming", ".beta.", "legacy"))
