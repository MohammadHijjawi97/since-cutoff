"""Model providers against fakes: the Claude Code CLI (a fake ``subprocess.run``) and the HTTP
APIs (the local server). No model is ever called."""

from __future__ import annotations

import gc
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from since_cutoff.errors import ProviderError
from since_cutoff.providers import check_spec, claude_code, make_provider
from since_cutoff.providers.claude_code import FIRST_CALL_TIMEOUT, ClaudeCodeProvider
from since_cutoff.providers.http_api import AnthropicProvider, OpenAICompatibleProvider
from tests.conftest import Reply


# ---------------------------------------------------------------- Claude Code
def answer(text: str = "done", **extra: Any) -> tuple[str, str, int]:
    """What ``claude -p --output-format json`` prints for a successful call."""
    data = {"type": "result", "subtype": "success", "is_error": False, "result": text, **extra}
    return json.dumps(data) + "\n", "", 0


def failure(message: str, subtype: str = "success") -> tuple[str, str, int]:
    data = {"type": "result", "subtype": subtype, "is_error": True, "result": message}
    return json.dumps(data) + "\n", "", 1


class FakeClaude:
    """``subprocess.run`` for the claude CLI: each call takes the next scripted outcome (a
    ``(stdout, stderr, exit code)`` triple or an exception) and is recorded with the system
    prompt it was given and what its working directory held at the time."""

    def __init__(self) -> None:
        self.outcomes: list[tuple[str, str, int] | BaseException] = []
        self.calls: list[SimpleNamespace] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        system_file = Path(cmd[cmd.index("--system-prompt-file") + 1])
        self.calls.append(
            SimpleNamespace(
                cmd=cmd,
                system_file=system_file,
                system=system_file.read_text("utf-8"),
                cwd_files=sorted(p.name for p in Path(kwargs["cwd"]).iterdir()),
                **kwargs,
            )
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        stdout, stderr, code = outcome
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """A ClaudeCodeProvider factory wired to a FakeClaude; ``slept`` lists its backoffs."""
    fake, slept = FakeClaude(), []
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # its empty working directories
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: "/opt/bin/claude")
    monkeypatch.setattr(claude_code.subprocess, "run", fake)
    monkeypatch.setattr(claude_code, "time", SimpleNamespace(sleep=slept.append))

    def provider(*outcomes: Any, model: str | None = None, **kwargs: Any) -> ClaudeCodeProvider:
        fake.outcomes.extend(outcomes)
        return ClaudeCodeProvider(model, **kwargs)

    return SimpleNamespace(provider=provider, fake=fake, slept=slept)


def test_claude_is_asked_without_tools_and_with_the_prompts_off_the_command_line(
    claude, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")  # set when since-cutoff runs inside Claude Code
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("SINCE_CUTOFF_TEST_VAR", "kept")
    usage = {"input_tokens": 12, "output_tokens": 3}
    p = claude.provider(answer("ok", total_cost_usd=0.002, usage=usage), model="opus", effort="low")
    system = "Answer with code only.\nÜnïcode and a second line."
    out = p.complete(system, "Write a function.\nWith two lines.")

    call = claude.fake.calls[0]
    assert call.cmd[:2] == ["/opt/bin/claude", "-p"]
    assert call.cmd[call.cmd.index("--model") + 1] == "opus"
    assert call.cmd[call.cmd.index("--effort") + 1] == "low"
    assert call.cmd[call.cmd.index("--tools") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in call.cmd
    assert all("\n" not in arg for arg in call.cmd)  # Windows' claude.cmd cuts at a newline
    assert call.input == "Write a function.\nWith two lines." and call.system == system
    # No project context: the call runs in a folder of its own that holds only its system
    # prompt, removed afterwards.
    assert call.cwd_files == [call.system_file.name] and call.system_file.parent == Path(call.cwd)
    assert Path(call.cwd) != Path.cwd() and not any(Path(call.cwd).iterdir())
    assert "CLAUDECODE" not in call.env and "CLAUDE_CODE_ENTRYPOINT" not in call.env
    assert call.env["SINCE_CUTOFF_TEST_VAR"] == "kept"
    assert call.timeout == FIRST_CALL_TIMEOUT  # until a call has worked
    assert (out.text, out.cost_usd, out.input_tokens, out.output_tokens) == ("ok", 0.002, 12, 3)
    assert p.key == "claude-code:opus@low"


def test_the_working_directory_goes_with_the_provider(claude) -> None:
    """Each provider left an empty folder in the temporary directory, one per run."""
    p = claude.provider(answer())
    p.complete("s", "u")
    workdir = Path(claude.fake.calls[0].cwd)
    assert workdir.is_dir()
    del p
    gc.collect()
    assert not workdir.exists()


def test_the_model_that_wrote_most_of_the_answer_is_the_model_tested(claude) -> None:
    usage = {"claude-haiku-4-5": {"outputTokens": 40}, "claude-opus-4-1": {"outputTokens": 900}}
    p = claude.provider(answer(modelUsage=usage))
    assert p.key == "claude-code:default" and p.model_name is None
    assert p.complete("s", "u").model == "claude-opus-4-1"
    assert (p.model_name, p.key) == ("claude-opus-4-1", "claude-code:claude-opus-4-1")
    assert "--model" not in claude.fake.calls[0].cmd and "--effort" not in claude.fake.calls[0].cmd


def test_a_claude_that_hangs_on_the_first_call_fails_fast(claude) -> None:
    """An expired login makes the CLI wait forever: the first call gets 180 s, not 600."""
    p = claude.provider(subprocess.TimeoutExpired("claude", FIRST_CALL_TIMEOUT), timeout=600)
    with pytest.raises(ProviderError) as info:
        p.complete("s", "u")
    assert str(info.value).startswith("Claude Code did not answer within 180s")
    assert "claude auth login" in str(info.value)
    assert len(claude.fake.calls) == 1


def test_after_a_success_a_timeout_is_retried_with_the_full_timeout(claude) -> None:
    timeout = subprocess.TimeoutExpired("claude", 600)
    p = claude.provider(answer("first"), timeout, answer("second"), timeout=600)
    assert p.complete("s", "u").text == "first"
    assert p.complete("s", "u").text == "second"
    assert [c.timeout for c in claude.fake.calls] == [FIRST_CALL_TIMEOUT, 600, 600]
    assert claude.slept == []  # a timeout already waited long enough

    p = claude.provider(answer(), timeout, timeout, timeout, timeout=600)
    p.complete("s", "u")
    with pytest.raises(ProviderError, match=r"^claude call failed: claude timed out after 600s$"):
        p.complete("s", "u")


def test_overload_and_rate_limits_are_retried_with_backoff(claude) -> None:
    p = claude.provider(
        failure("API Error: 529 Overloaded"), failure("Rate limit reached"), answer()
    )
    assert p.complete("s", "u").text == "done"
    assert claude.slept == [2.0, 4.0]


def test_the_last_attempt_is_not_followed_by_a_wait(claude) -> None:
    """It slept 8 more seconds after the third overloaded answer, then failed anyway."""
    p = claude.provider(*[failure("API Error: 529 Overloaded")] * 3)
    with pytest.raises(ProviderError, match=r"^claude call failed: API Error: 529 Overloaded$"):
        p.complete("s", "u")
    assert len(claude.fake.calls) == 3 and claude.slept == [2.0, 4.0]


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (failure("Prompt is too long"), "claude call failed: Prompt is too long"),
        (failure("", subtype="error_max_turns"), "claude call failed: error_max_turns"),
        (
            ("", "error: unknown option '--effort'\n", 1),
            "claude call failed: error: unknown option '--effort'",
        ),
        (("Segmentation fault", "", 139), "claude call failed: Segmentation fault"),
        (("", "", 137), "claude call failed: exit code 137"),
        (("[1, 2]\n", "", 0), "claude call failed: [1, 2]"),
    ],
    ids=["model-error", "error-subtype", "stderr", "not-json", "killed", "not-an-object"],
)
def test_other_failures_are_reported_at_once(claude, outcome, message) -> None:
    with pytest.raises(ProviderError) as info:
        claude.provider(outcome).complete("s", "u")
    assert str(info.value) == message
    assert len(claude.fake.calls) == 1 and claude.slept == []


def test_a_claude_that_cannot_start_or_is_missing_is_a_provider_error(claude, monkeypatch) -> None:
    with pytest.raises(ProviderError, match=r"^could not run claude: \[Errno 8\] Exec format"):
        claude.provider(OSError(8, "Exec format error")).complete("s", "u")
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="the `claude` CLI was not found on PATH"):
        make_provider("claude-code:sonnet")


def test_claude_code_specs_make_claude_code_providers(claude) -> None:
    p = make_provider("claude:sonnet", effort="high", timeout=30)
    assert isinstance(p, ClaudeCodeProvider)
    assert (p.model, p.effort, p.timeout) == ("sonnet", "high", 30)
    assert p.key == "claude-code:sonnet@high"
    assert make_provider("claude-code").model is None


# ------------------------------------------------------------------ HTTP APIs
@pytest.fixture
def anthropic(http_server, monkeypatch) -> AnthropicProvider:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return AnthropicProvider("claude-x", base_url=http_server.url + "/")


@pytest.fixture
def openai(http_server, monkeypatch) -> OpenAICompatibleProvider:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return OpenAICompatibleProvider(
        "openai", "gpt-x", base_url=http_server.url, key_env="OPENAI_API_KEY"
    )


def test_anthropic_answers_are_the_text_blocks(anthropic, http_server) -> None:
    content = [
        {"type": "thinking", "thinking": "Let me think."},
        {"type": "text", "text": "def f():\n"},
        {"type": "text", "text": "    pass\n"},
    ]
    http_server.routes["/v1/messages"] = [Reply(body={"content": content})]
    out = anthropic.complete("s", "u")
    assert out.text == "def f():\n    pass\n"
    assert (out.model, out.input_tokens, out.output_tokens) == ("claude-x", None, None)
    assert http_server.seen[0].headers["anthropic-version"] == "2023-06-01"
    assert anthropic.key == "anthropic:claude-x" and anthropic.model_name == "claude-x"


def test_an_overloaded_api_is_retried(anthropic, http_server, slept) -> None:
    ok = {"content": [{"type": "text", "text": "ok"}], "model": "claude-x-20260101"}
    http_server.routes["/v1/messages"] = [Reply(529, {"type": "error"}), Reply(body=ok)]
    out = anthropic.complete("s", "u")
    assert (out.text, out.model) == ("ok", "claude-x-20260101") and slept == [1.0]


def test_api_errors_name_the_api_and_the_status(anthropic, openai, http_server, slept) -> None:
    error = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
    http_server.routes["/v1/messages"] = [Reply(401, error)]
    http_server.routes["/chat/completions"] = [Reply(404, {"error": {"message": "no such model"}})]
    with pytest.raises(ProviderError) as info:
        anthropic.complete("s", "u")
    assert str(info.value).startswith(
        f"Anthropic API error: {http_server.url}/v1/messages: HTTP 401: "
    )
    assert "invalid x-api-key" in str(info.value)
    with pytest.raises(ProviderError, match=r"^openai API error: .*: HTTP 404: .*no such model"):
        openai.complete("s", "u")
    assert len(http_server.seen) == 2 and slept == []  # neither is retried


@pytest.mark.parametrize("which", ["anthropic", "openai"])
def test_a_reply_that_is_not_json_is_a_provider_error(which, request, http_server) -> None:
    """A wrong --base-url that reaches a web page, or a proxy's error page with status 200,
    ended the whole run with a JSONDecodeError traceback."""
    provider = request.getfixturevalue(which)
    page = Reply(body="<html><body>Welcome to the dashboard</body></html>")
    http_server.routes["/v1/messages"] = http_server.routes["/chat/completions"] = [page]
    with pytest.raises(ProviderError, match="did not answer with JSON"):
        provider.complete("s", "u")


@pytest.mark.parametrize(
    "reply",
    [
        {"choices": []},
        {"error": {"message": "upstream error"}},
        {"choices": [{"finish_reason": "error"}]},
        {"choices": [{"message": None}]},
        {"choices": None},
        ["not", "an", "object"],
        {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]},
        {"choices": [{"message": {"content": "ok"}}], "usage": ["tokens"]},
    ],
    ids=[
        "no-choices",
        "error-object",
        "no-message",
        "null-message",
        "null-choices",
        "a-list",
        "content-parts",
        "usage-list",
    ],
)
def test_unexpected_openai_replies_are_provider_errors(openai, http_server, reply) -> None:
    http_server.routes["/chat/completions"] = [Reply(body=reply)]
    with pytest.raises(ProviderError, match=r"^openai: unexpected response: "):
        openai.complete("s", "u")


@pytest.mark.parametrize(
    "reply",
    [
        ["not", "an", "object"],
        {"content": None},
        {"content": "just text"},
        {"content": [{"type": "text", "text": None}]},
        {"content": [], "usage": ["tokens"]},
    ],
    ids=["a-list", "null-content", "string-content", "null-text", "usage-list"],
)
def test_unexpected_anthropic_replies_are_provider_errors(anthropic, http_server, reply) -> None:
    """A gateway's JSON that is not a Messages API answer ended the whole run with an
    AttributeError or TypeError traceback: the engine only expects ProviderError."""
    http_server.routes["/v1/messages"] = [Reply(body=reply)]
    with pytest.raises(ProviderError, match=r"^anthropic: unexpected response: "):
        anthropic.complete("s", "u")


def test_an_empty_openai_answer_is_empty_text(openai, http_server) -> None:
    body = {
        "choices": [{"message": {"content": None, "refusal": "no"}}],
        "usage": {"prompt_tokens": 9},
    }
    http_server.routes["/chat/completions"] = [Reply(body=body)]
    out = openai.complete("s", "u")
    assert (out.text, out.model, out.input_tokens, out.output_tokens) == ("", "gpt-x", 9, None)
    assert (openai.key, openai.model_name) == ("openai:gpt-x", "gpt-x")


def test_base_urls_come_from_the_flag_then_the_environment(monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(key, "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1/")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example/")
    assert make_provider("openai:gpt-x").base_url == "https://proxy.example/v1"
    # OPENAI_BASE_URL is OpenAI's own variable: other providers keep their URL.
    assert make_provider("deepseek:deepseek-chat").base_url == "https://api.deepseek.com/v1"
    assert make_provider("ollama:qwen3:8b").base_url == "http://localhost:11434/v1"
    assert make_provider("anthropic:claude-x").base_url == "https://gateway.example"
    flag = make_provider("anthropic:claude-x", base_url="http://127.0.0.1:1/")
    assert flag.base_url == "http://127.0.0.1:1"


@pytest.mark.parametrize(
    ("flag", "env", "expected"),
    [
        # --base-url wins also when OPENAI_BASE_URL is set (#94: the variable used to win).
        ("http://127.0.0.1:1/", "https://proxy.example/v1", "http://127.0.0.1:1"),
        (None, "https://proxy.example/v1/", "https://proxy.example/v1"),
        (None, None, "https://api.openai.com/v1"),
    ],
    ids=["flag", "environment", "default"],
)
def test_openai_base_url_is_the_flag_then_the_environment_then_the_default(
    monkeypatch, flag, env, expected
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    if env is None:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", env)
    assert make_provider("openai:gpt-x", base_url=flag).base_url == expected


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("anthropic", "anthropic needs a model, e.g. anthropic:claude-sonnet-4-5"),
        ("openai-compatible:my-model", "openai-compatible needs --base-url"),
        ("deepseek:deepseek-chat", "DEEPSEEK_API_KEY is not set"),
    ],
)
def test_incomplete_specs_say_what_is_missing(monkeypatch, spec, message) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ProviderError) as info:
        make_provider(spec)
    assert str(info.value) == message


def test_a_local_openai_compatible_server_needs_no_key(http_server, monkeypatch) -> None:
    """The README calls OPENAI_API_KEY optional here; it failed with "OPENAI_API_KEY is not
    set", so a local vLLM or LM Studio server needed a made-up key."""
    reply = {"choices": [{"message": {"content": "ok"}}]}
    http_server.routes["/v1/chat/completions"] = [Reply(body=reply)]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    local = make_provider("openai-compatible:my-model", base_url=f"{http_server.url}/v1/")
    assert local.complete("s", "u").text == "ok" and local.key == "openai-compatible:my-model"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proxy")  # a server that wants one still gets it
    make_provider("openai-compatible:my-model", base_url=f"{http_server.url}/v1").complete("s", "u")
    assert [s.headers.get("authorization") for s in http_server.seen] == [None, "Bearer sk-proxy"]


def test_a_bare_unknown_name_is_asked_for_its_provider() -> None:
    with pytest.raises(ProviderError, match=r"Did you mean '<provider>:mystery'\? Providers: "):
        check_spec("mystery")
