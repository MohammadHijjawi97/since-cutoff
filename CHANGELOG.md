# Changelog

## 0.1.0 (unreleased)

First release.

- `since-cutoff scan`: find the dependency versions a model could have seen at its training
  cutoff and statically diff their public API against the versions your project uses.
- `since-cutoff run`: probe the model on the most relevant breaking changes, score its code with
  basedpyright against both versions (stale / wrong / deprecated / correct), write verified
  AGENTS.md notes and measure their effect on held-out tasks.
- Lockfiles: uv.lock, poetry.lock, pdm.lock, pylock.toml, Pipfile.lock; requirements files;
  `.venv` metadata; pyproject.toml.
- Providers: Claude Code CLI, Anthropic API, OpenAI, OpenRouter, DeepSeek, Ollama and any
  OpenAI-compatible endpoint.
- Claude Code plugin with a `since-cutoff` skill.
- `--effort` (default `low`) for Claude Code calls, and only API-knowledge errors (unknown names,
  imports and parameters, missing arguments, arity) count as stale or wrong; type-strictness
  complaints do not.
- A reproducible example project in `examples/agent-app` and a first real run in the README.
