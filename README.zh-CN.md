# since-cutoff

[English](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md) | 简体中文

**你的编程 Agent 学会你的依赖库时，那些库还没有改版。**
since-cutoff 列出*你项目锁定的依赖版本*在模型训练截止日期之后改了哪些公开 API，
再从中抽样测试模型，找出它会写错的变更，并为这些变更写简短的 AGENTS.md 说明。
打分由类型检查器完成，而不是另一个 LLM；只有示例代码能通过你锁定版本类型检查的说明才会保留，否则改为一句对变更的直接陈述。


## 问题是什么

每个模型都有训练截止日期，但你的 lockfile 没有。下面是 `since-cutoff scan` 在一个真实示例项目里找到的例子
（模型：Claude Sonnet 4.5，训练截止 2025 年 7 月；依赖：当前最新版本）：

| 库 | 模型见过的版本 | 你用的版本 | 会坏在哪里 |
|---|---|---|---|
| anthropic | 0.60.0 | 1.8.0 | `messages.create(temperature=..., top_p=..., top_k=...)` 不再被接受 |
| huggingface-hub | 0.34.3 | 2.0.0 | `hf_hub_download(resume_download=..., force_filename=..., local_dir_use_symlinks=...)` 已移除 |
| langchain-core | 0.3.72 | 1.6.5 | `retriever.get_relevant_documents()`、`llm.predict()` 已移除 |
| openai | 1.98.0 | 3.19.2 | 21 个破坏性变更，6 个新的弃用 |

在这个示例项目中，9 个依赖里有 7 个在模型截止日期之后改了公开 API（静态对比共标出 317 个破坏性变更和 23 个新的弃用，其中一部分是内部实现，生成任务时会被跳过）。
只学过旧 API 的 Agent 写出的代码会在导入或调用时报错；更糟的是，旧写法有时只是“已弃用”，代码还能跑。

[Context7](https://github.com/upstash/context7) 这类文档工具会在 Agent 查询时获取某个库的最新文档。
since-cutoff 回答的是另一个问题：在你锁定的版本里，这个模型实际会写错哪些变更。它只为这些变更写说明，
并在**留出任务（held-out）**上对比有无说明时的结果，检查说明是否有用。

## 特性

- **`scan`**：对每个依赖，比较模型截止日期时的版本和你锁定的版本，静态列出其间的破坏性变更（不调用模型，不需要 API key）。
- **`run`**：用需要这些变更 API 的小任务测试模型，并用类型检查器分别对**两个版本**打分：过时、错误、已弃用或正确。
- **经过验证的修复**：一行一条的 AGENTS.md / CLAUDE.md 说明，只有示例代码能通过你锁定版本的类型检查才会保留，并在留出任务上复测。
- **`mcp`**：MCP 服务器，Claude Code、Codex、Cursor、VS Code、Claude Desktop 或 Gemini CLI 都可以在写代码前查询某个库在截止日期之后的变更。
- **CI**：GitHub Action（在任务摘要里显示结果）、pre-commit 钩子，其他 CI 可用 `scan --markdown` / `--fail-on-changes`。
- **随处可用**：Claude Code 插件和技能，或 Anthropic、OpenAI、OpenRouter、DeepSeek、Ollama 以及任何 OpenAI 兼容接口。
- **支持各种 lockfile**：uv、Poetry、PDM、pylock、Pipenv、requirements 文件或 `.venv`。
- **安全、可复现**：从不运行包代码或模型写的代码；所有结果都有缓存；输出完整的 JSON 和 Markdown 报告。

## 真实运行结果

两个 Claude 模型，在 [`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app) 这个 9 个依赖的示例项目上运行（任务和说明由 Claude Opus 4.6 生成）：

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run.svg" alt="since-cutoff run 结果" width="860"></p>

| | Claude Haiku 4.5 | Claude Opus 4.6 |
|---|---|---|
| 训练截止 | 2025 年 2 月 | 2025 年 5 月 |
| 被测 API 变更 | 20 | 16 |
| **过时** / 写错 / 已弃用 / 正确 | **5** / 1 / 2 / 12 | **7** / 0 / 3 / 6 |
| 出现过时用法的库 | 5 个中 3 个 | 4 个中 2 个 |
| 生成的说明（经类型检查验证） | 8 条（7 条），约 391 token | 10 条（7 条），约 437 token |
| **留出任务正确率：无说明 -> 有说明** | **14% -> 57%**（14 对） | **5% -> 65%**（20 对） |
| 之前答对的 API 加入说明后 | 6/6 仍正确 | 6/6 仍正确 |

更强的模型并不更“新”：Opus 4.6 会很自信地写出截止日期之后已被移除的 API，例如 `anthropic.HUMAN_PROMPT` 配合 `client.completions`。
样本小、只有一个项目：请把它当作演示，而不是基准测试。

## 快速开始

```bash
# 列出模型截止日期之后的 API 变更（很快，不调用模型）
uvx since-cutoff scan

# 测试模型、生成经过验证的说明，并写入 AGENTS.md
uvx since-cutoff run --apply
```

也可以安装：`pipx install since-cutoff`（或 `pip install since-cutoff`），然后运行 `since-cutoff`。
请在项目根目录运行（包含 `uv.lock`、`poetry.lock`、`pdm.lock`、`pylock.toml`、`Pipfile.lock`、
`requirements*.txt`、`pyproject.toml` 或 `.venv` 的目录）。

### 在 Claude Code 中使用

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

然后让 Claude “检查一下我们哪些依赖你已经过时了”，或运行 `/since-cutoff:since-cutoff`。
测量由一个全新的、没有任何工具的模型副本完成，所以 Agent 不会给自己打分。
插件还会启动下面介绍的 MCP 服务器。

### 在其他编程 Agent 中使用

```bash
npx skills add MohammadHijjawi97/since-cutoff
```

通过开源的 [skills](https://github.com/vercel-labs/skills) 命令行，把同一个技能安装到 Codex、Cursor、Gemini CLI、GitHub Copilot、OpenCode 等读取 `SKILL.md` 的 Agent 中。
在 Claude Code 之外使用时，请告诉工具要测试哪个模型，例如 `since-cutoff scan --model openai:gpt-5.4`。MCP 工具需要按下一节的方法另行添加。

### 示例提示词

- “我们的哪些依赖在你的训练截止日期之后改了公开 API？”（Agent 运行 `since-cutoff scan` 或调用 MCP 工具 `project_changes`：不调用模型，不需要 API key）
- “测一下你实际会写错哪些变更，并把经过验证的说明加到 AGENTS.md。”（Agent 先征得你的同意，再运行 `since-cutoff run --quick --apply`；`run` 会把提示词发给模型服务商，并消耗你的 API 额度或 Claude Code 用量）
- “写 httpx 代码之前，先查一下 httpx 在你的截止日期之后改了什么。”（Agent 调用 MCP 工具 `api_changes`）

## 在任意 Agent 中使用（MCP）

`since-cutoff mcp` 是一个 MCP 服务器：编程 Agent 可以在写代码之前先问一句“这个库在我的训练截止日期之后改了什么？”。
它提供三个只读工具：

- `api_changes(package, model, symbol=...)`：一个库从模型截止日期时的版本到最新（或指定）版本之间的 API 变更，破坏性变更排在最前；
- `project_changes(project_dir, model)`：对项目的每个依赖（按锁定版本）做同样的检查，你的代码已经用到的 API 排在最前；
- `model_cutoff(model)`：查询模型的训练截止日期。

Agent 传入自己的模型 id，所以结果只包含这个模型不可能见过的变更。工具只静态读取 PyPI 和包的源码：不调用模型、不需要 API key、不执行包代码。

```bash
# Claude Code
claude mcp add --scope user since-cutoff -- uvx since-cutoff@latest mcp

# Gemini CLI
gemini mcp add --scope user since-cutoff uvx since-cutoff@latest mcp
# 或者安装为扩展（启动的是同一个服务器）
gemini extensions install https://github.com/MohammadHijjawi97/since-cutoff
```

这些配置使用 `@latest`，这样 `uvx` 会使用新版本，而不是一直复用第一次缓存的版本（插件自带的 `.mcp.json` 则固定到确切版本）。

Cursor（`~/.cursor/mcp.json`）和 Claude Desktop（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "since-cutoff": { "command": "uvx", "args": ["since-cutoff@latest", "mcp"] }
  }
}
```

在较大的项目上，第一次调用 `project_changes` 需要下载所有有变更的依赖的 wheel，可能要几分钟；结果会被缓存，之后的调用只需几秒。可以先在项目里运行一次 `since-cutoff scan` 来预热缓存（它和服务器共用同一个缓存）。

Codex、VS Code 的配置和一段真实的输出示例见[英文 README](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md#use-it-from-any-agent-mcp)。

## 支持的模型

| `--model` | 使用 | 需要 |
|---|---|---|
| `claude-code`（默认） | 你的 Claude Code 登录（订阅或 API key） | `claude` 命令行 |
| `claude-code:sonnet`、`claude-code:claude-haiku-4-5` | 指定某个 Claude 模型 | `claude` 命令行 |
| `anthropic:<model>` | Anthropic API | `ANTHROPIC_API_KEY` |
| `openai:<model>` | OpenAI API | `OPENAI_API_KEY` |
| `openrouter:<vendor/model>` | OpenRouter | `OPENROUTER_API_KEY` |
| `deepseek:<model>` | DeepSeek API | `DEEPSEEK_API_KEY` |
| `ollama:<model>` | 本地 Ollama | 运行中的 Ollama |
| `openai-compatible:<model>` | 任何 OpenAI 兼容接口 | `--base-url`，可选 `OPENAI_API_KEY` |

训练截止日期来自 [models.dev](https://models.dev)（内置离线快照）。`since-cutoff models qwen` 可以查看，`--cutoff 2025-07` 可以手动指定。

## 工作原理

1. 读取 lockfile，确定每个依赖的确切版本，以及模型截止日期当天最新的版本。
2. 用 griffe 对两个版本做**静态** API 对比：删除/移动的对象、删除或新增必填的参数、仅限关键字/仅限位置参数、新增的弃用标记（PEP 702 `@deprecated`，或库自己的名称含 "deprecat" 的装饰器）。
3. 把每个变更写成几条简短的编程任务（不点名被改动的标识符）。一条用于测试，其余留作验证。
4. 被测模型在**没有工具、没有文档**的情况下作答。
5. 用 basedpyright 针对**两个版本**分别做类型检查：对旧版本有效、对你的版本无效，且错误涉及变更过的 API，即为 **stale（过时）**。
6. 为失败项生成说明，只有示例代码通过类型检查的说明才会保留；再在留出任务上成对比较“有/无说明”的正确率。

生成的代码**永远不会被执行**；包代码只做静态读取。详见 [docs/how-it-works.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md)（英文）。

## 在 CI 中使用

**GitHub Action**：在每个 PR 上扫描项目，并把摘要（每个依赖在截止日期时的版本、你锁定的版本、主要变更，你的代码用到的排在最前）写入任务摘要页。
和 `scan` 一样只读取 PyPI 和 models.dev：不调用模型，不需要 API key。

```yaml
- uses: actions/checkout@v7
- uses: MohammadHijjawi97/since-cutoff@v0
  with:
    model: anthropic:claude-sonnet-4-5  # 团队使用的模型
    # fail-on-changes: "true"          # 有依赖在截止日期后改了 API 时让这一步失败
```

**pre-commit**（lockfile、requirements 文件或 `pyproject.toml` 变更时运行；需要在 `args` 里给出 `--model`）：

```yaml
repos:
  - repo: https://github.com/MohammadHijjawi97/since-cutoff
    rev: v0.2.0
    hooks:
      - id: since-cutoff-scan
        args: [--model=anthropic:claude-sonnet-4-5]
```

其他 CI：

```bash
# Markdown 摘要；有依赖在截止日期后改了 API 时退出码为 3
since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown summary.md --fail-on-changes

# 同时测试模型（需要对应的 API key 或 claude 命令行）
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

全部输入和输出见[英文 README](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md#use-in-ci)。

## 局限

- 目前仅支持 Python，TypeScript（`.d.ts` + `tsc`）是下一步。
- 类型检查器能发现错误的名称、参数和 PEP 702 弃用，但看不到签名不变的行为变化。
- 只对比公开 API：跳过以 `_` 开头的名称，以及包内自带的测试、基准和示例模块。
- 探测的是按相关性排序的**样本**，不是全部变更。

## 隐私与支持

since-cutoff 不收集个人数据，也没有遥测。它在你的机器上读取项目的依赖文件和 Python 代码，从 PyPI 获取公开的包数据，从 models.dev 获取模型截止日期；只有 `run` 会把提示词发给你选择的模型服务商，内容包括包名、版本、公开 API 签名和文档字符串、生成的任务以及模型自己的回答，从不包含你的源代码。
结果保存在 `.since-cutoff/` 和本地缓存中（`since-cutoff cache clear` 可清除缓存）；使用 `--apply` 时还会在 `AGENTS.md`/`CLAUDE.md` 中写入一个带标记的区块。
详见 [PRIVACY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/PRIVACY.md)（英文）。

支持与问题反馈：[GitHub Issues](https://github.com/MohammadHijjawi97/since-cutoff/issues)。安全问题请见 [SECURITY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/SECURITY.md)（英文）。

## 贡献

欢迎提 Issue 和 PR，参见 [CONTRIBUTING.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CONTRIBUTING.md)。离线测试用一个玩具库和脚本化模型跑完整流程，不需要任何 API key。

## 许可证

MIT © [Mohammad Hijjawi](https://github.com/MohammadHijjawi97)
