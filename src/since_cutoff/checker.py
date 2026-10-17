"""Static scoring of model-written code with the basedpyright type checker.

Generated code is **never executed**. Each snippet is type-checked against the extracted
sources of one exact package version (plus that version's own runtime dependencies, so types
built on pydantic, google-api-core, azure-core, ... resolve), inside an isolated environment that
contains nothing else. Diagnostics are then attributed: only errors that involve the package
under test count, so a snippet that also imports ``numpy`` (absent from the checker environment)
is not penalised for it.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import venv
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from since_cutoff.cache import DiskCache
from since_cutoff.errors import CheckerError
from since_cutoff.pypi import SourceTree

API_RULES = {
    "reportAttributeAccessIssue",
    "reportCallIssue",
    "reportArgumentType",
    "reportAssignmentType",
    "reportReturnType",
    "reportIndexIssue",
    "reportAbstractUsage",
    "reportFunctionMemberAccess",
    "reportPrivateImportUsage",
    "reportGeneralTypeIssues",
    "reportOptionalMemberAccess",
    "reportOptionalSubscript",
    "reportOptionalIterable",
    "reportOptionalCall",
    "reportOptionalOperand",
    "reportOptionalContextManager",
    "reportTypedDictNotRequiredAccess",
    "reportOperatorIssue",
    "reportUnusedCoroutine",
    "reportDeprecated",
}
_MODULE_IN_MESSAGE = re.compile(r'(?:module|Import) "([\w.]+)"')
_CODE_BLOCK = re.compile(r"```(?:python|py|python3)?[ \t]*\r?\n(.*?)```", re.S | re.I)
_CHECKER_COMMENT = re.compile(r"#\s*(pyright|type|mypy|basedpyright)\s*:", re.I)
_BUILTINS = set(dir(builtins))


@dataclass(frozen=True)
class Diagnostic:
    rule: str | None
    message: str
    line: int
    col: int
    stmt: tuple[int, int] = (0, 0)
    context: str = ""

    @property
    def key(self) -> tuple[int, int, str | None]:
        return (self.line, self.col, self.rule)

    def short(self) -> str:
        first = self.message.splitlines()[0] if self.message else ""
        return f"line {self.line + 1}: {first}"


@dataclass
class CheckResult:
    syntax_ok: bool = True
    uses_package: bool = False
    identifiers: frozenset[str] = frozenset()
    api_errors: list[Diagnostic] = field(default_factory=list)
    deprecations: list[Diagnostic] = field(default_factory=list)
    other_errors: list[Diagnostic] = field(default_factory=list)


# What parsing a model's answer can raise besides a SyntaxError: a NUL byte is a ValueError
# before Python 3.12, and code nested too deeply a RecursionError or MemoryError.
_UNPARSABLE = (SyntaxError, ValueError, RecursionError, MemoryError)


def extract_code(text: str) -> str | None:
    """Pull the Python code out of a model answer (the longest fenced block, or bare code)."""
    blocks: list[str] = _CODE_BLOCK.findall(text or "")
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        ast.parse(stripped)
    except _UNPARSABLE:
        return None
    return stripped + "\n"


def sanitize(code: str) -> str:
    """Remove checker-control comments (``# pyright: ignore``, ``# type: ignore``, file-level
    ``# pyright: basic``) so that generated code cannot switch the checker off."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return code
    # The lines as the tokenizer numbers them: str.splitlines() also splits at a form feed or
    # U+2028, which moved every comment after one to the wrong line, and kept it.
    lines = io.StringIO(code).readlines()
    for tok in reversed(tokens):
        if tok.type == tokenize.COMMENT and _CHECKER_COMMENT.search(tok.string):
            row, col = tok.start
            line = lines[row - 1]
            lines[row - 1] = line[:col].rstrip() + ("\n" if line.endswith("\n") else "")
    return "".join(lines)


class Checker:
    def __init__(
        self, cache: DiskCache, *, python_version: str | None = None, timeout: float = 600.0
    ) -> None:
        self.cache = cache
        self.python_version = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
        self.timeout = timeout
        self._command = pyright_command()

    # ----------------------------------------------------------------- env
    def _isolated_python(self) -> Path:
        """An empty virtualenv so the checker never sees the user's site-packages."""
        env_dir = (
            self.cache.dir("envs") / f"empty-py{sys.version_info.major}{sys.version_info.minor}"
        )
        exe = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if not exe.exists():
            shutil.rmtree(env_dir, ignore_errors=True)
            venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(env_dir)
        return exe

    # --------------------------------------------------------------- check
    def check(
        self,
        tree: SourceTree,
        snippets: dict[str, str],
        *,
        extra_roots: Iterable[Path] = (),
        import_names: tuple[str, ...] | None = None,
    ) -> dict[str, CheckResult]:
        """Type-check many snippets against one package version in a single checker run.

        ``extra_roots`` are the package's runtime dependencies. ``import_names`` overrides the
        names treated as "the package" (used to attribute removed top-level modules).
        """
        if not snippets:
            return {}
        names = import_names or tree.import_names
        results: dict[str, CheckResult] = {}
        work = Path(tempfile.mkdtemp(prefix="since-cutoff-check-"))
        try:
            src_dir = work / "snippets"
            src_dir.mkdir()
            files: dict[str, str] = {}
            for sid, code in snippets.items():
                name = f"s_{re.sub(r'[^A-Za-z0-9_]', '_', sid)}.py"
                (src_dir / name).write_text(sanitize(code), encoding="utf-8")
                files[name] = sid
                results[sid] = _prepare(code, names)
            config = {
                "include": ["snippets"],
                "extraPaths": [str(tree.root), *(str(p) for p in extra_roots)],
                "pythonVersion": self.python_version,
                "typeCheckingMode": "standard",
                "reportDeprecated": "error",
                "reportMissingModuleSource": "none",
                "reportMissingTypeStubs": "none",
                "enableTypeIgnoreComments": False,
                "reportUnnecessaryTypeIgnoreComment": "none",
                "analyzeUnannotatedFunctions": True,
            }
            (work / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")
            cmd = [
                *self._command,
                "--outputjson",
                "--pythonpath",
                str(self._isolated_python()),
                "-p",
                str(work / "pyrightconfig.json"),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    cwd=work,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CheckerError(f"type checker timed out after {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise CheckerError(f"could not run the type checker: {exc}") from exc
            try:
                report = json.loads(proc.stdout)
            except ValueError as exc:
                raise CheckerError(
                    f"type checker failed: {(proc.stderr or proc.stdout)[:500]}"
                ) from exc
            for diag in report.get("generalDiagnostics", []):
                fname = Path(str(diag.get("file", ""))).name
                hit = files.get(fname)
                if hit is None:
                    continue
                _attribute(results[hit], snippets[hit], diag, names)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return results


# basedpyright (and the Node.js it brings) is an extra of the package, ``since-cutoff[run]``:
# only ``run`` type-checks anything, and the two make up most of an installation.
NOT_INSTALLED = (
    "run checks the model's answers with the basedpyright type checker, which is not "
    'installed: pip install "since-cutoff[run]", or uvx --with basedpyright since-cutoff run'
)


def pyright_command() -> list[str]:
    """How to start basedpyright: the executable on PATH, else the installed module. Raises
    :class:`CheckerError` naming the install when there is neither, so that ``run`` can fail
    before it scans anything or calls a model."""
    exe = shutil.which("basedpyright")
    if exe:
        return [exe]
    try:
        import basedpyright  # noqa: F401
    except ImportError as exc:
        raise CheckerError(NOT_INSTALLED) from exc
    return [sys.executable, "-m", "basedpyright"]


# ------------------------------------------------------------ attribution
class _Snippet:
    """AST facts about one snippet: parse status, target imports and target-bound names."""

    def __init__(self, code: str, import_names: tuple[str, ...]) -> None:
        self.full = set(import_names)
        self.code = code
        self.tree: ast.Module | None
        try:
            self.tree = ast.parse(code)
        except _UNPARSABLE:
            self.tree = None
        self.bound: set[str] = set()
        self.imports_target = False
        self.star_target = False
        self.parents: dict[int, ast.AST] = {}
        self.identifiers: frozenset[str] = frozenset()
        if self.tree is not None:
            self._index()

    def is_target_module(self, module: str) -> bool:
        for name in self.full:
            if module == name or module.startswith(name + "."):
                return True
            if "." in name and name.startswith(module + "."):
                return True  # ``from google.cloud import storage`` for ``google.cloud.storage``
        return False

    def _index(self) -> None:
        assert self.tree is not None
        idents: set[str] = set()
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[id(child)] = node
            if isinstance(node, ast.Name):
                idents.add(node.id)
            elif isinstance(node, ast.Attribute):
                idents.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                idents.add(node.arg)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    idents.update(a.name.split("."))
                    if self.is_target_module(a.name):
                        self.imports_target = True
                        self.bound.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                idents.update(node.module.split("."))
                idents.update(a.name for a in node.names)
                if not self.is_target_module(node.module):
                    continue
                owned = _owns(tuple(self.full), node.module)
                for a in node.names:
                    if not owned and not _owns(tuple(self.full), f"{node.module}.{a.name}"):
                        continue  # ``from google.cloud import bigquery`` is another package
                    self.imports_target = True
                    if a.name == "*":
                        self.star_target = True
                    else:
                        self.bound.add(a.asname or a.name)
        self.identifiers = frozenset(idents)
        if self.star_target:
            self.bound |= self._free_names()
        self._propagate()

    def _free_names(self) -> set[str]:
        """Names used but never defined in the snippet: after ``from pkg import *`` they come from pkg."""
        assert self.tree is not None
        defined: set[str] = set()
        used: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                (defined if isinstance(node.ctx, (ast.Store, ast.Del)) else used).add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                defined.update(
                    (a.asname or a.name).split(".")[0] for a in node.names if a.name != "*"
                )
        return used - defined - _BUILTINS

    def _propagate(self) -> None:
        """Follow assignments, helpers, annotations and subclasses until nothing changes."""
        assert self.tree is not None
        changed = True
        while changed:
            changed = False
            for node in ast.walk(self.tree):
                targets: list[ast.expr] = []
                value: ast.expr | None = None
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign):
                    if (node.value is not None and self._rooted(node.value)) or self._rooted(
                        node.annotation
                    ):
                        changed |= self._bind(node.target)
                    continue
                elif isinstance(node, ast.NamedExpr):
                    targets, value = [node.target], node.value
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                    targets, value = [node.target], node.iter
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        if item.optional_vars is not None and self._rooted(item.context_expr):
                            changed |= self._bind(item.optional_vars)
                    continue
                elif isinstance(node, ast.arg) and node.annotation is not None:
                    if self._rooted(node.annotation):
                        changed |= self._bind_name(node.arg)
                    continue
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Helpers that return package objects (``def get_client() -> OpenAI``).
                    returns = [
                        n.value
                        for n in ast.walk(node)
                        if isinstance(n, ast.Return) and n.value is not None
                    ]
                    if (node.returns is not None and self._rooted(node.returns)) or any(
                        self._rooted(r) for r in returns
                    ):
                        changed |= self._bind_name(node.name)
                    continue
                elif isinstance(node, ast.ClassDef):
                    if any(self._rooted(b) for b in node.bases):
                        changed |= self._bind_name(node.name)
                    continue
                if value is not None and self._rooted(value):
                    for t in targets:
                        changed |= self._bind(t)

    def _bind(self, target: ast.expr) -> bool:
        if isinstance(target, (ast.Tuple, ast.List)):
            changed = False
            for element in target.elts:  # bind every element; do not short-circuit
                changed |= self._bind(element)
            return changed
        key = root_key(target)
        return self._bind_name(key) if key else False

    def _bind_name(self, name: str) -> bool:
        if name in self.bound:
            return False
        self.bound.add(name)
        return True

    def _rooted(self, expr: ast.AST) -> bool:
        for node in ast.walk(expr):
            key = root_key(node) if isinstance(node, ast.expr) else None
            if key and key in self.bound:
                return True
        return False

    def _spans(self, node: ast.AST, pos: tuple[int, int]) -> bool:
        start = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        end = (getattr(node, "end_lineno", 0) or 0, getattr(node, "end_col_offset", 0) or 0)
        return start <= pos <= end

    def involves_target(self, line: int, col: int) -> bool:
        """Does the expression at (line, col) belong to the package under test?

        Walks outward from the innermost expression but never crosses an argument boundary:
        a stdlib mistake inside ``client.send(datetime.utcnow())`` is not the package's fault.
        """
        if self.tree is None:
            return False
        pos = (line + 1, col)
        hits: list[tuple[int, ast.AST]] = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.expr, ast.keyword)) and self._spans(node, pos):
                start = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
                end = (getattr(node, "end_lineno", 0) or 0, getattr(node, "end_col_offset", 0) or 0)
                hits.append(((end[0] - start[0]) * 10_000 + (end[1] - start[1]), node))
        hits.sort(key=lambda h: h[0])
        for _, node in hits:
            candidate = self.parents.get(id(node)) if isinstance(node, ast.keyword) else node
            if isinstance(candidate, ast.expr):
                key = root_key(candidate)
                if key and key in self.bound:
                    return True
                if isinstance(candidate, ast.Call) and not isinstance(node, ast.keyword):
                    break
        # A declared type from the package (``x: pkg.Opts = {...}``, ``def f() -> pkg.Opts``).
        for node in ast.walk(self.tree):
            if isinstance(node, ast.AnnAssign) and self._spans(node, pos):
                return self._rooted(node.annotation)
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.returns is not None
                and self._rooted(node.returns)
                and any(isinstance(r, ast.Return) and self._spans(r, pos) for r in ast.walk(node))
            ):
                return True
        return False

    def import_module_at(self, line: int) -> str | None:
        if self.tree is None:
            return None
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno <= line + 1 <= (
                node.end_lineno or node.lineno
            ):
                if isinstance(node, ast.ImportFrom):
                    return node.module
                return node.names[0].name if node.names else None
        return None

    def statement_at(self, line: int) -> tuple[tuple[int, int], str]:
        """Span and source text of the smallest statement containing ``line`` (0-based)."""
        best: ast.stmt | None = None
        if self.tree is not None:
            for node in ast.walk(self.tree):
                if not isinstance(node, ast.stmt):
                    continue
                end = node.end_lineno or node.lineno
                if node.lineno <= line + 1 <= end and (
                    best is None
                    or end - node.lineno < (best.end_lineno or best.lineno) - best.lineno
                ):
                    best = node
        if best is None:
            return (line, line), ""
        text = ast.get_source_segment(self.code, best) or ""
        return (best.lineno - 1, (best.end_lineno or best.lineno) - 1), text[:2000]


def root_key(node: ast.AST) -> str | None:
    """``client.messages.create`` -> ``client``; ``self.client.x`` -> ``self.client``."""
    cur = node
    chain: list[str] = []
    while True:
        if isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Call):
            chain.clear()
            cur = cur.func
        elif isinstance(cur, (ast.Subscript, ast.Starred, ast.Await)):
            chain.clear()
            cur = cur.value
        elif isinstance(cur, ast.Name):
            if cur.id in ("self", "cls") and chain:
                return f"{cur.id}.{chain[-1]}"
            return cur.id
        else:
            return None


def _prepare(code: str, import_names: tuple[str, ...]) -> CheckResult:
    snip = _snippet(code, import_names)
    return CheckResult(
        syntax_ok=snip.tree is not None,
        uses_package=snip.imports_target,
        identifiers=snip.identifiers,
    )


_SNIPPET_CACHE: dict[tuple[str, tuple[str, ...]], _Snippet] = {}


def _snippet(code: str, import_names: tuple[str, ...]) -> _Snippet:
    key = (code, import_names)
    snip = _SNIPPET_CACHE.get(key)
    if snip is None:
        if len(_SNIPPET_CACHE) > 4096:
            _SNIPPET_CACHE.clear()
        snip = _SNIPPET_CACHE[key] = _Snippet(code, import_names)
    return snip


def _owns(import_names: tuple[str, ...], module: str) -> bool:
    """True if ``module`` is one of the package's modules (not just a namespace it shares)."""
    return any(module == n or module.startswith(n + ".") for n in import_names)


def _attribute(
    result: CheckResult,
    code: str,
    diag: dict[str, object],
    import_names: tuple[str, ...],
) -> None:
    severity = diag.get("severity")
    raw_rule = diag.get("rule")
    rule = raw_rule if isinstance(raw_rule, str) else None
    message = str(diag.get("message", ""))
    rng = diag.get("range") or {}
    start = rng.get("start", {}) if isinstance(rng, dict) else {}
    line, col = int(start.get("line", 0)), int(start.get("character", 0))
    if severity != "error" and rule != "reportDeprecated":
        return
    snip = _snippet(code, import_names)
    if snip.tree is None:
        result.syntax_ok = False
        return
    stmt, context = snip.statement_at(line)
    d = Diagnostic(rule, message, line, col, stmt, context)

    related = False
    if rule == "reportMissingImports" or "could not be resolved" in message:
        m = _MODULE_IN_MESSAGE.search(message)
        module = m.group(1) if m else ""
        # Only modules of the package itself count. Other packages, including siblings in a
        # shared namespace (google.api_core next to google.cloud.storage), are simply absent
        # from the checker environment.
        related = bool(module) and _owns(import_names, module)
        if not related:
            return
    elif "unknown import symbol" in message:
        module = snip.import_module_at(line) or ""
        symbol = re.search(r'"([\w]+)" is unknown import symbol', message)
        if module and _owns(import_names, module):
            related = True
        elif module and symbol and snip.is_target_module(module):
            # ``from google.cloud import X``: only X == storage belongs to google-cloud-storage.
            related = _owns(import_names, f"{module}.{symbol.group(1)}")
        if not related:
            return
    elif rule == "reportUndefinedVariable" and snip.star_target:
        related = True  # ``from pkg import *`` no longer provides this name
    elif rule in API_RULES:
        m = _MODULE_IN_MESSAGE.search(message)
        related = bool(m and snip.is_target_module(m.group(1))) or snip.involves_target(line, col)

    if not related:
        result.other_errors.append(d)
    elif rule == "reportDeprecated":
        result.deprecations.append(d)
    else:
        result.api_errors.append(d)
