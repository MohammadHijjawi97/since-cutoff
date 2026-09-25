"""Claude Code CLI provider: uses the user's own ``claude`` login (subscription or API key).

The model is called with every tool disabled (``--tools ""``), no MCP servers, no slash
commands, a replaced system prompt and an empty working directory, so answers reflect what the
model knows rather than what it can look up.

The system prompt is passed through ``--system-prompt-file`` and the task through stdin, so no
multi-line or long text ever goes on the command line (``claude.cmd`` shims on Windows cut
arguments at the first newline, and Windows limits command lines to 32,767 characters).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from since_cutoff.errors import ProviderError
from since_cutoff.providers.base import Completion

_RETRYABLE = ("overloaded", "rate limit", "rate_limit", "529", "timed out", "timeout", "ECONNRESET")
FIRST_CALL_TIMEOUT = 180.0


class ClaudeCodeProvider:
    provider_name = "claude-code"

    def __init__(self, model: str | None, *, timeout: float = 600.0, retries: int = 3) -> None:
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.resolved_model: str | None = None
        self._succeeded = False
        exe = shutil.which("claude")
        if exe is None:
            raise ProviderError(
                "the `claude` CLI was not found on PATH. Install Claude Code "
                "(https://docs.claude.com/en/docs/claude-code) or pick another --model provider"
            )
        self.exe_command: list[str] = [exe]
        self._workdir = tempfile.mkdtemp(prefix="since-cutoff-claude-")

    @property
    def model_name(self) -> str | None:
        return self.resolved_model or self.model

    @property
    def key(self) -> str:
        return f"claude-code:{self.resolved_model or self.model or 'default'}"

    def command(self, system_file: str) -> list[str]:
        cmd = [
            *self.exe_command,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        if self.model:
            cmd += ["--model", self.model]
        cmd += ["--tools", "", "--system-prompt-file", system_file]
        return cmd

    def complete(self, system: str, user: str) -> Completion:
        env = {
            k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
        }
        fd, system_file = tempfile.mkstemp(dir=self._workdir, prefix="system-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system)
        try:
            return self._complete(system_file, user, env)
        finally:
            Path(system_file).unlink(missing_ok=True)

    def _complete(self, system_file: str, user: str, env: dict[str, str]) -> Completion:
        last_error = ""
        for attempt in range(self.retries):
            # Until one call has succeeded, fail fast: an expired login makes the CLI hang.
            timeout = self.timeout if self._succeeded else min(self.timeout, FIRST_CALL_TIMEOUT)
            try:
                proc = subprocess.run(
                    self.command(system_file),
                    input=user,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    cwd=self._workdir,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if not self._succeeded:
                    raise ProviderError(
                        f"Claude Code did not answer within {timeout:.0f}s. If its login expired, "
                        "run `claude auth login` (a plain `claude -p hello` shows the real error)."
                    ) from exc
                last_error = f"claude timed out after {timeout:.0f}s"
                continue
            except OSError as exc:
                raise ProviderError(f"could not run claude: {exc}") from exc
            completion, error = self._parse(proc.stdout, proc.stderr, proc.returncode)
            if completion is not None:
                self._succeeded = True
                return completion
            last_error = error
            if "authenticat" in error.lower() or "login" in error.lower():
                raise ProviderError(
                    "Claude Code is not logged in (or its login expired). Run `claude auth login` "
                    f"and try again. Details: {error[:200]}"
                )
            if not any(s.lower() in error.lower() for s in _RETRYABLE):
                break
            time.sleep(min(2.0 * 2**attempt, 30.0))
        raise ProviderError(f"claude call failed: {last_error[:500]}")

    def _parse(self, stdout: str, stderr: str, code: int) -> tuple[Completion | None, str]:
        try:
            data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
        except (ValueError, IndexError):
            data = None
        if not isinstance(data, dict):
            return None, (stderr or stdout or f"exit code {code}").strip()
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            return None, str(data.get("result") or data.get("subtype") or "unknown error")
        usage = data.get("usage") or {}
        model_usage = data.get("modelUsage") or {}
        if model_usage:
            # The model that produced most output tokens is the one that answered.
            self.resolved_model = max(
                model_usage, key=lambda m: (model_usage[m] or {}).get("outputTokens", 0)
            )
        return (
            Completion(
                text=str(data.get("result") or ""),
                model=self.resolved_model,
                cost_usd=data.get("total_cost_usd"),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            ),
            "",
        )
