# Work OS 本地开发

## 启动

在项目目录运行：

```powershell
python -m venv .venv
npm install
npm run dev:full
```

浏览器打开 `http://127.0.0.1:5175`。API 在 `http://127.0.0.1:8106`，前端通过本地代理访问。此启动方式固定使用本地 SQLite，并跳过 `.env` 文件加载，不会连接 Neon。停止两个服务按 `Ctrl+C`。

需要 Node.js 和 Python 3.11 或更新版本。业务界面、API 和图标由项目本地源码提供；运行时不加载 CDN、远程字体或图片。npm 包只用于本地构建与 React 界面，虚拟环境中的 psycopg 驱动只用于连接 PostgreSQL。

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

浏览器仍打开 `http://127.0.0.1:5175`。顶部清楚显示“PostgreSQL 共享账套”或者“SQLite 离线账套”，不会把离线账套伪装为正式财务数据库。数据库必须预先迁移到当前版本，此启动入口**不会自动修改 Neon**。

**不要在本机离线账套和共享账套分别记录同一笔正式业务。** `npm run dev:full` 始终是独立的离线 SQLite 体验，数据不会自动同步；正式业务应使用 `dev:connected` 且运行前确认连接的是预期 Neon 分支。

当前财务中心支持银行流水精确匹配建议（人工确认后才核销）、发票批量导入（源文件先归档、冲突行拒绝、税务状态仍为待核实）。这不等同于真实银行/税务平台自动同步。
