#!/bin/sh
set -eu

resolve_browser_path() {
python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    print(p.chromium.executable_path or "")
PY
}

if [ -z "${BROWSER_EXECUTABLE_PATH:-}" ] || [ ! -x "${BROWSER_EXECUTABLE_PATH:-}" ]; then
    detected_browser_path="$(resolve_browser_path 2>/dev/null | tr -d '\r' | tail -n 1)"
    if [ -n "${detected_browser_path}" ] && [ -x "${detected_browser_path}" ]; then
        export BROWSER_EXECUTABLE_PATH="${detected_browser_path}"
    fi
fi

echo "[entrypoint] starting flow2api (headless browser mode)"
if [ -n "${BROWSER_EXECUTABLE_PATH:-}" ] && [ -x "${BROWSER_EXECUTABLE_PATH}" ]; then
    echo "[entrypoint] browser executable: ${BROWSER_EXECUTABLE_PATH}"
    "${BROWSER_EXECUTABLE_PATH}" --version || true
else
    echo "[entrypoint] warning: no valid browser executable found for personal/browser captcha" >&2
fi

exec python main.py
