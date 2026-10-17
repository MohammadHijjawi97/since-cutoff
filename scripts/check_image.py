"""Check a since-cutoff Docker image: its version, its user, and MCP tools/list with no network.

    python scripts/check_image.py IMAGE VERSION

CI's docker job runs it on the image it builds, and release.yml on the linux/amd64 image before
it pushes any.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

TOOLS = {"model_cutoff", "api_changes", "project_changes"}


def _output(*args: str) -> str:
    """What `docker run --rm ARGS` prints; its error output when it fails."""
    done = subprocess.run(
        ["docker", "run", "--rm", *args], capture_output=True, text=True, timeout=300, check=False
    )
    return (done.stdout if done.returncode == 0 else done.stderr).strip()


def mcp_tools(image: str) -> set[str]:
    """The tools that `docker run -i --network none IMAGE` (the MCP server) lists."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "check_image", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    server = subprocess.Popen(
        ["docker", "run", "--rm", "-i", "--network", "none", image],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert server.stdin is not None and server.stdout is not None
    timer = threading.Timer(300, server.kill)  # a server that never answers fails the check
    timer.start()
    try:
        for message in messages:
            server.stdin.write(json.dumps(message) + "\n")
            server.stdin.flush()
        for line in server.stdout:
            reply = json.loads(line)
            if reply.get("id") == 2:
                return {tool["name"] for tool in reply["result"]["tools"]}
        return set()
    finally:
        server.stdin.close()
        server.wait(timeout=60)
        timer.cancel()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    image, version = argv[1], argv[2]
    problems = []
    if (found := _output(image, "--version")) != f"since-cutoff {version}":
        problems.append(f"--version: {found!r}, not 'since-cutoff {version}'")
    if (uid := _output("--entrypoint", "id", image, "-u")) != "10001":
        problems.append(f"runs as uid {uid}, not 10001")
    if (tools := mcp_tools(image)) != TOOLS:
        problems.append(f"MCP tools/list: {sorted(tools)}, not {sorted(TOOLS)}")
    for problem in problems:
        print(f"{image}: {problem}", file=sys.stderr)
    if not problems:
        print(f"{image}: since-cutoff {version}, uid 10001, MCP tools {sorted(tools)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
