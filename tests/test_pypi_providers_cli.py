"""PyPI extraction, model providers (against local fakes) and the CLI."""

from __future__ import annotations

import io
import json
import sys
import threading
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from since_cutoff import cli, net
from since_cutoff.cache import DiskCache
from since_cutoff.errors import ProviderError
from since_cutoff.providers import make_provider, split_spec
from since_cutoff.providers.claude_code import ClaudeCodeProvider
from since_cutoff.pypi import PyPI, _extract_zip, _pick_artifact, _wheel_import_names
from tests.conftest import FakePyPI


# ----------------------------------------------------------------------- PyPI
def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_wheel_extraction_keeps_sources_and_blocks_path_traversal(tmp_path):
    blob = _zip(
        {
            "pkg/__init__.py": "x = 1\n",
            "pkg/_native.so": "binary",
            "pkg/py.typed": "",
            "../evil.py": "boom",
            "pkg-1.0.dist-info/top_level.txt": "pkg\n_private\n",
        }
    )
    names = _extract_zip(blob, tmp_path / "out", strip_first=False)
    root = tmp_path / "out"
    assert (tmp_path / "out/pkg/__init__.py").exists()
    assert (tmp_path / "out/pkg/py.typed").exists()
    assert not (tmp_path / "out/pkg/_native.so").exists()
    assert not (tmp_path / "evil.py").exists()
    assert _wheel_import_names(blob, names, root) == ["pkg"]


def test_import_names_without_top_level_txt(tmp_path):
    blob = _zip(
        {
            "alpha/__init__.py": "",
            "beta.py": "",
            "tests/test_x.py": "",
            "alpha-1.dist-info/RECORD": "",
        }
    )
    files = ["alpha/__init__.py", "beta.py", "tests/test_x.py", "alpha-1.dist-info/RECORD"]
    _extract_zip(blob, tmp_path, strip_first=False)
    assert _wheel_import_names(blob, files, tmp_path) == ["alpha", "beta"]


def test_pick_artifact_prefers_pure_wheels_then_sdists():
    files = (
        {"filename": "p-1-cp312-cp312-win_amd64.whl", "size": 10},
        {"filename": "p-1-py3-none-any.whl", "size": 50},
        {"filename": "p-1.tar.gz", "size": 5},
    )
    assert _pick_artifact(files)["filename"] == "p-1-py3-none-any.whl"
    assert _pick_artifact(({"filename": "p-1.tar.gz"},))["filename"] == "p-1.tar.gz"
    assert _pick_artifact(()) is None


def test_version_at_ignores_prereleases_and_later_releases(cache):
    pypi = FakePyPI(
        cache,
        {
            "x": [
                ("1.0", "2025-01-01"),
                ("1.1rc1", "2025-02-01"),
                ("1.1", "2025-06-01"),
                ("2.0", "2025-09-01"),
            ]
        },
        {},
    )
    assert pypi.version_at("x", date(2025, 5, 31)).version == "1.0"
    assert pypi.version_at("x", date(2025, 6, 1)).version == "1.1"
    assert pypi.version_at("x", date(2024, 1, 1)) is None
    assert pypi.latest("x").version == "2.0"


@pytest.mark.network
def test_real_pypi_metadata(tmp_path):
    pypi = PyPI(DiskCache(tmp_path))
    rel = pypi.version_at("packaging", date(2024, 1, 1))
    assert rel is not None and rel.version == "23.2"


# ------------------------------------------------------------------ providers
def test_split_spec():
    assert split_spec("ollama:qwen3:8b") == ("ollama", "qwen3:8b")
    assert split_spec("claude-code") == ("claude-code", None)
    assert split_spec("OpenAI:gpt-5.4") == ("openai", "gpt-5.4")


def test_unknown_provider_and_missing_keys(monkeypatch):
    with pytest.raises(ProviderError, match="unknown provider"):
        make_provider("nope:model")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        make_provider("anthropic:claude-x")
    with pytest.raises(ProviderError, match="needs a model"):
        make_provider("openai")


class _Handler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict[str, str], dict]]] = []
    reply: ClassVar[dict] = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(
            (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
        )
        data = json.dumps(type(self).reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.requests = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_anthropic_provider_protocol(server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _Handler.reply = {
        "model": "claude-x",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    p = make_provider("anthropic:claude-x", base_url=server)
    out = p.complete("sys", "user")
    path, headers, body = _Handler.requests[0]
    assert out.text == "hi" and out.output_tokens == 1
    assert path == "/v1/messages" and headers["x-api-key"] == "sk-test"
    assert body["system"] == "sys" and body["messages"] == [{"role": "user", "content": "user"}]
    assert "temperature" not in body


def test_openai_compatible_provider_protocol(server, monkeypatch):
    _Handler.reply = {
        "model": "qwen",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    }
    p = make_provider("ollama:qwen3:8b", base_url=server)
    assert p.complete("sys", "user").text == "ok"
    path, headers, body = _Handler.requests[0]
    assert path == "/chat/completions" and "authorization" not in headers
    assert body["model"] == "qwen3:8b" and body["messages"][0] == {
        "role": "system",
        "content": "sys",
    }


def test_openrouter_requests_carry_app_attribution(server, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _Handler.reply = {"model": "qwen", "choices": [{"message": {"content": "ok"}}]}

    make_provider("openrouter:qwen/qwen3-coder", base_url=server).complete("sys", "user")
    _path, headers, body = _Handler.requests[0]
    assert headers["authorization"] == "Bearer or-test"
    assert headers["http-referer"] == "https://github.com/MohammadHijjawi97/since-cutoff"
    assert headers["x-openrouter-title"] == headers["x-title"] == "since-cutoff"
    assert body["model"] == "qwen/qwen3-coder"

    # Only OpenRouter gets them.
    make_provider("openai:gpt-x", base_url=server).complete("sys", "user")
    _path, headers, _body = _Handler.requests[1]
    assert not {"http-referer", "x-title", "x-openrouter-title"} & set(headers)


def test_http_errors_become_provider_errors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    p = make_provider("openai:gpt-x", base_url="http://127.0.0.1:9")
    monkeypatch.setattr(
        net, "request", lambda *a, **k: (_ for _ in ()).throw(net.HTTPError("u", 401, "bad key"))
    )
    with pytest.raises(ProviderError, match="401"):
        p.complete("s", "u")


FAKE_CLAUDE = r"""
import json, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
assert "-p" in args and args[args.index("--tools") + 1] == "" and "--system-prompt-file" in args
mode = open(sys.argv[0] + ".mode").read().strip()
if mode == "ok":
    model = args[args.index("--model") + 1] if "--model" in args else "claude-sonnet-4-5-20250929"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "echo:" + prompt,
                      "total_cost_usd": 0.001, "usage": {"input_tokens": 5, "output_tokens": 2},
                      "modelUsage": {model: {"outputTokens": 2}}}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Failed to authenticate. API Error: 401 OAuth access token has expired."}))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: sys.executable)

    def make(mode: str, model: str | None = None) -> ClaudeCodeProvider:
        Path(str(script) + ".mode").write_text(mode, encoding="utf-8")
        p = ClaudeCodeProvider(model, retries=1)
        p.exe_command = [sys.executable, str(script)]
        return p

    return make


def test_claude_code_provider_resolves_the_real_model(fake_claude):
    p = fake_claude("ok")
    out = p.complete("be brief", "hello")
    assert out.text == "echo:hello"
    assert p.model_name == "claude-sonnet-4-5-20250929"
    assert p.key == "claude-code:claude-sonnet-4-5-20250929"


def test_claude_code_provider_explains_expired_logins(fake_claude):
    with pytest.raises(ProviderError, match="claude auth login"):
        fake_claude("auth").complete("s", "u")


# ------------------------------------------------------------------------ CLI
def test_default_subcommand_is_run():
    assert cli._argv_with_default([]) == ["run"]
    assert cli._argv_with_default(["--apply"]) == ["run", "--apply"]
    assert cli._argv_with_default(["scan", "."]) == ["scan", "."]
    assert cli._argv_with_default(["--version"]) == ["--version"]


def test_cli_models_offline(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path))
    assert cli.main(["models", "sonnet-4-5", "--offline"]) == 0
    assert "claude-sonnet-4-5" in capsys.readouterr().out


def test_cli_reports_errors_without_traceback(capsys, tmp_path):
    assert cli.main(["scan", str(tmp_path), "--cutoff", "2025-01"]) == 1
    assert "no dependencies found" in capsys.readouterr().err


def test_cli_rejects_bad_cutoff(capsys, tmp_path):
    (tmp_path / "requirements.txt").write_text("attrs==25.1.0\n")
    assert cli.main(["scan", str(tmp_path), "--cutoff", "someday"]) == 1
    assert "not a date" in capsys.readouterr().err


def test_cli_unapply(tmp_path, capsys):
    (tmp_path / "AGENTS.md").write_text(
        "keep\n\n<!-- since-cutoff:start -->\nx\n<!-- since-cutoff:end -->\n"
    )
    assert cli.main(["unapply", str(tmp_path)]) == 0
    assert (tmp_path / "AGENTS.md").read_text() == "keep\n"


def test_target_file_choice(tmp_path):
    assert cli._target_file(tmp_path, None).name == "AGENTS.md"
    (tmp_path / "CLAUDE.md").write_text("x")
    assert cli._target_file(tmp_path, None).name == "CLAUDE.md"
    (tmp_path / "AGENTS.md").write_text("y")
    assert cli._target_file(tmp_path, None).name == "AGENTS.md"
    assert cli._target_file(tmp_path, "docs/RULES.md") == tmp_path / "docs/RULES.md"
