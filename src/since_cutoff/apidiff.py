"""Static API diff between two versions of a package, built on griffe.

The diff answers one question: *which code that was correct for the old version is wrong for
the new one?* So it reports removed objects and parameters, parameters that became required
or keyword-only, objects that changed kind, and objects newly marked deprecated (PEP 702
``@deprecated``, or a library's own decorator whose name contains "deprecat"). Cosmetic changes
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
import keyword
import logging
import re
import sys
import textwrap
import warnings
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import cache
from importlib import metadata
from pathlib import Path
from typing import Any

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
DIFF_SCHEMA = 19

# Above this many removals in one package the release is a rewrite. Looking for similarly
# named replacements (difflib over every owner's members) then costs minutes and adds little.
FUZZY_LIMIT = 2000

# The parameters through which an SDK method sends request fields it has no parameter for.
REQUEST_EXTRAS = ("extra_body", "extra_query")

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

# Where APIChange.hint comes from (APIChange.hint_source).
HINT_DECORATOR = "decorator"  # the old version's deprecation decorator
HINT_DOCSTRING = "docstring"  # a line of the old docstring that says "deprecated" (and names it)
HINT_PARAM_DOC = "param_doc"  # the removed parameter's own entry in the old docstring
HINT_WARNING = "warnings.warn"  # the old code's warnings.warn text when the parameter is passed

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
            if self.parameter:
                return f"`{call}`: parameter `{self.parameter}` is deprecated{pkg}{extra}"
            if self.call_form:
                return f"`{path}` called as `{self.call_form}` is deprecated{pkg}{extra}"
            return f"`{path}` is deprecated{pkg}{extra}"
        return f"`{path}` changed{pkg}"

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


def load_api(import_name: str, root: Path) -> Any:
    """Load one top-level package statically. ``foo-stubs`` directories load as ``foo``."""

    # griffe warns about every annotation it cannot resolve in third-party code; that is noise
    # for our purpose (and would spill into the user's terminal from worker processes).
    logging.getLogger("griffe").setLevel(logging.CRITICAL + 1)

    stubs = import_name.endswith("-stubs")
    name = import_name[: -len("-stubs")] if stubs else import_name
    if not stubs and _stubs_only(root, name):  # pandas-stubs' import name is pandas
        stubs = True
    with _quiet():
        module = _loader(root).load(name, try_relative_path=False, find_stubs_package=stubs)
    _alias_class_assignments(module)
    _mark_reexports(module)
    return module


def _loader(root: Path) -> Any:
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

    loader = Loader(
        extensions=griffe.load_extensions(Stubs()),
        search_paths=[str(root)],
        allow_inspection=False,
        store_source=True,
    )
    loader.finder = Finder([str(root)])  # what griffe makes from the same search paths
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
) -> list[dict[str, Any]]:
    """Diff two extracted source trees. Returns plain dicts (picklable across processes).

    ``old_requires`` / ``new_requires`` are the releases' Requires-Dist (SourceTree.requires):
    with them, a distribution the pinned release requires instead of one the older release
    required is a change of its own when the public API switched to its types
    (DEPENDENCY_SWITCHED).

    Never raises for problems in the analysed package: griffe can fail on unusual code, and one
    bad top-level module must not hide the changes found in the others.
    """
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
        )
    return [c.to_dict() for c in _group(changes)]


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
) -> list[APIChange]:
    changes: list[APIChange] = []
    # The trees are kept for the dependency pass only when a requirement went (a base one, or
    # one of an extra both releases define): in most release pairs none did (87% of 1991
    # measured for the base requirements), and the pass is skipped.
    dropped = _dropped_requirements(old_requires, new_requires, import_names, new_root)
    loaded: list[tuple[Any, Any]] = []
    for import_name in import_names:
        try:
            old = load_api(import_name, old_root)
        except Exception as exc:
            log.debug("cannot load %s %s from %s: %s", package, old_version, import_name, exc)
            continue
        try:
            new = load_api(import_name, new_root)
        except Exception as exc:
            if not _module_exists(new_root, import_name):
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
                    )
                )
            else:
                log.debug("cannot load %s %s from %s: %s", package, new_version, import_name, exc)
            continue
        if dropped:
            loaded.append((old, new))
        try:
            changes.extend(_Differ(package, old_version, new_version, old, new).run())
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
        self, package: str, old_version: str, new_version: str, old: Any, new: Any
    ) -> None:
        self.package = package
        self.old_version = old_version
        self.new_version = new_version
        self.old = old
        self.new = new
        self._public_map: dict[str, str] | None = None
        self._new_index: dict[str, list[str]] | None = None
        self._old_names: set[str] | None = None  # see old_module_level_names
        # Every public path of each module-level object, per version (see _path_index).
        self._path_indexes: dict[int, dict[str, list[str]]] = {}
        # The names in the ``__all__`` of the modules below a module (see _listed_below).
        self._exported_below: dict[int, set[str]] = {}
        self._lost_bases: dict[str, bool] = {}
        # Removed inherited members, by the member they were inherited from.
        self._inherited: dict[str, list[Any]] = {}
        self._sources: dict[int, _Source] = {}
        # The parsed __getattr__ of the lazy module each module swaps in (see _served_lazily).
        self._lazy_hooks: dict[int, tuple[Any, ast.AST] | None] = {}
        self._tables: dict[tuple[int, str], set[str]] = {}
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
            out.extend(self._deprecations())
        except Exception as exc:
            log.debug("deprecation scan failed for %s: %s", self.package, exc)
        return out

    def _breakages(self) -> list[Any]:
        """Collect griffe's breakages, keeping everything found before any internal error."""
        import griffe

        found: list[Any] = []
        try:
            gen = griffe.find_breaking_changes(self.old, self.new)
            while True:
                try:
                    found.append(next(gen))
                except StopIteration:
                    return found
        except Exception as exc:
            log.debug("breakage detection stopped early for %s: %s", self.package, exc)
        # Fall back to diffing each public submodule on its own, so one bad alias costs one module.
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
        seen: set[tuple[str, str, str]] = set()
        unique = []
        for b in found:
            k = (b.kind.name, b.obj.path, str(getattr(b.old_value, "name", b.old_value)))
            if k not in seen:
                seen.add(k)
                unique.append(b)
        return unique

    # ---------------------------------------------------------------- helpers
    def _change(
        self, kind: str, obj: Any, *, old_obj: Any = None, new_obj: Any = None, **kw: Any
    ) -> APIChange:
        """The change to ``obj``, which is ``old_obj`` (the old version's object) or ``new_obj``
        (the new version's); the paths of both make ``import_paths``."""
        path = self.public_path(obj.path) or obj.path
        parent = getattr(obj, "parent", None)
        owner = parent.name if parent is not None and getattr(parent, "is_class", False) else None
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
        return change

    def _from_breakage(self, b: Any) -> APIChange | None:
        kind = b.kind.name
        obj = b.obj
        if not self.is_public(obj.path, module=_kind_of(obj) == "module"):
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
        old_obj = _get(self.old, _rel(obj.path, self.old.path)) or obj
        if kind == "OBJECT_CHANGED_KIND":
            before = str(getattr(b.old_value, "value", b.old_value))
            if _kind_of(old_obj) != before:
                # Reported through an alias: griffe gives the target's path, not the name that
                # changed, so the old object cannot be located reliably.
                return None
            new_obj = self.new_object(obj.path)
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
        new_fn = self.new_object(obj.path)
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
            signatures = [new_fn, *overloads]
            if any(_accepts_var_keyword(f) for f in signatures):
                return None
            if any(param in {p.name for p in f.parameters} for f in signatures):
                return None
        suggestions: list[str] = []
        rename = False  # suggestions is the new name of the parameter, renamed in place
        if kind == "PARAMETER_REMOVED":
            pkind = _pkind(b.old_value)
            renamed = _renamed(old_fn, new_fn, param)
            if pkind in _VARIADIC:
                if _absorbed(old_fn, new_fn, pkind):
                    return None  # what *args or **kwargs took, the new signature names
                param = ("**" if pkind == "var_keyword" else "*") + param
            elif pkind == "positional_only" and renamed is not None:
                return None  # a positional-only parameter renamed: its name is not API
            elif _accepts_var_keyword(new_fn) and pkind != "positional_only":
                if renamed is None or not _required(renamed):
                    return None  # still accepted through **kwargs
                # The new name is then missing.
                suggestions = _rename_hint(b.old_value, renamed, new_fn)
                rename = bool(suggestions)
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
        change = self._change(
            ckind,
            new_fn,
            old_obj=old_fn,
            new_obj=new_fn,
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
        for origin, objs in self._inherited.items():
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
            form = None
            if message is not None:
                marks = [(None, message, None)]
                form = _deprecated_form(obj)
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
                    old_signature=signature_of(old_obj),
                    new_signature=signature_of(obj),
                    old_doc=doc_summary(old_obj),
                    new_doc=doc_summary(obj),
                )
                change.library_names = self.library_names(change, obj if parameter else None)
                yield change

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
        that called the same method of its base (``super().get(...)``) was an override of it.
        """
        cls = self.new_object(obj.parent.path)
        if not getattr(cls, "is_class", False):
            return False
        package = self.new.path.split(".")[0]
        for base in getattr(cls, "bases", None) or []:
            path = str(base if isinstance(base, str) else getattr(base, "canonical_path", "") or "")
            if not path or path.split(".", 1)[0] == package:
                continue  # griffe follows the package's own bases
            found = _outside_attribute(path, obj.name)
            if found is None:
                found = self._vendored_attribute(path, obj.name)
            if found or (found is None and _calls_super(obj)):
                return True
        return False

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
                return True, warning
            if found is None:  # keep looking for a message: the hook may name it elsewhere
                found = (True, "" if "deprecat" in ast.unparse(scope).lower() else None)
        return found or self._served_lazily(obj, module, statements)

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
        if any(_skipped_segment(p) for p in path.split(".")):
            return False
        public = self.public_path(path)
        return public is not None and not _non_api_path(public, module=module)

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
    """Does the new version's docstring (the callable's, and its class's for ``__init__``)
    say that ``removed`` was removed, or that ``added`` was added, without naming the other?
    Then the parameter in the same place is a new one, not a rename: click 8.2's
    ``CliRunner`` took ``catch_exceptions: bool = True`` where 8.1 took ``mix_stderr: bool =
    True``, and says "Added the ``catch_exceptions`` parameter." and "``mix_stderr``
    parameter has been removed."."""
    owners = [fn, getattr(fn, "parent", None)] if getattr(fn, "name", "") == "__init__" else [fn]
    for owner in owners:
        lines = _doc_lines(owner)
        for i in range(len(lines)):
            text = _sentence(lines, i) or ""
            if _mentions(text, removed) == _mentions(text, added):
                continue  # neither, or a replacement ("removed in favour of ``new``")
            if _mentions(text, removed) and re.search(r"\bremoved\b", text, re.IGNORECASE):
                return True
            if _mentions(text, added) and _ADDED_NOTE.match(text):
                return True
    return False


# A version note that something is new: ".. versionadded:: 2.0", "Added the ``x`` parameter."
# (after ".. versionchanged:: 2.0" too), "New in 2.0".
_ADDED_NOTE = re.compile(
    r"\.\.\s*versionadded::|(?:\.\.\s*versionchanged::\s*\S+\s*)?(?:added|new)\b", re.I
)


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


def iter_public_objects(root: Any) -> Iterator[tuple[Any, str]]:
    """Publicly reachable objects of a loaded package, as ``(object, public_path)`` pairs.

    Objects defined in public modules are yielded under their own path or a shorter re-export
    (``pkg.Session`` for ``pkg.sessions.Session``); objects defined in private modules under the
    shortest public path that re-exports them (``pkg.Client`` for ``pkg._client.Client``). A plain
    import (``from pkg.protocol import X`` in ``pkg.server``) is a path only to an object of a
    private module that nothing re-exports. Each object is yielded once.
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
        for member in members:
            if _private(member.name) or _skipped_segment(member.name):
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
            if getattr(target, "is_module", False) and _non_api_path(path, module=True):
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
            if is_module and _non_api_path(path, module=True):
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
# A name alone in quotes is written as code too, as warnings often write it (httpx 0.27: "Use
# 'proxy' or 'mounts' instead."; click 8.2: "Use 'Command' instead."); not an apostrophe.
_QUOTED = re.compile(r"(?<![\w'\"])(['\"])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\1(?![\w'\"])")
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
    backticks or quotes, called, dotted, snake_case or CamelCase. With ``words``, plain words
    of the prose come last (the signatures baseline tries them; the notes do not)."""
    text = _ROLE.sub(" ", text)
    code: list[str] = []
    for span in _CODE_SPAN.findall(text):
        code += _NAME.findall(span.lstrip("~"))  # Sphinx's :func:`~pkg.name`
    code += [m.group(2) for m in _QUOTED.finditer(text)]
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
    + r"(?P<alt>(?:\s*(?:,|\bor\b)\s*"
    + _WRITTEN.format(r"[\w.]+")
    + r")*)\s+(?:instead|in\s+its\s+place)\b",
    r"\b(?:replaced|superseded)\s+(?:by|with)\s+[`'\"]?(?P<n>[\w.]+)",
    r"\brenamed\s+(?:to|as)\s+[`'\"]?(?P<n>[\w.]+)",
    r"\bin\s+favou?r\s+of\s+[`'\"]?(?P<n>[\w.]+)",
    r"\bdeprecated\W+(?:please\s+)?use\s+[`'\"]?(?P<n>[\w.]+)",
)


def stated_alternatives(match: re.Match[str]) -> list[str]:
    """The names a STATED_REPLACEMENT match gives after its first: ``mounts`` in "Use 'proxy'
    or 'mounts' instead", in the text's order."""
    alt = re.sub(r"=[^\s`'\",]*", "", match.groupdict().get("alt") or "")
    return [n.strip(".") for n in re.findall(r"[A-Za-z_][\w.]*", alt) if n != "or"]


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


def _accepts_var_keyword(fn: Any) -> bool:
    try:
        return any(
            "var_keyword" in str(p.kind) or "VAR_KEYWORD" in str(p.kind) for p in fn.parameters
        )
    except Exception:
        return False


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
        strings = [a if isinstance(a, str) else None for a in args]
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


def _message(node: ast.AST) -> str:
    """The text of a message expression: a string, an f-string (its values as "..."), strings
    joined with ``+``, or the template of ``"...".format(...)`` or ``"..." % x``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "..." for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _message(node.left) + _message(node.right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _message(node.left)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _message(node.func.value)
    return ""


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


def _deprecated_form(obj: Any) -> str | None:
    """The signature of the ``@overload`` of ``obj`` that PEP 702 deprecates, when the function
    itself and its other overloads are not (pydantic's ``with_config(*, config=...)``,
    cachetools' ``cached`` with a positional ``info``): only that call form is deprecated."""
    overloads = list(getattr(obj, "overloads", None) or [])
    if obj not in overloads and _pep702_decorator(obj) is not None:
        return None
    marked = [o for o in overloads if _pep702_decorator(o) is not None]
    if not marked or len(marked) == len(overloads):
        return None
    signature = signature_of(marked[0]) or ""
    return signature.split(" -> ")[0] or None


def _resolves_to_pep702(obj: Any, path: str) -> bool:
    if not path.endswith(".deprecated"):
        return False
    try:
        target = obj.modules_collection.get_member(path)
        final = target.final_target if getattr(target, "is_alias", False) else target
        return str(getattr(target, "target_path", "")) in _PEP702 or str(final.path) in _PEP702
    except Exception:
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
        out.append(rep)
    out.sort(key=lambda c: (KIND_PRIORITY.get(c.kind, 9), c.path, c.parameter or ""))
    return out


def _is_secondary(c: APIChange) -> bool:
    text = f"{c.owner or ''}.{c.path}".lower()
    return any(s in text for s in ("async", "withraw", "withstreaming", ".beta.", "legacy"))
