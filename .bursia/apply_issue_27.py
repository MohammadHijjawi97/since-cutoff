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
    "src/since_cutoff/hosts.py",
    "from since_cutoff.models import bare_model_id",
    "from since_cutoff.models import PROVIDER_ALIASES, bare_model_id",
)

replace_once(
    "src/since_cutoff/hosts.py",
    '''    (re.compile(r"grok-"), "xai"),
    (re.compile(r"(?:mistral|codestral|devstral|magistral|ministral)-"), "mistral"),
)''',
    '''    (re.compile(r"grok-"), "xai"),
    (re.compile(r"(?:mistral|codestral|devstral|magistral|ministral)-"), "mistral"),
    (re.compile(r"(?:qwen|qwq|qvq)"), "alibaba"),
    (re.compile(r"kimi-"), "moonshotai"),
    (re.compile(r"glm-"), "zai"),
    (re.compile(r"llama-"), "llama"),
)''',
)

replace_once(
    "src/since_cutoff/hosts.py",
    '''    name = (provider or "").strip().lower()
    if name in _CALLABLE:
        return f"{_CALLABLE[name]}:{model}"
    return _maker_spec(model) or (f"{name}:{model}" if name else model)
''',
    '''    name = (provider or "").strip().lower()
    if name in _CALLABLE:
        return f"{_CALLABLE[name]}:{model}"

    maker_spec = _maker_spec(model)
    if maker_spec is not None:
        maker = maker_spec.split(":", 1)[0]
        if maker in {"alibaba", "moonshotai", "zai", "llama"} and name:
            if PROVIDER_ALIASES.get(name) == maker:
                return maker_spec
            return f"{name}:{model}"
        return maker_spec
    return f"{name}:{model}" if name else model
''',
)

replace_once(
    "src/since_cutoff/models.py",
    '''    "xai": "xai",
    "mistral": "mistral",
}''',
    '''    "xai": "xai",
    "mistral": "mistral",
    "alibaba": "alibaba",
    "qwen": "alibaba",
    "dashscope": "alibaba",
    "moonshotai": "moonshotai",
    "moonshot": "moonshotai",
    "kimi": "moonshotai",
    "zai": "zai",
    "zhipu": "zai",
    "glm": "zai",
    "llama": "llama",
    "meta": "llama",
}''',
)

replace_once(
    "src/since_cutoff/cli.py",
    '''    priority = {"anthropic": 0, "openai": 1, "google": 2, "deepseek": 3, "xai": 4, "mistral": 5}''',
    '''    priority = {
        "anthropic": 0,
        "openai": 1,
        "google": 2,
        "deepseek": 3,
        "xai": 4,
        "mistral": 5,
        "alibaba": 6,
        "moonshotai": 7,
        "zai": 8,
        "llama": 9,
    }''',
)

replace_once(
    "tests/test_hosts.py",
    '''@pytest.mark.parametrize("model", ["my-local-model", "openai", "deepseek/"])
def test_a_model_name_since_cutoff_cannot_place_is_reported_not_guessed(''',
    '''@pytest.mark.parametrize(
    ("model", "spec"),
    [
        ("dashscope/qwen3-coder-plus", "alibaba:qwen3-coder-plus"),
        ("moonshot/kimi-k2.7-code", "moonshotai:kimi-k2.7-code"),
        ("glm-4.6", "zai:glm-4.6"),
        ("llama-3.3-70b-instruct", "llama:llama-3.3-70b-instruct"),
    ],
)
def test_aider_recognises_additional_model_makers(
    root: Path, home: Path, model: str, spec: str
) -> None:
    write(root / ".aider.conf.yml", f"model: {model}\\n")
    assert detect(root, home) == (spec, ".aider.conf.yml")


@pytest.mark.parametrize(
    ("model", "spec"),
    [
        ("qwen3-coder-plus", "alibaba:qwen3-coder-plus"),
        ("kimi-k2.7-code", "moonshotai:kimi-k2.7-code"),
        ("glm-4.6", "zai:glm-4.6"),
        ("llama-3.3-70b-instruct", "llama:llama-3.3-70b-instruct"),
    ],
)
def test_opencode_bare_ids_resolve_to_their_makers(
    root: Path, home: Path, model: str, spec: str
) -> None:
    write(root / "opencode.json", json.dumps({"model": model}))
    assert detect(root, home) == (spec, "opencode.json")


@pytest.mark.parametrize("model", ["my-local-model", "openai", "deepseek/"])
def test_a_model_name_since_cutoff_cannot_place_is_reported_not_guessed(''',
)

replace_once(
    "tests/test_models.py",
    '''def test_lookup_handles_dated_and_dotted_ids(registry):
    assert registry.require("claude-sonnet-4.5", "claude-code").knowledge == date(2025, 7, 31)
    assert registry.require("claude-haiku-4-5-20251001").knowledge == date(2025, 2, 28)
    assert registry.require("qwen3-coder:30b", "ollama").knowledge == date(2025, 4, 30)


''',
    '''def test_lookup_handles_dated_and_dotted_ids(registry):
    assert registry.require("claude-sonnet-4.5", "claude-code").knowledge == date(2025, 7, 31)
    assert registry.require("claude-haiku-4-5-20251001").knowledge == date(2025, 2, 28)
    assert registry.require("qwen3-coder:30b", "ollama").knowledge == date(2025, 4, 30)


def test_lookup_prefers_additional_model_makers_over_resellers(tmp_path):
    registry = ModelRegistry(DiskCache(tmp_path), offline=True)
    assert registry.require("kimi-k2.7-code").provider == "moonshotai"
    assert registry.require("qwen3-coder-plus", "qwen").provider == "alibaba"
    assert registry.require("glm-4.6", "glm").provider == "zai"
    assert registry.require("llama-3.3-70b-instruct", "meta").provider == "llama"


''',
)

replace_once(
    "tests/test_mcp_server.py",
    '''def test_model_ids_prefer_the_model_maker_over_resellers(tools: Tools) -> None:
    # gpt-5.4 is listed by github-copilot too, which sorts first alphabetically.
    assert tools.model_cutoff("gpt-5.4").startswith("gpt-5.4 (openai")


''',
    '''def test_model_ids_prefer_the_model_maker_over_resellers(tools: Tools) -> None:
    # gpt-5.4 is listed by github-copilot too, which sorts first alphabetically.
    assert tools.model_cutoff("gpt-5.4").startswith("gpt-5.4 (openai")
    assert tools.model_cutoff("kimi-k2.7-code").startswith("kimi-k2.7-code (moonshotai")


''',
)

replace_once(
    "CHANGELOG.md",
    '''## 0.5.0 - 2026-09-28

''',
    '''## 0.5.0 - 2026-09-28

- Bare and routed Qwen, Kimi, GLM and Llama model ids are recognised as their model makers
  (Alibaba, Moonshot AI, Z.ai and Llama), so cutoff lookup prefers first-party entries over
  reseller listings.

''',
)
