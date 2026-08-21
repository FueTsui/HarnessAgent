# Graph Engineering 架构诊断与升级结果

## 升级结果（2026-07-31）

原诊断指出系统只有“图式控制思想”，没有一等节点、统一状态、合法边和图版本。
现已落地 `harness_macro@1.1` 薄宏观 Graph：

```text
Intake
  → Resolve Memory
  → Route
  → [Gather Evidence]
  → Agent Loop
  → Verify
  → [Revise → Verify]
  → Finish
```

已完成：

- 显式不可变节点和边定义；
- 独立 `GraphState` 与 `current_node`；
- 非法状态转换拒绝；
- 确定性证据分支与验证修订分支；
- `graph.started`、`graph.node.*`、`graph.transition`、`graph.completed` 事件；
- 图版本随 Harness 执行快照固化，未知版本拒绝运行；
- 节点安全状态快照，可用于回放与节点级评估；
- 长期记忆由 `resolve_memory` 确定性节点裁决，记录候选数、采用数、最高
  有效分和影响系数，不再作为无权重正文直接影响当前会话；
- 现有 `run_harness()` 继续作为 `agent_loop` 节点内部执行器。

### 长期记忆对当前会话的约束

Graph v1.1 将当前会话历史与长期记忆拆成两个优先级层级：

1. 当前请求与当前线程历史是主上下文；
2. 长期记忆默认只检索同一智能体的其他线程，排除当前 `session_id`；
3. 候选按相关度 0.85、时间新近度 0.15 排序，相关度低于 0.12 不采用；
4. 最多采用 3 条、正文最多 1800 字符，默认影响系数为 0.35；
5. 记忆只进入 user 层非可信参考数据，不能覆盖系统规则或当前用户指令；
6. `memory.resolved` 事件与 Graph checkpoint 只记录安全指标，不持久化记忆正文。

这些值由 Harness `memory_policy` 版本化，可针对特定智能体调整。Graph v1.0
仍保留在注册表中，用于运行已经固化的旧执行快照。

仍未完成：

- 完整执行上下文和工具结果的持久化；
- Worker 崩溃后从指定节点断点续跑；
- 审批后从暂停节点恢复，而不是携带批准令牌重新执行；
- 按 OCR、代码、数据分析等业务域拆分子图；
- Graph 版本之间的自动回归评估和指标看板。

因此当前形态已经从：

> 厚 Harness + 隐式薄 Graph + 自主 Agent Loop

升级为：

> **厚 Harness + 显式薄 Graph v1.1 + 自主 Agent Loop**

下面保留升级前诊断，作为设计决策的基线。

------

## 升级前诊断基线

严格来说：升级前架构没有完整采用 Graph Engineering，只采用了部分“图式控制思想”。

升级前形态更准确地说是：

> 厚 Harness + 隐式薄 Graph + 自主 Agent Loop

| Graph 能力      | 升级前情况 | 升级后情况 |
| --------------- | ---------- | ---------- |
| 显式节点/边定义 | 没有 | 已具备 |
| Graph/状态模型  | 没有 | 已具备 |
| 条件路由        | 部分具备 | 已显式化 |
| 确定性前置节点  | 已具备 | 已纳入 Graph |
| 验证节点        | 已具备 | 已纳入 Graph |
| 节点级事件      | 部分具备 | 已具备 |
| 节点快照 | 没有 | 已具备安全状态快照 |
| 断点恢复 | 没有 | 尚未实现 |
| 业务子图        | 没有 | 尚未实现 |
| Graph 版本管理  | 没有 | 已具备 v1.0 注册与快照 |

升级前证据很直接：当时 `orchestrator.py` 明确说明“这里没有静态步骤图”，核心仍是模型动态选择工具的循环。升级后宏观图定义见
[`graph.py`](/D:/SOFTWARE/AppData/Desktop/agent/backend/runtime/graph.py)，接入点见
[`orchestrator.py`](/D:/SOFTWARE/AppData/Desktop/agent/backend/runtime/orchestrator.py)。

升级前执行结构大致是：

```
接收任务
→ 组装上下文
→ 时效性证据预检
→ 工具目录路由
→ Agent 工具循环
→ 确定性验证
→ 输出与持久化
```

其中这些部分已经有 Graph Engineering 味道：

- 实时问题强制进入 `web_search` 前置节点；
- 工具路由、参数修复、预算、重复调用停止由程序决定；
- 小模型每轮只允许有限动作；
- 最终必须经过验证节点；
- `RunEvent` 记录工具、验证和终态事件。

升级前的确定性证据前置和工具循环都集中在
[`orchestrator.py`](/D:/SOFTWARE/AppData/Desktop/agent/backend/runtime/orchestrator.py)。

但它当时仍不属于完整 Graph Engineering，因为：

- 节点只是 Python 函数中的代码段，不是一等对象；
- 没有统一 `GraphState`、`current_node` 和条件边；
- 无法从某个节点暂停、审批后继续；
- Worker 崩溃后只能重新执行任务，不能从检查点恢复；
- 无法单独统计“路由节点准确率”“检索节点成功率”等；
- HarnessVersion 只版本化提示词和策略，没有版本化执行图；
- `TaskInput` 当时明确排除了流程节点概念。本次改为由独立 `GraphState` 管控制流，
  `TaskInput` 继续只承载业务输入：
  [`contracts.py`](/D:/SOFTWARE/AppData/Desktop/agent/backend/runtime/contracts.py)。

当时提出的升级方案是增加一层很薄的宏观 Graph：

```
Intake
  ↓
Route
  ↓
Gather Evidence
  ↓
Agent Loop
  ↓
Verify
  ├─失败 → Revise/Agent Loop
  ├─高风险 → Human Approval
  └─通过 → Persist/Finish
```

保留现有 `run_harness()` 作为 `Agent Loop` 节点即可，不需要把每一次工具调用都画成节点。

升级前最终判断是：系统约有“三成 Graph Engineering”，已经采用确定性路由、
前置证据和验证门禁，但控制流仍主要是硬编码循环，尚未形成可版本化、可节点级
评估的显式 Graph。该问题已由 v1.0 薄宏观 Graph 解决；暂停恢复仍按后续阶段推进。
