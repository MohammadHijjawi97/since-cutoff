# since-cutoff

[English](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md) | 简体中文

**你的编程 Agent 学会你的依赖库时，那些库还没有改版。**
since-cutoff 会找出：在*你项目锁定的确切版本*里，模型到底会把哪些 API 写错；
然后用一段很短的 AGENTS.md 说明来修复。每条说明都由类型检查器验证，而不是让另一个 LLM 打分。


## 问题是什么

每个模型都有训练截止日期，但你的 lockfile 没有。下面是 `since-cutoff scan` 在一个真实示例项目里找到的例子
（模型：Claude Sonnet 4.5，训练截止 2025 年 7 月；依赖：当前最新版本）：

| 库 | 模型见过的版本 | 你用的版本 | 会坏在哪里 |
|---|---|---|---|
| anthropic | 0.60.0 | 1.8.0 | `messages.create(temperature=..., top_p=..., top_k=...)` 不再被接受 |
| huggingface-hub | 0.34.3 | 2.0.0 | `hf_hub_download(resume_download=..., force_filename=..., local_dir_use_symlinks=...)` 已移除 |
| langchain-core | 0.3.72 | 1.6.5 | `retriever.get_relevant_documents()`、`llm.predict()` 已移除 |
| openai | 1.98.0 | 3.19.2 | 26 个破坏性变更，6 个新的弃用 |

在这个示例项目中，9 个依赖里有 7 个在模型截止日期之后改了公开 API（静态对比共标出 483 处变更，其中不少是内部实现，生成任务时会被跳过）。
只学过旧 API 的 Agent 写出的代码会在导入或调用时报错；更糟的是，旧写法有时只是“已弃用”，代码还能跑。

文档检索类工具会把整份文档塞进上下文然后碰运气。since-cutoff 则是先**测量**模型实际会写错哪些变更，
**只**写必要的说明，并在**留出任务（held-out）**上**证明**这些说明确实有效。

## 特性

- **`scan`**：对每个依赖，比较模型截止日期时的版本和你锁定的版本，静态列出其间的破坏性变更（不调用模型，不需要 API key）。
- **`run`**：用需要这些变更 API 的小任务测试模型，并用类型检查器分别对**两个版本**打分：过时、错误、已弃用或正确。
- **经过验证的修复**：一行一条的 AGENTS.md / CLAUDE.md 说明，只有示例代码能通过你锁定版本的类型检查才会保留，并在留出任务上复测。
- **随处可用**：Claude Code 插件和技能，或 Anthropic、OpenAI、OpenRouter、DeepSeek、Ollama 以及任何 OpenAI 兼容接口。
- **支持各种 lockfile**：uv、Poetry、PDM、pylock、Pipenv、requirements 文件或 `.venv`。
- **安全、可复现**：从不运行包代码或模型写的代码；所有结果都有缓存；输出完整的 JSON 和 Markdown 报告。

## 一次真实运行

Claude Haiku 4.5（训练截止 2025 年 2 月），在 [`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app) 这个 9 个依赖的示例项目上运行，由 Claude Opus 4.6 生成任务和说明：

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run.svg" alt="since-cutoff run 结果" width="860"></p>

- **5 个被测依赖中有 3 个出现过时 API 用法。** 20 个被测 API 变更：5 个 stale（过时）、1 个 wrong、2 个 deprecated、12 个正确。
- 它写出的过时代码（对它学过的版本有效，对锁定版本无效）：`messages.create(temperature=...)`（anthropic 1.8）、`hf_hub_download(resume_download=...)`、`local_dir_use_symlinks=...`、`proxies=...`（huggingface-hub 2.0）、`client.beta.vector_stores`（openai 3.x）。
- **修复只需 8 条说明，约 391 个 token**，其中 7 条经过类型检查器验证。
- **留出任务正确率：无说明 14%，有说明 57%**（14 对任务；7 个变更中修复了 4 个，95% 置信区间 25–84%）。之前答对的 6 个 API 在加入说明后仍然正确。

样本小、只有一个模型和一个项目：请把它当作演示，而不是基准测试。

## 快速开始

```bash
# 列出模型截止日期之后的 API 变更（很快，不调用模型）
uvx --from git+https://github.com/MohammadHijjawi97/since-cutoff since-cutoff scan

# 测试模型、生成经过验证的说明，并写入 AGENTS.md
uvx --from git+https://github.com/MohammadHijjawi97/since-cutoff since-cutoff run --apply
```

也可以安装：`pipx install git+https://github.com/MohammadHijjawi97/since-cutoff`，然后运行 `since-cutoff`。
请在项目根目录运行（包含 `uv.lock`、`poetry.lock`、`pdm.lock`、`pylock.toml`、`Pipfile.lock`、
`requirements*.txt`、`pyproject.toml` 或 `.venv` 的目录）。

### 在 Claude Code 中使用

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

然后让 Claude “检查一下我们哪些依赖你已经过时了”，或运行 `/since-cutoff:since-cutoff`。
测量由一个全新的、没有任何工具的模型副本完成，所以 Agent 不会给自己打分。

## 支持的模型

| `--model` | 使用 | 需要 |
|---|---|---|
| `claude-code`（默认） | 你的 Claude Code 登录（订阅或 API key） | `claude` 命令行 |
| `anthropic:<model>` | Anthropic API | `ANTHROPIC_API_KEY` |
| `openai:<model>` | OpenAI API | `OPENAI_API_KEY` |
| `openrouter:<vendor/model>` | OpenRouter | `OPENROUTER_API_KEY` |
| `deepseek:<model>` | DeepSeek API | `DEEPSEEK_API_KEY` |
| `ollama:<model>` | 本地 Ollama | 运行中的 Ollama |
| `openai-compatible:<model>` | 任何 OpenAI 兼容接口 | `--base-url` |

训练截止日期来自 [models.dev](https://models.dev)（内置离线快照）。`since-cutoff models qwen` 可以查看，`--cutoff 2025-07` 可以手动指定。

## 工作原理

1. 读取 lockfile，确定每个依赖的确切版本，以及模型截止日期当天最新的版本。
2. 用 griffe 对两个版本做**静态** API 对比：删除/移动的对象、删除或新增必填的参数、仅限关键字/仅限位置参数、PEP 702 弃用。
3. 把每个变更写成几条简短的编程任务（不点名被改动的标识符）。一条用于测试，其余留作验证。
4. 被测模型在**没有工具、没有文档**的情况下作答。
5. 用 basedpyright 针对**两个版本**分别做类型检查：对旧版本有效、对你的版本无效，且错误涉及变更过的 API，即为 **stale（过时）**。
6. 为失败项生成说明，只有示例代码通过类型检查的说明才会保留；再在留出任务上成对比较“有/无说明”的正确率。

生成的代码**永远不会被执行**；包代码只做静态读取。详见 [docs/how-it-works.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md)（英文）。

## 在 CI 中使用

```bash
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

## 局限

- 目前仅支持 Python，TypeScript（`.d.ts` + `tsc`）是下一步。
- 类型检查器能发现错误的名称、参数和 PEP 702 弃用，但看不到签名不变的行为变化。
- 探测的是按相关性排序的**样本**，不是全部变更。

## 贡献

欢迎提 Issue 和 PR，参见 [CONTRIBUTING.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CONTRIBUTING.md)。离线测试用一个玩具库和脚本化模型跑完整流程，不需要任何 API key。

## 许可证

MIT © [Mohammad Hijjawi](https://github.com/MohammadHijjawi97)
