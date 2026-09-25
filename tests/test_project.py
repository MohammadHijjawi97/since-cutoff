from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from since_cutoff.errors import ProjectError
from since_cutoff.project import load_project, parse_lockfile, parse_requirements, scan_sources


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
    assert {"hf_hub_download", "resume_download", "create", "messages"} <= project.identifiers


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


def test_unknown_lockfile_content_is_reported(tmp_path):
    write(tmp_path, "uv.lock", "this is = = not toml")
    with pytest.raises(ProjectError, match=r"could not parse uv\.lock"):
        parse_lockfile(tmp_path / "uv.lock")
