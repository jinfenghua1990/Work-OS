# Work-OS 离线部署（另一台 Mac）

这套资产把当前本机 8082 的离线部署完整搬到另一台 macOS 电脑：前端 8082（监听局域网）+ Python API 8106（仅 loopback）+ 初始账套快照。

## 账套数据（全量）

仓库里放了两份数据，内容等价，都是空账套（75 张表、schema 46、29 行默认科目表，**无任何真实业务数据**），且都已 checkpoint 为单一文件，不再依赖 `-wal`/`-shm`：

- `seed/workos.sqlite3`：用 SQLite backup API 从全新 schema 46 账套导出的**单一一致快照**。部署脚本用它初始化新机器，是推荐入口。
- `runtime-db/workos.sqlite3`：本机运行账套在导出时的**单一一致快照**（已 checkpoint），供原样还原或核对。当前账套与 seed 一样没有业务数据，因此两者内容等价。

两个快照都已经是 schema 46，首次启动不需要再执行迁移。若要用于运行，直接复制为 `backend/data/workos.sqlite3` 即可。

当前账套没有业务数据，所以全量上传不涉及隐私。若将来录入真实凭证/银行流水/票据：新机器产生的数据留在本地 `backend/data/`（已被 `.gitignore` 排除），**不要提交含真实业务数据的账套文件**——本仓库是 public。

## 前置条件

- macOS，Node.js 20+（含 npm）、Python 3.11+
- 无需 PostgreSQL、Neon、R2、`.env` —— 离线模式完全本地

## 一键部署

在仓库根目录执行（会装依赖、灌入种子账套、构建前端、生成并加载两个 LaunchAgent）：

```bash
chmod +x deploy/setup.sh
./deploy/setup.sh
```

脚本做这些事：
1. **前置检查**：探测 `npm`/`python3` 绝对路径（不依赖固定 PATH，兼容 nvm/Homebrew），检查 8082/8106 端口未被占用；不满足则报错退出
2. `npm install` + `npm run build`
3. 若本地 `backend/data/workos.sqlite3` 不存在，从 `deploy/seed/workos.sqlite3` 复制一份作为初始账套（**已存在则不覆盖**，保护本地数据）
4. 把 `deploy/launchagents/*.plist` 的占位符替换为实际值（`__REPO__` 仓库路径、`__HOME__` 用户目录、`__NPM__`/`__PYTHON__` 运行时绝对路径），安装到 `~/Library/LaunchAgents/`
5. `launchctl bootstrap` + `kickstart` 加载两个常驻服务
6. 等待健康检查（最多 20 秒），**失败则打印日志位置并退出非 0**，不会假报"部署完成"
7. **校验运行时 schema**：读取 `/api/health` 里的 `schema_version` 与 `required_schema_version`，两者不一致时同样退出非 0——避免"服务起来了但账套是旧版"这种假成功

## 访问

- 本机：`http://127.0.0.1:8082`
- 局域网：脚本结束时打印本机 IP，同一网段设备用 `http://<IP>:8082` 访问

## 端口与网络

| 端口 | 服务 | 监听 |
|---|---|---|
| 8082 | Vite preview（前端 + `/api` 代理） | `0.0.0.0`（局域网可达） |
| 8106 | Python 离线 API | `127.0.0.1`（仅本机，不暴露） |

前端把 `/api` 反向代理到 8106，并把请求的 `Origin` 改写为 `http://127.0.0.1:8106`，落在后端白名单内，因此局域网访问不会被 403。

## 运维

```bash
# 核对运行中的账套 schema（未启用登录时）
curl -s http://127.0.0.1:8082/api/health
# {"status":"ok",...,"schema_version":46,"required_schema_version":46,"schema_ready":true}

# 改完前端代码
npm run build
launchctl kickstart -k gui/$(id -u)/com.gino.workos-web

# 查看状态 / 日志
launchctl list | grep com.gino.workos
tail -f ~/Library/Logs/WorkOS/web.err.log
tail -f ~/Library/Logs/WorkOS/api.log

# 卸载服务
launchctl bootout gui/$(id -u)/com.gino.workos-web
launchctl bootout gui/$(id -u)/com.gino.workos-api
```

## 安全提醒

8082 对局域网开放且**无访问密码**。适合内网演示，不要暴露到公网，也不要把它当正式财务账套使用。生产切换走 `docs/PRODUCTION_ROLLOUT_V45.md` 的门禁。

> 若本机启用了登录密码，`/api/health` 会返回 401，`deploy/setup.sh` 此时只判断"服务已起来"并跳过 schema 比对；这种情况下请改用 `python backend/check_database.py`（PostgreSQL）或直接查 SQLite 的 `schema_migrations` 最大值来核对版本。
