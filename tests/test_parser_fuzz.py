"""Fuzzing the readers of a project's dependency files.

Whatever a lockfile, requirements file, pyproject.toml, Pipfile or virtual environment holds,
reading the project either works or fails with a ProjectError, which the CLI prints as one
line ("error: could not parse uv.lock: ..."); never with a traceback. The files are
generated: random bytes, and documents shaped like each format with anything in any place.
"""

from __future__ import annotations

import codecs
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hypothesis import example, given
from hypothesis import strategies as st
from packaging.utils import canonicalize_name

from since_cutoff.errors import ProjectError
from since_cutoff.project import Project, load_project, parse_lock

LOCKFILES = ("uv.lock", "poetry.lock", "pdm.lock", "pylock.toml", "Pipfile.lock")
FILES = (
    *LOCKFILES,
    "pylock.dev.toml",
    "pyproject.toml",
    "Pipfile",
    "requirements.txt",
    "requirements/dev.txt",
    ".python-version",
)
PYTHONS = st.sampled_from([None, "3.9", "3.13"])


def read(files: dict[str, bytes], python: str | None = None) -> Project | None:
    """Read a project made of ``files`` as a scan does, lockfiles also on their own; None when
    it is refused with a ProjectError. A result holds only well-formed dependencies."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        for name in files:
            if name in LOCKFILES or name.startswith("pylock."):
                try:
                    lock = parse_lock(root / name, python=python)
                except ProjectError:
                    continue
                assert all(isinstance(v, str) for v in lock.versions.values())
        try:
            project = load_project(root, python=python)
        except ProjectError:
            return None
    for dep in project.dependencies:
        assert canonicalize_name(dep.name) == dep.name
        assert dep.version is None or isinstance(dep.version, str)
    return project


# ------------------------------------------------------------------ raw bytes
TEXT = st.builds(
    str.encode, st.text(max_size=200), st.sampled_from(["utf-8", "utf-8-sig", "utf-16", "utf-32"])
)  # the encoders of UTF-16 and UTF-32 write a byte order mark, as Notepad does


@given(
    st.sampled_from(FILES),
    st.binary(max_size=300) | TEXT | st.binary(max_size=99).map(codecs.BOM_UTF16_LE.__add__),
    PYTHONS,
)
def test_any_bytes_in_a_dependency_file_are_read_or_refused(
    name: str, data: bytes, python: str | None
) -> None:
    read({name: data}, python)


# ------------------------------------------------------- documents of each format
def toml(value: Any) -> str:
    """A TOML value (inline form) for what the strategies below generate."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):  # JSON escapes are TOML escapes, but for DEL
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list):
        return "[" + ", ".join(toml(v) for v in value) + "]"
    return "{" + ", ".join(f"{toml(str(k))} = {toml(v)}" for k, v in value.items()) + "}"


def toml_document(table: dict[str, Any]) -> bytes:
    return "".join(f"{toml(k)} = {toml(v)}\n" for k, v in table.items()).encode()


# Anything TOML or JSON can hold, for any place in a document.
ANYTHING = st.recursive(
    st.text(max_size=8) | st.integers(-2, 2) | st.booleans(),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner, max_size=3)
    ),
    max_leaves=12,
)


def either(real: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """What the tools write there, mostly; anything else sometimes."""
    return real | real | ANYTHING


def table(**fields: st.SearchStrategy[Any]) -> st.SearchStrategy[dict[str, Any]]:
    """A table where each field may be missing."""
    return st.fixed_dictionaries({}, optional={k: either(v) for k, v in fields.items()})


NAME = st.sampled_from(["requests", "Flask", "zope.interface", "typing_extensions", "app"])
VERSION = st.sampled_from(["1.0", "2.32.3", "0.1.0a1", "1!2.0.post1", "not a version", ""])
MARKER = st.sampled_from(
    [
        "python_full_version >= '3.12'",
        "sys_platform == 'win32'",
        "python_version ~= 'abc'",
        "platform_release >= '5'",
        "extra == 'dev'",
        "not a marker",
    ]
)
REQUIREMENT = st.sampled_from(
    [
        "requests>=2",
        "numpy==1.26.4",
        "x @ https://example.com/x-1.0.whl",
        "pkg[extra]==1.0; python_version < '3.8'",
        "foo==2.0; platform_release >= '5'",
        "bar===1.0-custom",
        "bad spec (",
    ]
)
URL = st.sampled_from(
    ["https://pypi.org/simple", "https://user:token@private.example/simple", "git+https://x/y"]
)
SOURCE = table(
    registry=URL,
    editable=st.just("."),
    virtual=st.just("."),
    git=st.just("https://github.com/o/r?rev=main#7fc59da2c4df0123"),
    path=st.just("../lib"),
    url=URL,
    type=st.sampled_from(["git", "url", "legacy", "pypi", "directory", "file"]),
    reference=st.just("main"),
    resolved_reference=st.just("7fc59da2c4df0123"),
)
DEPENDENCY = table(name=NAME, marker=MARKER, extra=st.lists(NAME, max_size=2))
LOCKED = table(
    name=NAME,
    version=VERSION,
    source=SOURCE,
    dependencies=st.lists(DEPENDENCY, max_size=3),
    **{
        "optional-dependencies": st.dictionaries(
            NAME, st.lists(DEPENDENCY, max_size=2), max_size=2
        ),
        "dev-dependencies": st.dictionaries(NAME, st.lists(DEPENDENCY, max_size=2), max_size=2),
        "resolution-markers": st.lists(MARKER, max_size=2),
    },
    git=st.just("https://github.com/o/r"),
    revision=st.just("7fc59da2c4df0123"),
    path=st.just("../lib"),
    index=URL,
    vcs=table(url=URL),
    archive=table(url=URL),
)
POETRY_SPEC = st.one_of(
    st.sampled_from(["^1.2", "1.2", ">=1,<2", "*", "~1.2", "=1.0", "bad"]),
    table(version=VERSION, markers=MARKER, python=st.just("<3.9"), git=URL, path=st.just(".")),
    st.lists(table(version=VERSION, python=st.just("<3.9")), max_size=2),
)
POETRY_TABLE = st.dictionaries(NAME | st.just("python"), either(POETRY_SPEC), max_size=3)
PYPROJECT = table(
    project=table(
        name=NAME,
        dependencies=st.lists(REQUIREMENT, max_size=3),
        **{
            "optional-dependencies": st.dictionaries(NAME, st.lists(REQUIREMENT, max_size=2)),
            "requires-python": st.sampled_from([">=3.9", ">= 3.11, <4", "3"]),
        },
    ),
    tool=table(
        uv=table(sources=st.dictionaries(NAME, SOURCE | st.lists(SOURCE, max_size=2))),
        poetry=table(
            dependencies=POETRY_TABLE,
            **{"dev-dependencies": POETRY_TABLE},
            group=st.dictionaries(NAME, table(dependencies=POETRY_TABLE), max_size=2),
        ),
        pdm=table(),
    ),
    **{
        "dependency-groups": st.dictionaries(
            NAME, st.lists(REQUIREMENT | table(**{"include-group": NAME}), max_size=3)
        ),
        "build-system": table(
            requires=st.lists(st.sampled_from(["poetry-core", "pdm-backend"]), max_size=2),
            **{"build-backend": st.sampled_from(["poetry.core.masonry.api", "pdm.backend"])},
        ),
    },
)
PIPFILE = table(
    packages=st.dictionaries(NAME, either(POETRY_SPEC), max_size=3),
    **{"dev-packages": st.dictionaries(NAME, either(POETRY_SPEC), max_size=3)},
)
PIPFILE_LOCK = table(
    default=st.dictionaries(NAME, table(version=st.sampled_from(["==1.0", "1.0", ""]))),
    develop=st.dictionaries(NAME, table(version=st.just("==2.0"), git=URL, editable=st.booleans())),
    _meta=table(hash=table(sha256=st.just("0" * 8))),
)
DOCUMENTS: dict[str, tuple[st.SearchStrategy[Any], Callable[[Any], bytes]]] = {
    "uv.lock": (table(version=st.just(1), package=st.lists(LOCKED, max_size=4)), toml_document),
    "poetry.lock": (table(package=st.lists(LOCKED, max_size=4)), toml_document),
    "pdm.lock": (table(package=st.lists(LOCKED, max_size=4)), toml_document),
    "pylock.toml": (table(packages=st.lists(LOCKED, max_size=4)), toml_document),
    "pyproject.toml": (PYPROJECT, toml_document),
    "Pipfile": (PIPFILE, toml_document),
    "Pipfile.lock": (either(PIPFILE_LOCK), lambda doc: json.dumps(doc).encode()),
}


@st.composite
def projects(draw: st.DrawFn) -> dict[str, bytes]:
    """One to three of the dependency files, each shaped like its format."""
    names = draw(st.sets(st.sampled_from(sorted(DOCUMENTS)), min_size=1, max_size=3))
    files = {}
    for name in names:
        strategy, write = DOCUMENTS[name]
        files[name] = write(draw(strategy))
    return files


# Found by this test; each ended the scan with an AttributeError or a TypeError.
@example({"Pipfile.lock": b'""'}, None)
@example({"pyproject.toml": b'tool = ""\n'}, None)
@example({"pyproject.toml": b'"dependency-groups" = {"dev" = 3}\n'}, None)
@example({"uv.lock": b'package = [{name = "x", source = [false]}]\n'}, None)
@example({"pylock.toml": b'packages = [{name = ["1.0"], version = "1"}]\n'}, None)
@given(projects(), PYTHONS)
def test_any_document_shaped_like_a_dependency_file_is_read_or_refused(
    files: dict[str, bytes], python: str | None
) -> None:
    read(files, python)


# ----------------------------------------------------------- requirements files
REQUIREMENT_LINE = st.one_of(
    REQUIREMENT,
    st.sampled_from(
        [
            "",
            "# via -r requirements.in",
            "    # via",
            "    #   flask",
            "#   -r requirements.in",
            "# This file is autogenerated by pip-compile with Python 3.12",
            "# uv pip compile pyproject.toml",
            "click==8.1.7  # via flask, -r requirements.in",
            "-r requirements/dev.txt",
            "-r requirements.txt",
            "--requirement missing.txt",
            "-r",
            "-e .",
            "--index-url https://example.com/simple",
            "./local/pkg",
            "requests==2.0 \\",
            "    --hash=sha256:abc",
            "attrs==23.1.0 --hash=sha256:0",
        ]
    ),
    st.text(max_size=30),
)


@given(
    st.lists(REQUIREMENT_LINE, max_size=8),
    st.lists(REQUIREMENT_LINE, max_size=4),
    st.sampled_from(["\n", "\r\n"]),
)
def test_any_requirements_file_is_read_or_refused(
    lines: list[str], included: list[str], eol: str
) -> None:
    read(
        {
            "requirements.txt": eol.join(lines).encode(),
            "requirements/dev.txt": "\n".join(included).encode(),
            "requirements.in": b"flask\n",
        }
    )


# ---------------------------------------------------------- virtual environment
METADATA = st.lists(
    st.sampled_from(
        [
            "Metadata-Version: 2.1",
            "Name: requests",
            "Name: Zope.Interface",
            "Name:",
            "Version: 2.0",
            "Version: 1!2.0.post1",
            "Version:",
            "",
            "Summary: Name: x",
        ]
    )
    | st.text(max_size=20),
    max_size=6,
).map(lambda lines: "\n".join(lines).encode())


@given(
    st.dictionaries(
        st.sampled_from(["requests-2.0", "zope.interface-5", "x"]),
        METADATA | st.binary(max_size=80),
        max_size=3,
    ),
    st.sampled_from([".venv/Lib/site-packages", ".venv/lib/python3.12/site-packages"]),
    st.booleans(),
)
def test_any_installed_package_metadata_is_read_or_skipped(
    dists: dict[str, bytes], site: str, declared: bool
) -> None:
    files = {".venv/pyvenv.cfg": b"home = /usr/bin\n"}
    files.update({f"{site}/{name}.dist-info/METADATA": meta for name, meta in dists.items()})
    if declared:
        files["requirements.txt"] = b"requests\n"
    read(files)
