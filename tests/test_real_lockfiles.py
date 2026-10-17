"""Golden tests: what load_project reads from the dependency files of ten real projects.

Each directory in tests/fixtures/lockfiles is an excerpt of a real project's dependency files
at a fixed commit; its SOURCE.md names the repository, the commit and the license, and says
what was kept and what it tests. The excerpts keep what made each project hard to read: a uv
workspace, git sources, a lockfile left over from another tool, pip-compile annotations,
packages locked once per platform or Python, extras full of environment markers.

For each project the tests pin every dependency (its version, whether it is direct, where the
version comes from, why it is not looked up on PyPI), the declared range of each unpinned
one, the version source and the warnings. These are what a scan reports, so a change to a
table is a change in what since-cutoff tells its users about that project: make one only
when the new answer is the right one.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import packaging.markers
import pytest

from since_cutoff import project as project_module
from since_cutoff.project import LATEST, Project, load_project

FIXTURES = Path(__file__).parent / "fixtures" / "lockfiles"
DIRECT, INDIRECT = True, False
Row = tuple[str | None, bool, str, str | None]


def load(tmp_path: Path, name: str, python: str | None = None) -> Project:
    """A fixture's project, read from a copy (so that nothing lands in the checkout)."""
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root)
    return load_project(root, python=python)


def table(project: Project) -> dict[str, Row]:
    """name -> (version, direct, where the version comes from, why it is not on PyPI)."""
    return {d.name: (d.version, d.direct, d.source, d.non_pypi) for d in project.dependencies}


def ranges(project: Project) -> dict[str, str]:
    """The declared range of each dependency that nothing pins."""
    return {d.name: d.specifier for d in project.dependencies if d.specifier}


@dataclass(frozen=True)
class Golden:
    dependencies: dict[str, Row]
    version_source: str
    python_version: str | None
    transitive: bool = True
    ranges: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- uv.lock
# zylon-ai/private-gpt: a uv.lock that is not a workspace. The direct dependencies are the base
# ones and those of every extra (lint, test and typecheck too); the project itself is not one.
PRIVATE_GPT = Golden(
    version_source="uv.lock",
    python_version="3.11",
    dependencies={
        "anthropic": ("0.122.0", DIRECT, "uv.lock", None),
        "anyio": ("4.12.1", INDIRECT, "uv.lock", None),
        "arq": ("0.26.3", DIRECT, "uv.lock", None),
        "cachetools": ("7.0.1", DIRECT, "uv.lock", None),
        "celery": ("5.4.0", DIRECT, "uv.lock", None),  # "celery==5.4.0,<6" agrees with the lock
        "fastapi": ("0.136.1", DIRECT, "uv.lock", None),  # "fastapi[all]"
        "flower": ("2.0.1", DIRECT, "uv.lock", None),
        "huggingface-hub": ("1.7.1", DIRECT, "uv.lock", None),  # also "huggingface_hub"
        "ibm-db": ("3.2.9", DIRECT, "uv.lock", None),  # platform_machine != 'aarch64'
        "ibm-db-sa": ("0.4.4", DIRECT, "uv.lock", None),
        "injector": ("0.24.0", DIRECT, "uv.lock", None),
        "mcp": ("2.0.0", DIRECT, "uv.lock", None),
        # Locked twice: 1.20.1 for Windows, 1.25.1 for Linux and macOS.
        "onnxruntime": ("1.25.1", INDIRECT, "uv.lock", None),
        "openai": ("2.34.0", DIRECT, "uv.lock", None),
        "partial-json-parser": ("0.2.1.1.post7", DIRECT, "uv.lock", None),
        "playwright": ("1.60.0", DIRECT, "uv.lock", None),
        "pytest": ("9.0.2", DIRECT, "uv.lock", None),
        "python-multipart": ("0.0.27", DIRECT, "uv.lock", None),
        "pyyaml": ("6.0.3", DIRECT, "uv.lock", None),
        "requests": ("2.33.1", DIRECT, "uv.lock", None),
        "ruff": ("0.15.21", DIRECT, "uv.lock", None),
        "sqlalchemy": ("2.0.49", DIRECT, "uv.lock", None),
        "sqlgpt-parser": ("0.0.1a5", DIRECT, "uv.lock", None),  # it has pre-releases only
        "starlette": ("0.52.1", INDIRECT, "uv.lock", None),
        "torch": ("2.11.0", DIRECT, "uv.lock", None),
        "transformers": ("5.7.0", DIRECT, "uv.lock", None),
        "ty": ("0.0.59", DIRECT, "uv.lock", None),
        "types-pyyaml": ("6.0.12.20250915", DIRECT, "uv.lock", None),
    },
)

# modelcontextprotocol/python-sdk: a uv workspace. Its members (mcp, mcp-types and three
# examples, one of them virtual) are not dependencies, and what they depend on is direct: an
# example server's click, a dev group's pyright. mcp declares its own dependencies as dynamic,
# so only the lock names them.
PYTHON_SDK = Golden(
    version_source="uv.lock",
    python_version="3.10",
    dependencies={
        "anthropic": ("0.121.0", DIRECT, "uv.lock", None),
        "anyio": ("4.10.0", DIRECT, "uv.lock", None),
        "click": ("8.2.1", DIRECT, "uv.lock", None),
        "coverage": ("7.13.0", DIRECT, "uv.lock", None),
        "cryptography": ("50.0.0", INDIRECT, "uv.lock", None),
        "datamodel-code-generator": ("0.57.0", DIRECT, "uv.lock", None),
        "griffelib": ("2.1.0", DIRECT, "uv.lock", None),
        "httpcore": ("1.0.9", INDIRECT, "uv.lock", None),
        "httpx2": ("2.10.0", DIRECT, "uv.lock", None),
        "idna": ("3.18", INDIRECT, "uv.lock", None),
        "jsonschema": ("4.25.1", DIRECT, "uv.lock", None),
        "pydantic": ("2.12.5", DIRECT, "uv.lock", None),
        "pydantic-settings": ("2.10.1", DIRECT, "uv.lock", None),
        "pyjwt": ("2.13.0", DIRECT, "uv.lock", None),
        "pyright": ("1.1.405", DIRECT, "uv.lock", None),
        "pytest": ("9.1.1", DIRECT, "uv.lock", None),
        "python-dotenv": ("1.2.3", DIRECT, "uv.lock", None),
        "pywin32": ("311", DIRECT, "uv.lock", None),  # sys_platform == 'win32'
        "rich": ("14.1.0", DIRECT, "uv.lock", None),
        "ruff": ("0.12.12", DIRECT, "uv.lock", None),
        "starlette": ("1.6.0", DIRECT, "uv.lock", None),
        "strict-no-cover": (
            "0.1.1",
            DIRECT,
            "uv.lock",
            "git+https://github.com/pydantic/strict-no-cover@7fc59da2c4df",  # the locked commit
        ),
        "tomli": ("2.4.1", DIRECT, "uv.lock", None),  # python_version < '3.11'
        "typer": ("0.17.4", DIRECT, "uv.lock", None),
        "typing-extensions": ("4.15.0", DIRECT, "uv.lock", None),
        "uvicorn": ("0.35.0", DIRECT, "uv.lock", None),
        "zensical": ("0.0.50", DIRECT, "uv.lock", None),
    },
)

# ------------------------------------------------------------------------ poetry.lock
# Textualize/rich: the old Poetry layout, [tool.poetry.dependencies] (python is not a
# dependency) with an optional one behind an extra, and [tool.poetry.dev-dependencies].
RICH = Golden(
    version_source="poetry.lock",
    python_version=None,  # no [project] table, so no requires-python
    dependencies={
        "appnope": ("0.1.4", INDIRECT, "poetry.lock", None),
        "attrs": ("21.4.0", DIRECT, "poetry.lock", None),
        "black": ("22.12.0", DIRECT, "poetry.lock", None),
        "click": ("8.1.8", INDIRECT, "poetry.lock", None),
        "colorama": ("0.4.6", INDIRECT, "poetry.lock", None),  # markers per group
        "exceptiongroup": ("1.3.0", INDIRECT, "poetry.lock", None),
        "ipython": ("8.12.3", INDIRECT, "poetry.lock", None),
        "ipywidgets": ("8.1.7", DIRECT, "poetry.lock", None),  # the jupyter extra
        "markdown-it-py": ("3.0.0", DIRECT, "poetry.lock", None),
        "mdurl": ("0.1.2", INDIRECT, "poetry.lock", None),
        "mypy": ("1.14.1", DIRECT, "poetry.lock", None),
        "pre-commit": ("2.21.0", DIRECT, "poetry.lock", None),
        "pygments": ("2.19.2", DIRECT, "poetry.lock", None),  # "^2.13.0" pins nothing
        "pytest": ("7.4.4", DIRECT, "poetry.lock", None),
        "pytest-cov": ("3.0.0", DIRECT, "poetry.lock", None),
        "tomli": ("2.3.0", INDIRECT, "poetry.lock", None),
        "typing-extensions": ("4.13.2", DIRECT, "poetry.lock", None),
    },
)

# python-poetry/poetry: PEP 621 dependencies in Poetry's style ("cleo (>=2.1.0,<3.0.0)") next
# to [tool.poetry.group.*] groups, an optional one included.
POETRY = Golden(
    version_source="poetry.lock",
    python_version="3.10",
    dependencies={
        "build": ("1.6.1", DIRECT, "poetry.lock", None),
        "cachecontrol": ("0.14.4", DIRECT, "poetry.lock", None),
        "cleo": ("2.1.0", DIRECT, "poetry.lock", None),
        "dulwich": ("1.2.14", DIRECT, "poetry.lock", None),  # in [project] and the test group
        "filelock": ("3.32.6", INDIRECT, "poetry.lock", None),
        "findpython": ("0.8.0", DIRECT, "poetry.lock", None),
        "idna": ("3.19", INDIRECT, "poetry.lock", None),
        "keyring": ("25.7.0", DIRECT, "poetry.lock", None),
        "msgpack": ("1.2.2", INDIRECT, "poetry.lock", None),
        "mypy": ("2.3.1", DIRECT, "poetry.lock", None),
        "packaging": ("26.3", DIRECT, "poetry.lock", None),
        "pbs-installer": ("2026.9.1", DIRECT, "poetry.lock", None),
        "platformdirs": ("4.11.8", DIRECT, "poetry.lock", None),
        "poetry-core": ("2.5.0", DIRECT, "poetry.lock", None),
        "pre-commit": ("4.6.2", DIRECT, "poetry.lock", None),
        "pytest": ("9.1.1", DIRECT, "poetry.lock", None),
        "pytest-github-actions-annotate-failures": ("0.1.8", DIRECT, "poetry.lock", None),
        "pytest-xdist": ("3.8.0", DIRECT, "poetry.lock", None),
        # Locked twice: 3.14.5 below Python 3.15, 3.14.6 from 3.15. With no Python given, the
        # newest (see test_a_package_locked_once_per_python_range_gets_the_projects_version).
        "rapidfuzz": ("3.14.6", INDIRECT, "poetry.lock", None),
        "requests": ("2.34.2", DIRECT, "poetry.lock", None),
        "tomli": ("2.4.1", DIRECT, "poetry.lock", None),
        "trove-classifiers": ("2026.6.1.19", DIRECT, "poetry.lock", None),
        "types-requests": ("2.33.0.20260906", DIRECT, "poetry.lock", None),
        "virtualenv": ("21.7.9", DIRECT, "poetry.lock", None),
        "xattr": ("1.3.0", DIRECT, "poetry.lock", None),  # sys_platform == 'darwin'
    },
)

# --------------------------------------------------------------------------- pdm.lock
# pdm-project/pdm: pdm.lock does not say which packages are direct; [project], the extras and
# the dependency groups do. The lock leaves out the "template" extras (copier, cookiecutter).
PDM = Golden(
    version_source="pdm.lock, latest on PyPI for 2 unpinned",
    python_version="3.10",
    ranges={"copier": ">=8.0.0"},
    dependencies={
        "anyio": ("4.13.0", INDIRECT, "pdm.lock", None),
        "argcomplete": ("3.7.0", DIRECT, "pdm.lock", None),
        "blinker": ("1.9.0", DIRECT, "pdm.lock", None),
        "certifi": ("2026.4.22", DIRECT, "pdm.lock", None),
        "cookiecutter": (None, DIRECT, "unpinned", None),
        "copier": (None, DIRECT, "unpinned", None),
        "findpython": ("0.8.0", DIRECT, "pdm.lock", None),
        "hishel": ("1.2.1", DIRECT, "pdm.lock", None),  # locked again for "hishel[httpx]"
        "httpcore": ("1.0.9", DIRECT, "pdm.lock", None),
        "httpx": ("0.28.1", DIRECT, "pdm.lock", None),  # and for "httpx[socks]"
        "id": ("1.6.1", DIRECT, "pdm.lock", None),
        "idna": ("3.13", INDIRECT, "pdm.lock", None),
        "keyring": ("25.7.0", DIRECT, "pdm.lock", None),
        "mkdocstrings": ("1.0.4", DIRECT, "pdm.lock", None),
        "packaging": ("26.2", DIRECT, "pdm.lock", None),
        "parver": ("0.5", DIRECT, "pdm.lock", None),
        "platformdirs": ("4.9.6", DIRECT, "pdm.lock", None),
        "pytest": ("9.0.3", DIRECT, "pdm.lock", None),
        "pytest-cov": ("7.1.0", DIRECT, "pdm.lock", None),
        "rich": ("15.0.0", DIRECT, "pdm.lock", None),
        "setuptools": ("82.0.1", DIRECT, "pdm.lock", None),
        "socksio": ("1.0.0", INDIRECT, "pdm.lock", None),
        "tomli": ("2.4.1", DIRECT, "pdm.lock", None),
        "tox": ("4.53.1", DIRECT, "pdm.lock", None),
        "tox-pdm": ("0.7.2", DIRECT, "pdm.lock", None),
        "unearth": ("0.18.2", DIRECT, "pdm.lock", None),
        "virtualenv": ("21.3.1", DIRECT, "pdm.lock", None),
        "zensical": ("0.0.40", DIRECT, "pdm.lock", None),
    },
)

# ----------------------------------------------------------------------- Pipfile.lock
# bridgecrewio/checkov: [packages] and [dev-packages] are direct, the rest of the lock is not.
CHECKOV = Golden(
    version_source="Pipfile.lock",
    python_version=None,  # Pipfile's [requires] python_version is not read
    dependencies={
        "bc-jsonpath-ng": ("1.6.1", DIRECT, "Pipfile.lock", None),
        "boto3": ("1.35.49", DIRECT, "Pipfile.lock", None),
        "boto3-stubs-lite": ("1.43.74", DIRECT, "Pipfile.lock", None),  # extras = ["s3"]
        "botocore": ("1.35.99", INDIRECT, "Pipfile.lock", None),
        "colorama": ("0.4.6", DIRECT, "Pipfile.lock", None),
        "coverage": ("7.6.1", DIRECT, "Pipfile.lock", None),
        "dpath": ("2.1.3", DIRECT, "Pipfile.lock", None),
        "exceptiongroup": ("1.3.1", DIRECT, "Pipfile.lock", None),
        "idna": ("3.19", INDIRECT, "Pipfile.lock", None),
        "importlib-resources": ("6.5.2", DIRECT, "Pipfile.lock", None),
        "iniconfig": ("2.1.0", DIRECT, "Pipfile.lock", None),
        "jsonschema": ("4.25.1", DIRECT, "Pipfile.lock", None),  # in both sections
        "mypy": ("1.19.1", DIRECT, "Pipfile.lock", None),
        "networkx": ("2.6.3", DIRECT, "Pipfile.lock", None),
        "packaging": ("23.2", DIRECT, "Pipfile.lock", None),
        "pluggy": ("1.6.0", INDIRECT, "Pipfile.lock", None),
        "pycep-parser": ("0.5.1", DIRECT, "Pipfile.lock", None),
        # Markers for Python below 3.11 on x86-64 Linux and macOS, and an index.
        "pyston": ("2.3.5", DIRECT, "Pipfile.lock", None),
        "pyston-autoload": ("2.3.5", DIRECT, "Pipfile.lock", None),
        "pytest": ("7.4.4", DIRECT, "Pipfile.lock", None),
        "pytest-xdist": ("3.8.0", DIRECT, "Pipfile.lock", None),
        "requests": ("2.32.5", DIRECT, "Pipfile.lock", None),
        "schema": ("0.7.5", DIRECT, "Pipfile.lock", None),
        "setuptools": ("78.1.1", DIRECT, "Pipfile.lock", None),
        "six": ("1.17.0", INDIRECT, "Pipfile.lock", None),
        "tabulate": ("0.9.0", DIRECT, "Pipfile.lock", None),
        "termcolor": ("2.3.0", DIRECT, "Pipfile.lock", None),
        "tomli": ("2.4.1", DIRECT, "Pipfile.lock", None),
        "types-pyyaml": ("6.0.12.20250915", DIRECT, "Pipfile.lock", None),
        "urllib3": ("1.26.20", DIRECT, "Pipfile.lock", None),
        "yarl": ("1.22.0", DIRECT, "Pipfile.lock", None),
    },
)

# psf/requests-html: every Pipfile entry is "*", so the versions come from the lock alone. The
# project installs itself for its tests as `e1839a8 = {path = ".", editable = true}` (Pipenv
# names it after a hash; the lock calls it requests-html): that is not a dependency.
REQUESTS_HTML = Golden(
    version_source="Pipfile.lock",
    python_version=None,
    dependencies={
        "beautifulsoup4": ("4.11.2", INDIRECT, "Pipfile.lock", None),
        "bs4": ("0.0.1", DIRECT, "Pipfile.lock", None),
        "exceptiongroup": ("1.1.0", INDIRECT, "Pipfile.lock", None),
        "fake-useragent": ("1.1.1", DIRECT, "Pipfile.lock", None),
        "jeepney": ("0.8.0", INDIRECT, "Pipfile.lock", None),  # sys_platform == 'linux'
        "lxml": ("4.9.2", INDIRECT, "Pipfile.lock", None),
        "mypy": ("1.0.1", DIRECT, "Pipfile.lock", None),
        "parse": ("1.19.0", DIRECT, "Pipfile.lock", None),
        "pyppeteer": ("1.0.2", DIRECT, "Pipfile.lock", None),
        "pyquery": ("2.0.0", DIRECT, "Pipfile.lock", None),
        "pytest": ("7.2.1", DIRECT, "Pipfile.lock", None),
        "pytest-asyncio": ("0.20.3", DIRECT, "Pipfile.lock", None),
        "requests": ("2.28.2", DIRECT, "Pipfile.lock", None),
        "requests-file": ("1.5.1", DIRECT, "Pipfile.lock", None),
        "rfc3986": ("2.0.0", DIRECT, "Pipfile.lock", None),
        "sphinx": ("6.1.3", DIRECT, "Pipfile.lock", None),
        "tomli": ("2.0.1", INDIRECT, "Pipfile.lock", None),  # python_version < '3.11'
        "twine": ("4.0.2", DIRECT, "Pipfile.lock", None),
        "urllib3": ("1.26.14", INDIRECT, "Pipfile.lock", None),
        "w3lib": ("2.1.1", DIRECT, "Pipfile.lock", None),
        "white": ("0.1.2", DIRECT, "Pipfile.lock", None),
    },
)

# ---------------------------------------------------------------- requirements files
# psf/pypistats.org: pip-compile output, where "# via -r requirements.in" marks a direct
# dependency and "# via kombu" a transitive one, and a poetry.lock from 2020 that nothing uses:
# its versions and its lock-only packages (zipp) do not count.
PYPISTATS = Golden(
    version_source="requirements-dev.txt, requirements.txt",
    python_version="3.13",
    warnings=[
        "ignored poetry.lock: the project is not set up for Poetry, and 9 of its versions "
        "disagree with the pinned requirements (alembic 1.4.2 vs ==1.16.4, black 19.10b0 vs "
        "==26.5.1, celery 4.4.7 vs ==5.5.3, and 6 more)"
    ],
    dependencies={
        "alembic": ("1.16.4", INDIRECT, "pinned", None),
        "amqp": ("5.3.1", INDIRECT, "pinned", None),
        "black": ("26.5.1", DIRECT, "pinned", None),
        "celery": ("5.5.3", DIRECT, "pinned", None),
        "celery-redbeat": ("2.3.3", DIRECT, "pinned", None),
        "click": ("8.2.1", INDIRECT, "pinned", None),  # in both files, direct in neither
        "flask": ("3.1.1", DIRECT, "pinned", None),
        "flask-limiter": ("3.12", DIRECT, "pinned", None),
        "flask-migrate": ("4.1.0", DIRECT, "pinned", None),
        "flask-sqlalchemy": ("3.1.1", DIRECT, "pinned", None),
        "flower": ("2.0.1", DIRECT, "pinned", None),
        "google-api-core": ("2.25.1", INDIRECT, "pinned", None),  # "google-api-core[grpc]"
        "google-cloud-bigquery": ("3.35.1", DIRECT, "pinned", None),
        "greenlet": ("3.2.4", DIRECT, "pinned", None),
        "isort": ("6.0.1", DIRECT, "pinned", None),
        "kombu": ("5.5.4", INDIRECT, "pinned", None),
        "limits": ("5.5.0", INDIRECT, "pinned", None),
        "pip": ("25.2", INDIRECT, "pinned", None),  # pip-compile's "unsafe" section
        "pip-tools": ("7.5.0", DIRECT, "pinned", None),
        "redis": ("6.4.0", DIRECT, "pinned", None),
        "requests": ("2.32.4", DIRECT, "pinned", None),
        "setuptools": ("80.9.0", INDIRECT, "pinned", None),
        "sqlalchemy": ("2.0.43", INDIRECT, "pinned", None),
        "tornado": ("6.5.2", INDIRECT, "pinned", None),
        "typing-extensions": ("4.14.1", INDIRECT, "pinned", None),
        "vine": ("5.1.0", INDIRECT, "pinned", None),
        "wheel": ("0.45.1", INDIRECT, "pinned", None),
    },
)

# ------------------------------------------------------------ pyproject.toml, no lock
# facebookresearch/sam-audio: nothing is pinned, so each version is the latest on PyPI (in
# its declared range); the git references are not looked up on PyPI at all.
SAM_AUDIO = Golden(
    version_source=LATEST,
    python_version="3.11",
    transitive=False,
    ranges={"transformers": ">=4.54.0"},
    dependencies={
        "audiobox-aesthetics": (None, DIRECT, "unpinned", None),
        "dacvae": (
            None,
            DIRECT,
            "unpinned",
            "git+https://github.com/facebookresearch/dacvae.git",
        ),
        "einops": (None, DIRECT, "unpinned", None),
        "imagebind": (
            None,
            DIRECT,
            "unpinned",
            "git+https://github.com/facebookresearch/ImageBind.git",
        ),
        "laion-clap": (None, DIRECT, "unpinned", "git+https://github.com/lematt1991/CLAP.git"),
        "numpy": (None, DIRECT, "unpinned", None),
        "perception-models": (
            None,
            DIRECT,
            "unpinned",
            "git+https://github.com/facebookresearch/perception_models@unpin-deps",
        ),
        "pydub": (None, DIRECT, "unpinned", None),
        "torch": (None, DIRECT, "unpinned", None),
        "torchaudio": (None, DIRECT, "unpinned", None),
        "torchcodec": (None, DIRECT, "unpinned", None),
        "torchdiffeq": (None, DIRECT, "unpinned", None),
        "torchvision": (None, DIRECT, "unpinned", None),
        "transformers": (None, DIRECT, "unpinned", None),
    },
)

# unslothai/unsloth: one name declared in many extras, for some Pythons, platforms and CPUs.
# The declarations that can hold for the newest Python on Linux, Windows or macOS count; the
# newest pin wins, a plain PyPI requirement wins over direct URLs, and a name without a pin
# gets the range its declarations all allow. requires-python gives python_version 3.9 but
# does not choose the declarations (see test_pins_and_ranges_follow_the_projects_python).
UNSLOTH_TRITON_XPU = (
    "https://download.pytorch.org/whl/pytorch_triton_xpu-3.2.0-cp313-cp313-linux_x86_64.whl"
    "#sha256=814dccc8a07159e6eca74bed70091bc8fea2d9dd87b0d91845f9f38cde62f01c"
)
UNSLOTH = Golden(
    version_source="pyproject.toml, latest on PyPI for 20 unpinned",
    python_version="3.9",
    transitive=False,
    ranges={
        "click": ">=8.0",
        "cryptography": ">=42.0.0",  # not <=46.0.3: that one is for Windows on ARM only
        "diffusers": ">=0.39.0",
        "flash-attn": ">=2.6.3",
        "hf-xet": "<2.0,>=1.5.2",
        "huggingface-hub": "<2.0,>=0.34.0,>=1.31.0",
        "soundfile": ">=0.12.1",
        "structlog": ">=24.1.0",
        # Neither audio extra's markers hold for the newest Python, so both count, and no
        # version is in both ranges: the scan then takes the latest release.
        "torchcodec": "<0.12.0,<0.8.0,>=0.11.0,>=0.6.0",
        "transformers": (
            "!=4.52.0,!=4.52.1,!=4.52.2,!=4.52.3,!=4.53.0,!=4.54.0,!=4.55.0,!=4.55.1,!=4.57.0,"
            "!=4.57.4,!=4.57.5,!=5.0.0,!=5.1.0,<=5.5.0,>=4.51.3"
        ),
        "triton": ">=3.0.0",
        "trl": "!=0.19.0,<=0.24.0,>=0.18.2",
        "unsloth-zoo": ">=2026.9.7",
        "xformers": ">=0.0.22.post7",  # its direct URLs do not count
    },
    dependencies={
        "bitsandbytes": ("0.45.5", DIRECT, "pinned", None),
        "click": (None, DIRECT, "unpinned", None),
        "cryptography": (None, DIRECT, "unpinned", None),
        "datasets": ("4.3.0", DIRECT, "pinned", None),  # pinned in studio, a range elsewhere
        "diffusers": (None, DIRECT, "unpinned", None),
        "fastapi": ("0.141.1", DIRECT, "pinned", None),  # the pin for Python 3.10 and later
        "flash-attn": (None, DIRECT, "unpinned", None),
        "hf-transfer": (None, DIRECT, "unpinned", None),
        "hf-xet": (None, DIRECT, "unpinned", None),
        "huggingface-hub": (None, DIRECT, "unpinned", None),  # ==0.36.2 is for Python 3.9
        "nest-asyncio": ("1.6.0", DIRECT, "pinned", None),  # "nest_asyncio==1.6.0"
        "ninja": (None, DIRECT, "unpinned", None),
        "numpy": (None, DIRECT, "unpinned", None),
        "packaging": ("26.3", DIRECT, "pinned", None),
        "pandas": ("2.3.3", DIRECT, "pinned", None),  # >=3.0 is for Windows on ARM only
        "pydantic": ("2.13.4", DIRECT, "pinned", None),
        "pymupdf": ("1.27.2.3", DIRECT, "pinned", None),
        "pymupdf4llm": ("0.3.4", DIRECT, "pinned", None),
        # Declared by direct URL only, for Python 3.12 and 3.13: the last one is reported.
        "pytorch-triton-xpu": (None, DIRECT, "unpinned", UNSLOTH_TRITON_XPU),
        "pyyaml": (None, DIRECT, "unpinned", None),
        "rich": (None, DIRECT, "unpinned", None),
        "soundfile": (None, DIRECT, "unpinned", None),
        "sqlite-vec": ("0.1.9", DIRECT, "pinned", None),
        "structlog": (None, DIRECT, "unpinned", None),
        # ==2.10.0, ==2.12.1+cu126, ==2.12.1+cu130 and direct URLs: the newest pin.
        "torch": ("2.12.1+cu130", DIRECT, "pinned", None),
        "torchaudio": ("2.11.0+cu130", DIRECT, "pinned", None),
        "torchcodec": (None, DIRECT, "unpinned", None),
        "torchvision": ("0.27.1+cu130", DIRECT, "pinned", None),
        "transformers": (None, DIRECT, "unpinned", None),
        "triton": (None, DIRECT, "unpinned", None),
        "triton-windows": (None, DIRECT, "unpinned", None),  # >=3.8.0.post28 is for ARM64
        "trl": (None, DIRECT, "unpinned", None),
        "typer": ("0.27.1", DIRECT, "pinned", None),
        "unsloth-zoo": (None, DIRECT, "unpinned", None),
        "xformers": (None, DIRECT, "unpinned", None),
    },
)

GOLDEN = {
    "private-gpt": PRIVATE_GPT,
    "python-sdk": PYTHON_SDK,
    "rich": RICH,
    "poetry": POETRY,
    "pdm": PDM,
    "checkov": CHECKOV,
    "requests-html": REQUESTS_HTML,
    "pypistats": PYPISTATS,
    "sam-audio": SAM_AUDIO,
    "unsloth": UNSLOTH,
}


@pytest.mark.parametrize("name", GOLDEN)
def test_what_a_scan_reads_from_a_real_project(tmp_path, name):
    golden = GOLDEN[name]
    project = load(tmp_path, name)
    assert table(project) == golden.dependencies
    assert ranges(project) == golden.ranges
    assert project.version_source == golden.version_source
    assert project.warnings == golden.warnings
    assert project.transitive is golden.transitive
    assert project.python_version == golden.python_version
    assert project.resolution_python is None


# -------------------------------------------------- for the project's Python, anywhere
def test_a_package_locked_once_per_python_range_gets_the_projects_version(tmp_path):
    # Poetry locks rapidfuzz 3.14.5 for Python below 3.15 and 3.14.6 from 3.15. For a project
    # on Python 3.12 the lock's answer is 3.14.5; the last entry, 3.14.6, was reported.
    expected = {**POETRY.dependencies, "rapidfuzz": ("3.14.5", INDIRECT, "poetry.lock", None)}
    assert table(load(tmp_path / "flag", "poetry", python="3.12")) == expected
    root = tmp_path / "file" / "poetry"
    shutil.copytree(FIXTURES / "poetry", root)
    (root / ".python-version").write_text("3.12.7\n", encoding="utf-8")
    assert table(load_project(root)) == expected


# What changes for Python 3.9: the pins for Python before 3.10, and the ranges of the
# declarations for 3.9 only.
UNSLOTH_39 = {
    **UNSLOTH.dependencies,
    "fastapi": ("0.128.8", DIRECT, "pinned", None),
    "huggingface-hub": ("0.36.2", DIRECT, "pinned", None),
    "typer": ("0.23.2", DIRECT, "pinned", None),
}
UNSLOTH_39_RANGES = {
    **{k: v for k, v in UNSLOTH.ranges.items() if k not in ("diffusers", "huggingface-hub")},
    "torchcodec": "<0.8.0,>=0.6.0",  # the audio-torch280 extra's, for Python 3.9 to 3.13
}


def test_pins_and_ranges_follow_the_projects_python(tmp_path):
    project = load(tmp_path, "unsloth", python="3.9")
    assert table(project) == UNSLOTH_39
    assert ranges(project) == UNSLOTH_39_RANGES
    assert project.version_source == "pyproject.toml, latest on PyPI for 19 unpinned"


# The machines a scan runs on: CI runners and developers' laptops.
HOSTS = {
    "linux-x86_64": ("linux", "Linux", "posix", "x86_64"),
    "linux-aarch64": ("linux", "Linux", "posix", "aarch64"),
    "windows-amd64": ("win32", "Windows", "nt", "AMD64"),
    "windows-arm64": ("win32", "Windows", "nt", "ARM64"),
    "macos-arm64": ("darwin", "Darwin", "posix", "arm64"),
    "macos-x86_64": ("darwin", "Darwin", "posix", "x86_64"),
}


@pytest.fixture(params=HOSTS)
def host(request, monkeypatch):
    """Markers see this machine wherever they would see the one the tests run on."""
    platform, system, os_name, machine = HOSTS[request.param]
    fake = {
        "sys_platform": platform,
        "platform_system": system,
        "os_name": os_name,
        "platform_machine": machine,
    }
    real = packaging.markers.default_environment

    def environment() -> dict[str, str]:
        return {**real(), **fake}

    monkeypatch.setattr(packaging.markers, "default_environment", environment)
    monkeypatch.setattr(project_module, "default_environment", environment)
    return request.param


@pytest.mark.parametrize(
    ("name", "python", "expected", "expected_ranges"),
    [
        pytest.param("private-gpt", "3.11", PRIVATE_GPT.dependencies, {}, id="private-gpt"),
        pytest.param("unsloth", None, UNSLOTH.dependencies, UNSLOTH.ranges, id="unsloth"),
        pytest.param("unsloth", "3.9", UNSLOTH_39, UNSLOTH_39_RANGES, id="unsloth-3.9"),
    ],
)
def test_the_same_versions_on_every_machine(
    tmp_path, host, name, python, expected, expected_ranges
):
    # Markers were evaluated for the machine running the scan. With --python 3.11, a Windows
    # machine got private-gpt's Windows fork of onnxruntime (1.20.1) and the others 1.25.1;
    # on ARM, unsloth's triton-windows took the range of its Windows-on-ARM declaration.
    project = load(tmp_path, name, python)
    assert table(project) == expected
    assert ranges(project) == expected_ranges


# ------------------------------------------------------------------- the fixtures
@pytest.mark.parametrize("name", GOLDEN)
def test_windows_line_endings_read_the_same(tmp_path, name):
    # A checkout with core.autocrlf, or files written on Windows.
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root)
    for path in root.iterdir():
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    project = load_project(root)
    assert table(project) == GOLDEN[name].dependencies
    assert ranges(project) == GOLDEN[name].ranges
    assert project.warnings == GOLDEN[name].warnings


def test_every_fixture_has_a_golden_test_and_names_its_source():
    fixtures = {p.name for p in FIXTURES.iterdir() if p.is_dir()}
    assert fixtures == set(GOLDEN)
    for name in fixtures:
        source = (FIXTURES / name / "SOURCE.md").read_text(encoding="utf-8")
        assert re.search(r"https://github\.com/[\w.-]+/[\w.-]+", source), name
        assert re.search(r"Commit: \[`[0-9a-f]{40}`\]", source), name
        assert re.search(r"^- License: \S", source, re.M), name


def test_the_fixtures_stay_small():
    # They ship in the sdist.
    size = sum(p.stat().st_size for p in FIXTURES.rglob("*") if p.is_file())
    assert size < 300_000
