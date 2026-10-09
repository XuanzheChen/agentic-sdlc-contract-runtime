# Agentic SDLC Contract-Driven Runtime（PSC）

[English](README.md) | **简体中文**

agentic-sdlc-contract-runtime 是一个可移植的 Codex Skill，用于基于文件系统工件运行或编写 Contract-Driven Agentic SDLC（PSC）工作流。它让 Planner、Supervisor 和 Executor 保持相互独立，同时提供不可变版本化 Contract、可恢复状态、重试、升级（escalation）以及基于证据的验收机制。

## 它提供什么

- **Artifact-first 的 Supervisor 工作流**：Contract、runtime 状态、仓库状态、task、review 和验证证据是唯一持久事实来源。
- **不可变的 contract/vN/ Contract**：Requirement、Acceptance Criteria、Task 都使用稳定 ID。
- **一次性 Executor 边界**：Executor 只接收当前任务所需的 Contract/task 信息，不拥有工作流状态，也不能批准自己的工作。
- **确定性的 Runtime helper**：scripts/psc_runtime.py 负责 Contract 校验、发现、bootstrap、Bundle 导入和 Contract 激活。
- **阻塞式 Executor MCP**：Supervisor 通过一次 MCP tool call 等待 Executor 完成，不再使用 exec_command + write_stdin 高频轮询。

完整运行规范见 [SKILL.md](SKILL.md)，Contract schema、runtime protocol、Executor adapter 等细节见 [references/](references/)。

## 在 Codex 中使用

把本目录放到 Codex 能发现 Skill 的位置，例如项目内的：

~~~text
.agents/skills/agentic-sdlc-contract-runtime/
~~~

然后在 Codex 中调用：

~~~text
Use $agentic-sdlc-contract-runtime to resume or start a contract-driven workflow.
~~~

Supervisor 第一次在某个工作区使用时，会初始化可由用户直接编辑的：

~~~text
.agentic-sdlc/runtime.json
~~~

其中保存 Runtime Root、项目命名规则和 Executor 配置。凭据不写入 runtime 配置，认证始终保留在独立的 Executor 环境中。

---

## Executor 实时进度（MCP）

正常 `psc_invoke_executor` 仍保持**一次阻塞调用对应一次真实 Executor attempt**。MCP 入口通过工作线程执行原调用，并持续并行读取 Codex/DSH `--json` 的 stdout/stderr；结构化进度通过 request-scoped MCP `notifications/progress` 尽力发送。最终输出解析、token usage、scope 校验、重试预算与 Supervisor 验收的语义不变。进度不包含 `thinking`、完整 tool result 或原始命令；进度通知发送失败不会导致 Executor 失败。

每次实际启动都会留下进度文件：

```text
<活动 PSC 项目>/runtime/executor-progress.json
<活动 PSC 项目>/runtime/executor-progress/<run_id>.jsonl
```

状态文件记录步骤数、工具次数、任务、模型、运行时间、最后一次 Executor 事件时间与 heartbeat 时间。 面向用户的 MCP 进度通知和心跳耗时使用 `x h x m x s` 格式（例如 `1 h 2 m 3 s`）。JSON 新增 `elapsed_display` 和 `last_event_age_display`，并保留数值型 `elapsed_seconds`、`seconds_since_executor_event` 供已有脚本继续使用。进度 JSONL 只记录经筛选/脱敏的简短摘要；完整审核日志仍由原日志机制持有。

**请先测试 UI：**刷新 Supervisor 的 MCP 连接后，直接调用 `psc_progress_probe`。它用 20 秒发送 5 条 progress，不会启动 Executor，不占 retry 预算，也不会推进 PSC 工作流。协议发送成功不代表 Codex Desktop/TUI 会真正显示；若 UI 不展示，可在真实调用期间查看 `executor-progress.json`，不要改为 Supervisor 轮询。此 probe 不覆盖真实取消语义。

Codex Supervisor 应保持 PSC 的 `direct_only_tool_namespaces` 配置，防止 Code Mode 的 `exec/wait` 模型轮询改变长时间阻塞调用的生命周期。

---


## Blocking Executor MCP

正常的 Supervisor → Executor 调度应使用：

~~~text
psc_invoke_executor
~~~

该工具由 scripts/psc_mcp_server.py 提供。

它解决的是原先这种控制流：

~~~text
S
↓
exec_command
↓
30 秒后转 background terminal
↓
write_stdin
↓
S 再 inference
↓
write_stdin
↓
...
~~~

MCP 化后变成：

~~~text
S inference
↓
psc_invoke_executor(...)
↓
MCP tools/call 挂起等待
↓
invoke_executor() 阻塞等待 E
↓
E 完成
↓
MCP 返回
↓
同一个 Supervisor turn 自动继续
~~~

Executor 等待期间不需要 Supervisor 反复 inference。

### 使用独立的 MCP Python Runtime

PSC 的 MCP Python 属于**基础设施环境**，不属于你的产品项目。

不要为了让这个 Skill 工作而把 MCP SDK 安装进当前项目的 conda/venv/
IDE Python 环境。项目 Python、MCP Python、Executor 环境应该彼此独立。

先探测一个候选解释器：

~~~text
python scripts/probe_mcp_runtime.py --python <candidate-python> --repository <repository>
~~~

如果已经知道项目实际使用的 Python，再显式传入：

~~~text
python scripts/probe_mcp_runtime.py --python <candidate-python> --repository <repository> --project-python <project-python>
~~~

候选 MCP Python 必须满足：

- Python 3.10+
- import ssl 正常
- OpenSSL 可用
- python -m pip 正常
- 不位于产品项目仓库内
- 不是已知的项目 Python

如果返回 install_required，说明这个**独立环境**本身是健康的，只缺 MCP SDK。
此时仅安装到这个候选解释器：

~~~text
<candidate-python> -m pip install -r requirements-mcp.txt
~~~

如果项目环境本身缺失 SSL，例如某个 conda 环境无法 import ssl，不要为了
PSC 去修复或污染它；直接选择或创建另一个独立的 MCP Python 环境。

### 一次性注册本地 MCP server

在 **Supervisor 所使用的 Codex 配置**中注册本地 stdio MCP server，并把
上面选定的独立 Python 的绝对路径作为 command。

Windows 示例：

~~~toml
[mcp_servers.agentic_sdlc_executor]
command = "F:/Miniconda3/envs/psc-mcp/python.exe"
args = ["E:/path/to/agentic-sdlc-contract-runtime/scripts/psc_mcp_server.py"]
tool_timeout_sec = 3600

[features.code_mode]
direct_only_tool_namespaces = ["mcp__agentic_sdlc_executor"]
~~~

tool_timeout_sec 表示**一次 Executor MCP 调用允许持续的最长时间**，不是轮询间隔。

建议满足：

~~~text
tool_timeout_sec >= executor.timeout
~~~

如果 E 提前完成，MCP 会立即返回，不会等满这个时间。

对于 GPT-5.6 的 Code Mode Supervisor，`direct_only_tool_namespaces` 是必需项。
它会让 `mcp__agentic_sdlc_executor` 保持为顶层 direct model tool，而不是被
包进 `functions.exec` 的后台 cell。否则长时间 MCP 调用可能重新变成：
“cell 返回 → S 重采样 → wait → 再重采样”的轮询链。

如果配置中已经有其他 `direct_only_tool_namespaces`，只追加
`"mcp__agentic_sdlc_executor"`，不要覆盖已有值。修改后应使用刷新后的
Supervisor session，并确认 `psc_invoke_executor` 是直接可调用的 MCP tool。

正常 dispatch 必须只有一次直接 MCP tool call。禁止用 `functions.exec` /
JavaScript 包裹它，也禁止对 cell 使用 `wait` 或 `write_stdin`。当前 session
若只能以 Code Mode nested tool 方式访问该 MCP，则 fail closed，先刷新配置/
session，不继续执行 Executor。

---

## Preflight Checker（预检门禁）

每一次 Executor attempt 之前，MCP runtime 都会先执行一次**只读**的 Preflight Checker（PC）。该门禁位于 `psc_invoke_executor` 内部：即使 Supervisor 跳过可选的 `psc_preflight_check` 工具，也无法绕过门禁。`psc_preflight_check` 只用于显式「修复后复查」或查看被拦截的原因，永远不启动 E。

PC 与 E 使用**相同**的 adapter、executable、executor home、provider、model 与 effort，但它是独立的只读调用，拥有自己的 run id、超时、日志、报告与 token/耗时账本；PSC 不会为了运行 PC 改写 Executor home 或 `runtime.json`。

- **Codex**：强制 `--sandbox read-only --ask-for-approval never`（忽略 Executor 配置的 sandbox/approval policy），在仓库之外的空临时目录中执行，通过 `--output-schema` 约束严格报告 schema，prompt 走 stdin。
- **DSH**：使用短生命周期的命令行 `--patch` overlay 关闭 shell、文件写入、代码编辑、MCP client、外部工具与危险工具，并放在第一个 app-owned flag 之前。PSC 按「先 profile `cordis.patch.yml`、后 runtime patch」组合并校验每一项限制；无法证明时以 `dsh_tool_restrictions_unverifiable` fail closed。
- **没有自由文件系统访问**：runtime 收集有界、任务相关的证据包（Task 文件、任务范围 Contract packet、上一轮 Supervisor review、Contract 绑定、有界的 Allowed Scope/配置文件），计算确定性 SHA-256，并通过 runtime 自己控制的 prompt 传输。

PC 返回严格报告：`schema_version`、`decision`（`ALLOW`/`DENY`）、非空 `summary`、`findings`。每个 finding 都带 `evidence` 字符串以及 `resolution_owner`（`runtime`、`supervisor` 或 `planner`）。`ALLOW` 不允许出现 `blocking` finding；`DENY` 必须至少包含一个。非法 JSON、schema 违规、非零退出、超时、spawn 失败、DSH 限制不可验证、证据过期一律归为 `UNKNOWN`。

Runtime 强制执行：

- `ALLOW` 继续执行，并把精简的已验证事实以 `## Verified Preflight Facts` 注入 E 的 prompt；除非其中某个 hash 已不再匹配，否则该事实对本次 attempt 具有权威性。
- `DENY` 返回 `preflight_denied`，`UNKNOWN` 返回 `preflight_unknown`；两者都不会启动 E，都是 `retryable=false`，并且**都不消耗 Executor 重试预算**。
- E 启动之前会重新收集并重新计算全部 hash；任何变化都会以 `preflight_evidence_stale` 使决定失效。
- Supervisor 按 `resolution_owner` 修复后重新调度，下一次调度会以全新 hash 重新运行 PC。唯一绕过方式是用户在自己的 `runtime.json` 中显式设置 `preflight.enabled=false`，这是用户决定，绝不是 Supervisor 的捷径。

Checker 的 token 与耗时单独记账，报告留在 PSC project 的 runtime 下：

~~~text
<project>/runtime/preflight/latest.json
<project>/runtime/preflight/T-###-<timestamp>-<run>.json
<project>/runtime/preflight_token_usage.jsonl
<project>/runtime/preflight_token_usage_summary.json
~~~

不要把这些用量写进 `runtime/executor_token_usage.jsonl`，也不要当作 Executor 用量汇报。

---

## MCP 配置和 Executor 配置是两层东西

这是当前设计中最重要的边界之一。

### MCP 配置负责

~~~text
Supervisor
↓
如何找到 psc_mcp_server.py
↓
一次阻塞 tool call 最多允许多久
~~~

主要就是：

~~~text
command
args
tool_timeout_sec
~~~

### Executor 配置负责

真正的 E 仍由：

~~~text
.agentic-sdlc/runtime.json
~~~

以及独立 Executor Home 管理。

包括：

- adapter：codex / dsh
- executable
- executor_home
- config_source
- provider
- model
- effort
- profile
- approval_policy
- sandbox
- executor.timeout
- smoke_timeout

MCP wrapper **每次调用都会重新读取 runtime.json**。

因此，日后你修改 E 的：

~~~text
模型
reasoning effort
provider
Executor Home
Codex ↔ DSH
sandbox
approval policy
profile
~~~

通常都**不需要重新配置 MCP**。

只有下面这些情况通常需要改 MCP：

1. psc_mcp_server.py 的实际路径变了；
2. Python 启动命令变了；
3. Executor timeout 被提高到超过 tool_timeout_sec。

换句话说：

~~~text
MCP = 稳定运输/等待层
Executor = 可独立替换、可独立编辑的执行层
~~~

---

## MCP 返回值与完整 Executor 日志

MCP 不会默认把整个 Executor stdout/stderr 塞进 Supervisor context。

### 成功时

MCP 只返回紧凑元数据，例如：

~~~json
{
  "status": "completed",
  "reason": null,
  "exit_code": 0,
  "changed_paths": ["src/example.py"],
  "scope_violations": [],
  "artifact_paths": {
    "plan": ".../plan.md",
    "coding": ".../coding.md"
  },
  "log_path": ".../logs/executor/T-001-....log"
}
~~~

不会返回完整 stdout、stderr、completion，这样可以防止一次 Executor 输出把 S 的上下文膨胀数万 token。

### 失败时

失败结果会额外返回一个**严格限长的 diagnostic**：

- stderr：最后最多 8192 字符
- stdout：最后最多 4096 字符
- stderr_truncated
- stdout_truncated

例如：

~~~json
{
  "status": "failed",
  "reason": "process_failed",
  "exit_code": 1,
  "diagnostic": {
    "stderr_tail": "...",
    "stdout_tail": "...",
    "stderr_truncated": true,
    "stdout_truncated": false
  },
  "log_path": ".../logs/executor/T-003-....log"
}
~~~

### 完整 stdout/stderr 仍然保存在本地

MCP 的“压缩返回”不等于删除日志。

invoke_executor.py 仍然会把完整且经过 secret redaction 的执行日志写入：

~~~text
<workflow-project>/
└─ logs/
   └─ executor/
      ├─ T-001-....log
      ├─ T-002-....log
      └─ T-003-....log
~~~

日志包含：

~~~text
Command:
...

Exit code:
...

STDOUT
...

STDERR
...
~~~

因此 Supervisor 的失败复盘顺序应为：

~~~text
Executor failed
↓
先看 MCP diagnostic tail
↓
足够定位 → review / retry
↓
不足
↓
根据 log_path 定点读取相关范围
↓
必要时再扩大读取
~~~

默认不应整份读取超大日志。

正常 direct MCP 调用直接消费 structured result。只有人工调试 Code Mode
wrapper 时才使用 `r.structuredContent ?? r.content`；不要直接
`JSON.stringify(r)` 整个 wrapper，否则 `content` 与
`structuredContent` 可能重复进入 S 上下文。

---

## 初始化 Supervisor Runtime

初始化首先确定一个独立的 MCP Python Runtime，然后再初始化 Executor/runtime。
PSC 不会借用当前项目 Python，也不会因为 MCP 依赖缺失而修改项目 conda/venv。
同时，PSC 也不会借用当前 Supervisor Codex session 的 model、provider、sandbox、authentication 或 CODEX_HOME。

创建：

~~~text
.agentic-sdlc/runtime.json
~~~

以后先执行静态检查和真实 smoke：

~~~text
python scripts/invoke_executor.py status --repository <repository> --runtime-config <repository>/.agentic-sdlc/runtime.json

python scripts/invoke_executor.py smoke --repository <repository> --runtime-config <repository>/.agentic-sdlc/runtime.json
~~~

Smoke 会在隔离临时目录中真正启动所选 harness，并要求 Executor 创建精确 marker 文件。只有真实 Executor 能完成受限任务才算 PASS。

### 必需配置

| 配置 | 含义 |
| --- | --- |
| runtime_root | 保存 PSC workflow project 的大目录。 |
| project_naming | 新 workflow 的目录命名规则，例如 YYYYMMDD-{requirement}。 |
| executor.adapter | codex 或 dsh。 |
| executor.executable | 对应 harness 的 CLI 路径或 PATH 命令。 |
| executor.executor_home | 独立 Executor Home。 |
| executor.config_source | legacy 路由默认值来自 runtime 或 executor_home；新初始化通常让 Executor Home 继续管理 provider 定义/认证。 |
| executor.routing.provider / model / effort | 每次新初始化都必须由用户显式确认；PSC 对 Codex/DSH 都按 invocation 覆盖模型路由，不修改 Executor Home。 |
| executor.profile | DSH 使用的现有 profile。 |
| executor.approval_policy | Codex approval 模式。 |
| executor.sandbox | read-only / workspace-write / danger-full-access。 |
| executor.timeout | 正常任务最长运行秒数。 |
| executor.smoke_timeout | smoke 最长运行秒数。 |

Runtime 配置不得包含 API key、token、密码或复制的认证文件。

---

## Codex Executor

Codex adapter 每次 attempt 都会启动一个全新的：

~~~text
codex exec
~~~

子进程。

子进程只获得：

~~~text
CODEX_HOME=<executor_home>
~~~

Supervisor 自身的环境不会被修改。

### 使用 Executor Home + PSC 显式模型路由

新初始化会要求用户明确选择 provider、model 和 reasoning effort，并将其写入
`executor.routing`。Executor Home 继续管理 provider 定义、endpoint 与认证；
PSC 不会为了切模型去修改它。

推荐：

~~~json
{
  "schema_version": 1,
  "runtime_root": ".agentic-sdlc/developing",
  "project_naming": "YYYYMMDD-{requirement}",
  "executor": {
    "adapter": "codex",
    "executable": "codex",
    "executor_home": "E:\\codex-executor",
    "config_source": "executor_home",
    "routing": {
      "provider": "codexzh",
      "model": "gpt-6-luna",
      "effort": "medium"
    },
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "smoke_timeout": 120
  }
}
~~~

此时 `executor.routing` 是本次 PSC Executor 的 provider/model/effort
选择，Codex 会收到对应 CLI override；`<executor_home>/config.toml` 仍负责
provider 定义等独立环境配置。PSC 不读取 auth.json 内容，也不会把认证文件
复制到别处。修改 routing 或 Executor Home 的安全相关配置都会让 smoke
fingerprint 变化，需要重新跑 smoke。

旧 runtime.json 若没有 `executor.routing`，仍保留原来的
`config_source=runtime` / `executor_home` 继承行为。

---

## DeepSeek Harness Executor

DSH adapter 会启动独立 DSH 进程，并只给子进程：

~~~text
DSH_HOME=<executor_home>
~~~

示例：

~~~json
{
  "schema_version": 1,
  "runtime_root": ".agentic-sdlc/developing",
  "project_naming": "YYYYMMDD-{requirement}",
  "executor": {
    "adapter": "dsh",
    "executable": "dsh",
    "executor_home": "C:\\Users\\you\\.dsh",
    "config_source": "executor_home",
    "routing": {
      "provider": "codex",
      "model": "gpt-6-luna",
      "effort": "medium"
    },
    "profile": "headless",
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "smoke_timeout": 120
  }
}
~~~

DSH_HOME 继续管理 provider 定义、endpoint 与认证，但
`executor.routing` 决定当前 PSC invocation 实际使用的 provider/model/effort。
PSC 会生成短生命周期的 `--patch` 覆盖 DSH 的 `agent-default-model`，运行结束
后删除，因此切换 Luna / Sol / 其他模型不再需要改 DSH_HOME。

DSH 以 headless `--json` 模式运行，PSC 从 `final.text` 取最终 completion；
token 统计优先读取 durable Session 中当前 v2 的
`assistant/message` / `assistant/attempt` embedded usage，并统计 retry、子
session 和已有日志的 append 增量；durable usage 不可用时才回退到
`step_end.usage`。缺失 usage 会标记 unavailable/inexact，而不是返回伪造的 0。
旧版或自定义 DSH 若没有 headless JSON event stream，仍保留 framed JSON
兼容解析。

---

## Executor 隔离与健康检查

初始化时必须让用户显式确认 provider、model 和 reasoning effort；尤其 model 与 effort 是初始化必选项。不能从 Supervisor、CODEX_HOME、DSH_HOME、authentication 或旧项目自动推断这些值。

Executor 必须是独立环境。

对非交互的一次性 Codex Executor，推荐：

~~~json
{
  "approval_policy": "never",
  "sandbox": "workspace-write"
}
~~~

never 不代表 unrestricted access，真正的文件访问边界仍由 sandbox 控制。

如果使用 on-request，非交互 Executor 可能因为等待审批而卡住。

任何会改变 Executor fingerprint 的配置变化后，都需要重新执行 smoke。

---

## Executor 结构化输出与 Artifact

正常 Executor 必须返回严格结构化 completion。

Runtime 会把语义内容落盘为：

~~~text
developing/
└─ artifacts/
   └─ T-###/
      ├─ plan.md
      └─ coding.md
~~~

Supervisor 做验收时，优先读取：

- plan.md
- coding.md
- git diff / git status
- 测试结果
- 必要时的 log_path

Executor 自己的报告只是 evidence，不是 proof。最终 PASS/RETRY 由 Supervisor 独立判断。

---

## External Planner Contract Bundle

[prompts/contract-export.md](prompts/contract-export.md) 是给外部 Planner 使用的 Contract Export Prompt。

Planner 可以是 ChatGPT Web、另一个 Codex session、Claude 或人工辅助规划会话。Planner 不需要访问 Supervisor 会话。

交接方式：

~~~text
External Planner
↓
PSC-CONTRACT-BUNDLE.md
↓
Supervisor importer
↓
immutable contract/vN/
↓
PSC Supervisor / Executor workflow
~~~

导入：

~~~text
python scripts/psc_runtime.py import-bundle <bundle-path> \
  --repository <target-repository> \
  --runtime-config <target-repository>/.agentic-sdlc/runtime.json
~~~

一个 repository 可以拥有多个独立 workflow。

使用 --project-id <existing-id> 选择已有 workflow，或使用 --new-project-id <new-id> 显式创建新的 workflow。

Contract Bundle 只是**传输格式**。真正执行时只使用物化后的 contract/vN/，Executor 永远不直接解析 Bundle。

---

## Contract 版本与激活

Contract 不可变。

批准新版本时应创建：

~~~text
contract/vN+1/
~~~

而不是修改旧版本。

对于已有 workflow，新 Approved Contract 导入后不会自动改变当前执行版本。需要执行：

~~~text
python scripts/psc_runtime.py activate-contract   --project <workflow-project>   --repository <repository>
~~~

Runtime 会按照 Contract 中声明的 workflow policy 处理 pending task、失效范围和历史 artifact。

---

## 常用 Helper 命令

~~~text
python scripts/psc_runtime.py validate-contract <contract-dir> --repository <path>

python scripts/psc_runtime.py discover --repository <path> --runtime-config <path>

python scripts/psc_runtime.py bootstrap <contract-dir> --repository <path> --runtime-config <path>

python scripts/psc_runtime.py import-bundle <bundle-path> --repository <path> --runtime-config <path>

python scripts/psc_runtime.py auto-import --repository <path> --runtime-config <path>

python scripts/psc_runtime.py activate-contract --project <workflow-project> --repository <path>

python scripts/invoke_executor.py smoke --repository <path> --runtime-config <path>

python scripts/invoke_executor.py status --repository <path> --runtime-config <path>
~~~

原来的：

~~~text
python scripts/invoke_executor.py invoke ...
~~~

仍然保留用于人工调试、CI、recovery 和兼容场景。

但 **正常 Supervisor dispatch 不应再使用 CLI invoke + write_stdin polling**。

---

## 测试

运行：

~~~text
python -m pytest tests -q
~~~

GitHub Actions 会安装 pytest 和 requirements-mcp.txt，并覆盖：

- Contract Bundle parsing/materialization
- workflow bootstrap/discovery
- Executor isolation
- smoke fingerprint
- structured completion
- MCP server 实际实例化
- MCP compact result
- failure diagnostic tail
- stdout/stderr 不泄露到成功结果
- Executor path entrypoint
- Preflight Checker：ALLOW/DENY/UNKNOWN、配置不匹配、SHA 证据过期、E prompt 注入、scope fail-closed、不消耗重试预算、Codex/DSH 只读命令与 DSH patch 构造
- 原有 runtime hardening

---

## 核心设计原则

~~~text
P = Contract author / user-facing planner

Runtime = deterministic orchestration and durable state

S = semantic supervisor / independent verifier

E = disposable implementation worker
~~~

其中：

- P 和 S 可以是完全不同的 session / application / model。
- S 和 E 不共享 Executor Home。
- E 每次 invocation 都是 disposable worker。
- Contract 和 filesystem artifacts 是事实来源。
- Conversation 不是持久状态。
- Executor 不批准自己的工作。
- Supervisor 不负责等待进程轮询，等待由 MCP/runtime 层承担。
- 修改 E 不应迫使用户重新设计 S 或 MCP transport。

## 活跃工作流注册表与 GUI

PSC Runtime 维护独立活跃工作流索引，详见 references/workflow-registry.md。独立 Windows GUI 监视器及其 Neon 图标、构建脚本和 Windows CI 位于 [psc-executor-monitor 专属仓库](https://github.com/XuanzheChen/psc-executor-monitor)，与 Skill 分开维护。
