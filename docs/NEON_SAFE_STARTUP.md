# Work OS：Neon 数据库安全启动与迁移预检查

## 当前已核实状态（2026-11-20，只读查询）

| 配置 | 实际 |
|---|---|
| Neon 项目 | `work-os` |
| 区域 | AWS Singapore |
| 当前 PostgreSQL 版本 | 18 |
| 数据库实际名称 | `neondb`，**不是** `work_os` |
| 默认分支 | `production`，目前没有 Work OS 业务表 |
| 独立分支 | `work-os-local-dev`，数据库结构版本 35 |
| GitHub 已实现的数据库结构 | schema 39 |

开发/生产分支是**独立数据分支**，不能仅因为项目同名就认为已同步。不要将现有本地 SQLite 账套或旧 CoffeeBear 数据误认作已经写入 Neon。

## 安全行为（本次改动）

- 本地 SQLite 仍可以正常启动时初始化/升级本地结构。
- `WORKOS_DB_BACKEND=postgres` 时，API **只读检查** `schema_migrations` 与关键表是否完整。
- 如果版本低于 39、版本高于 39、无迁移表或丢失关键表，API 拒绝启动，**不会自动执行远程 DDL/DML**。
- 严禁仅凭一次网页刷新就对 Neon 生产分支自动创建/删除/升级账务数据表。
- 本地运行 `python backend/check_database.py` 查看当前连接的版本和缺失表。输出不包含连接密码。
- 真实数据库迁移必须先完成**独立备份**（数据库和档案文件均需要）、确认目标分支与结构差异、在测试分支演练回滚并审核数据，再显式执行迁移。
- CI 使用全新 disposable PostgreSQL 18 本地容器，已覆盖版本 35 → 39 迁移，并检查数据库事务提交/回滚，不连接 Neon。

## 本地使用

请将敏感连接串放在根目录被 Git 忽略的 `.env` 中，而不是源代码或公开 GitHub。

```env
WORKOS_DB_BACKEND=postgres
WORKOS_DATABASE_URL=postgresql://<用户名>:<密码>@<主机>/neondb?sslmode=require
```

上述内容仅是格式示例，**没有提供真实连接字符串**。

在本机运行：

```bash
python backend/check_database.py
```

如果显示 `current_version: 35`、`required_version: 39`、`ready: false`，说明目标库还不能用于正式记账；不要依赖后台自动升级。

## 尚未完成，不能宣称生产已交付

- 尚未对真实 Neon 分支执行版本 35 → 39 迁移；生产分支也尚未初始化业务表。
- 尚未执行数据迁移前后逐表核对、恢复演练、正式数据库备份与原始凭证文件同步备份。
- 尚未完成跨系统 ALSVID API 身份权限、税务申报/进项抵扣全闭环、正式多人角色及真实历史数据月结验收。

本阶段只实现**防误升级、可重复的安全预检查及本地临时 PostgreSQL 测试**。
