"""Static API diff between two versions of a package, built on griffe.

The diff answers one question: *which code that was correct for the old version is wrong for
the new one?* So it reports removed objects and parameters, parameters that became required
or keyword-only, objects that changed kind, and objects newly marked deprecated (PEP 702
``@deprecated``, or a library's own decorator whose name contains "deprecat"). Cosmetic changes
(defaults, attribute values, return annotations) are ignored.

Only the public API is compared. Besides ``_private`` names, test suites, benchmarks and
examples shipped inside a package are skipped (``pkg.testing`` and ``pkg.test`` directly under
the top-level package stay, since libraries such as pandas and numpy document them).

Packages are loaded statically (``allow_inspection=False``): no package code is imported.
"""

from __future__ import annotations

import builtins
import logging
import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from since_cutoff.cache import stable_hash

log = logging.getLogger(__name__)

DIFF_SCHEMA = 9

# Above this many removals in one package the release is a rewrite. Looking for similarly
# named replacements (difflib over every owner's members) then costs minutes and adds little.
FUZZY_LIMIT = 2000

REMOVED = "removed"
MOVED = "moved"
PARAM_REMOVED = "param_removed"
PARAM_REQUIRED = "param_required"
PARAM_KEYWORD_ONLY = "param_keyword_only"
PARAM_POSITIONAL_ONLY = "param_positional_only"
KIND_CHANGED = "kind_changed"
DEPRECATED = "deprecated"

KIND_PRIORITY = {
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
# Test helpers are API only directly under the top-level package (``pandas.testing``).
_TEST_MODULES = {"test", "testing"}
_OWNER_NORMALIZE = re.compile(r"^(Async)|(With(Raw|Streaming)Response)$")
_PEP702 = ("typing_extensions.deprecated", "warnings.deprecated", "typing.deprecated")
# Name parts of decorators that deprecate a parameter rather than the whole function
# (``deprecate_renamed_parameter``, ``deprecate_kwarg``, ``deprecate_nonkeyword_arguments``).
_PARAMETER_WORDS = {"param", "params", "parameter", "parameters", "arg", "args", "kwarg"}
_PARAMETER_WORDS |= {"kwargs", "argument", "arguments", "keyword", "nonkeyword", "positional"}
_VERSION = re.compile(r"v?\d+(\.\d+)*\w*")
_OLD_NAME_KEYS = ("old_name", "old_arg_name", "old_param", "old")
_NEW_NAME_KEYS = ("new_name", "new_arg_name", "new_param", "new")


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

    @property
    def id(self) -> str:
        return stable_hash(self.package, self.kind, self.path, self.parameter, self.moved_to)

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
        owner = _OWNER_NORMALIZE.sub("", self.owner or "")
        return f"{self.kind}:{self.module}:{owner}.{self.name}:{self.parameter or ''}"

    @property
    def concept_key(self) -> str:
        """Coarser identity used to avoid probing near-duplicates (e.g. beta mirrors of an API)."""
        kind = REMOVED if self.kind == MOVED else self.kind
        owner = _OWNER_NORMALIZE.sub("", self.owner or "")
        return f"{self.package}:{kind}:{owner}.{self.name}:{self.parameter or ''}"

    @property
    def display(self) -> str:
        target = f"{self.owner}.{self.name}" if self.owner else self.path
        if self.parameter and self.kind in (
            PARAM_REMOVED,
            PARAM_REQUIRED,
            PARAM_KEYWORD_ONLY,
            PARAM_POSITIONAL_ONLY,
            DEPRECATED,
        ):
            return f"{target}({self.parameter}=...)"
        return target

    @property
    def short_path(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else self.path

    def describe(self, *, short: bool = False, versioned: bool = True) -> str:
        """One human sentence describing the change (``versioned=False`` drops "(pkg 1.2)")."""
        pkg = f" ({self.package} {self.to_version})" if versioned else ""
        path = self.short_path if short else self.path
        call = f"{path}({self.parameter}=...)" if self.parameter else path
        if self.kind == MOVED:
            return f"`{path}` moved to `{self.moved_to}`{pkg}"
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
            return f"`{path}` is deprecated{pkg}{extra}"
        return f"`{path}` changed{pkg}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> APIChange:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------- loading
def load_api(import_name: str, root: Path) -> Any:
    """Load one top-level package statically. ``foo-stubs`` directories load as ``foo``."""
    import griffe

    # griffe warns about every annotation it cannot resolve in third-party code; that is noise
    # for our purpose (and would spill into the user's terminal from worker processes).
    logging.getLogger("griffe").setLevel(logging.CRITICAL + 1)

    stubs = import_name.endswith("-stubs")
    with _quiet():
        module = griffe.load(
            import_name[: -len("-stubs")] if stubs else import_name,
            search_paths=[str(root)],
            try_relative_path=False,
            allow_inspection=False,
            resolve_aliases=False,
            store_source=True,
            find_stubs_package=stubs,
        )
    _alias_class_assignments(module)
    _mark_reexports(module)
    return module


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
    return base.is_dir() or base.with_suffix(".py").exists() or base.with_suffix(".pyi").exists()


def diff_sources(
    package: str,
    old_version: str,
    old_root: Path,
    new_version: str,
    new_root: Path,
    import_names: list[str],
) -> list[dict[str, Any]]:
    """Diff two extracted source trees. Returns plain dicts (picklable across processes).

    Never raises for problems in the analysed package: griffe can fail on unusual code, and one
    bad top-level module must not hide the changes found in the others.
    """
    with _quiet():
        changes = _diff_imports(package, old_version, old_root, new_version, new_root, import_names)
    return [c.to_dict() for c in _group(changes)]


def _diff_imports(
    package: str,
    old_version: str,
    old_root: Path,
    new_version: str,
    new_root: Path,
    import_names: list[str],
) -> list[APIChange]:
    changes: list[APIChange] = []
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
                    )
                )
            else:
                log.debug("cannot load %s %s from %s: %s", package, new_version, import_name, exc)
            continue
        try:
            changes.extend(_Differ(package, old_version, new_version, old, new).run())
        except Exception as exc:
            log.debug("diff of %s failed: %s", import_name, exc)
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
        self._lost_bases: dict[str, bool] = {}
        # Removed inherited members, by the member they were inherited from.
        self._inherited: dict[str, list[Any]] = {}
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
    def _change(self, kind: str, obj: Any, **kw: Any) -> APIChange:
        path = self.public_path(obj.path) or obj.path
        parent = getattr(obj, "parent", None)
        owner = parent.name if parent is not None and getattr(parent, "is_class", False) else None
        return APIChange(
            package=self.package,
            from_version=self.old_version,
            to_version=self.new_version,
            kind=kind,
            path=path,
            name=obj.name,
            owner=owner,
            **kw,
        )

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
            if _compatible_kind_change(old_obj, new_obj, self):
                return None
            return self._change(
                KIND_CHANGED,
                old_obj,
                old_kind=_kind_label(old_obj),
                new_kind=_kind_label(new_obj) or str(getattr(b.new_value, "value", b.new_value)),
                old_signature=signature_of(old_obj),
                old_doc=doc_summary(old_obj),
                new_signature=signature_of(new_obj),
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
        param_obj = b.old_value if kind == "PARAMETER_REMOVED" else (b.new_value or b.old_value)
        param = param_obj.name
        if param in ("self", "cls") or param.startswith("_"):
            return None
        if kind == "PARAMETER_REMOVED" and param in deprecated_parameters(new_fn):
            return None  # still accepted under its old name, with a warning: a deprecation
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
        if kind == "PARAMETER_REMOVED":
            positional_only = _pkind(b.old_value) == "positional_only"
            if positional_only and _positional_rename(old_fn, new_fn, param):
                return None
            if _accepts_var_keyword(new_fn) and not positional_only:
                return None  # still accepted through **kwargs
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
            if _pkind(param_obj) == "positional_only" and _positional_rename(new_fn, old_fn, param):
                return None
            ckind = PARAM_REQUIRED
        return self._change(
            ckind,
            new_fn,
            parameter=param,
            hint=deprecation_hint(old_fn, param),
            suggestions=_close([p.name for p in new_fn.parameters], param)
            if ckind == PARAM_REMOVED
            else [],
            old_signature=signature_of(old_fn),
            new_signature=signature_of(new_fn),
            old_doc=doc_summary(old_fn),
            new_doc=doc_summary(new_fn),
        )

    def _removal(self, obj: Any) -> APIChange:
        moved_to, namesake = self.find_moved(obj)
        return self._change(
            MOVED if moved_to else REMOVED,
            obj,
            moved_to=moved_to,
            namesake=namesake,
            hint=deprecation_hint(obj),
            suggestions=[] if moved_to or not self.fuzzy else self.similar_names(obj),
            old_signature=signature_of(obj),
            old_doc=doc_summary(obj),
            new_doc=doc_summary(self.new_object(moved_to)) if moved_to else None,
            new_signature=signature_of(self.new_object(moved_to)) if moved_to else None,
        )

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
                rep = self._removal(base_member if base_member is not None else by_path[paths[0]])
                extra.append(rep)
            rest = [p for p in paths if p != rep.path]
            rep.occurrences += len(rest)
            rep.also = [*rep.also, *rest][:5]
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
            if message is not None:
                marks = [(None, message, None)]
            old_params = _parameter_names(old_obj)
            for parameter, text, decorator in marks:
                if parameter is not None and (
                    parameter in {m[0] for m in old_marks} or parameter not in old_params
                ):
                    continue  # deprecated before, or never accepted by the old version
                yield self._change(
                    DEPRECATED,
                    obj,
                    parameter=parameter,
                    deprecation=text or None,
                    deprecated_by=decorator,
                    old_signature=signature_of(old_obj),
                    new_signature=signature_of(obj),
                    old_doc=doc_summary(old_obj),
                    new_doc=doc_summary(obj),
                )

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
        """Public names in the same (new) owner that look like replacements for ``obj``."""
        parent = getattr(obj, "parent", None)
        owner = self.new_object(parent.path) if parent is not None else None
        if owner is None:
            return []
        try:
            names = [n for n in owner.members if not n.startswith("_")]
        except Exception:
            return []
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

    def find_moved(self, obj: Any) -> tuple[str | None, str | None]:
        """If a removed module-level object reappears elsewhere in the public API, report it.

        Returns ``(moved_to, namesake)``. A class counts as moved only when the candidate keeps
        at least half of its public members; otherwise the candidate is only a namesake (an
        unrelated class that happens to have the same name).
        """
        if self._new_index is None:
            index: dict[str, list[str]] = {}
            for o, public in iter_public_objects(self.new):
                index.setdefault(o.name, []).append(public)
            self._new_index = index
        name = obj.name
        if name.startswith("_") or name in _PRIVATE_EXEMPT:
            return None, None
        parent = getattr(obj, "parent", None)
        if parent is None or not getattr(parent, "is_module", False):
            return None, None  # class members: a same-named method elsewhere is almost never a move
        old_kind = _kind_of(obj)

        def plausible(p: str) -> bool:
            target = self.new_object(p)
            if p == obj.path or target is None or _kind_of(target) != old_kind:
                return False
            if not getattr(getattr(target, "parent", None), "is_module", False):
                return False
            # A different object that already existed under that path is not a move.
            previous = _get(self.old, _rel(p, self.old.path))
            return previous is None or previous.path == obj.path

        candidates = [p for p in self._new_index.get(name, []) if plausible(p)]
        if not candidates:
            return None, None
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
        if old_kind != "class":
            return candidates[0], None
        for p in candidates:
            if _same_class(obj, self.new_object(p)):
                return p, None
        return None, candidates[0]


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


def _positional_rename(fn: Any, other: Any, param: str) -> bool:
    """True if positional-only ``param`` of ``fn`` sits where ``other`` has a positional-only one."""
    try:
        mine = [p.name for p in fn.parameters if _pkind(p) == "positional_only"]
        theirs = [p.name for p in other.parameters if _pkind(p) == "positional_only"]
    except Exception:
        return False
    return param in mine and len(mine) == len(theirs)


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
    """A class or function replaced by an alias to something equally callable is not a break."""
    if new is None:
        return False
    if _kind_of(old) == "function" and _kind_of(new) == "class":
        return True  # functions that became classes are still callable
    if _is_attribute_like(old) and _is_attribute_like(new):
        return True  # attribute <-> (cached_)property: ``obj.name`` reads the same
    if getattr(new, "is_attribute", False):
        value = getattr(new, "value", None)
        target_path = (
            None
            if value is None or isinstance(value, str)
            else getattr(value, "canonical_path", None)
        )
        target = differ.new_object(str(target_path)) if target_path else None
        if target is not None:
            before, after = _kind_of(old), _kind_of(target)
            return after == before or (before == "function" and after == "class")
    return False


# ------------------------------------------------------------------ utilities
def _private(segment: str) -> bool:
    return segment.startswith("_") and segment not in _PRIVATE_EXEMPT


def _skipped_segment(segment: str) -> bool:
    return segment in _SKIP_SEGMENTS or segment.startswith("test_") or segment.endswith("_test")


def _non_api_path(path: str, *, module: bool = False) -> bool:
    """Is this inside (or, with ``module``, is it) a test suite, benchmark or example module?

    Only module segments are checked, so a member named ``examples`` stays API. ``test`` and
    ``testing`` directly under the top-level package (``pandas.testing``) stay API too.
    """
    parts = path.split(".")
    for i, part in enumerate(parts if module else parts[:-1]):
        if (part in _NON_API_MODULES and i > 0) or (part in _TEST_MODULES and i > 1):
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


def _same_class(old: Any, new: Any) -> bool:
    """Could ``new`` be ``old`` moved elsewhere? It must keep half of old's public members."""
    if not getattr(new, "is_class", False):
        return False
    names = _public_names(old, inherited=False) or _public_names(old, inherited=True)
    if not names:
        return True  # nothing to compare (``class Error(Base): pass``)
    kept = names & _public_names(new, inherited=True)
    return 2 * len(kept) >= len(names)


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

    Objects defined in public modules are yielded under their own path; objects defined in
    private modules are yielded under the public path that re-exports them (``pkg.Client`` for
    ``pkg._client.Client``). Each object is yielded once.
    """
    package = root.path.split(".")[0]
    seen: set[str] = set()
    stack: list[tuple[Any, str]] = [(root, root.path)]
    while stack:
        container, public = stack.pop()
        try:
            members = list(container.members.values())
        except Exception:
            continue
        for member in members:
            if _private(member.name) or _skipped_segment(member.name):
                continue
            target = member
            if getattr(member, "is_alias", False):
                try:
                    target = member.final_target
                except Exception:
                    continue  # re-export of another package, or unresolvable
                if not str(target.path).startswith(package + "."):
                    continue
            if target.path in seen:
                continue
            path = f"{public}.{member.name}"
            if getattr(target, "is_module", False) and _non_api_path(path, module=True):
                continue
            seen.add(target.path)
            yield target, path
            if getattr(target, "is_module", False) or getattr(target, "is_class", False):
                stack.append((target, path))


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
    try:
        if getattr(obj, "is_alias", False):
            obj = obj.final_target
    except Exception:
        return None
    parts: list[str] = []
    if param is None:
        for dec in getattr(obj, "decorators", None) or []:
            path = str(getattr(dec, "callable_path", "") or "")
            name = path.rsplit(".", 1)[-1]
            if "deprecat" not in path.lower() or _is_parameter_decorator(name):
                continue  # parameter decorators say nothing about the object itself
            if "property" in name.lower():
                continue  # descriptors deprecate a way of reading it, not the object
            args, kwargs = _decorator_call(dec)
            parts.append(_decorator_message(name, args, kwargs))
    try:
        doc = obj.docstring.value if obj.docstring else ""
    except Exception:
        doc = ""
    lines = (doc or "").splitlines()
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
        if param is not None and not re.search(rf"(?<![\w.]){re.escape(param)}(?!\w)", line):
            continue
        # Keep the rest of the sentence: ".. deprecated:: 1.0" is usually followed by the
        # advice, and docstrings wrap long sentences over several lines.
        chunk = [line.strip()]
        for j, follow in enumerate(lines[i + 1 : i + 4], start=i + 1):
            if not follow.strip() or _is_param_head(follow) or _doc_section(lines, j) is not None:
                break
            chunk.append(follow.strip())
        text = " ".join(" ".join(chunk).split())
        if text.endswith(":"):
            continue  # cut off before what it announces
        parts.append(text)
        break
    text = "; ".join(p.strip() for p in parts if p and p.strip())
    return text[:300] or None


def pep702_message(obj: Any) -> str | None:
    """Return the deprecation message ('' if none) when decorated with a PEP 702 decorator.

    Looks at the object itself and at its ``@overload`` signatures, and follows compatibility
    shims (``from pkg._compat import deprecated``) that re-export the standard decorator.
    """
    for candidate in [obj, *(getattr(obj, "overloads", None) or [])]:
        for dec in getattr(candidate, "decorators", None) or []:
            path = getattr(dec, "callable_path", "") or ""
            if path in _PEP702 or _resolves_to_pep702(candidate, path):
                return _first_string_argument(dec.value) or ""
    return None


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
            for p in obj.parameters:
                kind = str(p.kind)
                text = p.name
                if "var_positional" in kind or "VAR_POSITIONAL" in kind:
                    text = f"*{p.name}"
                    seen_kw_only = True
                elif "var_keyword" in kind or "VAR_KEYWORD" in kind:
                    text = f"**{p.name}"
                elif ("keyword_only" in kind or "KEYWORD_ONLY" in kind) and not seen_kw_only:
                    params.append("*")
                    seen_kw_only = True
                if p.annotation is not None and not text.startswith("*"):
                    text += f": {p.annotation}"
                if p.default is not None:
                    text += f" = {p.default}"
                params.append(text)
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
        rep.also = list(dict.fromkeys(others))[:5]
        out.append(rep)
    out.sort(key=lambda c: (KIND_PRIORITY.get(c.kind, 9), c.path, c.parameter or ""))
    return out


def _is_secondary(c: APIChange) -> bool:
    text = f"{c.owner or ''}.{c.path}".lower()
    return any(s in text for s in ("async", "withraw", "withstreaming", ".beta.", "legacy"))
