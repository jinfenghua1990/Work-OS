#!/usr/bin/env bash
# Work-OS 离线部署：在另一台 Mac 上一键复现 8082 服务。
# 用法：仓库根目录执行 ./deploy/setup.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="$HOME"
LAUNCH_DIR="$HOME_DIR/Library/LaunchAgents"
LOG_DIR="$HOME_DIR/Library/Logs/WorkOS"
DB="$REPO/backend/data/workos.sqlite3"
SEED="$REPO/deploy/seed/workos.sqlite3"

echo "==> 仓库: $REPO"

# 0. 前置检查：运行时与端口
echo "==> 检查运行时"
NPM_BIN="$(command -v npm || true)"
PYTHON_BIN="$(command -v python3 || true)"
[ -n "$NPM_BIN" ] || { echo "错误: 找不到 npm，请先安装 Node.js 20+"; exit 1; }
[ -n "$PYTHON_BIN" ] || { echo "错误: 找不到 python3，请先安装 Python 3.11+"; exit 1; }
echo "    npm: $NPM_BIN"
echo "    python3: $PYTHON_BIN"
for port in 8082 8106; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "错误: 端口 $port 已被占用，请先停掉占用进程或已有 Work-OS 服务"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN
    exit 1
  fi
done

# 1. 前端依赖与构建
echo "==> 安装前端依赖"
cd "$REPO"
npm install
echo "==> 构建前端"
npm run build

# 2. 初始化账套（不覆盖已有数据）
mkdir -p "$REPO/backend/data"
if [ ! -f "$DB" ]; then
  echo "==> 从种子快照初始化账套: $DB"
  cp "$SEED" "$DB"
else
  echo "==> 账套已存在，跳过初始化（本地数据不被覆盖）: $DB"
fi

# 3. 安装 LaunchAgent（替换路径占位符）
echo "==> 安装 LaunchAgent"
mkdir -p "$LAUNCH_DIR" "$LOG_DIR"
for label in com.gino.workos-api com.gino.workos-web; do
  src="$REPO/deploy/launchagents/$label.plist"
  dst="$LAUNCH_DIR/$label.plist"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME_DIR|g" \
      -e "s|__NPM__|$NPM_BIN|g" -e "s|__PYTHON__|$PYTHON_BIN|g" "$src" > "$dst"
  echo "    写入 $dst"
done

# 4. 加载服务（已加载则先卸载旧配置）
echo "==> 加载常驻服务"
for label in com.gino.workos-api com.gino.workos-web; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LAUNCH_DIR/$label.plist"
done
launchctl kickstart -k "gui/$(id -u)/com.gino.workos-api"
launchctl kickstart -k "gui/$(id -u)/com.gino.workos-web"

# 5. 等待就绪并打印访问地址
echo "==> 等待服务就绪"
READY=0
HEALTH_BODY="$(mktemp -t workos-health)"
for i in $(seq 1 20); do
  # 200 = 已就绪；401/403 = 服务已起来但本机启用了登录，同样算就绪
  code="$(curl -sS -o "$HEALTH_BODY" -w '%{http_code}' http://127.0.0.1:8082/api/health 2>/dev/null || true)"
  if [ "$code" = "200" ] || [ "$code" = "401" ] || [ "$code" = "403" ]; then
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  rm -f "$HEALTH_BODY"
  echo "错误: 20 秒内服务未就绪。请查看日志："
  echo "  $LOG_DIR/web.err.log"
  echo "  $LOG_DIR/api.err.log"
  echo "服务状态："
  launchctl list | grep com.gino.workos || true
  exit 1
fi

# 6. 校验运行时 schema 与代码要求的版本一致（避免"服务起来了但库是旧版"）
if [ "$code" = "200" ]; then
  if ! "$PYTHON_BIN" - "$HEALTH_BODY" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
current = payload.get("schema_version")
required = payload.get("required_schema_version")
print(f"    schema_version: {current} (required {required})")
if current is None or required is None or current != required:
    raise SystemExit(1)
PY
  then
    rm -f "$HEALTH_BODY"
    echo "错误: 运行中的账套 schema 与代码要求不一致，拒绝报告部署成功。"
    echo "  参考 deploy/README.md，用最新 seed 重建账套，或运行 python backend/check_database.py"
    exit 1
  fi
fi
rm -f "$HEALTH_BODY"

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<本机IP>')"
echo ""
echo "部署完成："
echo "  本机:     http://127.0.0.1:8082"
echo "  局域网:   http://$IP:8082"
echo "  API:      127.0.0.1:8106（仅本机）"
echo ""
echo "服务状态:"
launchctl list | grep com.gino.workos || true
