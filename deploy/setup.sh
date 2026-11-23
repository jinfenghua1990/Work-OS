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
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME_DIR|g" "$src" > "$dst"
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
for i in $(seq 1 20); do
  if curl -fsS -o /dev/null http://127.0.0.1:8082/api/health 2>/dev/null; then
    break
  fi
  sleep 1
done

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<本机IP>')"
echo ""
echo "部署完成："
echo "  本机:     http://127.0.0.1:8082"
echo "  局域网:   http://$IP:8082"
echo "  API:      127.0.0.1:8106（仅本机）"
echo ""
echo "服务状态:"
launchctl list | grep com.gino.workos || true
