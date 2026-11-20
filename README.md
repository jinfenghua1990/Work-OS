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
