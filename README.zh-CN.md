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
~~~

tool_timeout_sec 表示**一次 Executor MCP 调用允许持续的最长时间**，不是轮询间隔。

建议满足：

~~~text
tool_timeout_sec >= executor.timeout
~~~

如果 E 提前完成，MCP 会立即返回，不会等满这个时间。

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
| executor.config_source | runtime 或 executor_home。 |
| executor.provider / model / effort | Codex 且 config_source=runtime 时使用。 |
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

### 使用 Executor Home 管理模型配置

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
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "smoke_timeout": 120
  }
}
~~~

此时 provider/model/effort 来自：

~~~text
<executor_home>/config.toml
~~~

PSC 不读取 auth.json 内容，也不会把认证文件复制到别处。

如果你修改了 Executor Home 中相关非敏感配置，smoke fingerprint 会变化，需要重新跑 smoke。

### 使用 runtime.json 显式指定模型

当：

~~~json
"config_source": "runtime"
~~~

时，在 runtime.json 中配置 provider、model、effort，PSC 会通过 CLI override 传给独立的 codex exec。

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
    "profile": "headless",
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "smoke_timeout": 120
  }
}
~~~

DSH 自己拥有 provider/model/reasoning 配置，因此这里通常使用 config_source=executor_home。

---

## Executor 隔离与健康检查

初始化时不能从 Supervisor 自动推断 model、provider、CODEX_HOME、authentication 或 permission profile。

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
