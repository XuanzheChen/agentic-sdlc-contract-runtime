你现在已经完成了一个软件开发需求的规划工作。

接下来，你需要把**当前对话中已经完成的规划结果**整理成一个机器可导入的软件开发契约包。

你不需要理解后续开发框架，也不需要知道任何 Multi-Agent / Agentic SDLC / PSC 的内部实现。

你只需要完成这一件事：

> 将当前已经确定的需求、验收标准、实现建议、约束和任务拆分，转换成一个严格格式的 `PSC-CONTRACT-BUNDLE`。

后续会有一个完全独立的开发 Supervisor 读取这个 Bundle。

这个 Supervisor：

- 看不到当前聊天记录
- 看不到你的历史推理
- 不知道用户之前说过什么
- 不知道你为什么做出某个设计决定
- 不会自动补全缺失信息

因此最终 Bundle 必须：

- 自包含
- 明确
- 可执行
- 可验证
- 无聊天上下文依赖
- 严格符合下面定义的格式

---

# 1. 你的角色

你现在只是：

```text
Contract Compiler
```

你的任务不是重新规划项目。

不要重新设计已经确定的方案。

不要为了让文档显得完整而擅自加入：

- 新功能
- 新需求
- 新架构
- 新技术栈
- 新依赖
- 新约束
- 用户没有确认的新行为

你应该优先忠实整理：

```text
当前对话中已经确定的规划结果
```

---

# 2. 如果规划中仍有重大未决问题

如果存在会影响以下内容的重大未决问题：

- 实现方向
- 功能边界
- 数据语义
- 对外接口
- 验收结果
- 关键安全约束

不要自行猜测。

请在对应位置写：

```text
UNRESOLVED
```

并说明具体未决内容。

如果这个未决问题会导致开发无法安全开始，则：

```json
"status": "draft"
```

如果当前规划已经足以直接开发，则：

```json
"status": "approved"
```

不要为了强行输出 approved 而解决用户没有决定的问题。

---

# 3. Contract Version

如果用户没有明确告诉你这是已有 Contract 的修订版，则输出：

```json
"version": 1,
"supersedes": null
```

即使用户之前生成过一个**格式错误、导入失败、没有正式生效**的 Bundle，也仍然可以重新输出：

```text
version = 1
supersedes = null
```

只有当用户明确告诉你：

> 已经存在一个成功建立的正式 Contract vN，现在这是对其需求语义的修订

时，才增加版本号并设置 `supersedes`。

---

# 4. metadata.json 必须满足的格式

必须至少包含：

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

要求：

## schema_version

固定：

```json
1
```

## version

正整数。

初始 Contract：

```json
1
```

## status

只能使用：

```text
approved
draft
```

## created_by

使用：

```json
"external-planner"
```

## created_at

这是**必填字段**。

必须填写当前生成时刻的 ISO-8601 时间戳，例如：

```json
"created_at": "2026-08-26T01:20:00Z"
```

不要省略。

## supersedes

初始版本：

```json
null
```

只有真正修订已有正式 Contract 时才填写较小版本号。

## workflow_policy

初始 v1 默认使用：

```json
{
  "restart": "all"
}
```

更新已有 Contract 时，只能使用以下三种之一：

```json
{
  "restart": "all"
}
```

或：

```json
{
  "restart": "pending_only"
}
```

或：

```json
{
  "invalidate_from_task": "T-003"
}
```

一个 `workflow_policy` 中只能存在一种策略。

---

# 5. 不要写 repository 本地路径或 GitHub repository ID

除非用户明确要求，否则：

**不要在 metadata.json 中加入 `repository` 字段。**

特别不要写：

```json
"repository": "XuanzheChen/transit-scholar"
```

也不要写：

```json
"repository": "E:\\some\\local\\path"
```

Contract 应保持可移植，不应该绑定某台机器上的本地 clone 路径。

你可以加入：

```json
"project_name": "TransitScholar"
```

或：

```json
"contract_summary": "..."
```

但这些不是必填项。

---

# 6. Stable IDs

正式 Requirement 必须使用：

```text
REQ-001
REQ-002
REQ-003
...
```

Acceptance Criteria 必须使用：

```text
AC-001
AC-002
AC-003
...
```

开发 Task 必须使用：

```text
T-001
T-002
T-003
...
```

重要 Constraint 可以使用：

```text
C-001
C-002
C-003
...
```

要求：

- 同一命名空间 ID 唯一
- 不跳跃不是强制要求，但建议连续
- 所有引用必须真实存在
- 不允许引用不存在的 ID

---

# 7. requirements.md

Requirement 描述：

> 系统最终必须具备什么能力。

不要把 Requirement 写成代码操作步骤。

每个 Requirement 必须使用 Markdown 二级标题：

```markdown
## REQ-001
```

不要只写：

```text
REQ-001:
```

推荐格式：

```markdown
## REQ-001

Title: <简短标题>

Description:
<完整、自包含的需求描述>

Rationale:
<为什么需要它；如无必要可省略>

Priority: Must
```

Requirements 应该明确到独立开发 Agent 可以理解。

禁止写：

```text
按照之前讨论的方式实现
沿用前面的逻辑
和上面说的一样
继续上一阶段
```

必须重新明确写出实际要求。

---

# 8. acceptance.md

Acceptance Criteria 描述：

> 实现怎样才算通过。

每个 Acceptance Criterion 必须使用：

```markdown
## AC-001
```

每个 AC 应明确引用对应的 Requirement，例如：

```markdown
## AC-001

Requirements:
- REQ-001

Criterion:
When ...
the system MUST ...
```

Acceptance Criteria 必须尽量：

- 可观察
- 可验证
- 可测试
- 明确成功/失败边界

不要写：

```text
代码质量较好
性能不错
逻辑合理
体验流畅
```

除非已经定义可测量标准。

优先写：

```text
When X occurs,
the system MUST produce Y,
and MUST NOT produce Z.
```

---

# 9. implementation.md

这个文件描述：

> 推荐怎样实现 Contract。

可以包含：

- 架构方案
- 模块边界
- 数据流
- 重要接口
- 推荐实现顺序
- 与现有代码集成方式
- 已经确定的技术决策
- 迁移方案
- 风险点

必须明确区分：

```text
REQUIRED
```

和：

```text
RECOMMENDED
```

例如：

```markdown
## REQUIRED

- Existing public API X must remain compatible.
- Module Y must remain the single source of truth.

## RECOMMENDED

- Prefer implementing the new resolver as a separate adapter.
- Prefer adding regression tests before refactoring.
```

不要把“实现建议”偷偷升级成 Requirement。

---

# 10. constraints.md

记录开发不得违反的边界。

正式 Constraint 推荐：

```markdown
## C-001

<约束内容>
```

例如：

```markdown
## C-001

Existing Layer 1 public interfaces MUST remain backward compatible.
```

可以包含：

- 不允许修改的模块
- 必须兼容的接口
- 数据限制
- 安全要求
- 不允许添加的依赖
- Python / Node / Framework 版本
- 向后兼容
- 文件范围
- 用户要求保留的已有实现

不要为了数量而创造没有必要的 Constraint。

---

# 11. tasks.md —— 最重要的格式要求

Task 是后续 Coding Agent 的直接执行单元。

每个 Task 必须使用：

```markdown
## T-001
```

每一个 Task **都必须明确包含以下字段**：

```text
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

不得省略。

标准格式：

```markdown
## T-001

Title: <任务标题>

Goal:
<本 Task 最终要完成什么>

Requirements:
- REQ-001
- REQ-002

Acceptance Criteria:
- AC-001
- AC-003

Dependencies:
- None

Allowed Scope:
- src/example/**
- tests/example/**

Forbidden Scope:
- src/unrelated/**
- contracts/**

Implementation Notes:
- <本 Task 相关的实现提示>
- <如无特别说明可写 None>

Required Verification:
- python -m pytest tests/example -q
- <其他必须执行的验证>
```

特别注意：

## Requirements 必须存在

不允许某个 Task 没有：

```text
Requirements:
```

至少引用一个实际存在的 `REQ-###`，除非这个 Task 纯粹是验证任务，但即使是验证任务，也应该引用它所验证的 Requirement。

## Acceptance Criteria 必须存在

不允许某个 Task 没有：

```text
Acceptance Criteria:
```

至少引用一个实际存在的 `AC-###`。

## Dependencies 必须存在

没有依赖时写：

```markdown
Dependencies:
- None
```

有依赖时：

```markdown
Dependencies:
- T-001
- T-002
```

所有依赖 Task 必须真实存在。

不得形成循环依赖。

## Allowed Scope 必须存在

填写 repository-relative path。

例如：

```markdown
Allowed Scope:
- src/l2s3/**
- tests/l2s3/**
```

不要写本地绝对路径。

## Forbidden Scope 必须存在

没有特殊禁止范围时，可以写：

```markdown
Forbidden Scope:
- None
```

不要让同一个路径同时出现在 Allowed Scope 和 Forbidden Scope。

## Implementation Notes 必须存在

如果没有额外建议：

```markdown
Implementation Notes:
- None
```

## Required Verification 必须存在

至少写明这个 Task 完成后必须执行什么验证。

例如：

```markdown
Required Verification:
- python -m pytest tests/l2s3 -q
```

如果无法确定具体命令，也必须明确说明需要进行什么验证，而不是完全省略。

---

# 12. Task 粒度

Task 不应过大。

不要：

```text
T-001 完成整个需求
```

也不要拆成几十个没有独立工程意义的小步骤。

理想情况：

> 一个新的、没有历史记忆的 Coding Agent，只得到这个 Task 和 Contract，就可以理解自己需要实现什么。

Task 应尽量形成合理依赖，例如：

```text
T-001 基础 schema
T-002 parser
T-003 integration
T-004 regression tests
```

而不是让 Coding Agent 自己猜执行顺序。

---

# 13. Requirement / AC / Task 覆盖关系

输出前必须保证：

```text
Requirement
    ↓
至少有一个 Acceptance Criterion
    ↓
至少有一个 Task 实现或验证
```

并且：

```text
Task
↓
明确引用 Requirement
↓
明确引用 Acceptance Criteria
```

不允许出现孤立的：

```text
REQ
AC
T
```

---

# 14. 不要加入 Runtime 配置

这个 Bundle 只描述：

> 开发什么。

不要加入后续开发环境配置，例如：

- CODEX_HOME
- Codex provider
- Executor model
- reasoning effort
- API key
- auth token
- approval policy
- sandbox
- runtime_root
- Supervisor 配置
- Agentic SDLC 内部状态

除非这些本身确实是被开发产品的 Requirement。

---

# 15. Bundle 必须严格包含六个文件

最终 Bundle 必须声明并包含：

```text
metadata.json
requirements.md
acceptance.md
implementation.md
constraints.md
tasks.md
```

只能有这六个 canonical FILE section。

---

# 16. 最终输出格式

最终回复必须**只输出 Bundle 本身**。

不要：

- 解释
- 总结
- 前言
- 后记
- “以下是结果”
- 修改建议
- Markdown 代码围栏包住整个 Bundle

必须直接从：

```text
# PSC-CONTRACT-BUNDLE
```

开始。

严格采用：

```text
# PSC-CONTRACT-BUNDLE

## CONTRACT-MANIFEST

Version: 1
Status: approved

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


==================================================
FILE: requirements.md
==================================================

# Requirements

## REQ-001

Title: ...

Description:
...

Priority: Must


==================================================
FILE: acceptance.md
==================================================

# Acceptance Criteria

## AC-001

Requirements:
- REQ-001

Criterion:
...


==================================================
FILE: implementation.md
==================================================

# Implementation Recommendation

## REQUIRED

...

## RECOMMENDED

...


==================================================
FILE: constraints.md
==================================================

# Constraints

## C-001

...


==================================================
FILE: tasks.md
==================================================

# Task Breakdown

## T-001

Title: ...

Goal:
...

Requirements:
- REQ-001

Acceptance Criteria:
- AC-001

Dependencies:
- None

Allowed Scope:
- ...

Forbidden Scope:
- None

Implementation Notes:
- ...

Required Verification:
- ...


==================================================
END PSC-CONTRACT-BUNDLE
==================================================
```

Manifest 中：

```text
Version:
Status:
```

必须和 `metadata.json` 完全一致。

---

# 17. Approved Bundle 的额外限制

如果：

```json
"status": "approved"
```

则六个 canonical artifacts 中不得出现：

```text
UNRESOLVED
```

如果还有真正重大未决问题，则必须改成：

```text
Status: draft
```

以及：

```json
"status": "draft"
```

---

# 18. 最终生成前必须自行检查

不要输出检查过程，但在最终回答前自行确认：

1. `created_at` 已填写且是 ISO-8601 时间戳。
2. metadata 不包含 `repository` 本地路径或 GitHub `owner/repo` 标识。
3. `schema_version = 1`。
4. Manifest Version 与 metadata.version 一致。
5. Manifest Status 与 metadata.status 一致。
6. 正好存在六个 FILE section。
7. 每个 REQ 使用 `## REQ-###` 标题。
8. 每个 AC 使用 `## AC-###` 标题。
9. 每个 Task 使用 `## T-###` 标题。
10. 所有 ID 唯一。
11. 每个 AC 引用的 REQ 都真实存在。
12. 每个 Task 都有 `Requirements:`。
13. 每个 Task 都有 `Acceptance Criteria:`。
14. 每个 Task 的 REQ 引用都真实存在。
15. 每个 Task 的 AC 引用都真实存在。
16. 每个 Task 都有 `Dependencies:`。
17. 所有 Task dependency 都真实存在。
18. 没有 Task 自己依赖自己。
19. Task dependency 不存在循环。
20. 每个 Task 都有 `Allowed Scope:`。
21. 每个 Task 都有 `Forbidden Scope:`。
22. Allowed Scope 与 Forbidden Scope 不冲突。
23. 每个 Task 都有 `Implementation Notes:`。
24. 每个 Task 都有 `Required Verification:`。
25. 每个重要 Requirement 都至少被一个 AC 覆盖。
26. 每个重要 AC 都至少有一个 Task 实现或验证。
27. Approved Contract 不包含 `UNRESOLVED`。
28. 不存在“如前所述”“按照之前讨论”等聊天上下文依赖。
29. 没有加入 Runtime / Executor / API credential 配置。
30. 没有擅自改变已经完成的规划结论。

如果任何结构要求没有满足，请在输出最终 Bundle 之前自行修正。

最终只输出：

```text
PSC-CONTRACT-BUNDLE
```

本身。