# Harness 智能体平台

一个 Codex 风格智能体平台：FastAPI App Server、持久化 Turn Worker、不可变 Harness
版本、动态 Agent Loop、Skills/MCP/知识库能力与多轮任务前端。

## 架构要点

- 智能体不绑定静态工作流，也没有流程搭建器。
- `Agent` 只保存身份、可见性和能力绑定。
- 指令与运行策略保存在不可变 `HarnessVersion`；发布和回滚只切换活动版本。
- 状态按 `Project → Thread → Turn → Item` 分层；消息、工具、审批、计划、验证和终态
  都是 Turn 内的追加式 Item。
- 附件是 Thread 内具有稳定 ID、所有者、来源 Turn、哈希和存储身份的任务资产；澄清
  回复通过 `continuation_of_turn_id` 延续附件和显式能力快照，原件会只读物化到每轮
  隔离工作区，前端依据后端附件/事件快照恢复，而不是依赖浏览器内存中的文件标签。
- 核心不是固定流水线或预制 DAG，而是 `推理 → 工具 → 观察 → 更新上下文 → 再推理`
  的动态 Agent Loop；计划只在任务需要时由模型临时生成。
- 默认小模型控制层执行单步决策、工具候选路由、Schema 参数修复、规则化失败分析和
  确定性完成验证；高能力模型可按版本切换为 `standard` 策略。
- 队列表只负责领取、租约、重试和取消，不再承担 Turn 语义；可审计过程统一写入 `Item`。
- 文本交互把“传输全双工”和“Agent 全双工”分层处理：HTTP/NDJSON 保持可靠控制与流式
  输出；运行中的纯文本先进入可编辑的对话引导，附件或能力上下文进入队列。暂存消息可以
  在引导与队列间转换，也可以显式设为当前目标：系统原子创建同 Thread 的后继 Turn，并
  协作式取消旧执行。旧 Turn、已发生的工具副作用和控制事件不会被覆盖或伪造回滚。
- 完成验证会根据本轮真实执行动态选择评测 Skill，生成版本化 Evidence Tree，分别记录
  输出契约、计划完成度、工具证据、交互控制和产物交付检查；公开事件只包含结构化证据摘要，
  不暴露模型私有推理或原始工具输出。
- 改进实验室只生成候选版本；回归评估通过并人工批准后才能发布。
- 内置业务算法是独立的确定性 Capability，不是画布节点。
- 内置工具目录提供工作区文件、受限 Shell、Git、代码诊断、浏览器、Word 检查与
  内容不变格式化、HTML/图片产物、
  免费联网搜索/网页读取、Cron 与异步子智能体生命周期；root 在设置中统一启停工具并按 Agent 分配，
  运行时只暴露“全局启用 ∩ Agent 已分配”的工具，再由版本策略做路由、预算和超时控制。
- 联网搜索优先调用自托管 SearXNG；未配置时聚合 DuckDuckGo 与 Bing RSS，
  使用倒数排名融合、URL 去重和五分钟缓存，无需 API Token。Tavily 仅作为可选付费增强。

完整图示见 [ARCHITECTURE_HARNESS_2026-07-29.md](ARCHITECTURE_HARNESS_2026-07-29.md)。

## 启动

```powershell
.\.venv\Scripts\python.exe run.py
```

默认地址：

- 对话：`http://127.0.0.1:8000/`
- 设置：`http://127.0.0.1:8000/admin`
- OpenAPI：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/healthz`

生产环境必须在 `.env` 中设置 `APP_ENV=production`，配置相互独立且不少于 32 字节的
`JWT_SECRET`、`SECRET_MASTER_KEY`，使用强 `ROOT_PASSWORD`，并启用
`AUTH_COOKIE_SECURE=true`。应用会在启动时执行硬校验，不满足条件即拒绝启动；
`/healthz` 只暴露脱敏后的部署风险提示，不返回密钥内容。

正式发布前必须执行失败闭合的发布预检、SQLite Online Backup API 备份及恢复演练；
发布、回滚和 `SECRET_MASTER_KEY_PREVIOUS`/rewrap 边界见
[RELEASE.md](RELEASE.md)。

## 个人微信消息渠道

平台已接入腾讯维护的 `@tencent-weixin/openclaw-weixin` 兼容协议。管理员先在
“设置 → 用户管理”中为用户启用“消息渠道”模块；用户随后进入“设置 → 消息渠道”，
选择一个智能体并点击“连接个人微信”，用微信扫描二维码并按页面提示确认即可。

- 每个平台用户最多绑定一个个人微信渠道；同一微信账号不能同时归属多个平台用户。
- 扫码得到的微信身份、访问令牌和同步游标只保存在服务端，令牌与游标使用平台密钥加密，
  不会返回浏览器。
- 入站消息只能以该渠道所有者的身份调用绑定的智能体；运行文件写入
  `data/workspaces/user_<用户ID>/agent_<智能体ID>/run_<运行ID>`，Thread、Turn、记忆、
  审计记录和文件均按用户隔离。
- 同一用户在微信中的连续消息复用稳定会话；更换绑定智能体后会进入新的智能体会话空间。
- 当前支持文本消息和微信提供的语音转写文本。扫码页要求安全验证码时，可在同一弹窗提交。
- 服务重启后会自动恢复已绑定渠道的长轮询；点“解除绑定”会立即停止该渠道并清除凭据。

实现遵循 OpenClaw 的 `accountId → agentId` 路由和 `per-account-channel-peer` 会话隔离思想，
但消息执行仍进入本项目原有的 Harness/Job Worker，而不是另起一套 OpenClaw 运行时。
上游参考：
[OpenClaw 微信渠道文档](https://docs.openclaw.ai/zh-CN/channels/wechat)、
[`@tencent-weixin/openclaw-weixin`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)。

## 目录

```text
backend/
  api/             HTTP 控制平面、对话与改进实验室
  runtime/         Agent Loop、Thread/Turn/Item 存储、策略、工具控制与记忆
  capabilities/    知识、附件、模板和确定性行业工具
  llm/             模型与 MCP 客户端
  harness.py       不可变版本注册表与发布/回滚
  jobs.py          Turn 的持久化调度队列、租约与重试
  worker.py        可独立部署的执行 Worker
  scheduler.py     持久 Cron 调度（到期后进入统一 Job Worker）
frontend/
  index.html       ChatGPT 式对话工作区
  admin.html       智能体、版本和改进实验室控制台
data/
  branding/        品牌 Logo 与站点图标
  knowledge/       本地知识库
  uploads/         临时上传件
  exports/         Artifact 导出件
tests/
```

## 核心接口

- `GET/POST/PATCH /api/v1/agents`
- `GET/POST /api/v1/agents/{id}/versions`
- `POST /api/v1/agents/{id}/versions/{version}/publish`
- `POST /api/v1/chat`（创建 Thread/Turn，返回执行标识）
- `GET /api/v1/chat/turns`（读取历史 Turn）
- `GET /api/v1/chat/attachments/{attachment_id}`（按所有者授权读取原附件）
- `PATCH /api/v1/chat/threads/{thread_id}`
- `GET /api/v1/chat/turns/{turn_id}/stream`
- `POST /api/v1/chat/turns/{turn_id}/cancel`
- `POST /api/v1/chat/turns/{turn_id}/guidance`（运行中补充当前 Turn）
- `POST /api/v1/chat/turns/{turn_id}/interrupt`（打断并创建同 Thread 后继 Turn）
- `GET /api/v1/capabilities`
- `GET/POST/PATCH/DELETE /api/v1/schedules`
- `GET/POST/PATCH/DELETE /api/v1/channels`
- `POST /api/v1/channels/{id}/login`（生成或刷新微信二维码）
- `GET /api/v1/channels/{id}/login`（读取扫码状态）
- `POST /api/v1/channels/{id}/pair-code`（提交扫码安全验证码）
- `POST /api/v1/channels/{id}/disconnect`（解除个人微信绑定）
- `GET /api/v1/improvement/turns`
- `GET/POST /api/v1/improvement/proposals`
- `POST /api/v1/improvement/proposals/{id}/evaluation`
- `POST /api/v1/improvement/proposals/{id}/approve`

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

## 启用完整能力

新部署可运行一次幂等激活脚本，为默认智能体挂载工作区工程 Skill、只读代码审查
子智能体，并发布适合大工具目录的小模型 Harness 策略：

```powershell
.\.venv\Scripts\python.exe tools\activate_builtin_capabilities.py
```

文件和命令始终限制在 `AGENT_WORKSPACE_ROOT`（默认 `data/workspaces` 隔离目录）。Shell 只接受单条
白名单命令，拒绝管道、重定向、命令替换、父目录和绝对路径；Git 提交必须明确列出
暂存路径，且不会自动推送远程。
