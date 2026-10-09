from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import packaging.markers
import pytest
from packaging.markers import Marker
from packaging.specifiers import Specifier
from packaging.version import InvalidVersion, Version

from since_cutoff import project as project_module
from since_cutoff.errors import ProjectError
from since_cutoff.project import (
    ApiTypes,
    load_project,
    parse_lockfile,
    parse_requirements,
    scan_file,
    scan_sources,
    typed,
)


def write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return p


def versions(project) -> dict[str, tuple[str | None, bool, str]]:
    return {d.name: (d.version, d.direct, d.source) for d in project.dependencies}


def test_uv_lock_with_direct_and_transitive_dependencies(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "app"\nversion = "0"\ndependencies = ["anthropic>=1"]\n',
    )
    write(
        tmp_path,
        "uv.lock",
        """
        version = 1
        [[package]]
        name = "app"
        version = "0.1.0"
        source = { editable = "." }
        dependencies = [{ name = "anthropic" }]
        [package.dev-dependencies]
        dev = [{ name = "pytest" }]

        [[package]]
        name = "anthropic"
        version = "1.8.0"
        source = { registry = "https://pypi.org/simple" }

        [[package]]
        name = "httpx"
        version = "0.28.1"
        source = { registry = "https://pypi.org/simple" }

        [[package]]
        name = "pytest"
        version = "8.4.0"
        source = { registry = "https://pypi.org/simple" }
        """,
    )
    v = versions(load_project(tmp_path))
    assert v["anthropic"] == ("1.8.0", True, "uv.lock")
    assert v["pytest"] == ("8.4.0", True, "uv.lock")
    assert v["httpx"] == ("0.28.1", False, "uv.lock")
    assert "app" not in v


def test_poetry_lock(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        """
        [tool.poetry.dependencies]
        python = "^3.11"
        Requests = "^2.31"
        [tool.poetry.group.dev.dependencies]
        pytest = "8.0.0"
        """,
    )
    write(
        tmp_path,
        "poetry.lock",
        """
        [[package]]
        name = "requests"
        version = "2.32.3"
        [[package]]
        name = "pytest"
        version = "8.0.0"
        [[package]]
        name = "idna"
        version = "3.7"
        """,
    )
    v = versions(load_project(tmp_path))
    assert v["requests"] == ("2.32.3", True, "poetry.lock")
    assert v["pytest"][1] is True
    assert v["idna"][1] is False


def test_pylock_toml(tmp_path):
    write(tmp_path, "requirements.txt", "attrs\n")
    write(
        tmp_path,
        "pylock.toml",
        'lock-version = "1.0"\n[[packages]]\nname = "attrs"\nversion = "25.1.0"\n',
    )
    assert versions(load_project(tmp_path))["attrs"] == ("25.1.0", True, "pylock.toml")


def test_pipfile_lock(tmp_path):
    write(tmp_path, "Pipfile", "[packages]\nflask = '*'\n")
    write(
        tmp_path,
        "Pipfile.lock",
        json.dumps({"default": {"flask": {"version": "==3.1.0"}}, "develop": {}}),
    )
    assert versions(load_project(tmp_path))["flask"] == ("3.1.0", True, "Pipfile.lock")


def test_requirements_with_includes_hashes_markers_and_urls(tmp_path):
    write(tmp_path, "base.txt", "pydantic==2.13.5\n")
    write(
        tmp_path,
        "requirements.txt",
        """
        # comment
        -r base.txt
        Django==5.2 ; python_version >= "3.10"
        numpy>=2 \\
            --hash=sha256:abc
        requests[socks]==2.32.3  # pinned
        -e .
        git+https://github.com/x/y.git
        --index-url https://example.org
        """,
    )
    reqs = {
        r.name.lower(): str(r.specifier) for r in parse_requirements(tmp_path / "requirements.txt")
    }
    assert reqs == {
        "pydantic": "==2.13.5",
        "django": "==5.2",
        "numpy": ">=2",
        "requests": "==2.32.3",
    }
    v = versions(load_project(tmp_path))
    assert v["django"] == ("5.2", True, "pinned")
    assert v["numpy"] == (None, True, "unpinned")


def test_requirement_include_cycles_terminate(tmp_path):
    write(tmp_path, "a.txt", "-r b.txt\nattrs==1\n")
    write(tmp_path, "b.txt", "-r a.txt\n")
    assert [r.name for r in parse_requirements(tmp_path / "a.txt")] == ["attrs"]


def test_installed_versions_come_from_the_virtualenv(tmp_path):
    write(tmp_path, "requirements.txt", "rich\n")
    site = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    write(tmp_path, ".venv/pyvenv.cfg", "home = /usr\n")
    write(
        site,
        "rich-14.0.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: rich\nVersion: 14.0.0\n\nbody\n",
    )
    assert versions(load_project(tmp_path))["rich"] == ("14.0.0", True, "installed")


def test_python_version_and_imports(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "app"\nversion = "0"\nrequires-python = ">=3.11"\ndependencies = ["x"]\n',
    )
    write(
        tmp_path,
        "src/app/main.py",
        "import anthropic\nfrom huggingface_hub import hf_hub_download as d\nd(repo_id='x', resume_download=True)\nclient.messages.create()\n",
    )
    write(tmp_path, ".venv/lib/site.py", "import should_not_be_seen\n")
    write(tmp_path, "broken.py", "def (:\n")
    project = load_project(tmp_path)
    assert project.python_version == "3.11"
    assert project.imported_modules == {"anthropic", "huggingface_hub"}
    [main] = project.code_use(["huggingface_hub"])
    assert "huggingface_hub.hf_hub_download" in main.paths  # imported as `d`
    assert ("hf_hub_download", "resume_download") in main.keywords
    assert "messages.create" in main.pairs


def test_python_version_file_wins(tmp_path):
    write(tmp_path, "requirements.txt", "attrs\n")
    write(tmp_path, ".python-version", "3.12.4\n")
    assert load_project(tmp_path).python_version == "3.12"


def test_empty_project_is_a_clear_error(tmp_path):
    with pytest.raises(ProjectError, match="no dependencies found"):
        load_project(tmp_path)


def test_scan_sources_skips_virtualenvs(tmp_path):
    write(tmp_path, "venv/x.py", "import secret\n")
    write(tmp_path, "app.py", "import os\n")
    assert scan_sources(tmp_path)[0] == {"os"}


def test_imports_of_namespace_packages_match_the_distribution(tmp_path):
    write(tmp_path, "requirements.txt", "google-genai\ngoogle-cloud-storage\nrequests\n")
    write(
        tmp_path,
        "app.py",
        """
        from google import genai
        import azure.identity.aio as aio
        import requests.adapters
        from . import local
        """,
    )
    project = load_project(tmp_path)
    assert project.imported_modules == {"google", "azure", "requests"}
    # Only the namespace members the code imports count, not every google-* distribution.
    assert project.imports(["google.genai"])
    assert not project.imports(["google.cloud.storage"])
    assert project.imports(["azure.identity"]) and not project.imports(["azure.storage.blob"])
    assert project.imports(["requests"]) and not project.imports(["httpx", "local"])


def test_unknown_lockfile_content_is_reported(tmp_path):
    write(tmp_path, "uv.lock", "this is = = not toml")
    with pytest.raises(ProjectError, match=r"could not parse uv\.lock"):
        parse_lockfile(tmp_path / "uv.lock")


# A poetry.lock from 2020 next to pip-compile's requirements.txt (pypistats.org): the project
# no longer uses Poetry, so the lock's versions and lock-only packages must not count.
_STALE_POETRY_LOCK = """
[[package]]
name = "flask"
version = "1.1.2"
[[package]]
name = "zipp"
version = "3.1.0"
"""


def test_a_stale_lock_the_project_is_not_set_up_for_is_ignored(tmp_path):
    write(tmp_path, "pyproject.toml", '[project]\nname = "site"\nversion = "11"\n')
    write(tmp_path, "poetry.lock", _STALE_POETRY_LOCK)
    write(tmp_path, "requirements.txt", "flask==3.1.1\n")
    project = load_project(tmp_path)
    assert versions(project) == {"flask": ("3.1.1", True, "pinned")}  # no lock-only zipp
    assert project.version_source == "requirements.txt"
    assert project.transitive is False
    assert project.warnings == [
        "ignored poetry.lock: the project is not set up for Poetry, and 1 of its versions "
        "disagree with the pinned requirements (flask 1.1.2 vs ==3.1.1)"
    ]


def test_a_lock_the_project_uses_wins_over_pins_with_a_warning(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "site"\nversion = "1"\n[build-system]\n'
        'requires = ["poetry-core>=2"]\nbuild-backend = "poetry.core.masonry.api"\n',
    )
    write(tmp_path, "poetry.lock", _STALE_POETRY_LOCK)
    write(tmp_path, "requirements.txt", "flask==3.1.1\nattrs==25.1.0\n")
    project = load_project(tmp_path)
    v = versions(project)
    assert v["flask"] == ("1.1.2", True, "poetry.lock")
    assert v["attrs"] == ("25.1.0", True, "pinned")
    assert project.warnings == [
        "1 versions in poetry.lock disagree with the pinned requirements (flask 1.1.2 vs "
        "==3.1.1); the versions from poetry.lock are checked"
    ]
    # Versions from more than one place are all named.
    assert project.version_source == "poetry.lock, requirements.txt for 1 not in it"


def test_pip_compile_output_pins_the_tree_and_marks_the_direct_ones(tmp_path):
    write(tmp_path, "requirements.in", "celery>=4.3\nflask\n")
    write(
        tmp_path,
        "requirements.txt",
        """
        #
        # This file is autogenerated by pip-compile with Python 3.13
        # by the following command:
        #
        #    pip-compile --generate-hashes --output-file=requirements.txt requirements.in
        #
        amqp==5.3.1 \\
            --hash=sha256:43b3319e1b4e7d1251833a93d672b4af1e40f3d632d479b98661a95f117880a2
            # via kombu
        celery==5.5.3 \\
            --hash=sha256:0b5761a07057acee94694464ca482416b959568904c9dfa41ce8413a7d65d525
            # via
            #   -r requirements.in
            #   flower
        flask==3.1.1 \\
            --hash=sha256:07aae2bb5eaf77993ef57e357491839f5fd9f4dc281593a81a9e4d79a24f295c
            # via -r requirements.in
        kombu==5.5.4 \\
            --hash=sha256:a12ed0557c238897d8e518f1d1fdf84bd1516c5e305af2dacd85c2015115feb8
            # via
            #   celery
            #   flower

        # The following packages are considered to be unsafe in a requirements file:
        setuptools==80.9.0 \\
            --hash=sha256:062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922
            # via flower
        """,
    )
    # pip-compile before 5.0 wrote the annotation on the requirement's line.
    write(tmp_path, "requirements-dev.txt", "black==20.8b1  # via -r requirements-dev.in\n")
    write(tmp_path, "requirements-old.txt", "click==7.1.2  # via black, flask\n")
    project = load_project(tmp_path)
    direct = {d.name for d in project.dependencies if d.direct}
    assert direct == {"celery", "flask", "black"}
    assert {d.name for d in project.dependencies} >= {"amqp", "kombu", "setuptools", "click"}
    # It pins the transitive dependencies too, so --all-deps has them.
    assert project.transitive is True


def test_hand_written_requirements_are_all_direct(tmp_path):
    write(tmp_path, "requirements.txt", "flask==3.1.1  # the web framework\nkombu\n")
    project = load_project(tmp_path)
    assert all(d.direct for d in project.dependencies) and project.transitive is False


def test_scan_sources_keeps_the_projects_syntax_warnings_to_itself(tmp_path, recwarn):
    write(tmp_path, "app.py", 'import re\nWORD = re.compile("\\W+")\n')
    assert scan_sources(tmp_path).modules == {"re"}
    assert not [w for w in recwarn if "escape" in str(w.message)]


def test_a_list_or_string_where_a_table_belongs_is_read_as_missing(tmp_path):
    # Hand-edited files that pip, uv and Poetry would reject: the scan reads what it can.
    write(
        tmp_path,
        "pyproject.toml",
        """
        dependency-groups = ["pytest"]
        [project]
        name = "app"
        dependencies = "requests"
        optional-dependencies = ["httpx"]
        [tool]
        poetry = ["flask"]
        """,
    )
    write(tmp_path, "requirements.txt", "attrs==23.1.0\n")
    # "requests" is not six one-letter dependencies (r, e, q, u, s, t).
    assert versions(load_project(tmp_path)) == {"attrs": ("23.1.0", True, "pinned")}


@pytest.mark.parametrize(
    ("name", "text", "error"),
    [
        ("uv.lock", 'version = 1\npackage = { name = "attrs" }\n', None),
        (
            "uv.lock",
            '[[package]]\nname = "app"\nsource = { editable = "." }\ndependencies = ["attrs"]\n',
            None,
        ),
        ("poetry.lock", "[[package]]\nname = 1\nversion = 2\n", None),
        ("pylock.toml", 'packages = "attrs"\n', None),
        ("Pipfile.lock", '["attrs"]', "could not parse Pipfile.lock: not a JSON object"),
        ("Pipfile.lock", '{"default": ["attrs"]}', None),
    ],
)
def test_a_lockfile_laid_out_unlike_its_tool_is_read_or_refused_cleanly(
    tmp_path, name, text, error
):
    # What a lockfile holds in the wrong shape counts as missing, and the requirements file
    # pins attrs. Only a Pipfile.lock that is no JSON object at all is refused, in one line.
    write(tmp_path, name, text)
    write(tmp_path, "requirements.txt", "attrs==23.1.0\n")
    if error:
        with pytest.raises(ProjectError) as info:
            load_project(tmp_path)
        assert str(info.value) == error
        return
    project = load_project(tmp_path)
    assert versions(project) == {"attrs": ("23.1.0", True, "pinned")}
    assert project.version_source == f"{name}, requirements.txt for 1 not in it"


def test_a_uv_lock_fork_whose_markers_cannot_be_compared_counts(tmp_path):
    write(
        tmp_path,
        "uv.lock",
        """
        version = 1
        [[package]]
        name = "numpy"
        version = "2.2.6"
        resolution-markers = ["python_version ~= 'abc'"]
        [[package]]
        name = "numpy"
        version = "2.3.1"
        resolution-markers = [1, "python_full_version >= '3.11'"]
        """,
    )
    # For Python 3.10: the first fork may apply, the second (a stray 1 aside) does not.
    assert versions(load_project(tmp_path, python="3.10"))["numpy"][0] == "2.2.6"


@pytest.mark.parametrize("packaging_release", ["installed", "25"])
def test_markers_on_the_kernel_version_do_not_break_the_scan_on_linux(
    tmp_path, monkeypatch, packaging_release
):
    # packaging before 26 compares these as versions, and "6.5.0-1025-azure" is not one: it
    # raised InvalidVersion, which ended the scan. Packaging 26 finds them False instead, so
    # for the "25" run the comparison raises as it did in packaging 22 to 25.
    linux = {"platform_release": "6.5.0-1025-azure", "platform_version": "#1 SMP PREEMPT"}
    for module in (project_module, packaging.markers):
        real = module.default_environment
        monkeypatch.setattr(module, "default_environment", lambda real=real: {**real(), **linux})
    if packaging_release == "25":
        contains = Specifier.contains

        def contains_versions_only(self, item, prereleases=None):
            Version(str(item))  # InvalidVersion: '6.5.0-1025-azure'
            return contains(self, item, prereleases)

        monkeypatch.setattr(Specifier, "contains", contains_versions_only)
        with pytest.raises(InvalidVersion):  # the simulation is faithful
            Marker("platform_release >= '5'").evaluate()

    write(
        tmp_path / "reqs",
        "requirements.txt",
        "attrs==23.1.0; platform_release >= '5'\nidna==3.7; platform_version >= '1'\n",
    )
    assert set(versions(load_project(tmp_path / "reqs"))) == {"attrs", "idna"}
    write(  # and a uv.lock fork on the kernel's version
        tmp_path / "lock",
        "uv.lock",
        """
        version = 1
        [[package]]
        name = "numpy"
        version = "2.2.6"
        resolution-markers = ["python_full_version < '3.11'"]
        [[package]]
        name = "numpy"
        version = "2.3.1"
        resolution-markers = ["python_full_version >= '3.11' and platform_release >= '5'"]
        """,
    )
    assert versions(load_project(tmp_path / "lock", python="3.12"))["numpy"][0] == "2.3.1"


def test_project_code_python_itself_cannot_parse_is_skipped(tmp_path):
    # Generated code: 5000 strings joined with "+" are too deep for the parser of Python 3.13
    # (a RecursionError); 3.10 reads them.
    generated = "import attrs\nTABLE = " + " + ".join(['"x"'] * 5000) + "\n"
    try:
        ast.parse(generated)
    except (RecursionError, MemoryError):
        readable = False
    else:
        readable = True
    write(tmp_path, "requirements.txt", "requests==2.32.3\n")
    write(tmp_path, "generated.py", generated)
    # Too deep for every Python's parser (a MemoryError: "too complex to parse").
    write(tmp_path, "nested.py", "import yarl\nX = " + "-" * 10_000 + "1\n")
    write(tmp_path, "app.py", "import requests\n")
    project = load_project(tmp_path)
    assert project.imported_modules == ({"attrs", "requests"} if readable else {"requests"})


# ------------------------------------------------------- receiver typing (0.6)
def test_scan_file_records_what_the_receivers_are_assigned_and_annotated(tmp_path):
    use = scan_file(
        ast.parse(
            textwrap.dedent(
                """
                import re
                import pandas as pd
                from frames import Frame as F

                def load(path) -> pd.DataFrame:
                    df = pd.read_csv(path, sep=";")
                    df.groupby("k", axis=0).count()
                    sub = df.head()
                    sub.T.applymap(str)
                    data = {}
                    m = re.match("a", path)
                    with F() as f:
                        f.melt(id_vars="a")
                    return df

                class App:
                    frame: "F | None" = None

                    def make(self, df: F) -> F:
                        self.frame = F()
                        return self.frame.T
                """
            )
        ),
        "app.py",
    )
    assert dict(use.bound) == {"re": "re", "pd": "pandas", "F": "frames.Frame"}
    assert use.accesses >= {
        "pd.read_csv",
        "df.groupby",
        "df.groupby().count",
        "df.head",
        "sub.T",
        "sub.T.applymap",
        "f.melt",
        "re.match",
        "self.frame",
        "self.frame.T",
    }
    # A value that is no chain (a literal) is ``?``: the name is known not to be a package's.
    assert use.assigned == {
        ("df", "pd.read_csv()"),
        ("sub", "df.head()"),
        ("data", "?"),
        ("m", "re.match()"),
        ("f", "F()"),
        ("frame", "?"),
        ("self.frame", "F()"),
    }
    # Annotations: parameters, variables (a string one parsed), the file's own functions'
    # returns (``load()``) and methods' (``self.make()``).
    assert use.annotated == {
        ("load()", "pd.DataFrame"),
        ("frame", "F"),
        ("df", "F"),
        ("make()", "F"),
        ("self.make()", "F"),
    }
    assert use.chain_keywords >= {
        ("pd.read_csv", "sep"),
        ("df.groupby", "axis"),
        ("f.melt", "id_vars"),
    }
    assert use.reaches == frozenset() and use.loose == frozenset()  # typed() sets them


def test_typed_reads_the_values_from_the_packages_own_annotations():
    api = ApiTypes(
        [
            {
                "classes": {
                    "pandas.DataFrame": "pandas.core.frame.DataFrame",
                    "pandas.core.frame.DataFrame": "pandas.core.frame.DataFrame",
                    "pandas.core.generic.NDFrame": "pandas.core.generic.NDFrame",
                    "pandas.core.groupby.generic.DataFrameGroupBy": "pandas.core.groupby.generic.DataFrameGroupBy",
                },
                "bases": {"pandas.core.frame.DataFrame": ["pandas.core.generic.NDFrame"]},
                "returns": {
                    "pandas.read_csv": "pandas.core.frame.DataFrame|pandas.io.parsers.readers.TextFileReader",
                    "pandas.core.frame.DataFrame.groupby": "pandas.core.groupby.generic.DataFrameGroupBy",
                    "pandas.core.generic.NDFrame.head": "Self",
                },
                "attrs": {"pandas.core.frame.DataFrame.T": "pandas.core.frame.DataFrame"},
            },
            {
                # langchain-openai's shape: ``ChatOpenAI`` derives from langchain-core's class,
                # which the package names as it imports it (not canonically).
                "classes": {"lc_openai.ChatOpenAI": "lc_openai.chat.ChatOpenAI"},
                "bases": {"lc_openai.chat.ChatOpenAI": ["lc_core.language_models.BaseChatModel"]},
                "returns": {},
                "attrs": {},
            },
            {
                "classes": {
                    "lc_core.language_models.BaseChatModel": "lc_core.language_models.chat.BaseChatModel"
                },
                "bases": {
                    "lc_core.language_models.chat.BaseChatModel": ["lc_core.base.BaseLanguageModel"]
                },
                "returns": {},
                "attrs": {},
            },
        ]
    )
    assert api.canon("lc_openai.ChatOpenAI") == "lc_openai.chat.ChatOpenAI"
    assert api.ancestors("lc_openai.chat.ChatOpenAI") == [
        "lc_core.language_models.chat.BaseChatModel",
        "lc_core.base.BaseLanguageModel",
    ]
    assert api.paths_of("lc_core.language_models.chat.BaseChatModel") == [
        "lc_core.language_models.chat.BaseChatModel",
        "lc_core.language_models.BaseChatModel",
    ]
    assert api.member("pandas.core.frame.DataFrame", "head", call=True) == {
        "pandas.core.frame.DataFrame"
    }
    assert api.modules == {"pandas", "lc_openai", "lc_core"}
    f = scan_file(
        ast.parse(
            textwrap.dedent(
                """
                import re
                import pandas as pd
                from lc_openai import ChatOpenAI

                def f(path, other, data: "pd.DataFrame | None"):
                    df = pd.read_csv(path)
                    df.applymap(str)
                    df.groupby("k", axis=0).count()
                    df.head().T.swapaxes(0, 1)
                    data.applymap(str)
                    other.melt(id_vars="a")
                    m = re.match("a", path)
                    m.group(0)
                    items = []
                    items.count(1)
                    llm = ChatOpenAI(model="x")
                    llm.predict("q")

                class Mine(ChatOpenAI):
                    def go(self):
                        self.predict("q", stop=["x"])
                """
            )
        ),
        "main.py",
    )
    owners = {"predict": {"lc_core.language_models.chat.BaseChatModel"}}
    t = typed(f, api, keep={"melt", "group", "count", "applymap", "predict"}, owners=owners)
    added = t.members - f.members
    frame = {"pandas.DataFrame", "pandas.core.frame.DataFrame", "pandas.core.generic.NDFrame"}
    # ``df``: a DataFrame (or pandas' TextFileReader, the other return of read_csv), with its
    # base; a chain through ``groupby``; ``head()`` returns Self and ``T`` a DataFrame.
    assert {c for c, a in added if a == "applymap"} == frame | {
        "pandas.io.parsers.readers.TextFileReader"
    }
    assert ("pandas.core.groupby.generic.DataFrameGroupBy", "count") in added
    assert {c for c, a in added if a == "swapaxes"} == frame
    # ``llm``: a ChatOpenAI and, through another package, a BaseChatModel, but not the base
    # of that (``owners`` says ``BaseChatModel`` lost ``predict``: the nearest class counts).
    assert {c for c, a in t.members if a == "predict"} == {
        "lc_openai.ChatOpenAI",
        "lc_openai.chat.ChatOpenAI",
        "lc_core.language_models.BaseChatModel",
        "lc_core.language_models.chat.BaseChatModel",
    }
    assert ("lc_core.language_models.chat.BaseChatModel.predict", "stop") in t.keyword_paths
    assert ("pandas.DataFrame.groupby", "axis") in t.keyword_paths
    assert t.reaches == {
        "lc_core",
        "lc_core.language_models",
        "lc_core.language_models.BaseChatModel",
        "lc_core.language_models.chat",
        "lc_core.language_models.chat.BaseChatModel",
    }
    # ``other.melt``: a bare name the file shows nothing of. ``m`` is re's and ``items`` a
    # list; ``data`` and ``df`` are typed.
    assert t.loose == {"melt"}
    # Nothing to type from: the file as scanned, but for the names kept; ``df`` is pandas',
    # a package no map covers, so it is not a value left untyped either.
    assert typed(f, ApiTypes()) == f
    assert typed(f, ApiTypes(), keep={"melt", "applymap"}).loose == {"melt"}
