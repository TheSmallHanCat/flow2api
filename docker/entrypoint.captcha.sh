#!/bin/bash
set -e

# 1. 注入插件配置
EXTENSION_DIR="/home/chrome/extension"
if [ -n "$WS_URL" ]; then
    echo "[entrypoint] 注入 WebSocket 配置到 background.js..."
    # 拼接客户端标识到 WS_URL query params
    CONTAINER_NAME="${CLIENT_NAME:-$(hostname)}"
    SEPARATOR="?"
    echo "$WS_URL" | grep -q '?' && SEPARATOR="&"
    FULL_WS_URL="${WS_URL}${SEPARATOR}name=${CONTAINER_NAME}&source=docker"
    # sed 替换中 & 是特殊字符，需转义
    ESCAPED_URL=$(echo "$FULL_WS_URL" | sed 's/&/\\&/g')
    sed -i "s|let wsUrl = '';|let wsUrl = '${ESCAPED_URL}';|" "$EXTENSION_DIR/background.js"
    sed -i "s|let authKey = '';|let authKey = '${AUTH_KEY:-}';|" "$EXTENSION_DIR/background.js"
    echo "[entrypoint] WS_URL=$FULL_WS_URL"
    echo "[entrypoint] AUTH_KEY=${AUTH_KEY:+(已设置)}"
    grep "let wsUrl" "$EXTENSION_DIR/background.js" | head -1
else
    echo "[entrypoint] ⚠️ 未设置 WS_URL 环境变量！"
fi

# 2. 启动 dbus
if [ -x /usr/bin/dbus-daemon ]; then
    eval $(dbus-launch --sh-syntax 2>/dev/null) || true
fi

# 3. 启动虚拟显示器
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
XVFB_PID=$!
sleep 1

if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "[entrypoint] ❌ Xvfb 启动失败！"
    exit 1
fi
echo "[entrypoint] ✅ Xvfb 已启动 (PID=$XVFB_PID)"

# 4. 清理旧用户数据
USER_DATA_DIR="/home/chrome/data"
rm -rf "$USER_DATA_DIR"
mkdir -p "$USER_DATA_DIR"

# 5. 启动 Chromium
CHROME_ARGS=(
    --no-sandbox
    --disable-dev-shm-usage
    --disable-gpu
    --no-first-run
    --no-default-browser-check
    --disable-background-networking
    --disable-sync
    --disable-translate
    --disable-extensions-except="$EXTENSION_DIR"
    --load-extension="$EXTENSION_DIR"
    --user-data-dir="$USER_DATA_DIR"
    --lang=zh-CN
    "https://labs.google/fx"
)

echo "[entrypoint] 启动 Chromium ..."
chromium "${CHROME_ARGS[@]}" &>/dev/null &
CHROME_PID=$!

echo "[entrypoint] ✅ Chromium 已启动 (PID=$CHROME_PID)"
echo "[entrypoint] ✅ 等待插件连接..."

# 6. 进程监控
trap "kill $CHROME_PID $XVFB_PID 2>/dev/null; exit 0" SIGTERM SIGINT

while true; do
    if ! kill -0 $CHROME_PID 2>/dev/null; then
        echo "[entrypoint] ⚠️ Chromium 退出，2秒后重启..."
        sleep 2
        chromium "${CHROME_ARGS[@]}" &>/dev/null &
        CHROME_PID=$!
        echo "[entrypoint] ✅ Chromium 已重启 (PID=$CHROME_PID)"
    fi
    sleep 10
done
