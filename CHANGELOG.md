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
