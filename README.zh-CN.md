<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo.svg" width="96" height="96" alt="since-cutoff 标志">
</picture></p>

<h1 align="center">since-cutoff</h1>

**面向借助编程 Agent 开发的 Python 项目：since-cutoff 找出在模型训练截止日期之后发生变化的依赖 API，测出模型会写错其中哪些，再用简短的 AGENTS.md 说明来纠正；每条说明要么通过了类型检查器的验证，要么直接取自 API 对比结果。**

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero.svg" width="640" alt="你的编程模型学会你的依赖库时，那些库还没有改版。Claude Opus 4.6 在一个示例项目上的结果（用 since-cutoff 0.1.0 测得）：在探测的 16 个 API 变更中，有 7 个它用了之后已被移除的名称或参数；加入说明后，20 个留出任务的正确率从 5% 提高到 65%。试试：uvx since-cutoff scan（不调用模型，不需要 API key）。">
</picture></p>

[![PyPI](https://img.shields.io/pypi/v/since-cutoff)](https://pypi.org/project/since-cutoff/)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/LICENSE)
![Status: beta](https://img.shields.io/badge/status-beta-orange)

[English](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md) | **简体中文** | [Español](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md) | [Français](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md)

## 问题是什么

每个模型都有训练截止日期，而你的 lockfile 一直在更新。一个库在截止日期之后改了公开 API，学过旧版本的模型仍会按旧版本的写法调用。这样的代码有些会在导入或调用时报错；有些还能运行，因为旧写法只是被标记为弃用。

下面是 `since-cutoff scan` 针对 Claude Sonnet 4.5（训练截止 2025 年 7 月）在[示例项目](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app)中找到的部分变更。这个项目的 9 个依赖中有 6 个锁定在当前版本，另外 3 个没有锁定（工具使用它们的最新版本）：

| 库 | 截止日期时的版本 | 锁定版本 | 哪里会出错 |
|---|---|---|---|
| anthropic | 0.60.0 | 1.8.0 | `messages.create(temperature=..., top_p=..., top_k=...)` 不再被接受 |
| huggingface-hub | 0.34.3 | 2.0.0 | `hf_hub_download(resume_download=..., force_filename=..., local_dir_use_symlinks=...)` 这些参数已移除 |
| langchain-core | 0.3.72 | 1.6.5 | `retriever.get_relevant_documents()` 和 `llm.predict()` 已移除 |
| openai | 1.98.0 | 3.19.2 | 21 个破坏性变更，6 个新的弃用 |

在这个项目中，9 个依赖里有 7 个在截止日期之后改了公开 API。静态对比共标出 317 个破坏性变更和 23 个新的弃用；其中一部分属于内部实现，探测时会跳过。

这不是某一个模型或某一家厂商的问题。在 36 个常用的 Python AI 库和来自 OpenAI、Anthropic、Google、xAI、DeepSeek、Qwen、Moonshot、Mistral 的 21 个模型上，即使是测试中最新的模型（Claude Opus 5.5，训练截止 2026 年 6 月），这 36 个库里也有 20 个在它的截止日期之后发生了公开 API 的破坏性变更（[完整结果](https://mohammadhijjawi97.github.io/since-cutoff/ai-stack.html)，英文）：

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/ai-stack-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/ai-stack.svg" width="860" alt="柱状图：对来自 8 家厂商的 21 个模型，分别统计 36 个 Python AI 库中有多少在模型训练截止日期之后发生了公开 API 的破坏性变更、有多少发布了新的主版本。GPT-4o 为 36 个中的 21 个（其中 13 个库当时还不存在），2025 年初截止的模型为 33 个，Claude Opus 5.5（2026 年 6 月）为 20 个。">
</picture></p>

since-cutoff 针对这个问题做三件事：

1. **`scan`**：为每个依赖找出模型截止日期当天或之前发布的最新版本，并把它的公开 API 与你锁定的版本做对比。不调用模型，不需要 API key。
2. **`run`**：在不给工具、不给文档的情况下，让模型完成需要用到这些变更 API 的简短编程任务，再用类型检查器针对*两个*版本分别给每个回答打分：过时（stale）、写错（wrong）、已弃用（deprecated）或正确（correct）。全程没有 LLM 当评判。
3. **说明**：为每个失败项写一条一行的 AGENTS.md / CLAUDE.md 说明。模型写的说明只有在其中的示例能通过你所用版本的类型检查时才保留，否则改用从 API 对比结果得出的一句变更陈述；然后在留出任务（held-out）上，分别在有说明和无说明的情况下重新测试模型。

同样的对比结果也通过 [MCP 服务器](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md#在任意-agent-中使用mcp)提供给 Agent，通过 [GitHub Action 和 pre-commit 钩子](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md#在-ci-中使用)提供给 CI。

## 快速开始

```bash
# 列出模型截止日期之后的 API 变更（不调用模型，不需要 API key）
uvx since-cutoff scan

# 测试模型、生成经过验证的说明，并写入 AGENTS.md
uvx since-cutoff run --apply
```

也可以用 `pipx install since-cutoff`（或 `pip install since-cutoff`）安装，然后运行 `since-cutoff`。请在项目根目录运行：它会读取 `uv.lock`、`poetry.lock`、`pdm.lock`、`pylock.toml`、`Pipfile.lock`、`requirements*.txt`、`pyproject.toml` 或 `.venv`。不加 `--model` 时，它测试的是你的 Claude Code 当前使用的模型；要测试其他模型，请传入 `--model`（见[选择模型](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md#选择模型)）。`scan` 不花钱；`run` 会把提示词发送给模型服务商，消耗你的 API 额度或 Claude Code 用量。

`scan` 在示例项目上的输出：

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/scan.svg" width="100%" alt="在示例项目上运行 since-cutoff scan --model anthropic:claude-sonnet-4-5：9 个依赖中有 7 个在截止日期之后改了 API；静态对比：317 个破坏性变更，23 个新的弃用；一张表列出每个包的锁定版本、截止日期时的版本和变更数量，并为每个包给出一个变更示例"></p>

### 在 Claude Code 中使用

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

然后让 Claude“检查一下你对我们哪些依赖的了解已经过时了”，或者运行 `/since-cutoff:since-cutoff`。技能负责运行命令行工具；测量本身由一个全新的、不带任何工具的模型副本完成，所以 Agent 没法给自己打分。插件还会启动 [MCP 服务器](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md#在任意-agent-中使用mcp)，这样 Claude 可以在写代码之前先查一下某个库改了什么。

### 在其他编程 Agent 中使用

```bash
npx skills add MohammadHijjawi97/since-cutoff
```

这条命令通过开源的 [skills](https://github.com/vercel-labs/skills) 命令行工具，把同一个技能安装到 Codex、Cursor、Gemini CLI、GitHub Copilot、OpenCode 以及其他读取 `SKILL.md` 的 Agent 中。在 Claude Code 之外，需要告诉工具要测试哪个模型，例如 `since-cutoff scan --model openai:gpt-5.4`，并按下文的方法添加 MCP 服务器。

效果较好的提示词：

- “我们的哪些依赖在你的训练截止日期之后改了公开 API？”Agent 会运行 `since-cutoff scan` 或调用 MCP 工具 `project_changes`。
- “测一下你实际会写错其中哪些变更，并把经过验证的说明加到 AGENTS.md。”Agent 会先征求你的同意，再运行 `since-cutoff run --quick --apply`。
- “写 httpx 代码之前，先查一下 httpx 在你的截止日期之后改了什么。”Agent 会调用 MCP 工具 `api_changes`。

### 选择模型

| `--model` | 使用 | 需要 |
|---|---|---|
| `claude-code`（默认） | 你的 Claude Code 登录（订阅或 API key），当前模型 | `claude` 命令行工具 |
| `claude-code:sonnet`、`claude-code:claude-haiku-4-5` | 指定的 Claude 模型 | `claude` 命令行工具 |
| `anthropic:<model>` | Anthropic API | `ANTHROPIC_API_KEY` |
| `openai:<model>` | OpenAI API | `OPENAI_API_KEY` |
| `openrouter:<vendor/model>` | OpenRouter | `OPENROUTER_API_KEY` |
| `deepseek:<model>` | DeepSeek API | `DEEPSEEK_API_KEY` |
| `ollama:<model>` | 本地 Ollama | 正在运行的 Ollama |
| `openai-compatible:<model>` | 任何 OpenAI 兼容的服务 | `--base-url`，可选 `OPENAI_API_KEY` |

训练截止日期来自 [models.dev](https://models.dev)（内置一份快照，可离线使用）。`since-cutoff models sonnet` 可以列出这些日期；`--cutoff 2025-07` 可以覆盖截止日期；不加 `--model` 运行 `since-cutoff scan --cutoff 2025-07`，则只按这个日期扫描。

## 实测结果

两个 Claude 模型在 [`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app) 这个有 9 个依赖的示例项目上的结果，**用 since-cutoff 0.1.0 测得**，任务和说明由 Claude Opus 4.6 编写：

| | Claude Haiku 4.5 | Claude Opus 4.6 |
|---|---|---|
| 训练截止 | 2025 年 2 月 | 2025 年 5 月 |
| 探测的 API 变更 | 20 | 16 |
| **过时** / 写错 / 已弃用 / 正确 | **5** / 1 / 2 / 12 | **7** / 0 / 3 / 6 |
| 出现过时用法的库 | 探测的 5 个中有 3 个 | 探测的 4 个中有 2 个 |
| 写出的说明（其中通过类型检查验证的） | 8 条（7 条），约 391 token | 10 条（7 条），约 437 token |
| **留出任务正确率：无说明 -> 有说明** | **14% -> 57%**（14 对） | **5% -> 65%**（20 对） |
| 原本答对的 API 在加入说明后 | 6/6 仍正确 | 6/6 仍正确 |

*留出任务*是对每个失败变更的探测任务的改写；每个任务回答两次，一次不带说明，一次带说明，用同样的方式打分。最后一行重新检查模型原本就答对的 API，用来发现会起反作用的说明。

在这个样本中，更强的模型并没有更可靠：Opus 4.6 写出了在它截止日期之后已被移除的 API，例如 `anthropic.HUMAN_PROMPT` 配合 `client.completions`。两次运行中出现的过时代码（每一处都对模型学过的版本有效，对锁定版本无效）：`messages.create(temperature=...)`（anthropic 1.8），`hf_hub_download(resume_download=...)`、`local_dir_use_symlinks=...`、`force_filename=...` 和 `proxies=...`（huggingface-hub 2.0），以及 `client.beta.vector_stores`（openai 3.x）。

Claude Haiku 4.5 那次运行写出的说明（节选，原文照录）：

```markdown
<!-- since-cutoff:start -->
## Library changes after the model's training cutoff

**anthropic 1.8.0**
- `temperature=...` was removed from `messages.create()` in anthropic 1.8.0. Omit the `temperature` parameter entirely; there is no replacement.

**huggingface-hub 2.0.0**
- `hf_hub_download(..., resume_download=True)`: The `resume_download` parameter was removed in huggingface-hub 2.0.0. Omit it; downloads resume automatically.

**openai 3.19.2**
- `client.beta.vector_stores` is removed in openai 3.19.2. Use `client.vector_stores` instead.
<!-- since-cutoff:end -->
```

下图是 Claude Opus 4.6 那次运行的终端摘要，用 0.1.0 录制。探测结果与上表一致。卡片上的对比数量是 0.1.0 的数字（“725 changes flagged”）；对比逻辑修正之后，0.2.0 的 `scan` 对同一截止日期报告 513 个破坏性变更和 48 个新的弃用。卡片上的“changes fixed”数量及其 95% 置信区间也是 0.1.0 的算法：这个区间属于这个数量，而不是 5% -> 65% 这两个比率；而且 0.2.0 及更早的版本即使某个留出任务在没有说明时就已经答对，也会把该变更算作已修复。

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run-opus.svg" width="100%" alt="since-cutoff 0.1.0 在 Claude Opus 4.6 上的运行结果：探测的 4 个依赖中有 2 个出现过时的 API 用法；探测了 16 个 API 变更：7 个过时、0 个写错、3 个已弃用、6 个正确；10 条说明；留出任务正确率（无说明 -> 有说明）：5% -> 65%（20 对任务）；以及过时调用的列表"></p>

<details>
<summary>Claude Haiku 4.5 那次运行的同样摘要（同样用 0.1.0；0.2.0 的 scan 对它的截止日期报告 491 个破坏性变更和 50 个新的弃用）</summary>
<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run.svg" width="100%" alt="since-cutoff 0.1.0 在 Claude Haiku 4.5 上的运行结果：探测的 5 个依赖中有 3 个出现过时的 API 用法；探测了 20 个 API 变更：5 个过时、1 个写错、2 个已弃用、12 个正确；8 条说明；留出任务正确率（无说明 -> 有说明）：14% -> 57%（14 对任务）"></p>
</details>

样本小，只有两个模型、一个项目：请把它当作方法的演示，而不是基准测试。每次运行都会把完整报告（每个任务、每个回答和每条类型检查错误）写入 `.since-cutoff/report.md`。用当前版本重复这个实验（它的对比和排序都有变化，所以探测不会完全相同）：`cd examples/agent-app && since-cutoff run --model claude-code:claude-haiku-4-5 --task-model claude-code:claude-opus-4-6`。0.2.0 之后的版本还能保存一次运行所用的任务：加上 `--tasks-out tasks.json`，其他人就可以用 `--tasks-from tasks.json` 在完全相同的任务上重复这次运行，换一个模型或换一组说明都可以。非常欢迎把你自己项目上的结果发到 [Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6)。

测量方法的细节，以及这些数字能说明什么、不能说明什么，见[这篇文章](https://mohammadhijjawi97.github.io/since-cutoff/)（英文）。

## 在任意 Agent 中使用（MCP）

`since-cutoff mcp` 是一个 MCP 服务器，让编程 Agent 可以在写代码之前先问一句“这个库在我的训练截止日期之后改了什么？”。它提供三个只读工具：

| 工具 | 回答什么 |
|---|---|
| `api_changes(package, model, symbol=...)` | 一个库从模型截止日期时的版本到最新（或指定）版本之间改了什么，破坏性变更排在最前 |
| `project_changes(project_dir, model)` | 对项目的每个依赖按锁定版本做同样的检查，你的代码已经用到的 API 排在最前 |
| `model_cutoff(model)` | 模型的训练截止日期，来自 [models.dev](https://models.dev) |

Agent 传入自己的模型 id，所以结果涵盖的正是这个模型不可能见过的内容。这些工具只静态读取 PyPI 和包的源码：不调用模型，不需要 API key，不执行任何包代码。

**Claude Code**

```bash
claude mcp add --scope user since-cutoff -- uvx since-cutoff@latest mcp
```

**Codex**（`~/.codex/config.toml`）

```toml
[mcp_servers.since-cutoff]
command = "uvx"
args = ["since-cutoff@latest", "mcp"]
startup_timeout_sec = 60
tool_timeout_sec = 900
```

**Cursor**（`~/.cursor/mcp.json`）和 **Claude Desktop**（`claude_desktop_config.json`）

```json
{
  "mcpServers": {
    "since-cutoff": { "command": "uvx", "args": ["since-cutoff@latest", "mcp"] }
  }
}
```

**VS Code**（`.vscode/mcp.json`）

```json
{
  "servers": {
    "since-cutoff": { "type": "stdio", "command": "uvx", "args": ["since-cutoff@latest", "mcp"] }
  }
}
```

**Gemini CLI**

```bash
gemini mcp add --scope user since-cutoff uvx since-cutoff@latest mcp
# 或者安装为扩展，启动的是同一个服务器：
gemini extensions install https://github.com/MohammadHijjawi97/since-cutoff
```

`@latest` 让 `uvx` 使用新发布的版本，而不是一直复用第一次缓存的版本（插件自带的 `.mcp.json` 固定到确切版本）。如果客户端找不到 `uvx`，请安装 [uv](https://docs.astral.sh/uv/)，或者写上完整路径（`which uvx`）。这个服务器已登记在 [MCP Registry](https://registry.modelcontextprotocol.io) 中，名称为 `io.github.MohammadHijjawi97/since-cutoff`。

在较大的项目上，第一次调用 `project_changes` 会下载每个有变更的依赖的 wheel，可能需要几分钟（transformers 这类特别大的包最慢）。结果会被缓存，之后的调用只需几秒。要预热缓存，可以先在项目里运行一次 `since-cutoff scan`，它和服务器共用同一个缓存。默认工具超时较短的客户端可能需要把超时调长，就像上面的 Codex 示例那样。

`api_changes("huggingface-hub", model="claude-haiku-4-5")` 的返回结果（真实输出，有删节）：

```markdown
# huggingface-hub 0.29.1 -> 2.0.0

- From 0.29.1 (2025-02-20): the newest release on or before 2025-02-28 (training cutoff of claude-haiku-4-5, from models.dev)
- To 2.0.0 (2026-09-24): the latest release on PyPI
- 116 breaking changes, 0 new deprecations (removed or moved 62, parameters removed 43, parameters now required 9, changed kind 1, now keyword-only or positional-only 1)

## Removed or moved

- `huggingface_hub.InferenceApi` was removed; similar names now: `inference`, `InferenceEndpoint`, `InferenceClient`
...

## Parameters removed

- `huggingface_hub.snapshot_download(resume_download=...)`: parameter `resume_download` was removed; similar parameters now: `force_download`
- `huggingface_hub.file_download.hf_hub_download(force_filename=...)`: parameter `force_filename` was removed; similar parameters now: `filename`
...

Not listed: 76 breaking changes, 0 new deprecations (removed or moved 47, parameters removed 29). Narrow with symbol="..." or raise limit.
```

加上 `symbol="hf_hub_download"` 后，只列出这个函数的 8 个变更（`resume_download=`、`force_filename=`、`local_dir_use_symlinks=` 和 `proxies=`，分别出现在该函数和 `HfApi` 上）。`symbol` 也可以按代码里的调用写法传入：`client.messages.create` 会找到 `Messages.create` 的变更。

## 在 CI 中使用

### GitHub Action

在每个 pull request 上扫描项目，并在任务页面添加一份摘要：每个依赖在模型截止日期时的版本、你锁定的版本和主要变更，涉及你的代码所用名称的变更排在最前。和 `scan` 一样，它只读取 PyPI 和 models.dev：不调用模型，不需要 API key。

```yaml
# .github/workflows/since-cutoff.yml
name: since-cutoff
on:
  pull_request:
    paths: ["**/*.lock", "**/pylock*.toml", "**/requirements*.txt", "**/pyproject.toml"]
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: MohammadHijjawi97/since-cutoff@v0
        with:
          model: anthropic:claude-sonnet-4-5  # 团队写代码用的模型
```

| 输入 | 默认值 | 说明 |
|---|---|---|
| `model` | 必填 | 格式与 `--model` 相同（`provider:model`）；只用到它的训练截止日期 |
| `working-directory` | `.` | 项目目录 |
| `only`、`exclude` | | 逗号分隔的 PyPI 包名 |
| `cutoff` | | 覆盖训练截止日期（`YYYY-MM` 或 `YYYY-MM-DD`） |
| `fail-on-changes` | `false` | 有依赖在截止日期之后改了 API 时，让这一步失败 |
| `step-summary` | `true` | 把 Markdown 摘要加到任务摘要（job summary）中 |
| `cache` | `true` | 在多次运行之间保留 PyPI 元数据、包源码和 API 对比结果（`fail-on-changes` 让任务失败时也会保留） |
| `args` | | 额外的 `since-cutoff scan` 参数，例如 `--all-deps --limit 20` |
| `since-cutoff-version` | `0.2.0` | 要运行的 since-cutoff 版本，或 `latest` |

输出：`changed-packages`（逗号分隔）、`changes`（破坏性变更数）、`deprecations`、`markdown`（摘要文件的路径，例如可用来把摘要发成 pull request 评论）和 `report`（完整报告的路径）。能通过多个导入路径访问的同一个变更只计一次。

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/MohammadHijjawi97/since-cutoff
    rev: v0.2.0
    hooks:
      - id: since-cutoff-scan
        args: [--model=anthropic:claude-sonnet-4-5]  # 加上 --fail-on-changes 可以阻止提交
```

这个钩子在 lockfile、requirements 文件或 `pyproject.toml` 变更时运行，并打印扫描结果。需要在 `args` 里给出 `--model`。它要访问 PyPI，所以在 pre-commit.ci 上请跳过它（`ci: {skip: [since-cutoff-scan]}`）。

### 其他 CI

```bash
# 适用于任何 CI 的 Markdown 摘要；有依赖在截止日期之后改了 API 时，退出码为 3
since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown summary.md --fail-on-changes

# 同时测试模型（需要对应的 API key，或 claude 命令行工具）
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

`--markdown -` 把摘要打印到标准输出（常规输出则改为输出到标准错误）。退出码：`0` 正常；`1` 出错（例如没有任何模型回答能被打分）；`2` 用法错误；`3` 用 `run --fail-on-stale` 时发现了过时的 API 用法，或用 `scan --fail-on-changes` 时发现了 API 变更。

## 工作原理

分三个阶段。第一个阶段不需要模型；在另外两个阶段，每个回答都由类型检查器打分，模型写的每条说明也都由它检查：

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works.svg" width="640" alt="三个阶段。扫描（不调用模型）：从 lockfile 得到你的确切版本，找到模型截止日期时的版本，再用 griffe 做静态 API 对比。探测：给出需要用到该变更的简短任务，模型凭记忆作答，basedpyright 针对两个版本检查回答。修复与验证：模型写的说明只有示例能通过类型检查才保留，否则改用从 API 对比结果得出的变更陈述；留出任务在无说明和有说明时各回答一次，--apply 把一个区块写入 AGENTS.md。">
</picture></p>

| 结果 | 含义 |
|---|---|
| **stale**（过时） | 代码对模型学过的版本有效、对你的版本无效，并且错误涉及一个有变更的 API |
| **wrong**（写错） | 对你的版本无效，但不能用变更来解释（臆造或误用了 API） |
| **deprecated**（已弃用） | 有效，但用到了在你的版本中标记为 `@deprecated` 的 API |
| **correct**（正确） | 对你的版本有效，并且确实用到了变更后的 API |
| untouched / off-task / invalid / error | 不计入任何比率，但始终会在报告中列出 |

所有回答都由类型检查器针对确切的包版本打分，每个包都在各自独立的环境中检查，环境里装有该包自己的运行时依赖。没有 LLM 当评判，每个数字都能追溯到 `results.json`。详见 [docs/how-it-works.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md)（英文）。

## 它会运行、发送和保存什么

- **不运行任何包代码，也不运行模型写的代码。** 包只做静态读取（griffe 关闭 inspection；只解压 `.py`/`.pyi` 文件，并检查路径和大小）。模型的回答只在本地用 basedpyright 做类型检查。
- **获取**：从 PyPI 获取公开的包元数据和 wheel，从 models.dev 获取模型的训练截止日期（内置快照，可离线使用）。Git、本地路径、workspace 和私有源中的依赖，绝不会按名称去公共 PyPI 上查找。
- **发送**：只有 `run` 会发送提示词，并且只发给你选择的模型服务商，内容包括包名、版本、变更 API 的公开签名和文档字符串、生成的任务，以及（用于写说明的）模型自己的回答。从不包含你的源代码。`scan`、MCP 服务器、GitHub Action 和 pre-commit 钩子不向任何模型发送任何内容。
- **保存**：结果保存在项目中的 `.since-cutoff/` 目录（该目录会自动被 git 忽略）和一个本地缓存里（`since-cutoff cache path` 显示缓存位置，`since-cutoff cache clear` 清除缓存）。使用 `--apply` 时，它会在 `AGENTS.md`/`CLAUDE.md` 中写入一个带标记的区块，文件其余部分逐字节保持不变；`since-cutoff unapply` 可以移除这个区块。
- **没有遥测**，不需要账号，不收集个人数据。重复运行直接读取缓存，所以不花钱，结果也可复现（`--fresh` 会重新询问模型）。详见 [PRIVACY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/PRIVACY.md)（英文）。

## 局限

- 目前仅支持 Python。下一步是 TypeScript（`.d.ts` 对比、`tsc`）（[#1](https://github.com/MohammadHijjawi97/since-cutoff/issues/1)）。
- 类型检查器能发现错误的名称、错误的参数和 PEP 702 弃用，但看不到签名不变而行为改变的情况，也看不到只在运行时发出警告的弃用。`scan` 还会列出用库自己的装饰器（名称包含 "deprecat"）声明的弃用，但 `run` 不会探测这些弃用。
- 对比只覆盖公开 API：`_private` 名称，以及包内自带的测试、基准测试和示例，都会被跳过。
- 探测的是破坏性变更中按优先级排序的一个**样本**（你的代码已经用到的符号优先），而不是全部。
- “模型见过的版本”指截止日期当天或之前发布的最新版本。模型对最近的版本了解得更少，所以实际的过时可能开始得更早。
- 留出任务是针对同一个变更的改写：它们能说明一条说明修正了*这个*变更，不能说明模型整体变强了。

## 与其他工具的比较

| 工具类型 | 做什么 | since-cutoff 与之的关系 |
|---|---|---|
| 文档检索类 MCP 服务器：[Context7](https://github.com/upstash/context7)、[Ref](https://github.com/ref-tools/ref-tools-mcp)、[docs-mcp-server](https://github.com/arabold/docs-mcp-server) | Agent 查询某个库时，在回答当下提供最新文档 | 互补：since-cutoff 找出这个模型会写错哪些变更，让你知道哪里需要查文档或写说明，并在仓库里保留一条经过验证的简短说明 |
| 库自带的技能：[library-skills](https://github.com/tiangolo/library-skills)、[pydantic/skills](https://github.com/pydantic/skills) | 库的维护者随包发布给 Agent 的指引，与每个版本同步 | 适用于任何 PyPI 包，包括不提供任何指引的包，并能测出模型是否需要这些指引 |
| 依赖更新机器人：[Renovate](https://github.com/renovatebot/renovate)、[Dependabot](https://github.com/dependabot/dependabot-core) | 创建更新锁定版本的 pull request | GitHub Action 可以在这些 pull request 上运行，列出模型没有见过的 API 变更 |
| 基准测试：[GitChameleon 2.0](https://arxiv.org/abs/2507.12367)、[VersiCode](https://arxiv.org/abs/2406.07411)、[CodeUpdateArena](https://arxiv.org/abs/2407.06249)、[LibEvolutionEval](https://arxiv.org/abs/2412.04478) | 在固定的历史任务集上衡量模型处理库版本的能力 | 在你锁定的版本上测量这个模型，并用类型检查器验证修复效果 |

另外两个较小的工具也在解决同一个问题：[cutoff](https://github.com/sandeepsirodia/cutoff) 面向库的维护者，让模型写程序，并在库的当前版本上运行这些程序来做检测；[postcut](https://github.com/justi/postcut) 把 Ruby 的 `Gemfile.lock` 转换成一份截止日期之后的变更简报。since-cutoff 基于 [griffe](https://mkdocstrings.github.io/griffe/)、[basedpyright](https://github.com/DetachHead/basedpyright)、[models.dev](https://models.dev) 和 [rich](https://github.com/Textualize/rich) 构建。

## 参与贡献

since-cutoff 还很年轻。目前最有帮助的是：

- **在你的项目上运行它**，把发现的结果（包括误报）发到 [Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6)。
- **认领一个 [good first issue](https://github.com/MohammadHijjawi97/since-cutoff/labels/good%20first%20issue)**：小而独立的任务，比如支持另一种 lockfile 格式，或添加一个模型服务商预设。
- **更大的工作**标记为 [help wanted](https://github.com/MohammadHijjawi97/since-cutoff/labels/help%20wanted)，例如 [TypeScript 支持](https://github.com/MohammadHijjawi97/since-cutoff/issues/1)。
- **报告 bug 或奇怪的结果**：请提交到 [Issues](https://github.com/MohammadHijjawi97/since-cutoff/issues)。

[CONTRIBUTING.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CONTRIBUTING.md)（英文）介绍了代码结构和需要通过的检查；离线测试套件用一个玩具库和一个脚本化的模型跑通整个流程，不需要 API key。安全问题请见 [SECURITY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/SECURITY.md)（英文）。

## 引用

如果你在研究中使用了 since-cutoff，请引用它（见 [`CITATION.cff`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CITATION.cff)）。

## 许可证

MIT © [Mohammad Hijjawi](https://github.com/MohammadHijjawi97)
