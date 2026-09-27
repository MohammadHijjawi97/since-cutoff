"""Static API diff between two versions of a package, built on griffe.

The diff answers one question: *which code that was correct for the old version is wrong for
the new one?* So it reports removed objects and parameters, parameters that became required
or keyword-only, objects that changed kind, and objects newly marked ``@deprecated``
(PEP 702). Cosmetic changes (defaults, attribute values, return annotations) are ignored.

Packages are loaded statically (``allow_inspection=False``): no package code is imported.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from since_cutoff.cache import stable_hash

log = logging.getLogger(__name__)

DIFF_SCHEMA = 7

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
_OWNER_NORMALIZE = re.compile(r"^(Async)|(With(Raw|Streaming)Response)$")
_PEP702 = ("typing_extensions.deprecated", "warnings.deprecated", "typing.deprecated")


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
        """Coarser identity used to avoid probing near-duplicates (e.g. beta mirrors of an API).

        For parameter changes the owner is dropped: a module function and the class method
        that mirrors it (``hf_hub_download`` vs ``HfApi.hf_hub_download``) removing the same
        parameter are one concept and should be probed once.
        """
        kind = REMOVED if self.kind == MOVED else self.kind
        if kind in (PARAM_REMOVED, PARAM_REQUIRED, PARAM_KEYWORD_ONLY, PARAM_POSITIONAL_ONLY):
            return f"{self.package}:{kind}:{self.name}:{self.parameter or ''}"
        owner = _OWNER_NORMALIZE.sub("", self.owner or "")
        return f"{self.package}:{kind}:{owner}.{self.name}:{self.parameter or ''}"

    @property
    def display(self) -> str:
        target = f"{self.owner}.{self.name}" if self.owner else self.path
        if self.kind in (PARAM_REMOVED, PARAM_REQUIRED, PARAM_KEYWORD_ONLY, PARAM_POSITIONAL_ONLY):
            return f"{target}({self.parameter}=...)"
        return target

    @property
    def short_path(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else self.path

    def describe(self, *, short: bool = False) -> str:
        """One human sentence describing the change."""
        pkg = f"{self.package} {self.to_version}"
        path = self.short_path if short else self.path
        call = f"{path}({self.parameter}=...)" if self.parameter else path
        if self.kind == MOVED:
            return f"`{path}` moved to `{self.moved_to}` ({pkg})"
        if self.kind == REMOVED:
            return f"`{path}` was removed ({pkg})"
        if self.kind == PARAM_REMOVED:
            return f"`{call}`: parameter `{self.parameter}` was removed ({pkg})"
        if self.kind == PARAM_REQUIRED:
            return f"`{call}`: parameter `{self.parameter}` is now required ({pkg})"
        if self.kind == PARAM_KEYWORD_ONLY:
            return f"`{call}`: `{self.parameter}` is now keyword-only ({pkg})"
        if self.kind == PARAM_POSITIONAL_ONLY:
            return f"`{path}`: `{self.parameter}` is now positional-only ({pkg})"
        if self.kind == KIND_CHANGED:
            return f"`{path}` changed kind ({pkg})"
        if self.kind == DEPRECATED:
            meaningful = self.deprecation and self.deprecation.strip(" .").lower() != "deprecated"
            extra = f": {self.deprecation}" if meaningful else ""
            return f"`{path}` is deprecated ({pkg}){extra}"
        return f"`{path}` changed ({pkg})"

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
    module = griffe.load(
        import_name[: -len("-stubs")] if stubs else import_name,
        search_paths=[str(root)],
        try_relative_path=False,
        allow_inspection=False,
        resolve_aliases=False,
        store_source=True,
        find_stubs_package=stubs,
    )
    _mark_reexports(module)
    return module


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
    return [c.to_dict() for c in _group(changes)]


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

    def run(self) -> list[APIChange]:
        out: list[APIChange] = []
        for b in self._breakages():
            try:
                change = self._from_breakage(b)
            except Exception as exc:
                log.debug("skipping breakage %s: %s", getattr(b, "obj", b), exc)
                continue
            if change is not None:
                out.append(change)
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
        if not self.is_public(obj.path):
            return None
        # For parameter and kind breakages griffe reports the NEW object; look up the old one.
        old_obj = _get(self.old, _rel(obj.path)) or obj
        if kind == "OBJECT_REMOVED":
            parent = getattr(obj, "parent", None)
            if (
                obj.name == "__init__"
                and parent is not None
                and self.new_object(parent.path) is not None
            ):
                return None  # the class is still constructible (inherited or synthesized __init__)
            moved_to = self.find_moved(obj)
            return self._change(
                MOVED if moved_to else REMOVED,
                obj,
                moved_to=moved_to,
                hint=deprecation_hint(obj),
                suggestions=[] if moved_to else self.similar_names(obj),
                old_signature=signature_of(obj),
                old_doc=doc_summary(obj),
                new_doc=doc_summary(self.new_object(moved_to)) if moved_to else None,
                new_signature=signature_of(self.new_object(moved_to)) if moved_to else None,
            )
        if kind == "OBJECT_CHANGED_KIND":
            new_obj = self.new_object(obj.path)
            if _compatible_kind_change(old_obj, new_obj, self):
                return None
            return self._change(
                KIND_CHANGED,
                old_obj,
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

    def _deprecations(self) -> Iterator[APIChange]:
        for obj, _public in iter_public_objects(self.new):
            if not (getattr(obj, "is_function", False) or getattr(obj, "is_class", False)):
                continue
            message = pep702_message(obj)
            if message is None:
                continue
            old_obj = _get(self.old, _rel(obj.path))
            if old_obj is None or pep702_message(old_obj) is not None:
                continue  # new object (model cannot know it anyway) or already deprecated
            yield self._change(
                DEPRECATED,
                obj,
                deprecation=message or None,
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

    def is_public(self, path: str) -> bool:
        if any(_skipped_segment(p) for p in path.split(".")):
            return False
        return self.public_path(path) is not None

    # ------------------------------------------------------------- lookups
    def new_object(self, path: str | None) -> Any:
        return _get(self.new, _rel(path)) if path else None

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

    def find_moved(self, obj: Any) -> str | None:
        """If a removed module-level object reappears elsewhere in the public API, report it."""
        if self._new_index is None:
            index: dict[str, list[str]] = {}
            for o, public in iter_public_objects(self.new):
                index.setdefault(o.name, []).append(public)
            self._new_index = index
        name = obj.name
        if name.startswith("_") or name in _PRIVATE_EXEMPT:
            return None
        parent = getattr(obj, "parent", None)
        if parent is None or not getattr(parent, "is_module", False):
            return None  # class members: a same-named method elsewhere is almost never a move
        old_kind = _kind_of(obj)

        def plausible(p: str) -> bool:
            target = self.new_object(p)
            if p == obj.path or target is None or _kind_of(target) != old_kind:
                return False
            if not getattr(getattr(target, "parent", None), "is_module", False):
                return False
            # A different object that already existed under that path is not a move.
            previous = _get(self.old, _rel(p))
            return previous is None or previous.path == obj.path

        candidates = [p for p in self._new_index.get(name, []) if plausible(p)]
        if not candidates:
            return None
        old_parts = obj.path.split(".")

        def score(p: str) -> tuple[int, int]:
            parts = p.split(".")
            common = 0
            for a, c in zip(old_parts, parts, strict=False):
                if a != c:
                    break
                common += 1
            return (-common, len(parts))

        return sorted(candidates, key=score)[0]


def _rel(path: str | None) -> str:
    return path.split(".", 1)[1] if path and "." in path else ""


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
    """Plain attributes and ``@property``/``@cached_property`` functions are read the same way."""
    if getattr(obj, "is_attribute", False):
        return True
    if "property" in (getattr(obj, "labels", None) or set()):
        return True
    for dec in getattr(obj, "decorators", None) or []:
        path = str(getattr(dec, "callable_path", "") or dec.value)
        if path.split(".")[-1] in ("property", "cached_property"):
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


def _has_private_segment(path: str) -> bool:
    return any(_private(p) for p in path.split(".")[1:])


def _kind_of(obj: Any) -> str | None:
    if obj is None:
        return None
    try:
        return str(obj.kind.value)
    except Exception:
        return None


def _get(root: Any, rel: str) -> Any:
    if not rel:
        return root
    try:
        obj = root[rel]
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
            seen.add(target.path)
            path = f"{public}.{member.name}"
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


_DEPRECATION_KW = re.compile(
    r"\b(alternative|alternative_import|message|reason|instead|removal|since)\s*=\s*[\"']([^\"']+)[\"']"
)


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
            path = getattr(dec, "callable_path", "") or ""
            if "deprecat" not in path.lower():
                continue
            found = dict(_DEPRECATION_KW.findall(str(dec.value)))
            alternative = found.get("alternative") or found.get("alternative_import")
            if alternative:
                alternative = alternative.strip()
                parts.append(alternative if " " in alternative else f"use {alternative} instead")
            elif found.get("message") or found.get("reason"):
                parts.append(found.get("message") or found.get("reason") or "")
            else:
                first = _first_string_argument(dec.value)
                if first and not re.fullmatch(r"v?\d+(\.\d+)*\w*", first.strip()):
                    parts.append(first)
    try:
        doc = obj.docstring.value if obj.docstring else ""
    except Exception:
        doc = ""
    lines = (doc or "").splitlines()
    for i, line in enumerate(lines):
        if "deprecat" not in line.lower():
            continue
        if param is not None and not re.search(rf"(?<![\w.]){re.escape(param)}(?!\w)", line):
            continue
        # Keep continuation lines (".. deprecated:: 1.0" is usually followed by the advice).
        chunk = [line.strip()]
        for follow in lines[i + 1 : i + 3]:
            if not follow.strip() or not follow[:1].isspace():
                break
            chunk.append(follow.strip())
        parts.append(" ".join(" ".join(chunk).split()))
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
        rep.occurrences = len(members)
        rep.also = [m.path for m in members[1:6]]
        out.append(rep)
    out.sort(key=lambda c: (KIND_PRIORITY.get(c.kind, 9), c.path, c.parameter or ""))
    return out


def _is_secondary(c: APIChange) -> bool:
    text = f"{c.owner or ''}.{c.path}".lower()
    return any(s in text for s in ("async", "withraw", "withstreaming", ".beta.", "legacy"))
