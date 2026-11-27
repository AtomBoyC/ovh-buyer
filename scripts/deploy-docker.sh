#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

BUILD_LOCAL=0
if [ "${1:-}" = "--build" ]; then
  BUILD_LOCAL=1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未检测到 Docker。Linux 服务器可先执行:"
  echo "   curl -fsSL https://get.docker.com | sh"
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  echo "❌ 未检测到 docker compose 插件。"
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  RAND_PW="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 || echo "ovh$(date +%s)")"
  if sed --version >/dev/null 2>&1; then
    sed -i "s/^PANEL_PASSWORD=.*/PANEL_PASSWORD=${RAND_PW}/" .env
  else
    sed -i '' "s/^PANEL_PASSWORD=.*/PANEL_PASSWORD=${RAND_PW}/" .env
  fi
  echo "🔐 已自动生成 .env 并设置随机面板密码: ${RAND_PW}"
  echo "   (可随时编辑 .env 修改端口、密码或通知参数)"
fi

mkdir -p data/cache
chmod 755 data data/cache
if [ "$(id -u)" = "0" ]; then
  chown -R "${PUID:-1000}:${PGID:-1000}" data 2>/dev/null || true
fi

if [ "$BUILD_LOCAL" -eq 1 ]; then
  echo "🐳 正在从本地源码构建并启动 OVH Sniper 容器..."
  $COMPOSE_CMD -f docker-compose.yml -f docker-compose.build.yml up -d --build
else
  echo "🐳 正在拉取预构建镜像 ghcr.io/atomboyc/ovh-buyer:latest 并启动..."
  if ! $COMPOSE_CMD pull; then
    echo "⚠️ 拉取远程镜像失败，自动回退为本地构建..."
    $COMPOSE_CMD -f docker-compose.yml -f docker-compose.build.yml up -d --build
  else
    $COMPOSE_CMD up -d
  fi
fi

PORT_VAL="$(grep -E '^PORT=' .env | tail -n 1 | cut -d '=' -f 2- || true)"
PORT_VAL="${PORT_VAL:-8888}"
PW_VAL="$(grep -E '^PANEL_PASSWORD=' .env | tail -n 1 | cut -d '=' -f 2- || true)"

echo ""
echo "============================================================"
echo "✅ OVH Sniper Docker 部署完成!"
echo "🌐 访问地址: http://<你的服务器IP>:${PORT_VAL}"
echo "🔑 面板密码: ${PW_VAL:-未设置}"
echo "📜 查看日志: $COMPOSE_CMD logs -f"
echo "🛑 停止服务: $COMPOSE_CMD down"
echo "============================================================"
