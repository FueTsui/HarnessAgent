# Codex 风格任务编排审计验收报告

审计日期：2026-08-03  
审计范围：`backend/runtime/orchestrator.py`、`backend/runtime/task_store.py`、`backend/jobs.py`、`backend/api/chat.py`、`frontend/static/app.js`、`frontend/static/style.css` 及相关测试。

## 一、验收结论

本次审计发现原实现已经具备计划胶囊、稳定步骤 ID、运行过程展示和历史回放基础，但首次检查时尚未完全达到设计要求，主要缺口为：

1. 计划只有 `plan.updated`，没有创建与步骤生命周期事件。
2. 计划没有显式 revision，也不能拒绝陈旧 revision 覆盖。
3. 步骤没有 `failed` 与 `skipped` 状态。
4. 实时事件没有统一携带 `task_id、event_id、timestamp、revision`。
5. 页面快照与实时订阅之间存在事件竞态窗口，连接中断后只保留后台运行状态，没有自动续接流。
6. 触屏设备没有点击外部收起任务卡片。
7. 完成步骤使用实心圆点，不是明确的勾选标识。

上述缺口已在本次工作中整改。整改后，设计条目均达到实现或兼容实现标准，可以通过验收。

## 二、逐项验收矩阵

| 编号 | 验收项 | 结果 | 实现证据 |
|---|---|---|---|
| 1 | 每次请求创建唯一 task_id | 通过 | Job 与 Turn 共用 UUID；创建时写入持久队列和 Turn |
| 2 | 复杂任务生成结构化计划 | 通过 | `update_plan` 仅在多步目标中启用并产生结构化计划事件 |
| 3 | 每个步骤具有稳定 step_id | 通过 | 标题匹配、同形改写和新增步骤采用稳定 ID 协调算法 |
| 4 | 支持 pending 状态 | 通过 | 后端 schema、事件和前端渲染均支持 |
| 5 | 支持 in_progress 状态 | 通过 | 同时最多一个进行中步骤；前端显示旋转状态标识 |
| 6 | 支持 completed 状态 | 通过 | 产生 `step.completed`；前端显示勾选图标 |
| 7 | 支持 failed 状态 | 通过 | 产生 `step.failed`；前端使用失败样式 |
| 8 | 支持 skipped 状态 | 通过 | 产生 `step.skipped`；前端使用删除线和虚线状态标识 |
| 9 | 任务 queued 状态 | 通过 | 入队时写入 `task.queued` |
| 10 | 任务 planning 状态 | 通过 | Worker 领取任务时写入 `task.started(status=planning)` |
| 11 | 任务 executing 状态 | 通过 | 开始业务工具或 Agent Loop 执行时写入 `task.status` |
| 12 | 任务 waiting_approval 状态 | 通过 | 审批暂停时写入任务状态并实时推送审批事件 |
| 13 | 任务 finalizing 状态 | 通过 | 最终验证与答复整理前写入 `task.status` |
| 14 | completed / failed / cancelled 终态 | 通过 | 队列权威终态与 `task.*` 持久事件同步提交 |
| 15 | 计划允许动态增删、修改和重排 | 通过 | 计划不锁死长度；保留未变化步骤 ID，新步骤获得新 ID |
| 16 | 计划 revision 与修改原因 | 通过 | `plan.created/updated` 含 revision、reason、explanation |
| 17 | revision 冲突保护 | 通过 | 陈旧 revision 被拒绝，并要求读取最新计划后重试 |
| 18 | 结构化实时事件 | 通过 | 计划、步骤、任务、工具、审批、状态与消息增量均使用结构化事件 |
| 19 | 统一事件身份字段 | 通过 | 流事件包含 task_id、event_id、timestamp、revision |
| 20 | 不展示原始思维链 | 通过 | 前端只显示用户可见状态说明和公开审计事件 |
| 21 | 输入框上方任务进度胶囊 | 通过 | 运行中且已有计划时显示“第 n / N 步” |
| 22 | 鼠标悬停展开、移出收起 | 通过 | hover 由任务进度坞统一控制；连接层避免跨间隙闪退 |
| 23 | 触屏点击切换、点击外部收起 | 通过 | `(hover: none)` 下采用点击切换并监听外部 pointerdown |
| 24 | 完成后隐藏浮动胶囊 | 通过 | 仅 `work.status === running` 时显示浮动进度坞 |
| 25 | 消息历史保留计划和执行结果 | 通过 | 完成任务的 Process 从持久 Item 回放到助手消息 |
| 26 | 刷新和断线恢复 | 通过 | 活跃任务快照 + `after_revision` 游标补发 + event_id 去重 + 自动重连 |
| 27 | 计划更新复用原节点 | 通过 | 前端按 step_id 复用步骤及其活动记录 |
| 28 | 卡片不推动页面布局 | 通过 | 绝对定位于 Composer 上方，向上弹出 |
| 29 | 视觉、动画与无障碍 | 通过 | 340px 卡片、半透明背景、阴影、focus、ARIA、reduced-motion |
| 30 | 自动化验收 | 通过 | 单元测试覆盖动态计划、revision 冲突、事件信封、游标回放和 UI 标记；Playwright 覆盖真实交互 |

## 三、事件协议

所有实时事件使用以下公共信封：

```json
{
  "task_id": "turn/job uuid",
  "event_id": "持久 Item UUID 或确定性流事件 ID",
  "timestamp": "UTC ISO-8601",
  "revision": 12,
  "event_type": "plan.updated",
  "type": "runtime",
  "payload": {}
}
```

其中：

- 持久任务事件的 revision 使用 Turn Item sequence，可严格排序并用于断线游标。
- `assistant.message.delta` 使用已发送文本偏移作为 revision，断线后可从已持久化的 partial result 补发。
- 页面重连时携带 `after_revision`；服务端先建立实时订阅，再读取游标之后的持久事件，从而闭合快照与订阅之间的竞态窗口。
- 同一事件同时出现在持久补发和内存队列时，前端按 event_id 去重。

## 四、计划协议

首次成功调用产生 `plan.created`，后续调用产生 `plan.updated`：

```json
{
  "revision": 2,
  "reason": "根据验证结果补充兼容性检查",
  "explanation": "根据验证结果补充兼容性检查",
  "steps": [
    {"id": "step_1", "step": "完成主体实现", "status": "completed"},
    {"id": "step_2", "step": "验证兼容性", "status": "in_progress"},
    {"id": "step_3", "step": "整理交付结果", "status": "pending"}
  ]
}
```

更新方可以提交其看到的 revision。若与服务端当前 revision 不一致，运行时拒绝本次更新，不覆盖较新的计划。

## 五、验收方法

1. Python 全量测试：`python -m unittest discover -s tests -p "test_*.py"`，共 136 项，全部通过。
2. JavaScript 语法检查：`node --check frontend/static/app.js`。
3. Playwright 真实浏览器检查：
   - 运行任务默认只显示胶囊；
   - hover 胶囊后计划卡片展开；
   - 从胶囊移动到卡片时保持展开；
   - mouseleave 后收起；
   - 模拟触屏时点击胶囊切换、点击外部收起；
   - 动态计划状态与步骤总数正确呈现。

## 六、部署注意事项

本次改动没有新增数据库列，不需要新增迁移。任务过程继续存入现有 Turn / Item 追加式模型。部署时重启 API 与 Worker 进程即可加载新的任务状态和事件协议；旧任务使用的 `steps` 参数仍保留兼容入口。
