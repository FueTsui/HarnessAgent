# 发布、迁移与回滚

本手册把“本地验证”“远端 CI”“真实外部链路验证”和“生产上线”分开记录。任何一层通过都不能替代后一层；`/healthz` 正常也不能证明 HTTPS、反向代理、真实 Provider/MCP 或备份恢复已经验收。

## 1. 发布前硬门槛

在用于发布的已提交、干净工作树执行：

```powershell
.\venv\Scripts\python.exe tools\release_preflight.py
```

默认预检失败闭合，并检查：

- `APP_ENV=production`、独立且足够长的 JWT/信封密钥、Secure Cookie 和不安全开关；
- `requirements.lock` 中每个依赖均精确固定并带 SHA-256；
- Git 存在 HEAD、工作树干净，数据库/WAL/浏览器证据/演练目录已被忽略；
- 数据库可连接且 Alembic revision 与应用 head 一致；
- SQLite 部署的 `integrity_check` 和 `foreign_key_check`。

CI 或单元测试如果尚未挂载部署数据库，可以显式使用 `--skip-database`。开发中的 unborn/dirty 仓库只能显式传入 `--allow-unborn`、`--allow-dirty`。这些参数是可见的例外，生产发布命令不得使用。预检只输出检查名称和脱敏状态，不输出环境变量值或数据库 URL。

## 2. SQLite 备份与恢复演练

SQLite 只支持单机部署。备份必须使用 Online Backup API，禁止在 WAL 活跃时直接复制 `app.db`：

```powershell
.\venv\Scripts\python.exe tools\sqlite_backup.py backup `
  --source C:\deploy\data\app.db `
  --target C:\deploy\backups\app-before-0019-20260821.db `
  --expected-revision 0018_memory_knowledge_projects
```

目标文件必须不存在，目标父目录必须预先创建。完成后工具验证完整性、外键和 revision，并输出 SHA-256、字节数和 UTC 时间；不输出业务行或凭据。

随后把备份恢复到另一个明确的新路径进行演练：

```powershell
.\venv\Scripts\python.exe tools\sqlite_backup.py restore-drill `
  --source C:\deploy\backups\app-before-0019-20260821.db `
  --target C:\deploy\rehearsal\restored.db `
  --expected-revision 0018_memory_knowledge_projects
```

对 `restored.db` 设置隔离的 `DATABASE_URL` 后运行迁移，再以 `tools\sqlite_backup.py` 的验证函数或发布预检确认 revision 为 `0019_release_reliability`。演练数据库、上传目录、工作区和端口必须与生产隔离。

## 3. 上线顺序

1. 确认远端 CI 的 Ubuntu、Windows、前端语法和 Windows Chromium 任务全部成功。
2. 在隔离环境完成上述备份恢复与 0018→0019 迁移演练。
3. 单独使用测试账号验证真实 Provider、MCP、OAuth/消息渠道；不得把本地 mock 通过写成真实链路通过。
4. 停止会写 SQLite 的 API/Worker/Scheduler，使用 Online Backup API 生成正式备份。
5. 部署相同 Git commit 与锁定依赖，运行 Alembic 迁移；不得手工删除旧表或 `app_settings` 中的旧治理 JSON。
6. 启动 API/Worker/Scheduler，运行默认发布预检、`/healthz`，再执行登录、对话、审批、运行中心和浏览器冒烟。
7. 记录 Git commit、迁移 revision、备份 SHA-256、CI run 和外部链路验收人/时间。记录中不得包含密钥。

## 4. 回滚

0019 包含追加式治理审计表和唯一性约束，不提供破坏性 Alembic downgrade。回滚应用时：

- 先停止所有写入进程；
- 使用 `restore-drill` 把上线前备份写到一个全新的目标，绝不覆盖当前生产库；
- 验证新目标的 integrity、foreign keys、revision 和 SHA-256；
- 由部署编排显式切换 `DATABASE_URL` 到已验证的新目标，保留失败上线数据库作只读取证；
- 恢复与该备份同一时点的应用版本、环境配置和外部凭据版本，再做健康与用户路径验证。

本工具不会替部署方覆盖、移动或删除生产数据库。

## 5. `SECRET_MASTER_KEY_PREVIOUS` 与 rewrap 边界

`SECRET_MASTER_KEY`、`SECRET_MASTER_KEY_PREVIOUS` 不在 SQLite 备份中，必须由独立密钥管理系统版本化。轮换顺序是：先配置新主密钥，把旧主密钥加入 `SECRET_MASTER_KEY_PREVIOUS`，验证旧密文仍可解密，然后通过经过审计的重写流程让每一条加密字段用新主密钥重新保存（rewrap）。

当前项目没有可证明覆盖所有加密列的批量 rewrap 命令，因此不能仅因服务启动成功就移除 previous key。只有逐表盘点并验证 Provider、MCP headers、OAuth、渠道令牌等全部密文已重写后才能移除。回滚到旧数据库时必须恢复能够解密该快照的旧密钥集合；丢失旧密钥不能靠数据库备份修复。任何密钥值、解密内容或请求头都不得写入 CI、发布记录、治理版本快照或命令输出。
