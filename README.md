# HarnessAgent

[下载 Windows 桌面版](https://github.com/FueTsui/HarnessAgent/releases/latest) ·
[使用与构建说明](docs/local-agent-windows.md)

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

依据《智能体与工具编排》的本轮职责划分、工具契约和交互设计见
[结构化编排设计](设计/结构化编排设计.md)。授权工具目录同时约束模型可见能力与实际分派；
实时和历史事件共用公开投影，工作区提供运行阶段、内联审批及可控的滚动跟随。

架构设计见 [按需智能体架构与工具编排方案](设计/按需智能体架构与工具编排方案.md)。
执行层已实现加密检查点与调用账本、参数绑定审批、只读并发、Skill 按需读取、
引导目标修订和有界工具修复；恢复、迁移和验收边界见 [运行时升级说明](docs/execution-runtime-upgrade.md)。

## 启动

Windows 独立桌面版：解压 `HarnessAgent-Desktop` 发布包后双击 `HarnessAgent.exe`，
应用窗口内直接使用项目、任务、对话和设置，Chromium、Python 运行时与依赖均已内置。
首次登录、独立数据目录和构建方法见 [本地程序说明](docs/local-agent-windows.md)。

对话工作区提供完整侧栏与 64px 窄栏，点击左下角头像打开账户菜单和后台设置。
问答工作区必须登录后使用，未登录访问首页会跳转登录页；旧访客 Cookie 和令牌不再有效。
访客账号入口、个人模型接口页面及其 API 已移除，模型由有权限的账户在后台统一管理。
升级迁移停用历史访客、撤销其会话并停用访客及个人模型配置，保留历史数据供管理员清理。
浏览器登录使用 HttpOnly、SameSite=Strict 的 Session Cookie，数据库仅保存随机会话凭证的哈希；
Cookie 不设置 Max-Age/Expires，服务器默认 12 小时失效。退出撤销当前浏览器会话，改密或禁用撤销既有身份授权。
浏览器会话恢复可能保留会话 Cookie，因此服务器到期与主动退出才是明确的失效边界。
个人偏好、项目背景和对话记忆集中到后台；普通账户可管理自己的设置，平台模块仍按权限开放。
后台左侧仅保留“个人、创建、优化、管理”四个类别，原有功能在顶部子导航切换；深链与类别内最近页面保持可用。
智能体的“模型与路由”可分别配置执行、路由、规划和验证模型，未指定的阶段沿用执行模型。
配置合同、实际触发条件与协议限制见 [模型职责与路由](docs/agent-model-routing.md)。
首次启动会应用 `0026_browser_sessions` 数据库迁移，新增服务端会话表；更新现有部署前请备份数据库。
升级前的浏览器 JWT Cookie 不再用于登录，需要重新登录；已有 API Key 接口保持可用。

```powershell
.\.venv\Scripts\python.exe run.py
```

默认地址：

- 对话：`http://127.0.0.1:8000/`
- 设置：`http://127.0.0.1:8000/admin`
- OpenAPI：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/healthz`

后台设置按“创建、优化、管理”分组，提供模块搜索和状态筛选：模型展示连接与健康，
服务提供“操场、自定义、数据”视图；工具统一收纳内置工具、MCP、Skill、自定义网络
服务与编程服务。知识库按需展开文档，用户、密钥和定时任务使用表格，评估支持从运行记录发起提案。
后台适配窄屏抽屉导航，并可通过 `/admin#guardrails` 等地址直接打开对应模块。

护栏通过“添加控件 → 选择智能体和模型 → 审查”向导创建命名策略，支持复用阻止列表、
手动术语/受限正则及 CSV 导入。当前检测器包括术语匹配、规则式提示注入检测与邮箱、
电话、身份证格式检测；语义内容安全、版权/根基性等未接入检测器的项目在界面中标为不可用。
配置明确区分用户输入、工具输入、工具输出和模型输出，并支持阻止或警告。模型输出规则
会先检查缓冲的响应再对用户输出；拦截不会触发模型故障切换。普通账号的策略只应用于自己的
执行，root 可设置跨账户策略。策略与阻止列表位于 `/api/v1/guardrails/policies` 和
`/api/v1/guardrails/blocklists`，更新使用 revision 防止并发覆盖。

root 的全局工具规则位于护栏“集成”页，保留 `GET/PUT /api/v1/guardrails` 与
`POST /api/v1/guardrails/preview`；可配置写操作拦截、强制审批、工具禁用清单和参数上限。
规则保存后对后续分派生效，不撤销已发生的操作，也不会放宽资源权限、沙盒或任务审批策略。

MCP 支持 Streamable HTTP、SSE 和 stdio。本地进程由 root 注册并授权，普通用户只能使用
已共享的连接。stdio 字段为 `command`、`args`（字符串数组）、`env` 和 `cwd`；直接启动
可执行程序，不经过 Shell，Windows 隐藏窗口并在超时/取消后清理进程树。环境只继承基础
系统白名单，凭据加密存储、返回时脱敏。标准 `mcpServers` JSON 可从工具页导入。

自定义服务通过 `/api/v1/services` 注册固定 HTTP(S) 接口或 root 管理的本地程序。编程服务
从 stdin 读取一行 JSON，在 stdout 返回一个 JSON 结果。可定义输入字段、超时、可见性和
现有智能体绑定；智能体还需获分配 `service_list`、`service_call` 内置工具才能调用。
服务调用复用内容护栏与精确服务审批范围；操场测试需确认执行，强制审批须转到任务中完成。
“数据”页仅显示当前账户最近 100 次执行，响应加密保存，不保存原始请求输入。

记忆支持用户自定义（绑定智能体）、上下文（绑定会话）、项目和全局四种范围，具备搜索、
启停、编辑与删除，使用 `/api/v1/memories` 管理。全局指当前账户跨项目使用，并非跨用户共享。
实际召回按会话归属和项目绑定筛选，并继续遵守智能体/会话的记忆开关、排除规则和上下文预算。
成员与权限中的可选模块使用服务器现有模块目录，新增服务、工具总览、记忆和护栏可独立授权。
重启应用后会自动升级到 `0023_service_registry`，保留现有 MCP、智能体和对话数据。

生产环境必须在 `.env` 中设置 `APP_ENV=production`，配置相互独立且不少于 32 字节的
`JWT_SECRET`、`SECRET_MASTER_KEY`，使用强 `ROOT_PASSWORD`，并启用
`AUTH_COOKIE_SECURE=true`。应用会在启动时执行硬校验，不满足条件即拒绝启动；
`/healthz` 只暴露脱敏后的部署风险提示，不返回密钥内容。

正式发布前必须执行失败闭合的发布预检、SQLite Online Backup API 备份及恢复演练；
发布、回滚和 `SECRET_MASTER_KEY_PREVIOUS`/rewrap 边界见
[RELEASE.md](RELEASE.md)。

## 个人微信消息渠道

平台已按腾讯维护的 `@tencent-weixin/openclaw-weixin` **2.4.9** 核对并升级兼容协议。管理员先在
“设置 → 用户管理”中为用户启用“消息渠道”模块；用户随后进入“设置 → 消息渠道”，
选择一个智能体并点击“连接个人微信”，用微信扫描二维码并按页面提示确认即可。

- 每个平台用户最多绑定一个个人微信渠道；同一微信账号不能同时归属多个平台用户。
- 扫码得到的微信身份、访问令牌和同步游标只保存在服务端，令牌与游标使用平台密钥加密，
  不会返回浏览器。
- 入站消息只能以该渠道所有者的身份调用绑定的智能体；运行文件写入
  `data/workspaces/user_<用户ID>/agent_<智能体ID>/run_<运行ID>`，Thread、Turn、记忆、
  审计记录和文件均按用户隔离。
- 同一用户在微信中的连续消息复用稳定会话；更换绑定智能体后会进入新的智能体会话空间。
- 当前支持文本消息和微信提供的语音转写文本，以及旧格式、消息 ID 和局部文本引用；
  引用缓存按绑定与发送者隔离并加密保存。扫码页要求安全验证码时，可在同一弹窗提交。
- 服务重启后会自动恢复已绑定渠道的长轮询；点“解除绑定”会立即停止该渠道并清除凭据。

实现遵循 OpenClaw 的 `accountId → agentId` 路由和 `per-account-channel-peer` 会话隔离思想，
但消息执行仍进入本项目原有的 Harness/Job Worker，而不是另起一套 OpenClaw 运行时。
升级后的微信轮次使用包含绑定身份的新会话标识，旧历史保留可查；媒体下载能力保持原有范围。
版本来源、缓存策略、迁移与测试边界见[微信 2.4.9 协议升级记录](docs/weixin-compatibility-20260921.md)。
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

### 护栏管理员审批

用户后台任务命中可审批的内容或工具护栏后，当前检查暂停，聊天页与管理页向 root 及具有 guardrails 模块权限的有效管理员显示站内待审批提醒（每 5 秒刷新）。审批仅放行当前检查，不提供可复用凭证，也不替代原有工具、资源与沙盒权限检查。拒绝、取消或等待超过 5 分钟后继续拦截；配置故障和扫描/参数容量限制不能审批豁免。策略预览及无后台任务上下文的调用保持原有行为。

审批记录保留用户、任务、命中规则、处理人和时间，不保存触发内容原文。服务启动时自动迁移至 0024_guardrail_reviews；更新代码后需重启服务。
