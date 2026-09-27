# modelcontextprotocol/python-sdk

An excerpt of `pyproject.toml` and `uv.lock` from https://github.com/modelcontextprotocol/python-sdk, at the repository root.

- Commit: [`f1b6589088534632fef92238ee9750951e3c0185`](https://github.com/modelcontextprotocol/python-sdk/tree/f1b6589088534632fef92238ee9750951e3c0185) (2026-09-23)
- License: MIT, https://github.com/modelcontextprotocol/python-sdk/blob/f1b6589088534632fef92238ee9750951e3c0185/LICENSE

## What was kept

- `uv.lock`: 32 of its 134 packages: 5 of the 17 workspace members (`mcp`, `mcp-types`,
  `mcp-example-stories`, `mcp-everything-server`, and `mcp-structured-output-lowlevel`, a
  virtual one), 24 direct dependencies and 3 transitive ones. `[manifest]` lists the kept
  members and the first two build constraints. Dependency lists keep the kept packages;
  each `wheels` list is cut to its first wheel.
- `pyproject.toml`: `[project]` without authors, maintainers, classifiers and URLs; the
  extras; `[tool.uv]` without its build constraints; the kept lines of the four dependency
  groups and of the version hook's dependencies; `[tool.uv.workspace]` and
  `[tool.uv.sources]`. Other tool settings are left out.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- a uv workspace: members (editable and virtual) are not dependencies, and what they
  depend on (an example server's `click`, its dev group's `pyright`) is direct;
- `dynamic = ["dependencies"]`: mcp's own dependencies are only in the lock;
- dependency groups (`dev`, `docs`, `translate`, `codegen`) and a self-reference
  (`mcp[cli]`) in one;
- a git source, reported with its locked commit (`strict-no-cover`);
- per-platform and per-Python markers (`pywin32`, `tomli`).
