from pathlib import Path

ROOT = Path(".")

def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}: {old[:80]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")

replace_once(
    "src/since_cutoff/mcp_server.py",
    """    for fn, title in entries:
        server.add_tool(
            _tool_function(fn),
            title=title,
            description=_description(fn.__doc__ or ""),
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
            structured_output=False,
        )
    return server
""",
    """    for fn, title in entries:
        server.add_tool(
            _tool_function(fn),
            title=title,
            description=_description(fn.__doc__ or ""),
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
            structured_output=False,
        )

    @server.prompt(
        name="check_project",
        title="Check a project's dependency changes",
        description="Check a project against the coding agent's own training cutoff.",
    )
    def check_project(project_dir: str = ".") -> str:
        return (
            f'Call project_changes(project_dir="{project_dir}", model=<your own model id>). '
            "Start with changed APIs the project uses in the old form, and keep the tool's "
            "evidence wording: removals, moves and parameter changes come from a static API diff; "
            "similar names are not confirmed replacements. Then suggest since-cutoff sync "
            "if the user wants to keep the resulting notes in AGENTS.md."
        )

    @server.prompt(
        name="before_upgrade",
        title="Check changes before a dependency upgrade",
        description="Check what project code may need to change before upgrading one package.",
    )
    def before_upgrade(package: str, to_version: str = "") -> str:
        target = (
            f', to_version="{to_version}"'
            if to_version
            else " (omit to_version to compare with the latest release)"
        )
        return (
            f"Read the pinned version of {package!r} from the project's lockfile. "
            f'Call api_changes(package="{package}", from_version=<pinned version>{target}, '
            "model=<your own model id>). List what the project's code must change, preserving "
            "the tool's evidence wording: removals, moves and parameter changes come from a "
            "static API diff; similar names are not confirmed replacements."
        )

    return server
""",
)

replace_once(
    "src/since_cutoff/mcp_server.py",
    """    \"\"\"The MCP server with the three read-only tools registered.

    Building it and listing its tools needs no network access and no API key.
    \"\"\"""",
    """    \"\"\"The MCP server with three read-only tools and two read-only prompts registered.

    Building it and listing its tools or prompts needs no network access and no API key.
    \"\"\"""",
)

replace_once(
    "tests/test_mcp_server.py",
    """def test_long_calls_report_progress(tools: Tools, tmp_path: Path) -> None:
""",
    """def test_server_lists_and_renders_read_only_prompts(tools: Tools) -> None:
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
""",
)

replace_once(
    "tests/test_mcp_server.py",
    """        async with Client(server, mode="legacy") as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"model_cutoff", "api_changes", "project_changes"}

    anyio.run(main)
""",
    """        async with Client(server, mode="legacy") as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"model_cutoff", "api_changes", "project_changes"}
            prompts = {p.name for p in (await client.list_prompts()).prompts}
            assert prompts == {"check_project", "before_upgrade"}

    anyio.run(main)
""",
)

replace_once(
    "README.md",
    """`since-cutoff mcp` is an MCP server that lets a coding agent ask "what changed in this library
since my training cutoff?" before it writes code. It has three read-only tools:
""",
    """`since-cutoff mcp` is an MCP server that lets a coding agent ask "what changed in this library
since my training cutoff?" before it writes code. It has three read-only tools and two prompts:
""",
)

replace_once(
    "README.md",
    """| `model_cutoff(model)` | a model's training cutoff, from [models.dev](https://models.dev) |

The agent passes its own model id, so the answer covers what changed after that model's training
""",
    """| `model_cutoff(model)` | a model's training cutoff, from [models.dev](https://models.dev) |

| prompt | asks the agent to |
|---|---|
| `check_project(project_dir=".")` | call `project_changes` with its own model id, start with old-form uses, and offer `since-cutoff sync` for AGENTS.md notes |
| `before_upgrade(package, to_version="")` | read the pinned package version, call `api_changes` for the intended upgrade, and list what project code needs to change |

The agent passes its own model id, so the answer covers what changed after that model's training
""",
)

replace_once(
    "CHANGELOG.md",
    """## 0.5.0 - 2026-09-28

""",
    """## 0.5.0 - 2026-09-28

- The MCP server exposes two short prompts, `check_project` and `before_upgrade`, that guide an
  agent to the existing read-only tools without adding network access or model calls to prompt
  discovery itself.

""",
)
