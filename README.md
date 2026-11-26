# Work OS 本地开发

## Schema 44/45/46 集成发布状态（待正式切换）

- **代码准备**：汇总红字发票、税率版本、具名财务权限、个人垫付/月结双人审批，以及银行核销/调整分录/发票税务状态的独立审批；Schema 46 增加 CoffeeBear 旧库行快照（只读、不可变、SHA-256 留存）。
- **当前开发分支仅代表代码与隔离测试**，并不表示 Neon 正式库已经迁移或线上页面已部署。
- **正式切换前**必须按 [Schema 43→46 上线验收清单](docs/PRODUCTION_ROLLOUT_V45.md) 对生产数据做备份/恢复与模拟迁移；完成 45 门禁后再执行 `backend/migrations/46_legacy_raw_records.sql`。新版本对不匹配数据库仍会拒绝启动，不会擅自升级生产库。
- 具名财务账号与审核流程详见 [财务审批说明](docs/FINANCE_NAMED_APPROVAL_V45.md)。单人本地模式不会等同于双人财务审批。

## 启动

在项目目录运行：

```powershell
python -m venv .venv
npm install
npm run dev:full
```

浏览器打开 `http://127.0.0.1:8085`（开发端口；8082 是本机部署预览使用的端口）。API 在 `http://127.0.0.1:8106`，前端通过本地代理访问。此启动方式固定使用本地 SQLite，并跳过 `.env` 文件加载，不会连接 Neon。停止两个服务按 `Ctrl+C`。

判断"服务是否连上了正确的账套版本"直接看健康接口：`curl -s http://127.0.0.1:8106/api/health`，返回里的 `schema_version` 与 `required_schema_version` 相等、`schema_ready` 为 `true` 才算就绪。数据库版本落后时代码会拒绝启动，而不是带着缺表的库继续跑。

需要 Node.js 和 Python 3.11 或更新版本。业务界面、API 和图标由项目本地源码提供；运行时不加载 CDN、远程字体或图片。npm 包只用于本地构建与 React 界面，虚拟环境中的 psycopg 驱动只用于连接 PostgreSQL。

本机常驻部署（macOS LaunchAgent）：`com.gino.workos-api`（离线 API，8106）和 `com.gino.workos-web`（8082 生产构建预览）。改完前端后运行 `npm run build`，再执行 `launchctl kickstart -k gui/$(id -u)/com.gino.workos-web`；日志在 `~/Library/Logs/WorkOS/`。

## 数据

本地离线启动固定使用 SQLite 文件 `backend/data/workos.sqlite3`，此目录不纳入版本管理；启动过程不会读取 `.env`，也不会连接远程数据库。SQLite 和 Neon 是分开的数据库，不会自动同步或迁移数据。

## 当前范围

主要工作台为咖啡、自行车、展会项目和财务核算中心。当前功能与未完成事项见 [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md)、[`docs/WORKBENCHES.md`](docs/WORKBENCHES.md) 和 [`docs/ACCOUNTING_RULES.md`](docs/ACCOUNTING_RULES.md)。


## 正式共享账套（本地界面 + Neon + R2）

本地运行但使用**同一个 Neon PostgreSQL** 财务账套；原始文件使用**Cloudflare R2**，本地/NAS 保留镜像。此模式不会悄悄回退为 SQLite。

先在未纳入版本管理的 `.env` 写入实际配置：`WORKOS_DATABASE_URL`、`WORKOS_AUTH_PASSWORD_HASH`、`WORKOS_R2_ACCOUNT_ID`、`WORKOS_R2_BUCKET`、`WORKOS_R2_ACCESS_KEY_ID`、`WORKOS_R2_SECRET_ACCESS_KEY`、`WORKOS_ARCHIVE_ROOT`。

```powershell
python -m pip install -r backend/requirements.txt
npm install
npm run dev:connected
```

浏览器仍打开 `http://127.0.0.1:8085`。顶部清楚显示“PostgreSQL 共享账套”或者“SQLite 离线账套”，不会把离线账套伪装为正式财务数据库。数据库必须预先迁移到当前版本，此启动入口**不会自动修改 Neon**。

**不要在本机离线账套和共享账套分别记录同一笔正式业务。** `npm run dev:full` 始终是独立的离线 SQLite 体验，数据不会自动同步；正式业务应使用 `dev:connected` 且运行前确认连接的是预期 Neon 分支。

当前财务中心支持银行流水精确匹配建议（人工确认后才核销）、发票批量导入（源文件先归档、冲突行拒绝、税务状态仍为待核实）。这不等同于真实银行/税务平台自动同步。

## Neon + NAS 原件备份与核验

共享账套的原件保存在 Cloudflare R2，本地/NAS 保存独立镜像。备份工具只读导出 Neon PostgreSQL 数据，不创建或修改财务单据，不删除旧备份。

在本地私有 `.env` 中，除了原有 `WORKOS_DATABASE_URL`、`WORKOS_ARCHIVE_ROOT`，增加：

```env
WORKOS_DB_BACKEND=postgres
WORKOS_BACKUP_ROOT=/Volumes/NAS/WorkOS/Backups
```

本机需安装与 PostgreSQL 服务器兼容的 `pg_dump` 和 `pg_restore` 命令。执行：

```bash
python backend/backup_connected.py
```

系统逐个比对 NAS 原件 SHA-256 和 Neon 归档索引，生成自定义 PostgreSQL dump，并通过 `pg_restore --list` 验证其结构；最终保存 `database.dump` 和 `manifest.json`，不覆盖旧文件。复核某次备份：

```bash
python backend/backup_connected.py --verify /Volumes/NAS/WorkOS/Backups/workos-实际备份目录
```

此校验**不等于实际恢复到空库的演练**；真实备份尚需在拥有 NAS 路径及 Neon 连接配置的本机运行，并且 R2 远端完整性仍需单独验收。
