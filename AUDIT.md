# 系统设计审计报告 · 绿碳能源投运智能体平台

> 审计视角：一名高级智能体工程师对当前实现做端到端评审。
> 审计范围：`backend/`（FastAPI 单体 + SQLite + 文件知识库 + MCP/LLM 客户端 + 子进程步骤执行器）、`frontend/`（原生 SPA）、权限与多租户模型、运行与扩展性。
> 结论摘要：**架构清晰、职责分明、确定性测算口径被严格保留，多租户归属模型设计良好。** 主要风险集中在 **服务端外联（SSRF）、子进程执行沙箱强度、密钥/口令默认值、长任务阻塞与水平扩展、文件型元数据并发** 五处。下文按严重度给出可执行建议。

---

## 1. 架构概览

| 层 | 实现 | 说明 |
|----|------|------|
| API | FastAPI 单体，路由按域拆分（auth/users/agents/providers/flows/mcp/skills/chat/open_api） | 同步依赖注入；`require_module` + `scope_owned`/`require_owner`/`require_use` 构成权限层 |
| 持久化 | SQLAlchemy 2.0 + SQLite（`check_same_thread=False`） | JSON-in-TEXT 存列表/配置（mcp_ids、skill_ids、routing、resources、permissions…） |
| 知识库 | `data/knowledge/<key>/` 文件夹 + `_datasets.json` 元数据 | bigram + 词重叠打分检索；数据集含归属/可见性 |
| LLM | OpenAI 兼容 `httpx` 客户端（文本/视觉/工具调用） | 多提供商；无视觉则回退本地视觉模型；重试 3 次 |
| MCP | 最小 JSON-RPC 客户端（Streamable HTTP / SSE） | `initialize → tools/list → tools/call`；工具以 function-calling 暴露 |
| 步骤执行 | `python -I -X utf8 -c` 子进程，30s 超时 | 公式（零代码生成）/ Python / AI 提示词三类步骤 |
| 多租户 | `created_by` + `is_public` + `can_manage` | root 全量；admin 拥有自建 + 可用公开；按模块授权（`require_module`） |
| 前端 | 原生 HTML/CSS/JS（无构建），JWT 存 localStorage | 侧栏式管理后台 + 动态表单对话页 |

**设计亮点（值得保留）**
- 确定性 Python 计算节点优先于 LLM 叙述值的不变量被一致维护；公式步骤自动生成并 `compile()` 校验。
- 归属模型边界清晰：`created_by is None` 的内置资源按 root 管理，避免被任意 admin 接管。
- 技能采用「指令注入 + 资源文件渐进式披露（`read_skill_resource` 工具）」，对齐 Agent Skills 思路。
- 单步失败仅在报告标注、不阻断流水线；视觉分支无图静默；anti-`<think>` 清洗三处布防。
- 停止问答用 `request.is_disconnected()` 真正取消服务端任务并不落库。

---

## 2. 发现与建议（按严重度）

### 🔴 高危

**H1. 服务端外联 SSRF（MCP / 模型提供商 URL）　✅ 已修复**
`mcp_client.list_tools/call_tool`、`providers` 的 `ping`/对话会以服务端身份请求 **管理员填写的任意 URL**（含内网与云元数据端点 `169.254.169.254`）。在多 admin 场景下 admin 仅为半可信，存在 SSRF 与内网探测面。
- 建议：解析目标 host，拒绝环回/链路本地/私网网段（或改为显式白名单）；对 `import-codex` 之外的外联统一走校验函数。
- **修复**：新增 `backend/net_guard.py::validate_outbound_url`，在 MCP（list_tools/call_tool）与管理员提供商客户端（`LLMClient(enforce_ssrf=True)` 的 `_post`/`ping`）请求前解析 DNS 并拒绝 环回/私网/链路本地/保留 网段；`.env` 默认本地模型（操作者可信）不校验。可经 `SSRF_ALLOW_PRIVATE=true` 或 `SSRF_ALLOWLIST`（host/CIDR）放行。残留风险：DNS rebinding 仅尽力而为，强隔离仍建议在网络层做出站策略。

**H2. 子进程步骤执行器沙箱强度不足　✅ 已修复**
`-I` 隔离了环境变量与 `site`，30s 超时可挡死循环，但**未限制内存/CPU/文件系统/网络**。恶意或失误的 Python/公式步骤可读写磁盘、发起外联、fork 轰炸（超时前可能已耗尽资源）。当前实质信任边界是「admin = 可信代码作者」。
- 建议：补充 OS 级 rlimits、禁网（命名空间/seccomp/`nsjail` 或容器），或显式声明信任边界并限制 Python 模式。
- **修复**：新增 `backend/pipeline/sandbox_runner.py`，步骤代码在其中执行并叠加三层防护——
  ①**审计钩子**（`sys.addaudithook`，安装后不可移除，跨平台）拦截 网络/子进程/ctypes/写文件/注册表 等高危事件；
  ②**资源限制**（POSIX `resource`：CPU 30s、地址空间 512MB、写文件上限；Windows 无 `resource` 时依赖审计钩子 + 墙钟超时）；
  ③**最小化环境**（`_sandbox_env` 仅保留 `SYSTEMROOT`/`TEMP` 等，剥离 `JWT_SECRET`/API Key/`DATABASE_URL`，防止经报告外带密钥）。
  同时**加固公式模式**：`_check_formula_safe` 以 AST 限定公式仅含 数值/已声明名称/四则运算/白名单函数，禁止属性访问、下标、推导式、`__import__` 等逃逸写法（公式模式对普通 admin 也安全）。
  已验证：网络/写文件/子进程被拦截、密钥不外泄、恶意公式被拒；正常步骤与既有测算不受影响。
  残留：为支持 `import` 仍允许**只读**文件访问（信息泄露面），强隔离建议在容器/网络层加固。

**H3. 默认密钥与口令　✅ 已修复**
`JWT_SECRET` 默认 `change-me-in-production`（23 字节 < HS256 推荐 32 字节，已触发 `InsecureKeyLengthWarning`）；root 默认口令 `Root@123456`。生产若未改即上线，令牌可被伪造。
- 建议：启动时若检测到默认 `JWT_SECRET`/默认 root 口令且非调试模式则**拒绝启动**；密钥强制 ≥32 字节。
- **修复**：`backend/main.py::_enforce_security_config` 于 startup 校验——`JWT_SECRET` 为默认/占位值或 <32 字节、或首次建库仍用默认 `ROOT_PASSWORD` → 抛错拒绝启动；开发可设 `ALLOW_INSECURE_DEFAULTS=true` 仅告警放行。已验证默认配置确实无法启动、强配置正常启动。

### 🟠 中危

**M1. 长任务阻塞 + 水平扩展受限　✅ 已修复（持久化任务队列 + 独立 worker；横向扩展可经 Postgres 落地）**
方案流水线在单个请求内串行多次 LLM 调用，可能数分钟，期间占用 worker 与 DB 会话。单进程同步 + SQLite 写串行 → 并发上不去，无法横向扩展。
- 建议：将长流水线迁移到后台任务队列（arq/RQ/Celery），返回 `job_id`，前端经 SSE/WebSocket 流式接收进度；DB 换 Postgres 以支持并发与多副本。
- **修复（一期·内存）**：`POST /api/v1/chat` 不再阻塞——立即返回 `job_id`；`GET /chat/jobs/{id}` 轮询、`POST /chat/jobs/{id}/cancel` 停止；前端改为提交→轮询→渲染。
- **修复（二期·持久化队列）**：`backend/jobs.py` 重写为 **DB 持久化任务队列**（新增 `jobs` 表）。`/chat` 把入参（含上传文件路径，已在请求内落盘）序列化为任务行后立即返回，**不在请求内持有 DB 会话**。新增 `backend/worker.py`：worker 用「带 status 条件的 UPDATE」**原子领取** pending 任务（SQLite/Postgres 均安全），周期心跳续租，终态写回 done/failed/cancelled。
  - **跨重启不丢**：任务状态落库；启动与周期性 `requeue_stale` 把心跳超时（worker 崩溃/重启）的 running 任务重新入队。
  - **独立 worker 进程**：`python run.py worker` 可与 API 进程分离、按需多开（API 进程设 `JOB_WORKER_ENABLED=false`）；单机默认进程内 worker。
  - **取消跨进程可用**：协作式取消（置 `cancel_requested`，运行任务在阶段检查点 `progress` 回调感知并中止）+ 进程内即时 `task.cancel()`。
  - **SQLite 加固**：开启 WAL + `busy_timeout=30s`（`database.py`），支撑队列短写与长任务并发。
  - 配置：`JOB_WORKER_ENABLED` / `JOB_WORKER_CONCURRENCY` / `JOB_POLL_SECONDS` / `JOB_HEARTBEAT_SECONDS` / `JOB_LEASE_SECONDS` / `JOB_TTL_SECONDS`。
  已验证：enqueue→claim→progress→finish 全流转、归属隔离、pending 取消、心跳超时重入队、过期清理、chat 端到端（worker 消费至终态、取消生效）。
- **残留**：**多副本横向扩展**已具备前提（持久化队列 + 可分离 worker + 原子领取），但 SQLite 仅适合单机；真正多副本需把 `DATABASE_URL` 切到 **Postgres**（schema 已兼容；多 worker 抢占建议后续加 `SELECT … FOR UPDATE SKIP LOCKED` 降低争用）。`open_api` 同步执行路径未改（开放 API 仍即时返回结果）。

**M2. 客户端未复用连接 / MCP 重复握手　✅ 已修复**
`LLMClient._post` 每次新建 `httpx.AsyncClient`；`mcp_client.call_tool` **每次工具调用都重新 `initialize + tools/list`**。多工具循环时延迟与开销显著。
- 建议：进程级共享 `httpx.AsyncClient`（连接池）；单次对话内对每个 MCP 维持一个会话并缓存工具清单。
- **修复**：`client.py` 引入进程级共享 `httpx.AsyncClient`（连接池，`get_http_client`，shutdown 关闭），`_post`/`ping` 复用之；新增 `mcp_client.McpConnection`，`run_simple_agent` 用 `AsyncExitStack` 为每个 MCP 服务**保持一条已初始化连接**，整段对话内多次 `tools/call` 不再重复 `initialize`/建连，结束统一关闭。

**M3. 文件型知识库元数据并发**
`_datasets.json` 的读改写无锁，并发新建/改可见性可能竞态损坏；知识检索每次查询读取并分词全部文档（无索引），大库下退化为 O(语料)。
- 建议：`_datasets.json` 加文件锁或迁移为 DB 表；为检索建立预计算倒排/向量索引。

**M4. 跨实例导入的 id 引用不可移植**
智能体/流程导出携带 `provider_id/workflow_id/mcp_ids/skill_ids` 等**实例内数字 id**；导入到另一实例/另一归属会 `require_use` 校验失败或误绑（同号不同物）。
- 建议：导出按**名称/稳定标识**引用，导入时按名解析并提示未命中的依赖。

**M5. 鉴权无限流/锁定**
`/auth/login` 与 `/open/v1/*` 无频率限制与失败锁定，可被暴力枚举。
- 建议：登录失败退避 + IP/账户级限流；开放 API Key 加调用配额。

**M6. 启动钩子与会话生命周期　✅ 大部分修复**
使用已弃用的 `@app.on_event("startup")`；长任务内 `db` 会话贯穿请求（含被取消路径）。
- 建议：改用 `lifespan` 上下文；长任务从会话中尽早取数、计算阶段不持有会话。
- **修复**：随 M1 二期改造，`main.py` 已迁移到 `lifespan`（初始化数据 → 启动进程内 worker → 退出统一清理 worker 与 httpx 连接池），移除两处 `@app.on_event`。请求侧不再持有长任务会话（已下沉到 worker）。
- **残留**：worker 内 `execute_chat` 仍在整段执行期持有一条短会话（与早期实现一致）；如需进一步降低占用，可先取数后释放会话、计算阶段不持有（WAL + busy_timeout 已缓解锁争用）。

### 🟡 低危 / 可维护性

**L1. JSON-in-TEXT 无引用完整性**：删除 provider/skill 后，agent 的 id 列表成悬挂引用（运行时 `resolve` 静默跳过，行为安全但不可见）。建议删除时联动清理或改链接表，并在 UI 标注失效引用。

**L2. 无迁移框架**：`_migrate` 仅支持「加列」，无法改类型/删列。规模上去后引入 Alembic。

**L3. 前端单文件巨石**：`admin.js` ~1200 行全局作用域、内联 `onclick`（对 CSP 不友好）。渲染已用 `escapeHtml` 防 XSS（良好），但建议逐步模块化并移除内联事件。

**L4. 技能大资源入库**：单技能资源最多约 6MB 存于单 TEXT 列，SQLite 可承载但偏重；超大技能建议落盘。

**L5. 测试与质量门禁**：`tests/smoke_test.py` 覆盖面好但为单脚本；缺单元测试（权限矩阵、MCP 解析、公式代码生成边界）与 lint/类型检查。建议引入 pytest 分层 + ruff/mypy + CI。

**L6. CORS `allow_origins=["*"]`**：凭据走 `Authorization` 头而非 Cookie，风险可控，但生产应收敛白名单。

---

## 3. 正确性抽查（通过项）

- 口令散列 PBKDF2-HMAC-SHA256 240k 迭代、随机 16B salt、`hmac.compare_digest` 校验 —— 稳妥。
- API Key 为 32B urlsafe 随机，存 sha256，明文仅创建时返回一次 —— 稳妥。
- 路径穿越防护：知识库读写用 `Path(filename).name`，导出下载 `resolve()` 后校验父目录 —— 稳妥。
- 归属判定 `owns_resource`、可见域 `scope_owned`、可用判定 `can_use` 语义自洽；自降级/自禁用/自删除被后端硬挡。
- 步骤公式名校验（中文/字母/数字/下划线、非数字开头）、`eval` 语法预编译、子进程结果 marker 解析 —— 稳妥。

---

## 4. 优先级路线图（建议顺序）

1. ~~**上线前置**（H3）：强制非默认 `JWT_SECRET`（≥32B）与非默认 root 口令，否则拒绝启动。~~ ✅ 已完成
2. ~~**外联收敛**（H1）：MCP/Provider URL 私网/链路本地拦截或白名单。~~ ✅ 已完成
3. ~~**执行沙箱**（H2）：子进程加 rlimits + 禁网，或明确信任边界并限制 Python 模式权限。~~ ✅ 已完成（审计钩子 + rlimits + 最小化环境 + 公式 AST 加固）
4. ~~**可扩展性**（M1/M2）：长流水线后台化 + 流式进度；共享 httpx 连接池；缓存 MCP 工具清单。~~ ✅ 已完成（持久化任务队列 + 可分离 worker + 进程级 httpx 连接池 + 单连接复用的 MCP 会话；横向扩展切 Postgres 即可落地）
5. **数据一致性与韧性**（M3/M5/L1）：`_datasets.json` 加锁或入库；鉴权限流；悬挂引用清理与提示。
6. **工程化**（L2/L5）：Alembic 迁移、pytest 分层、ruff/mypy、CI；前端逐步模块化。

---

*生成时间：2026-06-13 · 基于当前 `green-carbon-agent` 代码快照。建议在每次重大变更后重跑 `tests/smoke_test.py` 并复审本表中的高危项。*
