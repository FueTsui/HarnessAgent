# HarnessAgent 使用指南

本指南整理对话工作区、能力接入、权限与接口的详细行为。安装与快速开始见 [README](../README.md)，桌面版的数据目录和升级操作见 [Windows 本地版说明](local-agent-windows.md)。

## 目录

- [对话与任务](#对话与任务)
- [智能体与模型](#智能体与模型)
- [工具与联网检索](#工具与联网检索)
- [MCP 与自定义服务](#mcp-与自定义服务)
- [记忆与知识库](#记忆与知识库)
- [权限、护栏与审批](#权限护栏与审批)
- [个人微信消息渠道](#个人微信消息渠道)
- [部署与配置](#部署与配置)
- [HTTP API 参考](#http-api-参考)
- [开发验证](#开发验证)

## 对话与任务

工作区提供完整侧栏与窄栏，点击左下角头像打开账户菜单和后台设置。问答工作区需要登录；未登录访问首页会跳转登录页。个人偏好、项目背景和对话记忆集中在后台，普通账户可管理自己的设置，平台模块按权限开放。

### 消息引导与排队

运行中的纯文本可补充为当前任务的对话引导；带附件或能力上下文的消息进入队列。暂存消息可编辑，也可在引导和队列之间转换。

将消息显式设为当前目标时，系统在同一 Thread 内创建后继 Turn，并协作式取消旧执行。旧 Turn、已经发生的工具操作和控制事件会保留；取消不会撤销外部操作。HTTP/NDJSON 承担控制与流式输出，不能据此推断任意工具都能立即中断。

计划由模型按任务需要生成，运行过程记录消息、工具、审批、计划、验证和终态。实时流、断线重连和历史回放使用同一份公开事件投影，界面提供运行阶段、内联审批和滚动跟随控制。

### 附件与产物

附件是 Thread 内的任务资产，具有稳定 ID、所有者、来源 Turn、哈希和存储身份。澄清回复通过 `continuation_of_turn_id` 延续附件和显式能力快照；原件以只读方式物化到各轮隔离工作区。页面根据后端附件与事件快照恢复状态。

附件读取继续校验归属；产物通过任务内的下载入口交付。任务完成验证结合实际执行选择评测 Skill，以版本化证据树检查输出契约、计划完成度、工具证据、交互控制和产物。公开事件只包含结构化摘要，不包含模型私有推理或原始工具输出。

### 检查点与恢复

运行时记录加密检查点和工具调用账本，支持参数绑定审批、授权范围内的只读并发、Skill 按需读取、引导目标修订和有界工具修复。恢复可能复用已保存的结果；结果不确定的外部操作会阻止自动重放。

完整语义、升级与回退边界见 [运行时升级说明](execution-runtime-upgrade.md)。

## 智能体与模型

`Agent` 保存身份、可见性和能力绑定；指令和运行策略保存在不可变 `HarnessVersion`。发布、回滚通过切换活动版本完成。

执行采用“推理 → 工具 → 观察 → 更新上下文 → 再推理”的动态循环。默认小模型控制策略覆盖单步决策、工具候选路由、Schema 参数修复、规则化失败分析和确定性完成验证；高能力模型可按版本使用 `standard` 策略。业务算法作为独立的确定性 Capability 提供。

在智能体“模型与路由”中配置执行、规划、路由和验证模型；未指定的阶段继承执行模型。具体触发条件、继承方式与连接协议限制见 [模型职责与路由](agent-model-routing.md)。

改进实验室先生成候选版本，经回归评估与人工批准后才发布。任务已入队时使用其冻结的执行快照，后续配置修改不等于修改正在执行的任务。

后台以“个人、创建、优化、管理”组织功能，通过顶部子导航切换具体模块，并保留模块深链和类别内最近页面。例如 `/admin#guardrails` 可直达护栏模块。

## 工具与联网检索

内置目录包含工作区文件、受限 Shell、Git、代码诊断、浏览器、Word 检查与内容不变格式化、HTML/图片产物、联网搜索/网页读取、Cron 和异步子智能体生命周期。

`root` 统一启停工具并按 Agent 分配。运行时可见目录是“全局启用 ∩ Agent 已分配”，版本策略再控制路由、预算和超时；模型选择工具不会扩大实际分派权限。

所有文件与命令限制在 `AGENT_WORKSPACE_ROOT` 下，默认按用户和运行隔离。Shell 默认关闭；启用后只接受单条白名单命令，拒绝管道、重定向、命令替换及越界路径。Git 提交必须明确列出暂存路径，工具不会自动推送远程。

联网搜索优先使用自托管 SearXNG；未配置或不可用时，可聚合 DuckDuckGo 与 Bing RSS，通过倒数排名融合、URL 去重和五分钟缓存返回结果，无需搜索 API Token。Tavily 是可选付费增强。

新部署可选择运行幂等激活脚本，为默认智能体挂载工作区工程 Skill、只读代码审查子智能体，并发布适合大工具目录的小模型 Harness 策略。**该操作会修改平台配置和活动版本**，在确认目标数据目录和授权后执行：

```powershell
.\.venv\Scripts\python.exe tools\activate_builtin_capabilities.py
```

## MCP 与自定义服务

### MCP 连接

支持 Streamable HTTP、SSE 和 stdio，可在工具页导入标准 `mcpServers` JSON。平台是 MCP 客户端，具体工具及输入输出由接入服务提供。

stdio 的配置字段为 `command`、`args`（字符串数组）、`env` 和 `cwd`。本地进程由 `root` 注册、授权；普通用户使用已共享的连接。程序直接启动可执行文件，不经过 Shell；Windows 隐藏进程窗口，并在超时或取消后清理进程树。

子进程只继承基础系统环境白名单；保存的凭据加密存储，返回浏览器时脱敏。连接器本身需要的程序和依赖由部署方准备。

### 自定义网络与编程服务

通过 `/api/v1/services` 注册固定 HTTP(S) 接口或 `root` 管理的本地程序。编程服务从 stdin 读取一行 JSON，在 stdout 返回一个 JSON 结果；可定义输入字段、超时、可见性及智能体绑定。

智能体还需分配 `service_list`、`service_call` 工具才可调用。服务调用复用内容护栏和精确服务审批范围；操场测试需确认执行，强制审批转到任务中完成。

服务模块提供“操场、自定义、数据”视图。“数据”页只显示当前账户最近 100 次执行；响应加密保存，不保存原始请求输入。

## 记忆与知识库

记忆分为用户自定义（绑定智能体）、上下文（绑定会话）、项目和全局四种范围，支持搜索、启停、编辑和删除，通过 `/api/v1/memories` 管理。“全局”指当前账户跨项目使用，不是跨用户共享。

实际召回按会话归属和项目绑定筛选，并遵守智能体/会话记忆开关、排除规则及上下文预算。知识库在后台按需展开文档；Windows 全新数据目录不预置知识库。

## 权限、护栏与审批

### 账户与会话

工作区必须登录。浏览器使用 HttpOnly、SameSite=Strict 的 Session Cookie，数据库保存随机会话凭证的哈希。Cookie 不设置 `Max-Age` / `Expires`，服务器默认 12 小时到期；浏览器会话恢复可能保留 Cookie，主动退出与服务器到期才是明确的失效边界。

退出撤销当前浏览器会话；改密、禁用账号会撤销既有身份授权。历史访客入口、个人模型接口页面及其 API 已移除，模型由有权限的账户在后台管理。模块授权覆盖服务、工具、记忆、护栏等现有模块。

### 内容策略与全局工具规则

护栏通过“添加控件 → 选择智能体和模型 → 审查”创建命名策略，可复用阻止列表、手动术语/受限正则和 CSV 导入。

当前检测器包括术语匹配、规则式提示注入检测，以及邮箱、电话、身份证格式检测。语义内容安全、版权/根基性等未接入的检测器在界面标为不可用。

策略分别作用于用户输入、工具输入、工具输出和模型输出，支持阻止或警告。模型输出规则先检查缓冲响应，再向用户输出；拦截不触发模型故障切换。普通账号的策略只作用于自己的执行，`root` 可设置跨账户策略。

策略与阻止列表接口为 `/api/v1/guardrails/policies`、`/api/v1/guardrails/blocklists`，更新使用 revision 防止并发覆盖。`root` 的全局工具规则位于护栏“集成”页，对应 `GET/PUT /api/v1/guardrails` 与 `POST /api/v1/guardrails/preview`，可配置写操作拦截、强制审批、工具禁用和参数上限。

保存后的规则对后续分派生效，不会撤销已发生的操作，也不会放宽资源权限、工作区或任务审批策略。

### 管理员审批

后台任务命中可审批的内容或工具护栏时，当前检查暂停。聊天页和管理页每 5 秒刷新待审批提醒，`root` 及具有护栏模块权限的有效管理员可处理。

批准只放行当前检查，不提供可复用凭证，也不替代原有工具、资源与执行边界检查。拒绝、取消或等待超过 5 分钟后继续拦截；配置故障和扫描/参数容量限制不能审批豁免。策略预览或没有后台任务上下文的调用不进入此审批流程。

审批记录保留用户、任务、命中规则、处理人和时间，不保存触发内容原文。

## 个人微信消息渠道

当前实现以腾讯维护的 `@tencent-weixin/openclaw-weixin` 2.4.9 协议为兼容基线。

管理员先在“设置 → 用户管理”为用户启用“消息渠道”模块。用户随后进入“设置 → 消息渠道”，选择智能体并点击“连接个人微信”，扫描二维码并确认；若要求安全验证码，在同一弹窗提交。

- 每个平台用户最多绑定一个个人微信渠道；同一微信账号不能归属多个平台用户。
- 微信身份、访问令牌与同步游标只保存在服务端，令牌和游标加密保存，不返回浏览器。
- 入站消息以渠道所有者身份调用绑定的智能体；Thread、Turn、记忆、审计和文件按用户隔离。
- 连续消息复用稳定会话，更换智能体后进入新的智能体会话空间。
- 支持文本、微信提供的语音转写文本，以及旧格式、消息 ID 和局部文本引用；引用缓存按绑定与发送者隔离并加密保存。
- 重启后恢复已绑定渠道的长轮询；解除绑定立即停止渠道并清除凭据。

渠道执行复用本项目的 Harness / Job Worker，采用 `accountId → agentId` 路由和 `per-account-channel-peer` 会话隔离思路。运行文件位于数据根目录的 `workspaces/user_<用户ID>/agent_<智能体ID>/run_<运行ID>`。

上游参考：[OpenClaw 微信渠道文档](https://docs.openclaw.ai/zh-CN/channels/wechat)、[`@tencent-weixin/openclaw-weixin`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)。

## 部署与配置

### 启动入口

| 入口 | 用途 | 关键参数与行为 |
| --- | --- | --- |
| `HarnessAgent.exe` | 独立桌面工作区 | 可用 `--home` 指定独立数据目录 |
| `python local_agent.py serve` | 回环本地服务 | `--home`、`--port`、`--status-file`、`--managed` |
| `python run.py` | 已配置的源码 API 服务 | 使用部署环境；默认含进程内 Worker |
| `python run.py worker` | 独立任务 Worker | 与 API 使用相同的数据库和配置 |

`local_agent.py` 的 `--port` 默认为 `17650`，从起始端口寻找可用值，`0` 使用系统分配端口。`--status-file` 必须位于所选数据目录内。启动成功输出就绪地址，状态文件不含密钥。

本地服务可用 `Ctrl+C` 停止，托管程序可向 stdin 写入 `stop` 加换行。`--managed` 在 stdin 关闭时请求停止。本地启动器正常停止返回 `0`，启动失败返回 `1`，参数错误返回 `2`；它不提供任务 `--json` 调用协议。启动就绪等待上限为 90 秒，实际工具与模型超时由各自配置控制。

已准备好部署环境时，API 和独立 Worker 的启动命令分别为：

```powershell
.\.venv\Scripts\python.exe run.py
.\.venv\Scripts\python.exe run.py worker
```

使用独立 Worker 时，应在 API 进程配置 `JOB_WORKER_ENABLED=false`；SQLite 仅用于单机部署。不要让开发实例连接生产数据库。

### 配置与凭据

`run.py` 从环境变量和配置文件加载设置，默认配置文件位于项目根目录 `.env`，可由 `APP_ENV_FILE` 指定其他文件。`local_agent.py` 使用自己的用户目录，并固定其数据库、工作区与回环监听，不读取项目根目录的部署配置。

| 变量 | 用途与默认行为 |
| --- | --- |
| `APP_ENV` | `development`、`test` 或 `production` |
| `APP_HOST` / `APP_PORT` | `run.py` 默认为 `0.0.0.0:8000`；本地调试应显式绑定 `127.0.0.1` |
| `APP_ENV_FILE` | 选择配置文件 |
| `APP_DATA_DIR` | 默认 `data`，存放数据库、上传件和产物等 |
| `AGENT_WORKSPACE_ROOT` | 默认位于数据目录的 `workspaces`，应与源码、配置和数据库隔离 |
| `DATABASE_URL` | 默认使用数据目录中的 SQLite 数据库 |
| `JWT_SECRET` | 身份认证密钥，部署时使用非占位的随机值 |
| `SECRET_MASTER_KEY` | 已保存凭据与私有运行状态的加密主密钥 |
| `ROOT_USERNAME` / `ROOT_PASSWORD` | 首次创建管理员账号，不用于重置已有账号密码 |
| `AUTH_COOKIE_SECURE` | 生产 HTTPS 部署必须启用 |
| `JOB_WORKER_ENABLED` | 是否在 API 进程中运行 Worker，默认开启 |
| `SHELL_TOOL_ENABLED` | 默认关闭；启用前需准备受限执行环境 |
| `SEARXNG_URL` / `WEB_SEARCH_FALLBACK` | 联网检索服务与降级策略 |

模型、MCP 和服务凭据通过有权限的后台入口配置。仓库不包含 1Password 引用映射；部署方使用 1Password 时，维护自己的 `op://…` 引用并注入进程，避免将密钥放入命令参数、版本控制、截图或日志。

生产启动要求 `APP_ENV=production`，`JWT_SECRET` 和 `SECRET_MASTER_KEY` 相互独立且各不少于 32 字节，使用非默认的强管理员密码，并启用 `AUTH_COOKIE_SECURE=true`。启动检查不满足要求会拒绝启动；`/healthz` 仅返回脱敏的部署风险提示。

### 迁移、备份与升级

启动会应用数据库迁移。已有部署更新前先备份并演练恢复，核对实际源版本和当前迁移 head；不要将旧文档中的示例 revision 当作当前数据库状态。

会话与访客迁移会停用历史访客、撤销其会话和个人模型配置，保留历史数据供管理员清理。升级前的浏览器 JWT Cookie 不再作为登录会话，需要重新登录；API Key 接口继续按其权限使用。

桌面升级须备份整个用户目录，包括加密配置。在线 SQLite 备份使用 Online Backup API；不要在 WAL 活跃时直接复制数据库。正式发布执行失败闭合的预检、恢复演练和实际用户路径验证，步骤见 [发布、迁移与回滚](../RELEASE.md) 与 [运行时升级说明](execution-runtime-upgrade.md)。

## HTTP API 参考

### 地址与认证

桌面与本地启动器使用其显示的实际端口；`run.py` 的默认端口为 `8000`。在同一基址下：

| 路径 | 用途 |
| --- | --- |
| `/` | 对话工作区 |
| `/admin` | 后台设置 |
| `/docs` | 交互式 OpenAPI 文档 |
| `/openapi.json` | 当前应用的 OpenAPI Schema |
| `/healthz` | 脱敏健康状态 |

Web 工作区登录后使用会话 Cookie。第三方集成使用 `/open/v1/*` 和 `X-API-Key` 请求头，凭据从调用方的安全存储注入；请勿将其写入 URL。API Key 需要有效且所属账号已启用，只能访问该账号获准使用的资源。

### 常用接口

下表中的 `{id}` 表示该接口对应的资源标识，实际参数名与请求 Schema 以 `/docs` 为准。

| 方法与路径 | 用途 | 副作用 |
| --- | --- | --- |
| `GET/POST/PATCH /api/v1/agents` | 列出、创建、更新智能体；更新路径追加 `/{id}` | 只读或本地写入 |
| `GET/POST /api/v1/agents/{id}/versions` | 查询或创建 Harness 版本 | 只读或本地写入 |
| `POST /api/v1/agents/{id}/versions/{version}/publish` | 切换活动版本 | 本地写入 |
| `POST /api/v1/chat` | 创建 Thread / Turn，返回执行标识 | 本地写入、外联，可能计费 |
| `GET /api/v1/chat/turns` | 读取历史 Turn | 只读 |
| `GET /api/v1/chat/turns/{id}` | 读取单轮状态 | 只读 |
| `GET /api/v1/chat/turns/{id}/stream` | 读取实时或回放事件 | 只读 |
| `GET /api/v1/chat/attachments/{id}` | 按归属读取原附件 | 只读 |
| `PATCH /api/v1/chat/threads/{id}` | 修改会话信息 | 本地写入 |
| `POST /api/v1/chat/turns/{id}/cancel` | 请求取消当前执行 | 本地写入 |
| `POST /api/v1/chat/turns/{id}/guidance` | 运行中补充要求 | 本地写入，影响后续执行 |
| `POST /api/v1/chat/turns/{id}/interrupt` | 打断并创建后继 Turn | 本地写入、外联，可能计费 |
| `GET /api/v1/capabilities` | 读取能力目录 | 只读 |
| `GET/POST/PATCH/DELETE /api/v1/schedules` | 管理定时任务；修改、删除路径追加 `/{id}` | 只读、本地写入或删除；到期执行可能外联 |
| `GET/POST/PATCH/DELETE /api/v1/channels` | 管理消息渠道；修改、删除路径追加 `/{id}` | 只读、本地写入或删除 |
| `POST/GET /api/v1/channels/{id}/login` | 生成二维码或读取扫码状态 | 外联或只读 |
| `POST /api/v1/channels/{id}/pair-code` | 提交安全验证码 | 外联 |
| `POST /api/v1/channels/{id}/disconnect` | 解绑并清除渠道凭据 | 本地删除 |
| `GET /api/v1/improvement/turns` | 读取可评估运行记录 | 只读 |
| `GET/POST /api/v1/improvement/proposals` | 查询或创建候选提案 | 只读或本地写入 |
| `POST /api/v1/improvement/proposals/{id}/evaluation` | 执行评估 | 本地写入、外联，可能计费 |
| `POST /api/v1/improvement/proposals/{id}/approve` | 批准候选版本 | 本地写入 |

`POST /api/v1/chat` 使用表单/附件输入；第三方纯 JSON 调用使用下面的开放 API。

### 开放 API 任务调用

| 方法与路径 | 用途 |
| --- | --- |
| `GET /open/v1/agents` | 列出当前 Key 所属账号可访问的启用智能体 |
| `POST /open/v1/chat` | 提交纯 JSON 文本任务 |
| `POST /open/v1/chat/multipart` | 提交含文件的任务 |
| `GET /open/v1/chat/turns/{id}` | 按任务 ID 查询执行状态和结果 |

JSON 请求的最小业务内容示例：

```json
{
  "query": "整理一份项目计划"
}
```

可选 `agent_id` 指定智能体；省略时由服务按当前账号解析默认智能体。POST 任务可传 `Idempotency-Key` 请求头，同一 API Key 下使用稳定的业务标识，长度不超过 128 字符。

调用默认最多同步等待 60 秒；未结束时仍返回 `turn_id` 与当前 `status`，调用方再轮询。结果字段包括 `answer`、`export_files`、`thread_id`、`task_status`、`completion_status`、`completion_issues` 和 `plan_summary`。示例结构如下，字段值仅作说明：

```json
{
  "turn_id": "task-id",
  "status": "done",
  "task_status": "completed",
  "completion_status": "completed",
  "completion_issues": [],
  "plan_summary": {},
  "answer": "任务结果",
  "export_files": [],
  "thread_id": "thread-id"
}
```

`completed_with_issues` 表示仍有已记录的问题；不能只依据 HTTP 200 判断业务任务全部完成。

### 限流与错误

默认登录限制为每个 IP/用户名组合每 300 秒 10 次，并另有 IP 总量限制；聊天每用户每 60 秒 30 次；开放 API 每 Key 每 60 秒 60 次，并对未通过认证的 IP 限流。部署可调整 `LOGIN_RATE_*`、`CHAT_RATE_*`、`OPEN_API_RATE_*`；返回 429 时按服务提示稍后重试。

应用通常以 FastAPI 的 `{"detail": "错误说明"}` 返回错误，字段校验错误的 `detail` 可能为数组；当前未声明统一的 RFC 9457 错误协议。常见状态码为 401（未认证）、403（无权限）、404（资源不可见或不存在）、422（参数校验失败）、429（限流），开放 API 等待到任务失败时可返回 502。

## 开发验证

先安装 `requirements.lock` 中的锁定依赖，并使用独立数据目录、测试凭据和端口。配置文件选择器也应指向隔离位置；不要使用真实用户目录或部署数据库运行回归。

测试环境和迁移初始化的现有配置见 [security-regression 工作流](https://github.com/FueTsui/HarnessAgent/blob/main/.github/workflows/security-regression.yml)。完成隔离环境准备后执行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.\.venv\Scripts\python.exe tools\check_javascript.py
```

构建、单元测试、浏览器冒烟、真实模型/MCP/微信链路及生产发布分别验证。跳过或未运行的检查应单独记录，不能计作通过。
