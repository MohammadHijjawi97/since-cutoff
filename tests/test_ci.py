"""CI and releases: the Markdown summary, CI flags, the GitHub Action, the hook, versions."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from since_cutoff import __version__, cli
from since_cutoff import engine as engine_module
from since_cutoff.apidiff import DEPRECATED, PARAM_REMOVED, REMOVED, APIChange
from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine, ScanResult, Settings
from since_cutoff.errors import ProviderError
from since_cutoff.mcp_server import Tools
from since_cutoff.models import ModelRegistry
from since_cutoff.project import FileUse, load_project, scan_file
from since_cutoff.providers.base import Completion
from since_cutoff.report import (
    render_console,
    render_markdown,
    render_scan_changes,
    render_scan_markdown,
    summary,
)
from since_cutoff.selection import project_rank, usage, used_name, used_names, uses_text
from tests.conftest import ScriptedModel

ROOT = Path(__file__).resolve().parents[1]


def make_app(tmp_path: Path, pin: str = "toylib==2.0") -> Path:
    root = tmp_path / "app"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = ["{pin}"]\n'
    )
    (root / "main.py").write_text(
        "from toylib import Client\nClient().send('hi', temperature=0.2)\n"
    )
    return root


def scan_app(root: Path, cache: DiskCache, fake_pypi: Any, cutoff: date) -> ScanResult:
    engine = Engine(
        Settings(model="anthropic:claude-sonnet-4-5", cutoff=cutoff),
        store=cache,
        llm_cache=cache,
        pypi=fake_pypi,
    )
    return engine.scan(load_project(root), engine.resolve_target(allow_calls=False))


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: Any) -> None:
    """Make the CLI use the fake PyPI and a throwaway cache."""
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))

    def engine(settings: Settings, **kwargs: Any) -> Engine:
        return Engine(settings, **{**kwargs, "pypi": fake_pypi})

    monkeypatch.setattr(cli, "Engine", engine)


# ------------------------------------------------------------ Markdown summary
def test_markdown_summary_lists_what_the_code_uses_first(tmp_path, cache, fake_pypi):
    scan = scan_app(make_app(tmp_path), cache, fake_pypi, date(2025, 7, 31))
    md = render_scan_markdown(scan, limit=3)

    assert md.startswith("## since-cutoff scan\n")
    assert (
        "Model `claude-sonnet-4-5`, training cutoff **2025-07-31**, comparing from releases up "
        "to **2025-07-01** (source: --cutoff)"
    ) in md
    assert "- 1 of 1 dependency changed its API after the cutoff" in md
    assert "- Static diff: 5 breaking changes, 1 new deprecation" in md
    # The same columns, in the same order, as the terminal table and report.md.
    assert "| package | you use | at cutoff | status | breaking | deprecated |" in md
    assert (
        "| toylib | 2.0 (2025-10-01) | 1.0 (2025-01-10) | API changed, imported by your code | 5 | 1 |"
        in md
    )
    # The APIs the code uses lead (tests/test_scan_first.py); each package's changes follow,
    # folded, those that hit the project's own code first: the call with the removed
    # parameter, then another change to the same method.
    assert md.index("**Your code uses 1 API that changed after the cutoff**") < md.index("<details")
    assert (
        "<details><summary><b>toylib</b> 1.0 -> 2.0: 5 breaking, 1 deprecated, "
        "2 touching names your code uses</summary>" in md
    )
    listing = md.split("<b>toylib</b>")[1]
    items = [line for line in listing.splitlines() if line.startswith("- `")]
    assert len(items) == 3
    assert "parameter `temperature` was removed" in items[0]
    assert items[0].endswith("**your code uses `send` and `temperature`**")
    assert "(also changed under 1 other path)" in items[0]  # the AsyncClient twin is folded in
    assert "`stream` is now keyword-only" in items[1]
    assert items[1].endswith("**your code uses `send`**")
    assert "- ... and 3 more in the full report" in md  # 3 listed + 3 more = 5 + 1
    assert "A change reachable under several import paths is counted once." in md
    assert "(toylib 2.0)" not in md  # versions are in the section header
    assert md.rstrip().endswith(
        "Behaviour changes behind an unchanged signature are not shown.</sub>"
    )


def test_markdown_summary_for_new_and_known_dependencies(tmp_path, cache, fake_pypi):
    root = make_app(tmp_path, pin="toylib==1.0")
    known = render_scan_markdown(scan_app(root, cache, fake_pypi, date(2025, 7, 31)))
    assert "| package |" not in known and "<details" not in known
    assert "Not flagged (released before the cutoff, or no breaking changes): toylib 1.0" in known

    new = render_scan_markdown(scan_app(root, cache, fake_pypi, date(2024, 1, 1)))
    # The status says it; the reason, the same words, is not repeated after it.
    assert "| toylib | 1.0 (2025-01-10) | - | first released after the cutoff |" in new


def code(text: str) -> tuple[FileUse, ...]:
    """What one file with this code reaches through its imports."""
    return (scan_file(ast.parse(textwrap.dedent(text))),)


def test_a_common_method_name_needs_its_class_in_the_code() -> None:
    def change(kind: str, path: str, owner: str | None, parameter: str | None = None) -> APIChange:
        name = path.rsplit(".", 1)[-1]
        return APIChange("sdk", "1", "2", kind, path, name, owner, parameter)

    ids = code(
        """
        from sdk import Session, hf_hub_download
        client.messages.create(temperature=0.2)
        hf_hub_download("repo")
        Session()
        """
    )
    # client.messages.create(...): `messages` stands for the Messages resource.
    assert used_name(change(DEPRECATED, "sdk.Messages.create", "Messages"), ids) == "create"
    assert used_name(change(DEPRECATED, "sdk.Assistants.create", "Assistants"), ids) is None
    # A parameter counts only with its callable (and the callable's class) named in the code.
    removed = change(PARAM_REMOVED, "sdk.Messages.create", "Messages", "temperature")
    assert used_names(removed, ids) == ("create", "temperature")
    assert used_name(change(PARAM_REMOVED, "sdk.Runs.create", "Runs", "temperature"), ids) is None
    assert used_name(change(REMOVED, "sdk.hf_hub_download", None), ids) == "hf_hub_download"
    assert used_name(change(REMOVED, "sdk.Session.__init__", "Session"), ids) == "Session"


def test_a_parameter_counts_only_with_its_callable() -> None:
    def removed(path: str, parameter: str) -> APIChange:
        return APIChange(
            "hub", "1", "2", PARAM_REMOVED, path, path.rsplit(".", 1)[-1], None, parameter
        )

    # hf_hub_download(repo, "config.json", resume_download=True) is the only call in the code.
    ids = code(
        "from hub import hf_hub_download\n"
        "hf_hub_download(repo, 'config.json', resume_download=True)\n"
    )
    snapshot = removed("hub.snapshot_download", "resume_download")
    stale = removed("hub.hf_hub_download", "resume_download")
    other = removed("hub.hf_hub_download", "force_filename")
    assert used_name(snapshot, ids) is None  # it was marked "resume_download" before
    assert [usage(c, ids) for c in (stale, other, snapshot)] == [2, 1, 0]
    assert uses_text(stale, ids) == "`hf_hub_download` and `resume_download`"
    assert uses_text(other, ids) == "`hf_hub_download`"
    # The call the code really makes ranks first, whatever the scores say.
    ranked = sorted([snapshot, other, stale], key=lambda c: project_rank(c, ids))
    assert ranked == [stale, other, snapshot]


@pytest.mark.parametrize("width", [40, 60, 80, 100, 200])
def test_console_headline_renders_in_full(tmp_path, cache, fake_pypi, width):
    scan = scan_app(make_app(tmp_path), cache, fake_pypi, date(2025, 7, 31))
    console = Console(width=width, soft_wrap=True, record=True, force_terminal=True)
    render_console(console, scan)
    lines = console.export_text().splitlines()
    assert all(len(line) <= width for line in lines)  # wrapped, never cropped at the edge
    body = " ".join(line.strip("│ ") for line in lines if line.startswith("│"))
    assert "1 of 1 dependency changed its API after the cutoff" in body
    assert "Static diff: 5 breaking changes, 1 new deprecation" in body
    assert "(" not in body and ")" not in body  # no parenthesis a wrap could split
    if width >= 80:  # room for the subtitle: it is not cut
        assert any("versions from pyproject.toml ─" in line for line in lines)


# ------------------------------------------------------------------------ CLI
def test_scan_writes_markdown_and_fails_on_changes(tmp_path, capsys, fake_cli):
    root = make_app(tmp_path)
    summary = tmp_path / "out" / "summary.md"
    argv = ["scan", str(root), "--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]

    assert cli.main([*argv, "--markdown", str(summary)]) == 0
    assert "<b>toylib</b> 1.0 -> 2.0" in summary.read_text(encoding="utf-8")
    assert "Markdown summary:" in capsys.readouterr().out
    assert (root / ".since-cutoff" / "results.json").exists()

    assert cli.main([*argv, "--fail-on-changes"]) == 3
    known = make_app(tmp_path / "known", pin="toylib==1.0")
    assert cli.main(["scan", str(known), *argv[2:], "--fail-on-changes"]) == 0


def test_scan_markdown_to_stdout_keeps_the_console_on_stderr(tmp_path, capsys, fake_cli):
    root = make_app(tmp_path)
    argv = ["scan", str(root), "--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
    assert cli.main([*argv, "--markdown", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("## since-cutoff scan\n")
    assert "Full report" in captured.err

    assert cli.main([*argv, "--markdown", "-", "--json"]) == 2
    assert "cannot both write to stdout" in capsys.readouterr().err


def test_every_report_counts_the_same_changes(tmp_path, cache, fake_pypi):
    root = make_app(tmp_path)
    scan = scan_app(root, cache, fake_pypi, date(2025, 7, 31))
    [toylib] = scan.changed
    # 7 diff entries, 6 distinct changes: `Client.send` and `AsyncClient.send` lose the same
    # parameters, and `legacy_fetch` is reachable as `toylib.legacy_fetch` and
    # `toylib.helpers.legacy_fetch`.
    assert len(toylib.changes) == 7 and toylib.counts == (5, 1)

    s = summary(scan)
    assert (s["breaking_changes"], s["deprecations"]) == (5, 1)
    assert (s["packages"][0]["breaking_changes"], s["packages"][0]["deprecations"]) == (5, 1)

    md = render_scan_markdown(scan, limit=100)
    assert "- Static diff: 5 breaking changes, 1 new deprecation" in md
    assert "| API changed, imported by your code | 5 | 1 |" in md
    assert "<b>toylib</b> 1.0 -> 2.0: 5 breaking, 1 deprecated" in md
    listing = md.split("<b>toylib</b>")[1]  # after the notes block, which has bullets too
    assert sum(line.startswith("- `") for line in listing.splitlines()) == 6

    full = render_markdown(scan).split("## All changes found")[1]
    assert "### toylib 1.0 -> 2.0: 5 breaking, 1 deprecated" in full
    assert sum(line.startswith("- `") for line in full.splitlines()) == 6

    console = Console(width=200, record=True)
    render_scan_changes(console, scan, limit=4)
    text = console.export_text()
    assert "toylib 1.0 -> 2.0: 5 breaking, 1 deprecated" in text and "... 2 more" in text

    # The MCP tools count the same way.
    tools = Tools(cache, registry=ModelRegistry(cache, offline=True), pypi=fake_pypi)
    assert "5 breaking, 1 deprecated." in tools.project_changes(str(root), cutoff="2025-07-31")
    assert "- 5 breaking changes, 1 new deprecation (" in tools.api_changes(
        "toylib", cutoff="2025-07-31"
    )


def test_scan_with_only_a_cutoff_names_no_model(tmp_path, capsys, fake_cli, monkeypatch):
    def no_model(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cutoff alone must not look up, guess or ask for a model")

    monkeypatch.setattr(engine_module.Engine, "resolve_target", no_model)
    monkeypatch.setattr(engine_module, "_claude_settings_model", no_model)
    monkeypatch.setattr(cli, "detect_model", no_model)
    root = make_app(tmp_path)
    summary_md = tmp_path / "summary.md"
    argv = ["scan", str(root), "--cutoff", "2025-02-28", "--markdown", str(summary_md)]
    assert cli.main(argv) == 0

    out = capsys.readouterr().out
    assert (
        "Custom cutoff 2025-02-28, comparing from releases up to 2025-01-29 "
        "(from --cutoff; no model given)" in out
    )
    assert "Your code uses 1 API that changed after the cutoff (2025-02-28)" in out
    assert cli.main([*argv, "--all"]) == 0  # 0.3's summary panel
    assert "since-cutoff · custom cutoff 2025-02-28" in capsys.readouterr().out
    assert "sonnet" not in out and "assuming" not in out
    md = summary_md.read_text(encoding="utf-8")
    assert (
        "\nCustom cutoff **2025-02-28**, comparing from releases up to **2025-01-29** (given with --cutoff, no model) · project `app`"
        in md
    )
    report = (root / ".since-cutoff" / "report.md").read_text(encoding="utf-8")
    assert (
        "- Custom cutoff **2025-02-28**, comparing from releases up to **2025-01-29** (given with --cutoff, no model)"
        in report
    )
    data = json.loads((root / ".since-cutoff" / "results.json").read_text(encoding="utf-8"))
    assert (data["model"], data["model_spec"], data["cutoff"]) == (None, None, "2025-02-28")


# ------------------------------------------------ GitHub Action and pre-commit
def _read(name: str) -> str:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this checkout")
    return path.read_text(encoding="utf-8")


def test_action_pins_this_release_and_declares_its_interface() -> None:
    action = _read("action.yml")
    default = re.search(r"since-cutoff-version:\n(?:    .*\n)*?    default: \"(.+)\"", action)
    assert default is not None and default.group(1) == __version__
    for name in ("model", "working-directory", "only", "exclude", "fail-on-changes"):
        assert f"\n  {name}:\n" in action
    for name in ("changed-packages", "changes", "deprecations", "markdown", "report"):
        assert f"\n  {name}:\n" in action
    assert "branding:" in action
    # Inputs reach the script through the environment, never pasted into the shell code.
    run_blocks = re.findall(r"run: \|\n((?:        .*\n|\n)+)", action)
    assert run_blocks and not any("${{" in block for block in run_blocks)
    # The Marketplace limits the description to 125 characters.
    description = re.search(r"^description: (.+)$", action, re.M)
    assert description is not None and len(description.group(1).strip("\"'")) < 125


def test_action_saves_its_cache_before_it_fails() -> None:
    action = _read("action.yml")
    steps = action.split("\n    - name: ")[1:]
    names = [step.split("\n", 1)[0] for step in steps]
    assert names == [
        "Install uv",
        "Restore the since-cutoff cache",
        "Scan the dependencies",
        "Save the since-cutoff cache",
        "Report the result",
    ]
    _, restore, scan, save, report = steps
    # actions/cache saves only after a successful job: fail-on-changes would lose the cache.
    assert "uses: actions/cache/restore@" in restore and "uses: actions/cache/save@" in save
    assert "uses: actions/cache@" not in action
    assert "key: ${{ steps.cache.outputs.cache-primary-key }}" in save
    # The scan records its exit code and succeeds; the last step fails the job with that code.
    assert 'echo "exit-code=$status" >> "$GITHUB_OUTPUT"' in scan
    assert scan.rstrip().endswith("exit 0")
    assert "steps.scan.outputs.exit-code != '0'" in report
    assert 'exit "$SC_STATUS"' in report and "::error title=since-cutoff::" in report


# ------------------------------------------ workflows (OpenSSF Scorecard checks)
_USES = re.compile(r"^ *(?:- )?uses: ([^@\s]+)@?(\S*)(.*)$", re.M)  # action, ref, comment


def _workflows() -> dict[str, str]:
    folder = ROOT / ".github" / "workflows"
    if not folder.is_dir():
        pytest.skip(".github/workflows is not part of this checkout")
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(folder.glob("*.yml"))}


def _jobs(workflow: str) -> dict[str, str]:
    """Each job of a workflow, by name, with its lines."""
    parts = re.split(r"^  ([\w-]+):\n", workflow.split("\njobs:\n", 1)[1], flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_every_action_is_pinned_to_a_commit() -> None:
    # A tag or a branch can be moved to other code, a commit cannot (Pinned-Dependencies). The
    # composite action matters most: it runs in other people's jobs, with their token.
    files = {**_workflows(), "action.yml": _read("action.yml")}
    pins: dict[str, set[str]] = {}
    for name, text in files.items():
        for action, ref, comment in _USES.findall(text):
            if action.startswith("./"):
                continue  # this checkout's own action
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{name}: {action}@{ref} is not a commit"
            # The release it is, which Dependabot updates along with the commit.
            assert re.fullmatch(r" +# v\d+\.\d+\.\d+", comment), f"{name}: {action} has no version"
            pins.setdefault("/".join(action.split("/")[:2]), set()).add(ref + comment.strip())
    assert {"actions/checkout", "actions/cache", "pypa/gh-action-pypi-publish"} <= pins.keys()
    # One commit per repository everywhere: no file is left behind by an update.
    assert {repo: len(refs) for repo, refs in pins.items()} == dict.fromkeys(pins, 1)


def test_tokens_are_read_only_unless_a_job_needs_to_write() -> None:
    # Token-Permissions: a workflow's default token only reads, and the few jobs that write say so.
    writes: set[tuple[str, str, str]] = set()
    for name, text in _workflows().items():
        top = re.search(r"^permissions:(.*)\n((?:  .*\n)*)", text, re.M)
        assert top is not None, f"{name} leaves its token's permissions to the repository settings"
        assert "write" not in top.group(0), f"{name}: {top.group(0)}"
        for job, lines in _jobs(text).items():
            scopes = re.findall(r"^ +([\w-]+): write$", lines, re.M)
            writes |= {(name, job, scope) for scope in scopes}
    assert writes == {
        ("release.yml", "publish", "id-token"),  # PyPI trusted publishing
        ("release.yml", "mcp-registry", "id-token"),  # mcp-publisher login github-oidc
        ("release.yml", "github-release", "contents"),  # gh release create
        ("scorecard.yml", "analysis", "id-token"),  # publish_results
        ("scorecard.yml", "analysis", "security-events"),  # the SARIF upload to code scanning
    }
    # The pending publisher on pypi.org trusts release.yml in the `pypi` environment only.
    publish = _jobs(_workflows()["release.yml"])["publish"]
    assert "\n    environment: pypi\n" in publish
    assert "uses: pypa/gh-action-pypi-publish@" in publish


def test_scorecard_can_publish_its_results() -> None:
    # The Scorecard API refuses the results (and the badge goes stale) unless the workflow has
    # no env, defaults or write permissions of its own, and the job runs approved actions only,
    # no `run` step, no env, defaults, container or services, on an Ubuntu runner.
    workflow = _workflows()["scorecard.yml"]
    assert re.findall(r"^([\w-]+):", workflow, re.M) == ["name", "on", "permissions", "jobs"]
    assert "\npermissions: read-all\n" in workflow
    # Branch-Protection is checked when a rule changes, and Maintained at least once a week.
    assert re.search(r"^  push:\n    branches: \[main\]\n", workflow, re.M)
    assert "\n  branch_protection_rule:\n" in workflow and "\n  schedule:\n    - cron: " in workflow
    [job] = _jobs(workflow).values()
    assert "\n    runs-on: ubuntu-latest\n" in job
    assert not re.search(r"^    (env|defaults|container|services):", job, re.M)
    assert not re.search(r"^ +(- )?run:", job, re.M)
    actions = {action for action, _, _ in _USES.findall(job)}
    assert "ossf/scorecard-action" in actions
    assert actions <= {
        "actions/checkout",
        "actions/upload-artifact",
        "github/codeql-action/upload-sarif",
        "ossf/scorecard-action",
        "step-security/harden-runner",
    }
    assert "\n          publish_results: true\n" in job


@pytest.mark.parametrize(
    ("path", "hit"),
    [
        ("uv.lock", True),
        ("sub/poetry.lock", True),
        ("pdm.lock", True),
        ("pylock.toml", True),
        ("pylock.web.toml", True),
        ("Pipfile.lock", True),
        ("requirements.txt", True),
        ("requirements-dev.txt", True),
        ("requirements/prod.txt", True),
        ("pyproject.toml", True),
        ("src/app/main.py", False),
        ("README.md", False),
        ("my-requirements.txt.bak", False),
    ],
)
def test_pre_commit_hook_runs_when_dependencies_change(path: str, hit: bool) -> None:
    manifest = _read(".pre-commit-hooks.yaml")
    assert "- id: since-cutoff-scan" in manifest
    assert "entry: since-cutoff scan" in manifest
    assert "pass_filenames: false" in manifest
    files = re.search(r"^\s*files: '(.+)'$", manifest, re.M)
    assert files is not None
    assert bool(re.search(files.group(1), path)) is hit


def _check_versions() -> Any:
    path = ROOT / "scripts" / "check_versions.py"
    if not path.exists():
        pytest.skip("scripts/check_versions.py is not part of this checkout")
    spec = importlib.util.spec_from_file_location("check_versions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_version_field_names_this_release(capsys) -> None:
    check = _check_versions()
    found = check.versions()
    # The package, server.json (twice), the Claude Code, Agent Plugins, Codex and Gemini
    # manifests, the skill's metadata.version, both MCP launcher pins, CITATION.cff, the action's
    # default, the changelog and the README pins; the plugin's SessionStart hook is checked on
    # its own (see below).
    assert len(found) == 17
    assert dict.fromkeys(found, __version__) == found
    assert check.problems() == []  # including CITATION's date = the changelog's release date
    assert check.main(["check_versions.py", f"v{__version__}"]) == 0

    # A tag that does not match fails the release, naming every field, and the hook a release
    # from 0.4.0 on must have.
    assert check.main(["check_versions.py", "v99.0.0"]) == 1
    out = capsys.readouterr().out
    assert f"gemini-extension.json version: {__version__} (expected 99.0.0)" in out
    assert f"SKILL.md metadata.version: {__version__} (expected 99.0.0)" in out
    assert out.count("(expected 99.0.0") == len(found) + 1


def test_the_skill_version_is_its_frontmatter_metadata(tmp_path, monkeypatch) -> None:
    """The release check reads `version` under `metadata:` in the skill's frontmatter, quoted
    (the spec's metadata maps strings to strings), and nowhere else."""
    check = _check_versions()
    label = "skills/since-cutoff/SKILL.md metadata.version"
    assert check.versions()[label] == __version__
    monkeypatch.setattr(check, "ROOT", tmp_path)
    skill = tmp_path / "skills" / "since-cutoff" / "SKILL.md"
    skill.parent.mkdir(parents=True)

    def read(text: str) -> str | None:
        skill.write_text(text, encoding="utf-8")
        return check.versions()[label]

    meta = 'metadata:\n  author: Someone\n  version: "{}"\n'
    body = "\n# since-cutoff\n"
    assert read(f"---\nname: since-cutoff\n{meta.format('0.5.0')}---\n{body}") == "0.5.0"
    assert read(f"---\n{meta.format('1.0.0rc1')}name: since-cutoff\n---\n") == "1.0.0rc1"
    # A top-level version (not in the spec), one in the body, an unquoted one: none is the skill's.
    assert read('---\nname: since-cutoff\nversion: "0.5.0"\n---\n') is None
    assert read(f"---\nname: since-cutoff\n---\n\n{meta.format('0.5.0')}") is None
    assert read("---\nname: since-cutoff\nmetadata:\n  version: 0.5.0\n---\n") is None
    assert read("# since-cutoff\n") is None


def _skill_frontmatter(text: str) -> tuple[dict[str, str], dict[str, str], str]:
    """The top-level fields, the `metadata` entries and the body of a SKILL.md whose frontmatter
    is flat `key: value` lines, as the Agent Skills spec's fields are."""
    front = re.match(r"---\n(.*?\n)---\n(.*)", text, re.DOTALL)
    assert front is not None, "SKILL.md starts with YAML frontmatter"
    fields: dict[str, str] = {}
    metadata: dict[str, str] = {}
    for line in front.group(1).splitlines():
        entry = re.fullmatch(r"(  )?([\w-]+):(?: (.*))?", line)
        assert entry is not None, line
        indent, key, value = entry.groups()
        if indent:
            assert list(fields)[-1] == "metadata", line
            metadata[key] = (value or "").strip('"')
        else:
            fields[key] = value or ""
    return fields, metadata, front.group(2)


def test_the_skill_follows_the_agent_skills_spec() -> None:
    """claude.ai skill uploads, the Skills API and the spec's reference validator (skills-ref)
    reject any top-level field but these six ("Unexpected key(s) in SKILL.md frontmatter:
    argument-hint"). Skill catalogues read the frontmatter as data, where backticks and `$(`
    look like shell substitution, and look for the body's standard sections."""
    path = ROOT / "skills" / "since-cutoff" / "SKILL.md"
    if not path.exists():
        pytest.skip("the skill is not part of this checkout")
    fields, metadata, body = _skill_frontmatter(path.read_text(encoding="utf-8"))
    spec = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert set(fields) <= spec, set(fields) - spec
    assert fields["name"] == path.parent.name
    assert 0 < len(fields["description"]) <= 1024
    assert 0 < len(fields["compatibility"]) <= 500
    assert not [k for k, v in fields.items() if re.search(r"`|\$\(|\$\{", v)]
    assert set(metadata) == {"author", "version"} and metadata["version"] == __version__
    outside_code = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    headings = re.findall(r"^## (.+)$", outside_code, re.MULTILINE)
    standard = [
        "Overview",
        "Prerequisites",
        "Instructions",
        "Output",
        "Error Handling",
        "Examples",
        "Resources",
    ]
    assert [h for h in headings if h in standard] == standard, headings


def test_the_plugin_hook_ships_only_with_a_release_that_has_status(tmp_path, monkeypatch) -> None:
    """hooks/hooks.json runs `uvx since-cutoff==X status --hook`, and the plugin installs from
    the repository: with X = 0.3.2 (no `status` command) every session start would fail. So a
    release before 0.4.0 must not have it, and one from 0.4.0 on must, pinned to itself."""
    check = _check_versions()
    assert not check.has_status("0.3.2") and check.has_status("0.4.0rc1")
    assert check.has_status("0.4.0") and check.has_status("1.0.0")
    assert check.hook_problems(__version__) == []  # the repository as it is
    staged = ROOT / "hooks" / "hooks.json.in"
    if staged.exists():  # waiting for the release that has `status`
        [entry] = json.loads(staged.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
        command = entry["hooks"][0]["command"]
        found = re.fullmatch(r"uvx since-cutoff==(\S+) status --hook", command)
        assert found is not None and check.has_status(found.group(1))

    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.hook_problems("0.3.2") == []  # no hook: nothing to fail
    (tmp_path / "hooks").mkdir()
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "timeout": 60}]}]}}

    def pin(version: str) -> None:
        hook["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (  # type: ignore[index]
            f"uvx since-cutoff=={version} status --hook"
        )
        (tmp_path / "hooks" / "hooks.json").write_text(json.dumps(hook), encoding="utf-8")

    pin("0.3.2")
    [problem] = check.hook_problems("0.3.2")
    assert "which since-cutoff 0.3.2 does not have (0.4.0 added it)" in problem
    pin("0.4.0")
    assert check.hook_problems("0.4.0") == []
    assert check.hook_problems("0.4.1") == [
        "hooks/hooks.json SessionStart pin: 0.4.0 (expected 0.4.1)"
    ]
    (tmp_path / "hooks" / "hooks.json").unlink()
    (tmp_path / "hooks" / "hooks.json.in").write_text(json.dumps(hook), encoding="utf-8")
    assert check.hook_problems("0.4.0") == [
        "hooks/hooks.json SessionStart pin: missing (expected 0.4.0; hooks/hooks.json.in has it)"
    ]


# --------------------------------------------------- runs that measured nothing
class _TaskWriterDown(ScriptedModel):
    """The task writer's calls fail (rate limit); nothing else is ever reached."""

    def complete(self, system: str, user: str) -> Completion:
        if system.startswith("You write evaluation tasks"):
            raise ProviderError("claude call failed: You've hit your limit")
        return super().complete(system, user)


def test_a_run_that_could_not_probe_anything_fails(tmp_path, capsys, monkeypatch, fake_pypi):
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))

    def engine(settings: Settings, **kwargs: Any) -> Engine:
        return Engine(
            settings,
            **{**kwargs, "pypi": fake_pypi, "provider_factory": lambda spec: _TaskWriterDown()},
        )

    monkeypatch.setattr(cli, "Engine", engine)
    root = make_app(tmp_path)
    argv = ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
    # Without the guard this exited 0, so `--fail-on-stale` passed a run that measured nothing.
    assert cli.main([*argv, "--fail-on-stale"]) == 1
    out = capsys.readouterr().out
    assert "No API change could be probed" in out and "hit your limit" in out
