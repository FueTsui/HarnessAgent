# 智能体阶段模型与 routing v2

模型配置保存在现有 `Agent.routing` JSON 中，不新增数据库列。执行模型沿用 `Agent.provider_id` 和既有 `fixed / rules / policy` 路由；新增阶段模型用于规划、工具选择和答复修订。修改配置只影响后续提交的任务，已入队任务使用其冻结快照，启动时仍复核独立阶段模型的有效性和引用权限。

下面是 `PATCH /api/v1/agents/{agent_id}` 的配置示例。三个阶段均继承本轮执行模型，显式启用模型工具选择；改为 `deterministic` 可恢复规则选择。

```json
{
  "routing": {
    "version": 2,
    "mode": "fixed",
    "default_provider_id": null,
    "rules": [],
    "fallback_provider_ids": [],
    "strategy": "ordered",
    "health": {
      "enabled": true,
      "lookback_minutes": 60,
      "min_samples": 3,
      "max_error_rate": 0.6,
      "consecutive_failures": 3
    },
    "roles": {
      "planner": {"provider_id": null, "reasoning_effort": "", "max_tokens": null},
      "router": {"provider_id": null, "reasoning_effort": "", "max_tokens": null},
      "critic": {"provider_id": null, "reasoning_effort": "", "max_tokens": null}
    },
    "planning": {"mode": "auto"},
    "tool_routing": {"mode": "model", "confidence_threshold": 0.7},
    "review": {"mode": "on_failure"}
  }
}
```

四个运行角色的职责与触发位置如下。`roles` 只接受三个阶段名称，执行模型无需另设 `roles.executor`。

| 角色 | 运行位置 | 配置与默认行为 |
| --- | --- | --- |
| 执行模型 | 普通 Agent Loop、业务工具选择及最终答复 | `fixed` 使用智能体主模型；`rules` 按规则选择；`policy` 再应用健康、成本及备用策略。本轮用户明确选模型时，不自动添加备用模型。 |
| 规划 `planner` | 强制初始计划，以及已有计划未收束时的一次独立收尾 | `auto` 使用现有任务意图和长度启发式判断；`always` 对每次任务要求初始计划；`off` 不注册 `update_plan`。前两项仍要求客户端支持工具协议且工具策略允许计划能力。执行期间的普通计划更新由执行模型完成。 |
| 路由 `router` | 普通循环中，已有候选工具超过一个且当前不处于初始规划阶段 | 仅 `tool_routing.mode=model` 增加模型选择调用；默认 `deterministic` 沿用既有规则筛选。候选不足两个时直接沿用。 |
| 验证修订 `critic` | 最终答复的确定性验证发现可由文本修订解决的问题时 | 默认 `on_failure`，修订次数仍受 Harness 验证策略限制；`off` 关闭模型修订。缺工具证据、未完成计划等问题不能靠此阶段补做工作。 |

`planner` 的初始调用及收尾只允许 `update_plan`。`critic` 只修订文本，不调用业务工具；修订后重新运行确定性验证。关闭规划或修订不会取消权限、审批、产物检查、证据要求及完成状态判定。未通过的软条件按原策略保留 `completed_with_issues`，硬性证据缺口仍阻止成功完成。

每个阶段可把 `provider_id` 改为有引用权限且已启用的模型连接 ID；`null` 继承本轮选定的执行模型。`reasoning_effort` 接受空串、`minimal / low / medium / high / xhigh / max`；`max_tokens` 接受 `null` 或 `1..1000000` 整数。空参数继承连接设置，覆盖仅作用于该阶段快照，不修改模型连接和执行模型。上游服务是否支持某个推理档位，仍取决于所选连接和模型。

**ChatGPT 订阅连接的当前客户端不发送阶段推理强度及输出上限参数。** 可以选择该连接承担阶段角色，但这两项必须留空。保存独立角色、配置继承角色或本轮切换执行模型时，检测到这种不支持的覆盖会明确拒绝，避免设置静默失效。声明 `max_tokens_param=none` 的连接也不能设置阶段输出上限。这里的限制针对本项目现有客户端，不代表对上游服务完整能力的判断。

工具选择使用受限 `select_tools` 函数，输出为 `{"choices": ["已知工具名"], "confidence": 0.8}`。服务端要求恰好一个正确函数调用，验证字段、非空且无重复的候选子集，以及 `0..1` 的有限数值。额外字段、越界名称、无效结构或低于阈值都会恢复原来的确定性候选。模型不可调用 `select_tools` 来执行业务操作；该结果只是下一次执行模型调用的候选提示。必要证据工具及已开始的计划控制能力可在随后重新加入候选，始终限于当前授权目录；最终工具执行仍经过原有授权与参数校验。

独立阶段模型调用失败时尝试本轮执行模型；模型路由全部调用失败后沿用确定性候选。审批要求和内容护栏拒绝直接传播，不通过换模型绕过。角色引用在创建、更新、导入时校验；冻结及启动时按智能体创建者的引用权限检查独立模型，停用、删除或权限撤回后继承执行模型。修改执行模型时也会复核现有阶段参数的兼容性。只有智能体管理权限、没有模型管理权限的账号，可通过 `GET /api/v1/agents/model-options` 获取其可引用的启用模型简表；响应不含凭据或连接详情。

公共过程记录采用与历史回放相同的字段投影：

- `model.role.selected`：阶段、提供商 ID、是否继承执行模型。
- `model.role.fallback`：阶段、回退目标、固定原因 `role_unavailable`。
- `tools.selection`：选择来源、`accepted / invalid_selection / low_confidence / model_unavailable`、工具名称，以及可用时的估计置信值。
- 原有 `plan.*`、`verification.*` 和 `evaluation.*` 继续记录计划与验收证据。

这些新增事件不公开原始模型解释、推理文本、请求参数或完整上游返回。`confidence_kind` 固定为 `model_estimate`。

旧的无版本 `fixed / rules / policy` 配置仍可运行；缺省新选项为 `planning=auto`、`tool_routing=deterministic`、`review=on_failure`，复用既有规划和修订流程，不额外启用路由模型调用。后台保存时写入 v2，切换模式仍保留已配置的规则、健康策略、备用顺序、顶层扩展字段及健康策略扩展键，并校验暂不启用的提供商引用。规则不再静默截断为 32 条或 256 字符；超过 1024 条或单条匹配值超过 16000 字符时明确拒绝保存。迁移含非标准匹配类型的旧配置时也会提示修正；v2 支持 `keyword / length_gt / requires_image / reasoning`。

本实现借鉴 TypeSafe 的受限类型决策、按意图分流和阈值回退思路，参见官方 [System One](https://docs.typesafe.ai/concepts/system-one)、[Intent routing](https://docs.typesafe.ai/patterns/intent-routing) 与 [Confidence-gated routing](https://docs.typesafe.ai/patterns/confidence-routing)。项目没有接入 TypeSafe SDK、Jev 模型或其概率接口。当前 `confidence` 由普通模型自报，未经统计校准，不能解释为 Jev 的校准概率或任务成功率；高置信值也不授予权限、不替代验收。

实现入口为 [配置与结构化决策](../backend/model_roles.py)、[管理接口](../backend/api/agents.py)、[执行快照](../backend/api/chat.py)、[运行循环](../backend/runtime/orchestrator.py) 和 [公共投影](../backend/runtime/process_view.py)。[阶段模型测试](../tests/test_model_roles.py) 使用本地替身覆盖权限、快照、真实循环分派、参数传递、越界及低置信回退；这些结果不代表真实模型质量或概率校准验证。
