"""The MCP server: the tools against a fake PyPI, the SDK wiring, and stdio end to end."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any

import anyio
import pytest

from since_cutoff import __version__, cli, net
from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine, ModelTarget, Settings
from since_cutoff.errors import PackageIndexError, ProjectError, SinceCutoffError
from since_cutoff.mcp_server import SERVER_NAME, Tools, _symbol_terms, build_server
from since_cutoff.models import ModelRegistry, load_snapshot
from since_cutoff.project import load_project
from since_cutoff.pypi import SourceTree
from since_cutoff.report import render_scan_markdown
from tests.conftest import FakePyPI, compiled_fastlib, write_tree

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"


@pytest.fixture
def tools(cache: DiskCache, fake_pypi: Any) -> Tools:
    registry = ModelRegistry(cache, offline=True)  # the bundled models.dev snapshot
    return Tools(cache, registry=registry, pypi=fake_pypi, today=date(2026, 9, 1))


def make_app(
    tmp_path: Path,
    pin: str = "toylib==2.0",
    code: str = "from toylib import Client\nClient().send('hi', temperature=0.2)\n",
) -> Path:
    root = tmp_path / "app"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = ["{pin}"]\n'
    )
    (root / "main.py").write_text(code)
    return root


# ---------------------------------------------------------------- model_cutoff
@pytest.mark.parametrize(
    "spec",
    [
        "claude-sonnet-4-5",
        "claude-sonnet-4-5-20250929",
        "anthropic/claude-sonnet-4.5",
        "claude-code:claude-sonnet-4-5",
        "Claude-Sonnet-4-5[1m]",
    ],
)
def test_model_cutoff_accepts_the_usual_spellings(tools: Tools, spec: str) -> None:
    out = tools.model_cutoff(spec)
    assert out.startswith("claude-sonnet-4-5") and "(anthropic" in out
    assert "training cutoff 2025-07-31" in out
    assert 'api_changes(package, model="claude-sonnet-4-5' in out


def test_model_ids_prefer_the_model_maker_over_resellers(tools: Tools) -> None:
    # gpt-5.4 is listed by github-copilot too, which sorts first alphabetically.
    assert tools.model_cutoff("gpt-5.4").startswith("gpt-5.4 (openai")
    assert tools.model_cutoff("kimi-k2.7-code").startswith("kimi-k2.7-code (moonshotai")


@pytest.mark.parametrize(
    ("spec", "start", "cutoff"),
    [
        ("openai:gpt-5.4", "gpt-5.4 (openai", "2025-08-31"),
        ("openai/gpt-5.4", "gpt-5.4 (openai", "2025-08-31"),
        ("codex:gpt-5.4", "gpt-5.4 (openai", "2025-08-31"),
        ("google:gemini-2.5-pro", "gemini-2.5-pro (google", "2025-01"),
        ("gemini-2.5-pro", "gemini-2.5-pro (google", "2025-01"),
        ("google/gemini-2.5-pro", "gemini-2.5-pro (google", "2025-01"),
        ("gemini/gemini-2.5-pro", "gemini-2.5-pro (google", "2025-01"),  # Aider's spelling
        # Any provider the registry knows, not only those since-cutoff can call.
        ("github-copilot:gpt-5.4", "gpt-5.4 (github-copilot", "2025-08-31"),
    ],
)
def test_model_cutoff_takes_ids_from_any_vendor_the_registry_knows(
    tools: Tools, spec: str, start: str, cutoff: str
) -> None:
    out = tools.model_cutoff(spec)
    assert out.startswith(start) and f"training cutoff {cutoff}" in out


def test_the_change_tools_take_openai_and_google_ids(tools: Tools) -> None:
    out = tools.api_changes("toylib", model="google:gemini-2.5-pro")
    assert "the newest release on or before 2025-01-31" in out and "gemini-2.5-pro" in out
    assert "5 breaking changes, 1 new deprecation" in out
    out = tools.api_changes("toylib", model="openai:gpt-5.4")
    assert "the newest release on or before 2025-08-31" in out and "gpt-5.4" in out


def test_model_cutoff_maps_claude_aliases_and_says_so(tools: Tools) -> None:
    out = tools.model_cutoff("sonnet")
    assert "'sonnet' is an alias: assuming the newest sonnet model" in out


def test_unknown_models_fail_with_close_matches(tools: Tools) -> None:
    with pytest.raises(
        SinceCutoffError, match=r"unknown model .*Close matches: .*claude-sonnet-4-5"
    ):
        tools.model_cutoff("claude-sonet-4-5")
    with pytest.raises(SinceCutoffError, match="is empty"):
        tools.model_cutoff("  ")


# ----------------------------------------------------------------- api_changes
COMPILED_WARNING = (
    "- Warning: fastlib 2.0: fastlib.fast is a compiled module without a .py source or a .pyi "
    "stub, unlike in 1.0; since-cutoff does not run code, so changes to it and to the names "
    "taken from it are not reported"
)


@pytest.mark.parametrize("drop", [True, False], ids=["with-changes", "nothing-else"])
def test_api_changes_says_when_a_compiled_module_was_not_compared(tmp_path, cache, drop) -> None:
    """Issue #52: "No breaking changes" for a package whose module became compiled is not the
    whole answer; the agent is told what was not compared."""
    registry = ModelRegistry(cache, offline=True)
    pypi = compiled_fastlib(tmp_path, cache, drop=drop)
    tools = Tools(cache, registry=registry, pypi=pypi, today=date(2026, 9, 1))
    out = tools.api_changes("fastlib", cutoff="2025-07")
    assert COMPILED_WARNING in out.splitlines()
    assert ("fastlib.gone" in out) is drop
    assert ("No breaking changes or new deprecations found." in out) is not drop


def test_project_changes_says_when_a_compiled_module_was_not_compared(tmp_path, cache) -> None:
    registry = ModelRegistry(cache, offline=True)
    pypi = compiled_fastlib(tmp_path / "lib", cache)
    tools = Tools(cache, registry=registry, pypi=pypi, today=date(2026, 9, 1))
    out = tools.project_changes(
        str(make_app(tmp_path, "fastlib==2.0", "import fastlib\n")), cutoff="2025-07"
    )
    assert COMPILED_WARNING in out.splitlines()


def test_api_changes_lists_hard_breaks_first_with_replacements(tools: Tools) -> None:
    out = tools.api_changes("toylib", model="claude-sonnet-4-5")
    assert out.startswith("# toylib 1.0 -> 2.0\n")
    assert "From 1.0 (2025-01-10): the newest release on or before 2025-07-31" in out
    assert "To 2.0 (2025-10-01): the latest release on PyPI" in out
    assert "5 breaking changes, 1 new deprecation" in out
    sections = [
        "## Removed or moved",
        "## Parameters removed",
        "## Parameters now required",
        "## Now keyword-only or positional-only",
        "## Deprecated",
    ]
    positions = [out.index(s) for s in sections]
    assert positions == sorted(positions)
    # Each line says what to do instead when the diff knows it.
    assert "`toylib.legacy_fetch` was removed; the old docs said: " in out
    assert "Use fetch instead" in out
    assert "moved to `toylib.Session`; import it with `from toylib import Session`" in out
    assert "now `fetch(url: str, *, timeout: float, retries: int = 3) -> bytes`" in out
    assert "`toylib.Client.close` is deprecated: Use Client.shutdown() instead." in out
    # The same change under two public paths, or its async twin, is listed once.
    assert out.count("legacy_fetch` was removed") == 1
    assert "also changed under 1 other path, e.g. `toylib.AsyncClient.send`" in out
    assert "(toylib 2.0)" not in out  # versions are in the header, not on every line


@pytest.mark.parametrize(
    "symbol",
    [
        "client.send",
        "toylib.Client.send",
        "self.client.send(message='hi', temperature=0.2)",
        "await client.send('hi')",
        "`Client.send`",
        "AsyncClient.send",
        " send ",
    ],
)
def test_symbols_name_methods_the_way_code_calls_them(tools: Tools, symbol: str) -> None:
    out = tools.api_changes("toylib", cutoff="2025-07", symbol=symbol)
    assert f'2 of them match symbol "{symbol.strip()}"' in out
    assert "`temperature` was removed" in out and "`stream` is now keyword-only" in out
    assert "legacy_fetch" not in out


def test_a_variable_not_named_after_the_class_falls_back_to_the_method_and_says_so(
    tools: Tools,
) -> None:
    out = tools.api_changes("toylib", cutoff="2025-07", symbol="c.send")
    assert 'nothing names "c.send" exactly; showing the 2 changes to any `send`' in out
    assert "`temperature` was removed" in out and "legacy_fetch" not in out


def test_symbols_can_name_classes_parameters_and_several_things(tools: Tools) -> None:
    both = tools.api_changes("toylib", cutoff="2025-07", symbol="fetch, legacy_fetch")
    assert '2 of them match symbol "fetch, legacy_fetch"' in both
    klass = tools.api_changes("toylib", cutoff="2025-07", symbol="toylib.Client")
    assert '3 of them match symbol "toylib.Client"' in klass  # send twice, and close
    param = tools.api_changes("toylib", cutoff="2025-07", symbol="send.temperature")
    assert '1 of them match symbol "send.temperature"' in param


def test_a_symbol_that_matches_nothing_suggests_the_bare_name(tools: Tools) -> None:
    out = tools.api_changes("toylib", cutoff="2025-07", symbol="client.chat.completions.create")
    assert '0 of them match symbol "client.chat.completions.create"' in out
    assert "either it did not change between 1.0 and 2.0" in out
    assert 'retry with the bare function or class name (symbol="create")' in out
    assert "leave `symbol` out to see all 6 changes" in out


def test_symbol_terms() -> None:
    assert _symbol_terms("await client.messages.create(model=m, max_tokens=f(1))") == [
        ["client", "messages", "create"]
    ]
    assert _symbol_terms(" `Messages.create` , hf_hub_download(") == [
        ["messages", "create"],
        ["hf_hub_download"],
    ]
    assert _symbol_terms("()") == []


def test_api_changes_symbol_filter_and_limit(tools: Tools) -> None:
    out = tools.api_changes("toylib", cutoff="2025-07", symbol="LEGACY")
    assert '1 of them match symbol "LEGACY"' in out
    assert "legacy_fetch" in out and "Client.send" not in out
    none = tools.api_changes("toylib", cutoff="2025-07", symbol="nothing-like-this")
    assert '0 of them match symbol "nothing-like-this"' in none
    assert "check the spelling" in none

    short = tools.api_changes("toylib", cutoff="2025-07", limit=2)
    listed = [line for line in short.splitlines() if line.startswith("- `")]
    assert len(listed) == 2
    # The limit is shared between kinds, so one kind cannot crowd out the others.
    assert "## Removed or moved" in short and "## Parameters removed" in short
    assert "Not listed: 3 breaking changes, 1 new deprecation" in short
    assert 'symbol="..."' in short


def test_api_changes_with_explicit_versions(tools: Tools) -> None:
    out = tools.api_changes("toylib", from_version="1.0", to_version="2.0")
    assert (
        "From 1.0 (2025-01-10): as requested" in out and "To 2.0 (2025-10-01): as requested" in out
    )
    pinned = tools.api_changes("toylib==2.0", from_version="1.0")
    assert "To 2.0 (2025-10-01): as requested" in pinned
    same = tools.api_changes("toylib", from_version="2.0", to_version="1.0")
    assert "No changes: 1.0 is not newer than 2.0." in same


def test_api_changes_for_a_package_newer_than_the_model(tools: Tools) -> None:
    out = tools.api_changes("toylib", cutoff="2024-01")
    assert "no release on or before the cutoff" in out
    assert "Its whole API was released after your reported training cutoff" in out


def test_api_changes_errors_are_clear(tools: Tools) -> None:
    with pytest.raises(SinceCutoffError, match=r"pass `model`.*or `from_version`"):
        tools.api_changes("toylib")
    with pytest.raises(SinceCutoffError, match="cutoff: not a date"):
        tools.api_changes("toylib", cutoff="someday")
    with pytest.raises(PackageIndexError, match=r"toylib==9\.9 is not on PyPI"):
        tools.api_changes("toylib", cutoff="2025-07", to_version="9.9")
    with pytest.raises(PackageIndexError, match="no final releases"):
        tools.api_changes("no-such-package", cutoff="2025-07")
    with pytest.raises(SinceCutoffError, match="not a PyPI package name"):
        tools.api_changes("not a name!", cutoff="2025-07")


# ------------------------------------------------------------- project_changes
def test_project_changes_puts_what_the_code_uses_first(tools: Tools, tmp_path: Path) -> None:
    out = tools.project_changes(str(make_app(tmp_path)), model="claude-sonnet-4-5")
    assert out.startswith("# app: 1 dependency checked")
    assert "- Cutoff 2025-07-31 (training cutoff of claude-sonnet-4-5" in out
    assert "## toylib 1.0 (2025-01-10) -> 2.0 (2025-10-01)" in out
    assert "5 breaking, 1 deprecated. Your code imports it." in out
    touching = out.split("### Touching names your code uses\n\n")[1].split("\n\n")[0]
    first, second = touching.splitlines()
    assert first.endswith(
        "`temperature` was removed; also changed under 1 other path, e.g. `toylib.AsyncClient.send` "
        "[your code uses `send` and `temperature`]"
    )
    assert "`stream` is now keyword-only" in second
    assert second.endswith("[your code uses `send`]")

    one = tools.project_changes(str(tmp_path / "app"), cutoff="2025-07", limit_per_package=1)
    section = one.split("## toylib 1.0")[1]  # after "Your code uses these changed APIs"
    listed = [line for line in section.splitlines() if line.startswith("- `")]
    assert len(listed) == 1 and "temperature" in listed[0]  # the change the code hits
    assert 'more: api_changes("toylib", from_version="1.0", to_version="2.0"' in one


def test_a_parameter_counts_only_with_its_callable(tools: Tools, tmp_path: Path) -> None:
    # `temperature` is passed to a function of the project's own, and `send` is never called.
    code = "from toylib import Client\nconfigure(temperature=0.5)\nClient().close()\n"
    out = tools.project_changes(str(make_app(tmp_path, code=code)), cutoff="2025-07")
    touching = out.split("### Touching names your code uses\n\n")[1].split("\n\n")[0]
    assert touching.splitlines() == [
        "- `toylib.Client.close` is deprecated: Use Client.shutdown() instead. "
        "[your code uses `close`]"
    ]
    removed = next(line for line in out.splitlines() if "`temperature` was removed" in line)
    assert "your code uses" not in removed

    # A call with the changed parameter ranks above a change the call does not touch.
    code = "from toylib import Client\nClient().send('hi', stream=True)\n"
    out = tools.project_changes(str(make_app(tmp_path / "2", code=code)), cutoff="2025-07")
    touching = out.split("### Touching names your code uses\n\n")[1].split("\n\n")[0]
    first, second = touching.splitlines()
    assert "`stream` is now keyword-only" in first
    assert first.endswith("[your code uses `send` and `stream`]")
    assert "`temperature` was removed" in second and second.endswith("[your code uses `send`]")


def make_namespace_dists(tmp_path: Path, cache: DiskCache) -> FakePyPI:
    """Two distributions in one namespace package, like google-genai and google-cloud-storage."""
    releases: dict[str, list[tuple[str, str]]] = {}
    trees: dict[tuple[str, str], SourceTree] = {}
    for part in ("alpha", "beta"):
        dist = f"nspkg-{part}"
        releases[dist] = [("1.0", "2025-01-10"), ("2.0", "2025-10-01")]
        for version, params in (
            ("1.0", "message: str, temperature: float = 1.0"),
            ("2.0", "message: str"),
        ):
            root = write_tree(
                tmp_path / f"{dist}-{version}",
                {f"nspkg/{part}/__init__.py": f"def send({params}) -> str:\n    return message\n"},
            )
            trees[(dist, version)] = SourceTree(dist, version, root, (f"nspkg.{part}",))
    return FakePyPI(cache, releases, trees)


def test_only_imported_packages_get_your_code_marks(tmp_path: Path, cache: DiskCache) -> None:
    pypi = make_namespace_dists(tmp_path, cache)
    root = write_tree(
        tmp_path / "app",
        {
            "pyproject.toml": '[project]\nname = "app"\nversion = "0"\n'
            'dependencies = ["nspkg-alpha==2.0", "nspkg-beta==2.0"]\n',
            "main.py": "from nspkg import alpha\nalpha.send('x', temperature=0.1)\n",
        },
    )
    tools = Tools(cache, registry=ModelRegistry(cache, offline=True), pypi=pypi)
    out = tools.project_changes(str(root), cutoff="2025-07")
    assert out.index("## nspkg-alpha ") < out.index("## nspkg-beta ")
    alpha = out.split("## nspkg-alpha ")[1].split("## nspkg-beta ")[0]
    beta = out.split("## nspkg-beta ")[1]
    # `from nspkg import alpha` imports nspkg-alpha, not every nspkg.* distribution, and
    # names in the code say nothing about a package it does not import.
    assert "Your code imports it." in alpha
    assert "[your code uses `send` and `temperature`]" in alpha
    assert "Your code imports it." not in beta and "your code uses" not in beta

    cutoff = date(2025, 7, 31)
    engine = Engine(Settings(cutoff=cutoff), store=cache, llm_cache=cache, pypi=pypi)
    scan = engine.scan(load_project(root), ModelTarget.cutoff_only(cutoff))
    md = render_scan_markdown(scan)
    rows = [line for line in md.splitlines() if line.startswith("| nspkg-")]
    assert rows[0].startswith("| nspkg-alpha |") and "imported by your code" in rows[0]
    assert rows[1].startswith("| nspkg-beta |") and "imported by your code" not in rows[1]
    assert md.count("**your code uses") == 1


def test_project_changes_reports_quiet_and_new_dependencies(tools: Tools, tmp_path: Path) -> None:
    root = make_app(tmp_path, pin="toylib==1.0")
    known = tools.project_changes(str(root), cutoff="2025-07")
    assert "API changed after the cutoff: 0" in known
    assert (
        "Not listed (released before the cutoff, or no breaking change since): toylib 1.0" in known
    )

    new = tools.project_changes(str(root), cutoff="2024-01")
    assert "## First released after the cutoff" in new and "- toylib 1.0 (2025-01-10)" in new

    only = tools.project_changes(str(root), cutoff="2025-07", only=["requests"])
    assert "`only` names no dependency of this project: requests" in only


def test_project_dir_resolves_against_the_clients_project(
    tools: Tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_app(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert "## toylib 1.0" in tools.project_changes("app", cutoff="2025-07")


def test_project_changes_errors_are_clear(tools: Tools, tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="no dependencies found"):
        tools.project_changes(str(tmp_path), cutoff="2025-07")
    with pytest.raises(SinceCutoffError, match="pass `model`"):
        tools.project_changes(str(tmp_path))


# --------------------------------------------------------------------- server
def test_server_speaks_mcp_and_turns_failures_into_tool_errors(tools: Tools) -> None:
    from mcp import Client

    server = build_server(tools)

    async def main() -> None:
        async with Client(server, mode="legacy") as client:
            assert "api_changes" in (client.instructions or "")
            listed = {t.name: t for t in (await client.list_tools()).tools}
            assert set(listed) == {"model_cutoff", "api_changes", "project_changes"}
            api = listed["api_changes"]
            assert api.input_schema["required"] == ["package"]
            props = api.input_schema["properties"]
            assert set(props) == {
                "package",
                "model",
                "cutoff",
                "from_version",
                "to_version",
                "symbol",
                "limit",
            }
            assert (api.description or "").startswith("List the public API changes")
            # Every parameter is described, with an example, in the input schema.
            assert 'e.g. "claude-haiku-4-5"' in props["model"]["description"]
            assert 'e.g. "2025-02"' in props["cutoff"]["description"]
            assert '"client.messages.create"' in props["symbol"]["description"]
            for tool in listed.values():
                annotations = tool.annotations
                assert annotations is not None
                assert annotations.read_only_hint is True and annotations.destructive_hint is False
                assert annotations.idempotent_hint is True and annotations.open_world_hint is True
                schema = tool.input_schema["properties"]
                assert all(prop.get("description") for prop in schema.values()), tool.name
                assert "ctx" not in schema and "progress" not in schema
                description = tool.description or ""
                assert "Args:" not in description  # the parameters are in the schema instead
                assert "Read-only" in description and "\nReturns " in description
                assert "Use it " in description  # when to use it rather than the other tools
            assert "several minutes" in (listed["project_changes"].description or "")

            bad = await client.call_tool("api_changes", {"package": "toylib"})
            assert bad.is_error and "pass `model`" in bad.content[0].text  # type: ignore[union-attr]
            good = await client.call_tool(
                "api_changes", {"package": "toylib", "model": "claude-sonnet-4-5"}
            )
            assert not good.is_error
            assert good.content[0].text.startswith("# toylib 1.0 -> 2.0")  # type: ignore[union-attr]

    anyio.run(main)


def test_server_lists_and_renders_read_only_prompts(tools: Tools) -> None:
    from mcp import Client

    server = build_server(tools)

    async def main() -> None:
        async with Client(server, mode="legacy") as client:
            listed = {prompt.name: prompt for prompt in (await client.list_prompts()).prompts}
            assert set(listed) == {"check_project", "before_upgrade"}
            assert [(arg.name, arg.required) for arg in listed["check_project"].arguments] == [
                ("project_dir", False)
            ]
            assert [(arg.name, arg.required) for arg in listed["before_upgrade"].arguments] == [
                ("package", True),
                ("to_version", False),
            ]

            default_project = await client.get_prompt("check_project")
            project_text = default_project.messages[0].content.text
            assert 'project_changes(project_dir="."' in project_text
            assert "your own model id" in project_text
            assert "old form" in project_text
            assert "since-cutoff sync" in project_text

            project = await client.get_prompt("check_project", {"project_dir": "backend"})
            assert 'project_changes(project_dir="backend"' in project.messages[0].content.text

            upgrade = await client.get_prompt(
                "before_upgrade", {"package": "httpx", "to_version": "1.0"}
            )
            upgrade_text = upgrade.messages[0].content.text
            assert "pinned version" in upgrade_text
            assert 'api_changes(package="httpx"' in upgrade_text
            assert 'to_version="1.0"' in upgrade_text
            assert "similar names are not confirmed replacements" in upgrade_text

            latest = await client.get_prompt("before_upgrade", {"package": "httpx"})
            assert (
                "omit to_version to compare with the latest release"
                in latest.messages[0].content.text
            )

    anyio.run(main)


def test_long_calls_report_progress(tools: Tools, tmp_path: Path) -> None:
    from mcp import Client

    server = build_server(tools)
    root = make_app(tmp_path)
    calls: dict[str, list[tuple[float, float | None, str | None]]] = {}

    async def main() -> None:
        async with Client(server, mode="legacy") as client:
            for name, arguments in (
                ("project_changes", {"project_dir": str(root), "cutoff": "2025-07"}),
                ("api_changes", {"package": "toylib", "cutoff": "2025-07"}),
            ):
                seen = calls.setdefault(name, [])

                async def progress(done: float, total: float | None, message: str | None) -> None:
                    seen.append((done, total, message))  # noqa: B023 (awaited in this loop)

                result = await client.call_tool(name, arguments, progress_callback=progress)
                assert not result.is_error

    anyio.run(main)
    project = calls["project_changes"]
    assert [m for _, _, m in project] == [
        "Checking 1 dependency on PyPI",
        "Checking 1 dependency on PyPI (1 of 1)",
        "Diffing the API of 1 package released after its cutoff version",
        "Diffing the API of 1 package released after its cutoff version (1 of 1)",
    ]
    done = [d for d, _, _ in project]
    assert done == sorted(set(done))  # strictly increasing, as MCP requires
    assert all(total is not None and d <= total for d, total, _ in project)
    assert project[-1][0] == project[-1][1]  # finished: progress reached the total
    assert [m for _, _, m in calls["api_changes"]] == [
        "Diffing the API of toylib 1.0 -> 2.0",
        "Diffing the API of toylib 1.0 -> 2.0 (1 of 1)",
    ]


def test_the_server_starts_without_network_or_api_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp import Client

    def offline(*args: object, **kwargs: object) -> None:
        raise AssertionError("listing the tools must not touch the network")

    monkeypatch.setattr(net, "request", offline)
    monkeypatch.setattr("urllib.request.urlopen", offline)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "empty-cache"))
    server = build_server()  # the real Tools, with an empty cache

    async def main() -> None:
        async with Client(server, mode="legacy") as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"model_cutoff", "api_changes", "project_changes"}
            prompts = {p.name for p in (await client.list_prompts()).prompts}
            assert prompts == {"check_project", "before_upgrade"}

    anyio.run(main)


def test_the_cli_and_the_tools_do_not_import_the_sdk() -> None:
    code = (
        "import sys, since_cutoff.cli, since_cutoff.mcp_server; "
        "print(any(m == 'mcp' or m.startswith('mcp.') for m in sys.modules))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert done.stdout.strip() == "False"


def test_cli_has_an_mcp_subcommand() -> None:
    assert cli._argv_with_default(["mcp"]) == ["mcp"]
    args = cli.build_parser().parse_args(["mcp", "--max-download-mb", "20"])
    assert (args.command, args.max_download_mb) == ("mcp", 20.0)


class _Stdio:
    """A minimal JSON-RPC client over a subprocess's stdin/stdout, with timeouts."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def response(self, request_id: int, timeout: float = 90.0) -> dict[str, Any]:
        while True:
            line = self.lines.get(timeout=timeout)
            message = json.loads(line)  # anything else on stdout would corrupt the protocol
            assert message.get("jsonrpc") == "2.0", line
            if message.get("id") == request_id:
                return dict(message)


def test_stdio_server_end_to_end(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache")
    cache.set("models", "models-dev", load_snapshot())  # no network: the bundled snapshot
    env = {**os.environ, "SINCE_CUTOFF_CACHE": str(cache.root)}
    env.pop("CLAUDE_PROJECT_DIR", None)
    # No API key and no network: every request would go to a proxy that does not exist.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NO_PROXY", "no_proxy"):
        env.pop(key, None)
    env.update(HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9")
    with (tmp_path / "stderr.txt").open("wb") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-m", "since_cutoff", "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            cwd=tmp_path,
            env=env,
        )
        try:
            rpc = _Stdio(proc)
            rpc.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                }
            )
            init = rpc.response(1)["result"]
            assert init["serverInfo"]["name"] == "since-cutoff"
            assert init["serverInfo"]["version"] == __version__
            assert "model_cutoff" in init["instructions"]
            rpc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            rpc.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {t["name"] for t in rpc.response(2)["result"]["tools"]}
            assert names == {"model_cutoff", "api_changes", "project_changes"}

            call = {"name": "model_cutoff", "arguments": {"model": "claude-sonnet-4-5"}}
            rpc.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})
            result = rpc.response(3)["result"]
            assert not result.get("isError")
            assert "training cutoff 2025-07-31" in result["content"][0]["text"]

            call = {"name": "model_cutoff", "arguments": {"model": "no-such-model-x"}}
            rpc.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": call})
            failed = rpc.response(4)["result"]
            assert failed["isError"] is True
            assert "unknown model 'no-such-model-x'" in failed["content"][0]["text"]
        finally:
            assert proc.stdin is not None
            proc.stdin.close()  # end of input: the server shuts down
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    assert proc.returncode == 0, (tmp_path / "stderr.txt").read_text(errors="replace")


# ----------------------------------------------------------- registry, plugin
def _json(path: Path) -> Any:
    if not path.exists():
        pytest.skip(f"{path.name} is not part of this checkout")
    return json.loads(path.read_text(encoding="utf-8"))


def test_registry_entry_and_plugin_match_the_package() -> None:
    server = _json(ROOT / "server.json")
    assert server["name"] == SERVER_NAME
    assert server["version"] == __version__
    [package] = server["packages"]
    assert (package["registryType"], package["identifier"]) == ("pypi", "since-cutoff")
    assert package["version"] == __version__
    assert package["runtimeHint"] == "uvx"
    assert package["packageArguments"] == [{"type": "positional", "value": "mcp"}]
    # PyPI ownership proof for the MCP Registry: the marker must be in the package README.
    assert f"<!-- mcp-name: {SERVER_NAME} -->" in (ROOT / "README.md").read_text(encoding="utf-8")

    plugin = _json(ROOT / ".claude-plugin" / "plugin.json")
    assert plugin["version"] == __version__
    # The plugin's server comes from the root .mcp.json alone (declaring it in plugin.json too
    # would register it twice), pinned to this release as plugin directories expect.
    assert "mcpServers" not in plugin and "displayName" not in plugin
    launch = ["since-cutoff==" + __version__, "mcp"]
    assert _json(ROOT / ".mcp.json") == {
        "mcpServers": {"since-cutoff": {"command": "uvx", "args": launch}}
    }
    portable = _json(ROOT / "mcp.json")  # the Agent Plugins format, with a transport type
    assert portable["mcpServers"] == {
        "since-cutoff": {"type": "stdio", "command": "uvx", "args": launch}
    }


def test_readme_prepares_mcp_users_for_a_slow_first_call() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Use it from any agent (MCP)")[1].split("\n## ")[0]
    assert "tool_timeout_sec = 900" in section
    assert "several minutes" in section and "`since-cutoff scan`" in section


@pytest.mark.network
def test_server_json_matches_the_published_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from since_cutoff import net

    server = _json(ROOT / "server.json")
    assert server["$schema"] == SCHEMA_URL
    jsonschema.validate(server, net.get_json(SCHEMA_URL))


# --- symbol matching on realistic diffs ------------------------------------------------------


def _change(kind: str, path: str, name: str, **kw: Any) -> Any:
    from since_cutoff.apidiff import APIChange

    return APIChange("pkg", "1.0", "2.0", kind, path, name, **kw)


def test_a_removed_attribute_breaks_every_call_through_it() -> None:
    from since_cutoff.mcp_server import _matching

    gone = _change("removed", "anthropic.Client.completions", "completions", owner="Client")
    unrelated = _change(
        "param_removed",
        "anthropic.resources.Messages.create",
        "create",
        owner="Messages",
        parameter="temperature",
    )
    found, loose = _matching([gone, unrelated], "client.completions.create")
    assert found == [gone] and not loose


def test_the_loose_fallback_is_flagged() -> None:
    from since_cutoff.mcp_server import _matching

    runs = _change("deprecated", "openai.beta.threads.Runs.create", "create", owner="Runs")
    found, loose = _matching([runs], "client.chat.completions.create")
    assert found == [runs] and loose


def test_symbols_do_not_match_the_package_name_or_word_fragments() -> None:
    from since_cutoff.mcp_server import _matching, _names_package

    stream = _change(
        "param_removed",
        "anthropic.MessageStream.__init__",
        "__init__",
        owner="MessageStream",
        parameter="output_format",
    )
    legacy = _change("removed", "toylib.legacy_fetch", "legacy_fetch")
    assert _matching([stream], "messages") == ([], False)
    assert _matching([legacy], "legacy") == ([legacy], False)
    assert _names_package("anthropic", "anthropic", [stream])
    assert _names_package("hub", "huggingface-hub", [_change("removed", "hub.x", "x")])
    assert _names_package("huggingface_hub", "huggingface-hub", [stream])
    assert not _names_package("MessageStream", "anthropic", [stream])
    assert not _names_package("Anthropic", "anthropic", [stream])  # the client class


def test_a_class_named_like_its_package_is_a_filter() -> None:
    # redis 5.2.1 -> 6.4.0: symbol="Redis" named the package, so no filter was applied.
    from since_cutoff.mcp_server import _matching, _names_package

    tfcall = _change("removed", "redis.client.Redis.tfcall", "tfcall", owner="Redis")
    init = _change(
        "param_removed", "redis.client.Redis.__init__", "__init__", owner="Redis", parameter="x"
    )
    graph = _change(
        "removed",
        "redis.commands.redismodules.RedisModuleCommands.graph",
        "graph",
        owner="RedisModuleCommands",
        also=["redis.client.Redis.graph"],
    )
    cluster = _change(
        "removed", "redis.cluster.RedisCluster.get_retry", "get_retry", owner="RedisCluster"
    )
    parser = _change("removed", "redis.cluster.ClusterParser", "ClusterParser")
    changes = [tfcall, init, graph, cluster, parser]
    assert not _names_package("Redis", "redis", changes)
    assert _names_package("redis", "redis", changes)
    for symbol in ("Redis", "client.Redis"):
        assert _matching(changes, symbol) == ([tfcall, init, graph], False)


def test_naming_the_package_as_symbol_applies_no_filter(tools: Tools) -> None:
    out = tools.api_changes("toylib", cutoff="2025-07", symbol="toylib")
    assert "names the package itself, so no filter was applied" in out
