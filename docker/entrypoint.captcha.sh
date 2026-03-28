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

# 4. 启动窗口管理器（xdotool 需要窗口管理器来正确分发事件）
fluxbox &>/dev/null &
echo "[entrypoint] ✅ Fluxbox 已启动"

# 5. 清理旧用户数据
USER_DATA_DIR="/home/chrome/data"
rm -rf "$USER_DATA_DIR"
mkdir -p "$USER_DATA_DIR"

# 6. 启动 CloakBrowser（源码级指纹伪装的 Chromium）
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
)

# 代理配置（支持 http/socks5，如 http://user:pass@host:port）
if [ -n "$PROXY_URL" ]; then
    CHROME_ARGS+=(--proxy-server="$PROXY_URL")
    # 内网地址绕过代理（Docker 网络 + localhost）
    CHROME_ARGS+=(--proxy-bypass-list="localhost;127.0.0.1;flow2api;10.0.0.0/8;172.16.0.0/12;192.168.0.0/16")
    echo "[entrypoint] ✅ 代理已配置: $PROXY_URL"
fi

CHROME_ARGS+=("https://labs.google/fx")

echo "[entrypoint] 启动 CloakBrowser ..."
cloakbrowser-bin "${CHROME_ARGS[@]}" &>/dev/null &
CHROME_PID=$!

echo "[entrypoint] ✅ CloakBrowser 已启动 (PID=$CHROME_PID)"

# 7. 启动行为模拟（xdotool 产生 OS 级真实事件，isTrusted=true）
(
    sleep 8  # 等待 Chromium 窗口和页面加载
    echo "[entrypoint] ✅ 行为模拟已启动"

    while true; do
        # 随机选择一种行为
        ACTION=$((RANDOM % 10))

        case $ACTION in
            0|1|2)
                # 鼠标移动到随机位置
                X=$((RANDOM % 1000 + 100))
                Y=$((RANDOM % 500 + 100))
                xdotool mousemove --sync "$X" "$Y" 2>/dev/null || true
                sleep 0.2
                # 小幅抖动
                DX=$((RANDOM % 30 - 15))
                DY=$((RANDOM % 30 - 15))
                xdotool mousemove_relative -- "$DX" "$DY" 2>/dev/null || true
                ;;
            3|4)
                # 滚动页面
                TIMES=$((RANDOM % 3 + 1))
                for _ in $(seq 1 $TIMES); do
                    xdotool click --clearmodifiers $(( RANDOM % 2 == 0 ? 4 : 5 )) 2>/dev/null || true
                    sleep 0.3
                done
                ;;
            5)
                # 点击页面空白区域
                X=$((RANDOM % 800 + 200))
                Y=$((RANDOM % 400 + 150))
                xdotool mousemove --sync "$X" "$Y" 2>/dev/null || true
                sleep 0.1
                xdotool click 1 2>/dev/null || true
                ;;
            6)
                # 按 Tab 键（模拟浏览元素）
                xdotool key Tab 2>/dev/null || true
                ;;
            7)
                # 按方向键
                ARROW=$(echo "Up Down Left Right" | tr ' ' '\n' | shuf -n 1)
                xdotool key "$ARROW" 2>/dev/null || true
                ;;
            *)
                # 移动鼠标到页面内再移出（模拟浏览后离开）
                xdotool mousemove --sync $((RANDOM % 600 + 300)) $((RANDOM % 300 + 200)) 2>/dev/null || true
                sleep 0.5
                xdotool mousemove --sync 10 10 2>/dev/null || true
                ;;
        esac

        # 随机间隔 2-6 秒
        DELAY=$((RANDOM % 5 + 2))
        sleep "$DELAY"
    done
) &
BEHAVIOR_PID=$!

echo "[entrypoint] ✅ 等待插件连接..."

# 8. 进程监控
trap "kill $CHROME_PID $XVFB_PID $BEHAVIOR_PID 2>/dev/null; exit 0" SIGTERM SIGINT

while true; do
    if ! kill -0 $CHROME_PID 2>/dev/null; then
        echo "[entrypoint] ⚠️ CloakBrowser 退出，2秒后重启..."
        sleep 2
        cloakbrowser-bin "${CHROME_ARGS[@]}" &>/dev/null &
        CHROME_PID=$!
        echo "[entrypoint] ✅ CloakBrowser 已重启 (PID=$CHROME_PID)"
    fi
    sleep 10
done
