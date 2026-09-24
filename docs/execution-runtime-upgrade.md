# 执行运行时升级说明

本次升级在现有 Job/Worker、授权工具目录和公开过程视图之间增加私有检查点、工具调用账本与有边界的执行恢复。数据库版本为 `0028_runtime_durability`，上一个版本为 `0027_reasoning_config`。

实施范围是现有自研执行层；MCP 目录发现仍使用现有连接流程，Claude Agent SDK/DSH 可选后端、跨子任务共享预算和任意外部副作用事务不在本次实现中。

## 执行与恢复语义

| 机制 | 当前行为与边界 |
| --- | --- |
| 私有检查点 | 保存模型消息、待执行批次、下一轮位置、计划、任务契约、预算/修复计数、工具观察缓存和产物元数据。正常 Worker 绑定有效 Job lease 后启用；直接调用运行函数且未注入检查点存储，不代表持久恢复路径。 |
| 私有加密 | `RuntimeCheckpoint.state`、`ToolInvocation.arguments/result` 使用 `EncryptedText`，ORM 写入数据库时加密、读取时解密。运行标识、工具名、状态、摘要等索引元数据并非全部加密；检查点不备份外部文件或远端服务状态。 |
| 所有权与版本 | 读写验证任务归属；Worker 写入还校验运行状态、worker、lease token 和取消状态。检查点使用 revision 比较更新，旧 lease 不能继续提交检查点或工具结果。lease 失效不能撤销已经发出的远端操作。 |
| 工具账本 | 调用按 run、execution key、call id 标识，执行前登记参数摘要、能力版本和副作用分类。已完成的同一调用恢复时复用已保存的观察及上下文，不再次执行；同一标识更换参数或能力版本会拒绝。控制面操作由检查点恢复，不作为外部副作用结果复用。 |
| 不确定结果 | 已开始但未提交完成结果的写入、外部操作或未知副作用，恢复时标记 `unknown_outcome` 并停止自动重试；明确只读的调用可以重试。超时或异常也可能发生在外部效果已经提交之后。 |

这里**不保证外部副作用 exactly-once**：数据库账本与外部服务之间没有分布式事务。结果不确定时，先检查真实文件、远端记录或服务状态，再根据已确认的结果创建新任务；不能把未知结果当作失败后直接重放，也不能靠再次批准绕过这一状态。用户可见错误为“工具 X 的执行结果不确定；已停止自动重试，请核对实际结果后创建新任务。”账本写入或校验失败同样停止执行。

恢复针对已持久化的边界；尚未保存的模型请求可能重新执行。升级前没有账本的历史操作无法补出可靠的完成凭证。子执行有独立 execution key，但委派本身仍属于外部副作用，不能据此宣称整棵任务树可以无条件重放。

## 审批、并发与能力使用

- **精确审批绑定**：持久执行路径的审批同时绑定 run、用户、执行智能体、scope，以及 invocation id、规范化参数摘要、能力版本。令牌有时限且只能消费一次；改参数、换能力版本或换调用，旧批准不再适用。历史未绑定令牌不能授权新的绑定调用。
- **有限只读并发**：仅明确列入可信只读清单的内置工具，以及满足只读注解条件的 MCP 工具，可以在 `max_parallel_calls` 范围内并发。写入、控制、委派和未知工具形成串行屏障。模型收到的回复仍按调用顺序排列；一个调用异常时，等待已启动的同组调用收尾，不派发后续批次。MCP 的只读判断依赖服务注解真实性，不构成对远端代码的沙箱隔离。
- **Skill 按需读取**：初始提示包含已授权 Skill 的名称、描述和资源目录，不预载全部正文。通过 `read_skill_resource` 读取 `SKILL.md` 入口后记录激活；其他文本资源仍从本次授权快照读取，不能借资源路径任意访问磁盘。这里的激活记录不扩大工具权限，也不证明模型实际遵循了全部技能指令。
- **引导更新任务契约**：`guide` 补充要求，`redirect` 替换当前目标并保留原始目标；同一 guidance id 不重复应用。安全边界吸收引导后增加 revision，重新计算路由、规划和证据要求。旧目标的成功证据不会直接证明新目标已完成，既有执行预算和写入去重记录继续保留；引导不能新增授权能力。
- **有界动作修复**：模型准备结束而缺少必需产物或工具证据时，在审核未关闭、修复次数、剩余轮数及工具预算允许的条件下，返回正常工具循环补做实际操作。修复继续经过原有权限、审批与账本。纯文本修订另有边界；不能靠改写答复补齐执行证据。预算耗尽后仍缺必要证据会触发完成门禁，计划阻塞或失败也不能冒充全部完成。

## 公开过程与私有状态

实时流、重连回放和历史视图共同使用安全投影。新增事件只提供过程元数据：

| 事件 | 用户看到的含义 |
| --- | --- |
| `task.contract.updated` | 引导已更新任务契约，显示 revision 与 guide/redirect。 |
| `capability.activated` | 已读取 Skill 入口，显示能力种类与名称。 |
| `recovery.resumed` | 已从检查点恢复进度，显示版本和轮次。 |
| `invocation.reused` | 已复用此前保存的调用结果。 |
| `recovery.blocked` | 执行结果不确定，需核对实际状态；这不是新的审批请求。 |
| `verification.repair.started/completed` | 进入工具修复轮及修复后的验证结果，显示次数、问题数与是否通过；开始事件本身不证明工具已经成功。 |

新增事件不公开模型消息、参数、工具原始结果、参数摘要或推理内容。恢复/复用事件不直接改变任务终态；失败、完成及带问题完成继续由现有终态事件驱动。

## 0028 部署与回退

此处“离线迁移”指停写后迁移数据库，不是 Alembic `--sql` 模式。不要在旧、新 Worker 并行写入时滚动切换。完整发布要求仍见 [RELEASE.md](../RELEASE.md)，其中旧的 0018→0019 示例在本次部署应替换成实际源版本→0028。

1. 固定待发布 commit 与锁定依赖，在隔离环境运行下面的回归。盘点运行中及等待审批的旧任务；旧任务缺少新账本时，先核对其外部结果，不能推断可安全重放。
2. 使用数据库备份完成隔离恢复和迁移演练。生产切换前停止所有会写入的 API、Worker、Scheduler，再生成正式备份。SQLite 必须使用 Online Backup API，不直接复制活跃 WAL 数据库；目标文件必须不存在、父目录须已创建。
3. 独立保存该数据库快照对应的 `SECRET_MASTER_KEY` 和必要的 `SECRET_MASTER_KEY_PREVIOUS` 版本。数据库备份不包含这些密钥；丢失它们会导致私有检查点、调用结果和其他凭据无法解密。不要把密钥写入发布日志。
4. 在确认 `DATABASE_URL`、数据目录与密钥配置指向预期环境后，用同一发布环境执行迁移，并核对 revision 为 `0028_runtime_durability`。迁移增加两张运行时表和审批绑定列，不会为旧操作生成调用结果。
5. 由部署流程启动新版本，运行默认发布预检、健康检查和实际用户路径验收。保留备份摘要、代码版本、迁移版本与验收记录。回退时停写，将备份恢复到新路径并切换到匹配的旧应用及密钥集合；保留失败数据库取证，不用删除新表替代回退。

以下是源库已经确认处于 0027 时的命令示例；路径须替换为部署方已核验的路径。先在隔离环境演练，正式备份再按同样步骤执行：

```powershell
.\venv\Scripts\python.exe tools\sqlite_backup.py backup --source C:\deploy\data\app.db --target C:\deploy\backups\app-before-0028.db --expected-revision 0027_reasoning_config
.\venv\Scripts\python.exe tools\sqlite_backup.py restore-drill --source C:\deploy\backups\app-before-0028.db --target C:\deploy\rehearsal\restored.db --expected-revision 0027_reasoning_config
```

在部署环境显式配置好目标数据库后，迁移与发布检查分别为：

```powershell
.\venv\Scripts\python.exe -X utf8 -c "from backend.migrations import run_schema_migrations; run_schema_migrations()"
.\venv\Scripts\python.exe tools\release_preflight.py
```

不要把演练库的 `DATABASE_URL` 留给生产服务，也不要对原生产库执行恢复演练。PostgreSQL 使用部署方自己的数据库备份/恢复流程，不使用 SQLite 工具。

## 验证范围

完整回归入口：

```powershell
.\venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_*.py'
node --check frontend/static/run-workspace.js
node --check frontend/static/app.js
```

聚焦检查可运行 `tests.test_runtime_durability`、`tests.test_execution_primitives`、`tests.test_recovery_process_view` 与 `tests.test_run_workspace_recovery`。这些离线测试覆盖密文落盘、检查点版本/lease、结果复用、未知副作用阻断、精确审批、并发顺序、任务契约、Skill 目录，以及恶意事件负载过滤和不依赖浏览器的前端状态逻辑。

本说明不把单元测试、隔离数据库测试或 Node 测试视为真实模型、真实远端 MCP、浏览器操作、生产迁移或服务重启已经验收。外部效果在崩溃前后的实际一致性、真实部署备份恢复、真实用户界面与 Provider 链路仍须单独验证。

### 本次验证记录（2026-09-21）

- 使用独立临时数据目录和 SQLite 数据库运行完整回归：**783 tests OK**；14 个前端 JavaScript 文件语法检查、Python 编译检查、`git diff --check` 通过。
- 真实 SQLite 加密存储与模拟模型结合的测试验证：审批后恢复原批次、已完成写入不重复、父子审批恢复、超时后立即阻断、旧 lease 不能提交、已完成检查点直接返回，以及用户引导先持久化再确认。
- Playwright 使用 `127.0.0.1:8796` 和独立数据库，完成登录、对话页、恢复提示函数检查、运行中心导航及截图。测试库迁移到 `0028_runtime_durability`。证据目录为 `output/playwright/runtime-upgrade-20260921-231033/`。
- 浏览器检查关闭 Worker/Scheduler，因此 `/healthz` 的 Worker readiness 为 false；这不是完整 Worker 服务健康验收。登录时出现预期的未登录身份检查 401 和隔离目录缺少 favicon 的 404；运行中心页面未发现 JavaScript 异常，不能把整个登录流程称为零控制台错误。
- 浏览器检查使用的隔离服务、浏览器会话和临时数据库已清理，测试证据保留；业务数据库与原有服务未迁移、未重启。没有调用付费模型或真实外部 MCP 验证执行效果。
