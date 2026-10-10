# Troubleshooting

Use the message printed by `since-cutoff` as the starting point. These steps are diagnostic advice, not a claim that remote APIs, local credentials or a model have been tested.

## No dependency could be checked

The CLI says **"No dependency could be checked"** after the scan when it cannot obtain a usable dependency comparison. Read the per-package reasons immediately above this message. Confirm that the lockfile or requirements are present and that the package index is reachable. Corporate proxies may need `HTTPS_PROXY` / `NO_PROXY`; a company TLS certificate may need `SSL_CERT_FILE`. For a private package index, do not paste tokens or internal URLs into a public issue.

Start with the no-model command:

```bash
since-cutoff scan
```

If the package was skipped due to a download-size or source-discovery limit, review the limit and project configuration rather than assuming the model made an error. Network access is required for uncached package metadata; scanning does **not** submit your project to an LLM.

## Not written: there is no terminal to ask in

The message **"Not written: there is no terminal to ask in"** means an interactive `sync` confirmation is unavailable (for example in CI). Review the proposed notes first. Then use `since-cutoff sync --check` for a non-mutating CI status check, or `since-cutoff sync --yes` only when the intended update is approved. These switches are implemented in `src/since_cutoff/cli.py`.

```bash
since-cutoff sync --check
```

## The notes block was edited by hand

The CLI warns when **"the notes block was edited by hand"**. Preserve manual edits until reviewed. If replacing the hand-edited block is intentional, `since-cutoff sync --force` is the explicit override; do not use it as an automatic recovery step.

## Unknown model or training cutoff

Use `since-cutoff models` to examine recognized model names. An explicit cutoff is appropriate when the model is not in the bundled registry, but that date must come from reliable documentation rather than a guess. The model used by `run` remains a separate choice from its training cutoff.

```bash
since-cutoff models
```

## An MCP client cannot start or times out

If the MCP client cannot find `uvx`, check the installed `uv` executable and the client's environment/path. A first `project_changes` call may need more time while packages are inspected. Increase the client's tool timeout or run a standalone `scan` first to separate package metadata delays from a transport issue. Do not add API keys to a diagnostic log.

## Run fails to find a model provider or a checker

The `run` command can invoke an external model through its configured provider, unlike `scan`, `sync`, `status` and MCP inspection tools. Confirm the selected provider/CLI is installed and reachable, or use an offline scan until the provider is available. An optional type checker such as `basedpyright` must be installed before its path can pass; do not claim a model-side or type-checking result from a missing executable.

When reporting a bug, provide the command, `since-cutoff --version`, OS/Python versions and the exact sanitized diagnostic message. Never include an API key, private package index URL or customer source code.
