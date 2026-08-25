# PSC External Planner — Project Contract Export Prompt

你现在需要把我们在当前对话中已经完成的项目规划，整理成一个**可交付给独立开发 Supervisor 执行的 Project Contract**。

你不需要知道任何后续多 Agent 工作流，也不要假设后续执行者能够读取当前对话。

你的任务不是继续讨论方案，而是：

> 将当前对话中已经确定的需求、设计决策、约束、验收标准和开发任务，编译成一个完整、自包含、无上下文依赖的项目契约。

后续执行该契约的 Agent 将处于**完全独立的新会话**中。

它不会看到：

- 当前聊天记录
- 我们之前的讨论过程
- 你的推理过程
- “之前说过什么”
- “我们已经确认过什么”

因此，任何执行开发所必需的信息，都必须写进 Contract。

---

# 一、基本原则

请严格遵守以下原则。

## 1. Contract 必须自包含

禁止出现：

- “按照之前讨论的方案”
- “如前所述”
- “使用我们刚刚确定的方法”
- “根据用户之前的描述”
- “继续上一阶段”
- 任何依赖当前聊天上下文才能理解的表达

必须把实际内容重新明确写出来。

---

## 2. 不要重新设计已经确定的方案

优先忠实整理当前对话中已经达成的结论。

不要为了让文档“看起来更完整”而擅自增加：

- 新需求
- 新功能
- 新架构
- 新技术栈
- 新约束
- 用户没有要求的工程改造

可以补充为了工程执行而必需的细节，但必须与当前已经确定的设计一致。

---

## 3. 不确定内容不得伪装成确定需求

如果某项内容在当前讨论中仍然没有确定：

不要自行猜测。

将其明确标记为：

```text
UNRESOLVED
```

并说明：

- 未确定的内容是什么
- 为什么会影响开发
- 需要什么决策

如果这个未决项会导致开发无法安全开始，则 Contract 状态必须为：

```text
draft
```

而不是：

```text
approved
```

---

## 4. 我的这条消息视为“生成正式 Contract”的明确授权

如果当前讨论中的所有关键决策已经足以执行开发，则可以：

```json
"status": "approved"
```

如果仍然存在会影响实现方向或验收结果的重大未决项，则必须：

```json
"status": "draft"
```

不要为了输出 Approved Contract 而擅自解决重大歧义。

---

# 二、Contract 必须采用稳定 ID

所有正式需求使用：

```text
REQ-001
REQ-002
REQ-003
...
```

所有验收标准使用：

```text
AC-001
AC-002
AC-003
...
```

所有开发任务使用：

```text
T-001
T-002
T-003
...
```

如果存在重要约束，可以使用：

```text
C-001
C-002
C-003
...
```

这些 ID 必须在整个 Contract 中保持一致。

Task 必须明确引用对应的 REQ 和 AC。

---

# 三、Requirement 编写规则

Requirement 描述：

> 系统最终必须具备什么能力。

Requirement 不要写具体代码步骤。

每个 Requirement 至少包含：

```text
ID
Title
Description
Rationale（必要时）
Priority
```

Requirement 应该明确到一个独立开发 Agent 可以理解。

---

# 四、Acceptance Criteria 编写规则

Acceptance Criteria 是 Supervisor 后续验收实现的主要依据。

因此每个 AC 必须尽可能做到：

- 可观察
- 可验证
- 尽量可测试
- 避免主观表达
- 明确成功与失败边界

避免：

```text
代码质量要高
性能要好
体验要流畅
实现要合理
```

除非给出具体可验证定义。

优先写成类似：

```text
AC-004

When DOI extraction fails for a document,
the document processing pipeline MUST continue,
and the failure MUST be recorded without terminating the process.
```

Acceptance Criteria 必须引用它所覆盖的 Requirement。

---

# 五、Implementation Recommendation

implementation.md 描述：

> 推荐如何实现当前 Contract。

这里允许包含：

- 推荐架构
- 模块边界
- 技术方案
- 数据流
- 重要接口
- 推荐实现顺序
- 与现有代码的集成方式
- 明确已经确定的技术决策

但必须区分：

```text
REQUIRED
```

和：

```text
RECOMMENDED
```

如果某项实现方式只是建议，而不是 Contract 强制要求，必须明确标注为 RECOMMENDED。

不要把建议偷偷升级成 Requirement。

---

# 六、Constraints

constraints.md 记录实现过程中不得违反的边界。

例如：

- 不允许修改的模块
- 必须兼容的现有接口
- Python / Node / Framework 版本
- 不得引入的依赖
- 安全要求
- 性能要求
- 数据要求
- 向后兼容要求
- 用户明确要求保留的现有设计
- Repository 范围限制

每个重要约束使用：

```text
C-001
C-002
...
```

如果当前对话没有对应约束，不要凭空制造大量约束。

---

# 七、Task Breakdown

将 Contract 分解成 Executor 可以逐项执行的 Task。

原则：

> Task 是开发执行单元，不是 Requirement 的简单复制。

每个 Task 必须包含：

```text
Task ID

Title

Goal

Requirements

Acceptance Criteria

Dependencies

Allowed Scope

Forbidden Scope

Implementation Notes

Required Verification
```

示例：

```text
T-003

Requirements:
- REQ-002

Acceptance:
- AC-003
- AC-004

Dependencies:
- T-001
```

Task 粒度应该满足：

> 一个独立、无长期记忆的 Coding Agent，仅获得该 Task 和 Contract 后，可以理解自己应该完成什么。

避免任务过大，例如：

```text
T-001 完成整个系统
```

也避免无意义地过度拆分成几十个极小步骤。

---

# 八、Task Dependency

明确 Task 之间的依赖关系。

如果：

```text
T-003
```

必须依赖：

```text
T-001
T-002
```

必须明确写出。

不得依赖开发 Agent 自己猜测执行顺序。

如果任务可以并行，也可以明确：

```text
Dependencies: None
```

---

# 九、Contract Version

本次默认输出：

```text
version = 1
```

除非我在当前对话中明确告诉你这是已有 Contract 的更新版本。

对于初始版本：

```json
"supersedes": null
```

如果是更新已有 Contract，则根据我提供的信息设置正确版本。

禁止覆盖已有 Contract 的语义。

---

# 十、Initial Workflow Policy

对于初始 v1 Contract：

```json
"workflow_policy": {
  "restart": "all"
}
```

该字段主要用于后续 Contract Version 发生变化时。

如果本次确实是已有 Contract 的新版本：

必须根据本次需求变更的影响范围明确选择：

```text
restart = all
```

或者：

```text
restart = pending_only
```

或者：

```text
invalidate_from_task = T-XXX
```

如果无法可靠判断，不要擅自决定。

将 Contract 状态设为 draft，并说明需要确认的问题。

---

# 十一、不要包含 Runtime 配置

这个 Contract 描述的是：

> 开发什么。

不要包含 Supervisor Runtime 自己的配置。

例如不要加入：

- runtime_root
- Codex CODEX_HOME
- Executor provider
- API Key
- Executor model
- reasoning effort
- sandbox
- approval mode

除非它们本身确实是项目实现要求的一部分。

---

# 十二、最终输出格式

最终只输出一个：

```text
PSC-CONTRACT-BUNDLE
```

不要输出解释、前言、后记或额外建议。

严格使用以下结构：

```text
# PSC-CONTRACT-BUNDLE

## CONTRACT-MANIFEST

Version: 1
Status: approved | draft

Files:
- metadata.json
- requirements.md
- acceptance.md
- implementation.md
- constraints.md
- tasks.md


==================================================
FILE: metadata.json
==================================================

{完整 JSON}


==================================================
FILE: requirements.md
==================================================

# Requirements

完整内容


==================================================
FILE: acceptance.md
==================================================

# Acceptance Criteria

完整内容


==================================================
FILE: implementation.md
==================================================

# Implementation Recommendation

完整内容


==================================================
FILE: constraints.md
==================================================

# Constraints

完整内容


==================================================
FILE: tasks.md
==================================================

# Task Breakdown

完整内容


==================================================
END PSC-CONTRACT-BUNDLE
==================================================
```

---

# 十三、metadata.json Schema

至少包含：

```json
{
  "schema_version": 1,
  "version": 1,
  "status": "approved",
  "created_by": "external-planner",
  "created_at": "<ISO-8601 timestamp>",
  "supersedes": null,
  "workflow_policy": {
    "restart": "all"
  }
}
```

`created_at` is REQUIRED and must be an ISO-8601 timestamp. You may add useful fields such as:

```text
project_name
contract_summary
```

但不要加入聊天上下文、模型推理过程或无关元数据。

---

# 十四、输出前自行执行一致性检查

在最终输出前，请自行检查：

1. 每个 REQ ID 是否唯一。
2. 每个 AC ID 是否唯一。
3. 每个 Task ID 是否唯一。
4. Task 引用的 REQ 是否真实存在。
5. Task 引用的 AC 是否真实存在。
6. 每个重要 Requirement 是否至少有一个 Acceptance Criterion 覆盖。
7. 每个 Acceptance Criterion 是否有 Task 负责实现或验证。
8. Task dependency 是否不存在明显循环。
9. 是否存在依赖当前聊天记录才能理解的表达。
10. 是否擅自加入了用户未确认的重要需求。
11. 是否存在重大 UNRESOLVED 项。
12. 如果存在重大未决项，metadata.status 是否正确设置为 draft。

不要输出这份检查过程。

只输出检查完成后的最终 PSC-CONTRACT-BUNDLE。
