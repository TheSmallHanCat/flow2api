"""
基于 RT 的本地 reCAPTCHA 打码服务 (终极闭环版 - 无 fake_useragent 纯净版)
支持：自动刷新 Session Token、外部触发指纹切换、死磕重试
"""

import os
import sys
import subprocess
import signal

# 修复 Windows 上 playwright 的 asyncio 兼容性问题
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

import asyncio
import time
import re
import random
import uuid
import hashlib
import json
from copy import deepcopy
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple, Union
from datetime import datetime
from urllib.parse import urlparse, parse_qsl

from ..core.logger import debug_logger
from ..core.browser_runtime_status import (
    fail_runtime_prepare,
    finish_runtime_prepare,
    progress_runtime_prepare,
)
from ..core.config import config
from .recaptcha_runtime import TRUSTED_RECAPTCHA_HOOK_SOURCE, build_recaptcha_execute_script
from .browser_cookie_utils import build_browser_cookie_targets, build_cookie_signature, is_partitioned_cookie


# ==================== Docker 环境检测 ====================
def _is_running_in_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    # 方法1: 检查 /.dockerenv 文件
    if os.path.exists("/.dockerenv"):
        return True
    # 方法2: 检查 cgroup
    try:
        with open("/proc/1/cgroup", "r") as f:
            content = f.read()
            if "docker" in content or "kubepods" in content or "containerd" in content:
                return True
    except:
        pass
    # 方法3: 检查环境变量
    if os.environ.get("DOCKER_CONTAINER") or os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


def _is_truthy_env(name: str) -> bool:
    """判断环境变量是否为 true。"""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DOCKER_HEADED = _is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA") or _is_truthy_env(
    "ALLOW_DOCKER_BROWSER_CAPTCHA"
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED

BROWSER_ENVIRONMENT_PATCH_MARKER = "__flow2apiBrowserEnvironmentPatchInstalled__"


# ==================== playwright 自动安装 ====================
def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """运行 pip install 命令"""
    cmd = [sys.executable, "-m", "pip", "install", package]
    if use_mirror:
        cmd.extend(["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
    
    try:
        debug_logger.log_info(f"[BrowserCaptcha] 正在安装 {package}...")
        progress_runtime_prepare("browser", f"[BrowserCaptcha] 正在安装 {package}...")
        print(f"[BrowserCaptcha] 正在安装 {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ {package} 安装成功")
            print(f"[BrowserCaptcha] ✅ {package} 安装成功")
            return True
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] {package} 安装失败: {result.stderr[:200]}"
            )
            fail_runtime_prepare(
                "browser", f"{package} 安装失败，请检查网络或 Python 环境。"
            )
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装异常: {e}")
        fail_runtime_prepare("browser", f"{package} 安装异常: {e}")
        return False


def _run_playwright_install(use_mirror: bool = False) -> bool:
    """安装 playwright chromium 浏览器"""
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    env = os.environ.copy()
    
    if use_mirror:
        # 使用国内镜像
        env["PLAYWRIGHT_DOWNLOAD_HOST"] = "https://npmmirror.com/mirrors/playwright"
    
    try:
        debug_logger.log_info("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        progress_runtime_prepare("browser", "正在安装 chromium 浏览器，请稍候...")
        print("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, env=env
        )
        if result.returncode == 0:
            debug_logger.log_info("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            progress_runtime_prepare(
                "browser", "chromium 浏览器安装成功，正在完成收尾检查..."
            )
            print("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            return True
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] chromium 安装失败: {result.stderr[:200]}"
            )
            fail_runtime_prepare(
                "browser", "chromium 浏览器安装失败，请稍后重试或手动安装。"
            )
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装异常: {e}")
        fail_runtime_prepare("browser", f"chromium 浏览器安装异常: {e}")
        return False


def _ensure_playwright_installed() -> bool:
    """确保 playwright 已安装"""
    try:
        import playwright

        debug_logger.log_info("[BrowserCaptcha] playwright 已安装")
        return True
    except ImportError:
        pass
    
    debug_logger.log_info("[BrowserCaptcha] playwright 未安装，开始自动安装...")
    progress_runtime_prepare(
        "browser", "[BrowserCaptcha] playwright 未安装，开始自动安装..."
    )
    print("[BrowserCaptcha] playwright 未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_pip_install("playwright", use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    progress_runtime_prepare(
        "browser", "[BrowserCaptcha] 官方源安装失败，尝试国内镜像..."
    )
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install("playwright", use_mirror=True):
        return True
    
    debug_logger.log_error(
        "[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright"
    )
    fail_runtime_prepare(
        "browser",
        "[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright",
    )
    print(
        "[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright"
    )
    return False


def _ensure_browser_installed() -> bool:
    """确保 chromium 浏览器已安装"""
    try:
        detect_script = (
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    print(p.chromium.executable_path or '')\n"
        )
        env = os.environ.copy()
        env.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0") or "0",
        )
        result = subprocess.run(
            [sys.executable, "-c", detect_script],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        browser_path = (result.stdout or "").strip().splitlines()
        browser_path = browser_path[-1].strip() if browser_path else ""
        if result.returncode == 0 and browser_path and os.path.exists(browser_path):
            debug_logger.log_info(
                f"[BrowserCaptcha] chromium 浏览器已安装: {browser_path}"
            )
            finish_runtime_prepare(
                "browser", "检测到 chromium 浏览器已安装，正在准备实例..."
            )
            return True
    except Exception as e:
        debug_logger.log_info(f"[BrowserCaptcha] 检测浏览器时出错: {e}")
    
    debug_logger.log_info("[BrowserCaptcha] chromium 浏览器未安装，开始自动安装...")
    progress_runtime_prepare(
        "browser", "[BrowserCaptcha] chromium 浏览器未安装，开始自动安装..."
    )
    print("[BrowserCaptcha] chromium 浏览器未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_playwright_install(use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    progress_runtime_prepare(
        "browser", "[BrowserCaptcha] 官方源安装失败，尝试国内镜像..."
    )
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_playwright_install(use_mirror=True):
        return True
    
    debug_logger.log_error(
        "[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium"
    )
    fail_runtime_prepare(
        "browser",
        "[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium",
    )
    print(
        "[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium"
    )
    return False


# 尝试导入 playwright
async_playwright = None
Route = None
BrowserContext = None
PLAYWRIGHT_AVAILABLE = False

if DOCKER_HEADED_BLOCKED:
    debug_logger.log_warning(
        "[BrowserCaptcha] 检测到 Docker 环境，默认禁用有头浏览器打码。"
        "如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb。"
    )
    print("[BrowserCaptcha] ⚠️ 检测到 Docker 环境，默认禁用有头浏览器打码")
    print(
        "[BrowserCaptcha] 如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb"
    )
else:
    if IS_DOCKER and ALLOW_DOCKER_HEADED:
        debug_logger.log_warning(
            "[BrowserCaptcha] Docker 有头浏览器打码白名单已启用，请确保 DISPLAY/Xvfb 可用"
        )
        print("[BrowserCaptcha] ✅ Docker 有头浏览器打码白名单已启用")
    if _ensure_playwright_installed():
        try:
            from playwright.async_api import async_playwright, Route, BrowserContext

            PLAYWRIGHT_AVAILABLE = True
            # 检查并安装浏览器
            _ensure_browser_installed()
        except ImportError as e:
            debug_logger.log_error(f"[BrowserCaptcha] playwright 导入失败: {e}")
            print(f"[BrowserCaptcha] ❌ playwright 导入失败: {e}")


# 配置
LABS_URL = "https://flow.google.com"
BROWSER_SESSION_COOKIE_TARGET_URLS = (
    "https://flow.google.com/",
    "https://www.google.com/",
    "https://www.recaptcha.net/",
)


def _resolve_flow_page_warmup_seconds(value: Any) -> float:
    """Resolve the Flow page warmup wait while preserving an explicit zero."""
    if value is None:
        value = 6.0
    try:
        return max(0.0, min(30.0, float(value)))
    except (TypeError, ValueError):
        return 6.0


# ==========================================
# 代理解析工具函数
# ==========================================
def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    """解析代理URL（支持 socks5h://，Playwright 中按 socks5 处理）"""
    if not proxy_url:
        return None
    if not re.match(r"^(http|https|socks5h?|socks5)://", proxy_url):
        proxy_url = f"http://{proxy_url}"
    match = re.match(
        r"^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$",
        proxy_url,
    )
    if match:
        protocol, username, password, host, port = match.groups()
        browser_protocol = "socks5" if protocol.startswith("socks5") else protocol
        proxy_config = {"server": f"{browser_protocol}://{host}:{port}"}
        if username and password:
            proxy_config["username"] = username
            proxy_config["password"] = password
        return proxy_config
    return None


def normalize_browser_proxy_url(proxy_url: str) -> tuple[Optional[str], Optional[str]]:
    """将浏览器代理标准化为 Playwright/Chromium 可接受的格式。

    Chromium 不支持带账号密码的 socks5/socks5h 代理认证。
    对于 `socks5(h)://user:pass@host:port`，自动降级为 `http://user:pass@host:port`，
    方便兼容同时提供 HTTP/SOCKS5 双入口的代理服务商。

    Returns:
        (normalized_proxy_url, warning_message)
    """
    if not proxy_url:
        return None, None

    proxy_url = proxy_url.strip()
    match = re.match(
        r"^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$",
        proxy_url,
    )
    if not match:
        if not re.match(r"^(http|https|socks5h?|socks5)://", proxy_url):
            proxy_url = f"http://{proxy_url}"
        return proxy_url, None

    protocol, username, password, host, port = match.groups()
    if protocol.startswith("socks5") and username and password:
        normalized = f"http://{username}:{password}@{host}:{port}"
        warning = (
            f"检测到带认证的 {protocol.upper()} 代理。"
            "Chromium 不支持 socks5 用户名密码认证，"
            f"已自动改用 HTTP 代理启动浏览器: http://{host}:{port}"
        )
        return normalized, warning

    if protocol == "socks5h":
        proxy_url = f"socks5://{host}:{port}"

    return proxy_url, None


def validate_browser_proxy_url(proxy_url: str) -> tuple[bool, str]:
    if not proxy_url:
        return True, None
    normalized_proxy_url, _ = normalize_browser_proxy_url(proxy_url.strip())
    parsed = parse_proxy_url(normalized_proxy_url)
    if not parsed:
        return False, "代理格式错误"
    return True, None


class TokenBrowser:
    """共享浏览器槽位：复用 Chromium context，失败或空闲超时后回收。"""

    # UA pool updated on 2026-03-01 from browsers that scored >= 0.3.
    UA_LIST = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36 Edg/131.0.2903.146",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36 Edg/130.0.2849.142",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36 Edg/129.0.2792.124",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36 Edg/128.0.2739.111",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36 Edg/131.0.2903.146",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36 Edg/130.0.2849.142",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36 Edg/129.0.2792.124",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36 Edg/128.0.2739.111",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.3; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.2; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.1; rv:131.0) Gecko/20100101 Firefox/131.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.163 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.260 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 12; M2102J20SG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 11; M2012K11AC) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.260 Mobile Safari/537.36 EdgA/131.0.2903.146",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Mobile Safari/537.36 EdgA/130.0.2849.142",
        "Mozilla/5.0 (Linux; Android 12; M2102J20SG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Mobile Safari/537.36 EdgA/129.0.2792.124",
        "Mozilla/5.0 (Linux; Android 11; M2012K11AC) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Mobile Safari/537.36 EdgA/128.0.2739.111",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/132.0.6834.163 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; SM-S9110) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/27.0 Chrome/130.0.6723.172 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 12; SM-G9910) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/128.0.6613.186 Mobile Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/132.0.6834.95 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.112 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/132.2957.171 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/131.2903.146 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 OPR/117.0.0.0",
    ]
    
    # 分辨率池
    RESOLUTIONS = [
        (1920, 1080),
        (2560, 1440),
        (3840, 2160),
        (1366, 768),
        (1536, 864),
        (1600, 900),
        (1280, 720),
        (1360, 768),
        (1920, 1200),
        (1440, 900),
        (1680, 1050),
        (1280, 800),
        (2560, 1600),
        (2880, 1800),
        (3024, 1890),
        (3456, 2160),
        (1280, 1024),
        (1024, 768),
        (1400, 1050),
        (1920, 1280),
        (2736, 1824),
        (2880, 1920),
        (3000, 2000),
        (2256, 1504),
        (2496, 1664),
        (3240, 2160),
        (3200, 1800),
        (2304, 1440),
        (1800, 1200),
    ]
    
    def __init__(self, token_id: int, user_data_dir: str, db=None):
        self.token_id = token_id
        self.user_data_dir = user_data_dir
        self.db = db
        self._semaphore = asyncio.Semaphore(
            1
        )  # Only one active solve task is allowed per slot.
        self._solve_count = 0
        self._error_count = 0
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self._browser_proxy_active = False
        # Delay browser release after solve and track it by request_ref.
        self._pending_release_entries: Dict[str, Dict[str, Any]] = {}
        self._pending_release_lock = asyncio.Lock()
        # Browser mode keeps a shared in-memory browser instead of a persistent profile.
        self._shared_browser_lock = asyncio.Lock()
        self._shared_playwright = None
        self._shared_browser = None
        self._shared_context = None
        self._shared_keepalive_page = None
        self._shared_browser_pid: Optional[int] = None
        self._shared_bound_token_id: Optional[int] = None
        self._shared_bound_cookie_signature: Optional[str] = None
        self._pid_dir = os.path.join(os.getcwd(), "tmp", "browser_pids")
        self._pid_file = os.path.join(self._pid_dir, f"slot_{self.token_id}.pid")
        os.makedirs(self._pid_dir, exist_ok=True)
        self._shared_proxy_url: Optional[str] = None
        self._shared_launch_count = 0
        self._shared_reuse_count = 0
        self._runtime_generation = 0
        self._consecutive_browser_failures = 0
        self._solve_inflight = 0
        self._last_idle_since = time.monotonic()
        # harvest 模式状态：页面原生 StreamChat 请求体与会话 id
        self._harvest_streamchat_post: Optional[str] = None
        self._last_harvest_session_id: Optional[str] = None
        self._refresh_browser_profile()

    def _refresh_browser_profile(self):
        """Refresh the in-memory browser fingerprint profile."""
        base_w, base_h = random.choice(self.RESOLUTIONS)
        self._profile_user_agent = random.choice(self.UA_LIST)
        self._profile_viewport = {
            "width": base_w,
            "height": base_h - random.randint(0, 80),
        }
        self._profile_env_seed = hashlib.sha256(
            f"{self.token_id}:{time.time_ns()}:{uuid.uuid4().hex}".encode("utf-8")
        ).hexdigest()
        self._profile_hardware_concurrency = random.choice([4, 6, 8, 8, 12, 16])
        self._profile_device_memory = random.choice([4, 8, 8, 16])
        self._profile_gpu = random.choice(
            [
            {
                "vendor": "Google Inc. (Intel)",
                "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "unmaskedVendor": "Intel Inc.",
                "unmaskedRenderer": "Intel(R) UHD Graphics 620",
            },
            {
                "vendor": "Google Inc. (NVIDIA)",
                "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "unmaskedVendor": "NVIDIA Corporation",
                "unmaskedRenderer": "NVIDIA GeForce GTX 1650",
            },
            {
                "vendor": "Google Inc. (AMD)",
                "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "unmaskedVendor": "ATI Technologies Inc.",
                "unmaskedRenderer": "AMD Radeon(TM) Graphics",
            },
            ]
        )

    def _build_browser_environment_patch_config(self) -> Dict[str, Any]:
        viewport = dict(self._profile_viewport or {})
        width = int(viewport.get("width") or 1366)
        height = int(viewport.get("height") or 768)
        seed = str(getattr(self, "_profile_env_seed", "") or "")
        if not seed:
            seed = hashlib.sha256(
                f"{self.token_id}:{width}:{height}".encode("utf-8")
            ).hexdigest()
        digest = hashlib.sha256(seed.encode("utf-8")).digest()

        return {
            "seed": seed[:16],
            "navigator": {
                "language": "en-US",
                "languages": ["en-US", "en"],
                "hardwareConcurrency": int(
                    getattr(self, "_profile_hardware_concurrency", 8) or 8
                ),
                "deviceMemory": int(getattr(self, "_profile_device_memory", 8) or 8),
                "maxTouchPoints": 0,
                "pdfViewerEnabled": True,
                "webdriver": False,
            },
            "screen": {
                "width": width,
                "height": height,
                "availWidth": width,
                "availHeight": max(640, height - 40),
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "window": {
                "innerWidth": width,
                "innerHeight": height,
                "outerWidth": min(width + 16, width),
                "outerHeight": height + 88,
                "devicePixelRatio": 1,
            },
            "network": {
                "type": "wifi",
                "effectiveType": "4g",
                "rtt": 45 + int(digest[0] % 75),
                "downlink": round(6.0 + (int(digest[1] % 95) / 10.0), 1),
                "saveData": False,
            },
            "permissions": {
                "geolocation": "prompt",
                "notifications": "denied",
                "camera": "denied",
                "microphone": "denied",
                "display-capture": "denied",
            },
            "graphics": dict(getattr(self, "_profile_gpu", {}) or {}),
            "storage": {
                "quota": 120000000000 + int(digest[2] % 20) * 1000000000,
                "usage": 8000000 + int(digest[3] % 20) * 250000,
                "persisted": False,
            },
            "canvas": {
                "rgba": [
                    (int(digest[4]) % 5) - 2 or 1,
                    (int(digest[5]) % 5) - 2 or 1,
                    (int(digest[6]) % 5) - 2 or 1,
                    0,
                ],
                "pixelStep": 13 + (int(digest[7]) % 11),
            },
            "audio": {
                "floatDelta": round(
                    ((int(digest[8]) / 255.0) * 2.0 - 1.0) * 0.00003, 8
                ),
                "byteDelta": (int(digest[9]) % 5) - 2 or 1,
                "stride": 17 + (int(digest[10]) % 13),
            },
            "mediaDevices": {
                "devices": [
                    {
                        "kind": "audioinput",
                        "deviceId": f"aid-{seed[:12]}",
                        "groupId": f"grp-{seed[12:20]}",
                        "label": "",
                    },
                    {
                        "kind": "videoinput",
                        "deviceId": f"vid-{seed[20:32]}",
                        "groupId": f"grp-{seed[12:20]}",
                        "label": "",
                    },
                    {
                        "kind": "audiooutput",
                        "deviceId": f"aod-{seed[32:44]}",
                        "groupId": f"grp-{seed[44:52]}",
                        "label": "Default Audio Output",
                    },
                ],
            },
            "plugins": [
                {
                    "name": "PDF Viewer",
                    "filename": "internal-pdf-viewer",
                    "description": "Portable Document Format",
                    "mimeTypes": ["application/pdf", "text/pdf"],
                },
                {
                    "name": "Chrome PDF Viewer",
                    "filename": "internal-pdf-viewer",
                    "description": "Portable Document Format",
                    "mimeTypes": ["application/pdf", "text/pdf"],
                },
                {
                    "name": "Chromium PDF Viewer",
                    "filename": "internal-pdf-viewer",
                    "description": "Portable Document Format",
                    "mimeTypes": ["application/pdf", "text/pdf"],
                },
            ],
            "mimeTypes": [
                {
                    "type": "application/pdf",
                    "suffixes": "pdf",
                    "description": "Portable Document Format",
                    "pluginName": "PDF Viewer",
                },
                {
                    "type": "text/pdf",
                    "suffixes": "pdf",
                    "description": "Portable Document Format",
                    "pluginName": "PDF Viewer",
                },
            ],
        }

    def _build_browser_environment_patch_source(self) -> str:
        config_json = json.dumps(
            self._build_browser_environment_patch_config(),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return r"""
(() => {
    const marker = __MARKER_JSON__;
    if (window[marker]) {
        return;
    }
    const config = __CONFIG_JSON__;
    window[marker] = config.seed || true;

    const defineGetter = (target, key, getter, enumerable = true) => {
        if (!target) return;
        try {
            Object.defineProperty(target, key, { configurable: true, enumerable, get: getter });
        } catch (e) {}
    };
    const defineValue = (target, key, value, enumerable = false) => {
        if (!target) return;
        try {
            Object.defineProperty(target, key, { configurable: true, enumerable, writable: true, value });
        } catch (e) {
            try { target[key] = value; } catch (err) {}
        }
    };
    const makeNative = (fn, name) => {
        try {
            defineValue(fn, "name", name || fn.name || "");
            defineValue(fn, "toString", () => `function ${name || fn.name || ""}() { [native code] }`);
        } catch (e) {}
        return fn;
    };
    const clone = (value) => {
        try { return JSON.parse(JSON.stringify(value)); } catch (e) { return value; }
    };

    const nav = config.navigator || {};
    const navProto = Navigator.prototype;
    defineGetter(navProto, "webdriver", () => undefined);
    defineGetter(navigator, "webdriver", () => undefined);
    defineGetter(navProto, "language", () => nav.language || "en-US");
    defineGetter(navigator, "language", () => nav.language || "en-US");
    defineGetter(navProto, "languages", () => clone(nav.languages || ["en-US", "en"]));
    defineGetter(navigator, "languages", () => clone(nav.languages || ["en-US", "en"]));
    defineGetter(navProto, "hardwareConcurrency", () => Number(nav.hardwareConcurrency || 8));
    defineGetter(navigator, "hardwareConcurrency", () => Number(nav.hardwareConcurrency || 8));
    defineGetter(navProto, "deviceMemory", () => Number(nav.deviceMemory || 8));
    defineGetter(navigator, "deviceMemory", () => Number(nav.deviceMemory || 8));
    defineGetter(navProto, "maxTouchPoints", () => Number(nav.maxTouchPoints || 0));
    defineGetter(navigator, "maxTouchPoints", () => Number(nav.maxTouchPoints || 0));
    defineGetter(navProto, "pdfViewerEnabled", () => nav.pdfViewerEnabled !== false);
    defineGetter(navigator, "pdfViewerEnabled", () => nav.pdfViewerEnabled !== false);
    defineGetter(navProto, "cookieEnabled", () => true);
    defineGetter(navigator, "cookieEnabled", () => true);
    defineGetter(navProto, "onLine", () => true);
    defineGetter(navigator, "onLine", () => true);

    const screenProfile = config.screen || {};
    for (const key of ["width", "height", "availWidth", "availHeight", "colorDepth", "pixelDepth"]) {
        if (screenProfile[key] !== undefined) {
            defineGetter(Screen.prototype, key, () => Number(screenProfile[key]));
            defineGetter(screen, key, () => Number(screenProfile[key]));
        }
    }
    const win = config.window || {};
    for (const key of ["innerWidth", "innerHeight", "outerWidth", "outerHeight", "devicePixelRatio"]) {
        if (win[key] !== undefined) {
            defineGetter(window, key, () => Number(win[key]));
        }
    }

    defineGetter(Document.prototype, "visibilityState", () => "visible");
    defineGetter(document, "visibilityState", () => "visible");
    defineGetter(Document.prototype, "webkitVisibilityState", () => "visible");
    defineGetter(document, "webkitVisibilityState", () => "visible");
    defineGetter(Document.prototype, "hidden", () => false);
    defineGetter(document, "hidden", () => false);
    defineGetter(Document.prototype, "webkitHidden", () => false);
    defineGetter(document, "webkitHidden", () => false);
    try { document.hasFocus = makeNative(() => true, "hasFocus"); } catch (e) {}
    try { if (typeof window.focus === "function") window.focus(); } catch (e) {}

    const makeArrayLike = (items, namedKey) => {
        const arr = [];
        for (const item of items || []) {
            arr.push(item);
        }
        defineValue(arr, "item", makeNative((index) => arr[Number(index)] || null, "item"));
        defineValue(arr, "namedItem", makeNative((name) => arr.find((item) => item && item[namedKey] === name) || null, "namedItem"));
        defineValue(arr, "refresh", makeNative(() => undefined, "refresh"));
        for (const item of arr) {
            if (item && item[namedKey]) {
                try { Object.defineProperty(arr, item[namedKey], { configurable: true, enumerable: false, value: item }); } catch (e) {}
            }
        }
        return arr;
    };

    const mimeTypes = makeArrayLike((config.mimeTypes || []).map((item) => ({
        type: item.type,
        suffixes: item.suffixes || "",
        description: item.description || "",
        enabledPlugin: null,
    })), "type");
    const plugins = makeArrayLike((config.plugins || []).map((item) => {
        const plugin = {
            name: item.name,
            filename: item.filename || "",
            description: item.description || "",
            length: (item.mimeTypes || []).length,
        };
        (item.mimeTypes || []).forEach((type, index) => {
            const mt = mimeTypes.namedItem(type);
            if (mt) {
                mt.enabledPlugin = plugin;
                plugin[index] = mt;
                try { Object.defineProperty(plugin, type, { configurable: true, enumerable: false, value: mt }); } catch (e) {}
            }
        });
        plugin.item = makeNative((index) => plugin[Number(index)] || null, "item");
        plugin.namedItem = makeNative((name) => plugin[name] || null, "namedItem");
        return plugin;
    }), "name");
    defineGetter(navProto, "plugins", () => plugins);
    defineGetter(navigator, "plugins", () => plugins);
    defineGetter(navProto, "mimeTypes", () => mimeTypes);
    defineGetter(navigator, "mimeTypes", () => mimeTypes);

    const chromeObject = window.chrome || {};
    chromeObject.runtime = chromeObject.runtime || {
        PlatformOs: { MAC: "mac", WIN: "win", ANDROID: "android", CROS: "cros", LINUX: "linux", OPENBSD: "openbsd" },
        PlatformArch: { ARM: "arm", ARM64: "arm64", X86_32: "x86-32", X86_64: "x86-64" },
        PlatformNaclArch: { ARM: "arm", X86_32: "x86-32", X86_64: "x86-64" },
        RequestUpdateCheckStatus: { THROTTLED: "throttled", NO_UPDATE: "no_update", UPDATE_AVAILABLE: "update_available" },
        OnInstalledReason: { INSTALL: "install", UPDATE: "update", CHROME_UPDATE: "chrome_update", SHARED_MODULE_UPDATE: "shared_module_update" },
    };
    chromeObject.app = chromeObject.app || { isInstalled: false };
    chromeObject.csi = chromeObject.csi || makeNative(() => ({ startE: Date.now(), onloadT: Date.now(), pageT: 1, tran: 15 }), "csi");
    chromeObject.loadTimes = chromeObject.loadTimes || makeNative(() => ({
        requestTime: Date.now() / 1000,
        startLoadTime: Date.now() / 1000,
        commitLoadTime: Date.now() / 1000,
        finishDocumentLoadTime: Date.now() / 1000,
        finishLoadTime: Date.now() / 1000,
        firstPaintTime: Date.now() / 1000,
        firstPaintAfterLoadTime: 0,
        navigationType: "Other",
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
        npnNegotiatedProtocol: "h2",
        connectionInfo: "h2",
    }), "loadTimes");
    defineValue(window, "chrome", chromeObject, true);

    const permissions = config.permissions || {};
    if (navigator.permissions && typeof navigator.permissions.query === "function") {
        const originalQuery = navigator.permissions.query.bind(navigator.permissions);
        defineValue(navigator.permissions, "query", makeNative((descriptor) => {
            const name = descriptor && descriptor.name ? String(descriptor.name) : "";
            if (Object.prototype.hasOwnProperty.call(permissions, name)) {
                const status = {
                    name,
                    state: permissions[name],
                    onchange: null,
                    addEventListener: makeNative(() => undefined, "addEventListener"),
                    removeEventListener: makeNative(() => undefined, "removeEventListener"),
                    dispatchEvent: makeNative(() => true, "dispatchEvent"),
                };
                return Promise.resolve(status);
            }
            return originalQuery(descriptor);
        }, "query"));
    }
    try {
        if (window.Notification) {
            defineGetter(Notification, "permission", () => permissions.notifications || "denied");
            defineValue(Notification, "requestPermission", makeNative((callback) => {
                const value = permissions.notifications || "denied";
                if (typeof callback === "function") callback(value);
                return Promise.resolve(value);
            }, "requestPermission"));
        }
    } catch (e) {}

    const connectionProfile = config.network || {};
    const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection || {};
    for (const [key, value] of Object.entries(connectionProfile)) {
        defineGetter(connection, key, () => value);
    }
    if (!connection.addEventListener) defineValue(connection, "addEventListener", makeNative(() => undefined, "addEventListener"));
    if (!connection.removeEventListener) defineValue(connection, "removeEventListener", makeNative(() => undefined, "removeEventListener"));
    defineGetter(navProto, "connection", () => connection);
    defineGetter(navigator, "connection", () => connection);

    const mediaDevicesProfile = config.mediaDevices || {};
    const mediaDevices = navigator.mediaDevices || {};
    if (!mediaDevices.enumerateDevices) {
        defineValue(mediaDevices, "enumerateDevices", makeNative(() => Promise.resolve(clone(mediaDevicesProfile.devices || [])), "enumerateDevices"));
    }
    if (!mediaDevices.addEventListener) defineValue(mediaDevices, "addEventListener", makeNative(() => undefined, "addEventListener"));
    if (!mediaDevices.removeEventListener) defineValue(mediaDevices, "removeEventListener", makeNative(() => undefined, "removeEventListener"));
    defineGetter(navProto, "mediaDevices", () => mediaDevices);
    defineGetter(navigator, "mediaDevices", () => mediaDevices);

    const storageProfile = config.storage || {};
    if (navigator.storage) {
        if (typeof navigator.storage.estimate !== "function") {
            defineValue(navigator.storage, "estimate", makeNative(() => Promise.resolve({
                quota: Number(storageProfile.quota || 120000000000),
                usage: Number(storageProfile.usage || 8000000),
            }), "estimate"));
        }
        if (typeof navigator.storage.persisted !== "function") {
            defineValue(navigator.storage, "persisted", makeNative(() => Promise.resolve(!!storageProfile.persisted), "persisted"));
        }
    }

    const graphics = config.graphics || {};
    const patchWebGL = (proto) => {
        if (!proto || typeof proto.getParameter !== "function") return;
        const originalGetParameter = proto.getParameter;
        proto.getParameter = function(parameter) {
            try {
                const debugInfo = this.getExtension && this.getExtension("WEBGL_debug_renderer_info");
                if (debugInfo) {
                    if (parameter === debugInfo.UNMASKED_VENDOR_WEBGL) return graphics.unmaskedVendor || graphics.vendor || "Intel Inc.";
                    if (parameter === debugInfo.UNMASKED_RENDERER_WEBGL) return graphics.unmaskedRenderer || graphics.renderer || "Intel(R) UHD Graphics";
                }
                if (parameter === 37445) return graphics.unmaskedVendor || graphics.vendor || "Intel Inc.";
                if (parameter === 37446) return graphics.unmaskedRenderer || graphics.renderer || "Intel(R) UHD Graphics";
                if (parameter === 7936) return graphics.vendor || "Google Inc. (Intel)";
                if (parameter === 7937) return graphics.renderer || "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)";
            } catch (e) {}
            return originalGetParameter.apply(this, arguments);
        };
        makeNative(proto.getParameter, "getParameter");
    };
    try {
        patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
        patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
    } catch (e) {}

    try {
        if (window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype) {
            const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
            if (typeof originalGetImageData === "function") {
                CanvasRenderingContext2D.prototype.getImageData = function(...args) {
                    const imageData = originalGetImageData.apply(this, args);
                    try {
                        const data = imageData && imageData.data;
                        const rgba = config.canvas && config.canvas.rgba || [1, -1, 1, 0];
                        const step = Math.max(4, Number(config.canvas && config.canvas.pixelStep || 17) * 4);
                        for (let i = 0; data && i < data.length; i += step) {
                            data[i] = Math.max(0, Math.min(255, data[i] + rgba[0]));
                            data[i + 1] = Math.max(0, Math.min(255, data[i + 1] + rgba[1]));
                            data[i + 2] = Math.max(0, Math.min(255, data[i + 2] + rgba[2]));
                        }
                    } catch (e) {}
                    return imageData;
                };
                makeNative(CanvasRenderingContext2D.prototype.getImageData, "getImageData");
            }
        }
    } catch (e) {}

    try {
        if (window.AudioBuffer && AudioBuffer.prototype && typeof AudioBuffer.prototype.getChannelData === "function") {
            const originalGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(...args) {
                const channelData = originalGetChannelData.apply(this, args);
                try {
                    const stamp = "__flow2apiAudioPatch_" + (config.seed || "");
                    if (channelData && channelData.length && !channelData[stamp]) {
                        const stride = Math.max(1, Number(config.audio && config.audio.stride || 23));
                        const delta = Number(config.audio && config.audio.floatDelta || 0);
                        for (let i = 0; i < channelData.length; i += stride) {
                            channelData[i] = channelData[i] + delta;
                        }
                        defineValue(channelData, stamp, true);
                    }
                } catch (e) {}
                return channelData;
            };
            makeNative(AudioBuffer.prototype.getChannelData, "getChannelData");
        }
    } catch (e) {}
})();
""".replace("__MARKER_JSON__", json.dumps(BROWSER_ENVIRONMENT_PATCH_MARKER)).replace(
            "__CONFIG_JSON__", config_json
        )

    async def _apply_browser_environment_patch(self, target, *, label: str) -> bool:
        if target is None:
            return False

        signature = str(getattr(self, "_profile_env_seed", "") or "")
        if getattr(target, "_flow2api_environment_patch_signature", None) == signature:
            return True

        try:
            await target.add_init_script(self._build_browser_environment_patch_source())
            try:
                target._flow2api_environment_patch_signature = signature
            except Exception:
                pass
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 已注入浏览器环境补齐脚本 ({label})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 注入浏览器环境补齐脚本失败 ({label}): "
                f"{type(e).__name__}: {str(e)[:200]}"
            )
            return False

    def _get_slot_marker(self) -> str:
        return f"--flow2api-browser-slot={self.token_id}"

    def _read_pid_file(self) -> Optional[int]:
        try:
            if not os.path.exists(self._pid_file):
                return None
            with open(self._pid_file, "r", encoding="utf-8") as handle:
                raw = (handle.read() or "").strip()
            return int(raw or "0") or None
        except Exception:
            return None

    def _write_pid_file(self, pid: Optional[int]):
        self._shared_browser_pid = pid
        try:
            if pid:
                with open(self._pid_file, "w", encoding="utf-8") as handle:
                    handle.write(str(pid))
            elif os.path.exists(self._pid_file):
                os.remove(self._pid_file)
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} failed to write PID file: {e}"
            )

    def _is_pid_running(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        try:
            if sys.platform.startswith("win"):
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return str(pid) in (result.stdout or "")
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _pid_matches_slot(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        marker = self._get_slot_marker()
        try:
            if sys.platform.startswith("win"):
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                command_line = (result.stdout or "").strip()
            else:
                cmdline_path = f"/proc/{pid}/cmdline"
                if not os.path.exists(cmdline_path):
                    return False
                with open(cmdline_path, "rb") as handle:
                    command_line = (
                        handle.read()
                        .decode("utf-8", errors="ignore")
                        .replace("\x00", " ")
                    )
            return marker in command_line
        except Exception:
            return False

    async def _wait_pid_exit(
        self, pid: Optional[int], timeout_seconds: float = 5.0
    ) -> bool:
        if not pid:
            return True
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not self._is_pid_running(pid):
                return True
            await asyncio.sleep(0.2)
        return not self._is_pid_running(pid)

    def _kill_pid(self, pid: Optional[int], reason: str):
        if not pid:
            return
        try:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} browser process is still alive; force-killing PID={pid}, reason={reason}"
            )
            if sys.platform.startswith("win"):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} failed to kill PID={pid}: {e}"
            )

    async def _cleanup_stale_slot_process(self):
        stale_pid = self._read_pid_file()
        if not stale_pid:
            return
        if not self._is_pid_running(stale_pid):
            self._write_pid_file(None)
            return
        if not self._pid_matches_slot(stale_pid):
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} PID file points to a process that does not belong to this slot; ignoring PID={stale_pid}"
            )
            self._write_pid_file(None)
            return
        self._kill_pid(stale_pid, reason="stale_slot_process")
        await self._wait_pid_exit(stale_pid, timeout_seconds=3)
        self._write_pid_file(None)

    def _extract_browser_pid(self, browser) -> Optional[int]:
        candidates = [
            lambda obj: obj._impl_obj._connection._transport._proc.pid,
            lambda obj: obj._impl_obj._connection._transport._proc.pid
            if obj and obj._impl_obj
            else None,
        ]
        for getter in candidates:
            try:
                pid = getter(browser)
                if isinstance(pid, int) and pid > 0:
                    return pid
            except Exception:
                continue
        return None

    async def _ensure_shared_keepalive_page(self):
        """Ensure the shared browser always keeps one keepalive page alive."""
        keepalive_page = self._shared_keepalive_page
        try:
            if keepalive_page and not keepalive_page.is_closed():
                return keepalive_page
        except Exception:
            keepalive_page = None

        if not self._shared_context:
            return None

        keepalive_page = await self._shared_context.new_page()
        await self._apply_browser_environment_patch(keepalive_page, label="keepalive")
        try:
            await keepalive_page.goto("about:blank", wait_until="load", timeout=5000)
        except Exception:
            pass
        self._shared_keepalive_page = keepalive_page
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} keepalive page created"
        )
        return keepalive_page

    def _build_flow_project_url(self, project_id: Optional[str]) -> str:
        project_id = (project_id or "").strip()
        if project_id:
            return f"{LABS_URL}/project/{project_id}"
        return LABS_URL

    @staticmethod
    def _normalize_token_key(token_id: Optional[int]) -> Optional[int]:
        try:
            normalized = int(token_id or 0)
        except Exception:
            normalized = 0
        return normalized if normalized > 0 else None

    @classmethod
    def _build_google_cookie_targets(cls, cookie_storage: str) -> List[Dict[str, Any]]:
        """按浏览器 Cookie 作用域展开登录态，兼容 JSON 与 Header 格式。"""
        cookies = build_browser_cookie_targets(
            cookie_storage,
            default_url="https://flow.google.com/",
            fallback_urls=[
                "https://flow.google.com/",
                "https://www.google.com/",
            ],
        )
        # URL cookies retain host-only scope. Root paths can use Playwright's
        # URL form; nonroot paths are imported through CDP without widening scope.
        for cookie in cookies:
            if cookie.get("url") and cookie.get("path") == "/":
                parsed = urlparse(cookie["url"])
                cookie["url"] = f"{parsed.scheme}://{parsed.netloc}/"
                cookie.pop("path")
        return cookies

    @classmethod
    def _build_token_session_cookie_targets(
        cls, session_token: str
    ) -> List[Dict[str, Any]]:
        normalized_value = str(session_token or "").strip()
        if not normalized_value:
            return []

        expanded: List[Dict[str, Any]] = []
        for target_url in BROWSER_SESSION_COOKIE_TARGET_URLS:
            expanded.append(
                {
                "name": "__Secure-next-auth.session-token",
                "value": normalized_value,
                "url": target_url,
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
                }
            )
        return expanded

    async def _load_token_session_binding(
        self, token_id: Optional[int]
    ) -> tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
        token_key = self._normalize_token_key(token_id)
        if token_key is None or not self.db:
            return token_key, None, None, None

        try:
            token = await self.db.get_token(token_key)
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 读取 token({token_key}) Session Token 失败: {e}"
            )
            return token_key, None, None, None

        session_token = str(getattr(token, "st", "") or "").strip() if token else ""
        google_cookies = (
            str(getattr(token, "google_cookies", "") or "").strip() if token else ""
        )

        # 优先以 google_cookies 计算绑定指纹：新版 flow.google.com 依赖
        # Google 账号 cookies 维持登录态，st 仅作为旧数据的回退
        if google_cookies:
            cookie_signature = build_cookie_signature(
                google_cookies
            ) or hashlib.sha256(google_cookies.encode("utf-8")).hexdigest()
        elif session_token:
            cookie_signature = (
                build_cookie_signature(f"__Secure-next-auth.session-token={session_token}")
                or hashlib.sha256(session_token.encode("utf-8")).hexdigest()
            )
        else:
            cookie_signature = None
        return token_key, session_token, google_cookies, cookie_signature

    async def _ensure_shared_token_binding(
        self, context, token_id: Optional[int]
    ) -> bool:
        (
            token_key,
            session_token,
            google_cookies,
            cookie_signature,
        ) = await self._load_token_session_binding(token_id)

        if token_key is None:
            self._shared_bound_token_id = None
            self._shared_bound_cookie_signature = None
            return True

        if not (google_cookies or session_token) or not cookie_signature:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 缺少可用的登录态凭据，无法绑定账号态 (token_id={token_key})"
            )
            return False

        if (
            self._shared_bound_token_id == token_key
            and self._shared_bound_cookie_signature == cookie_signature
        ):
            return True

        if google_cookies:
            browser_cookies = self._build_google_cookie_targets(google_cookies)
        else:
            browser_cookies = self._build_token_session_cookie_targets(session_token)
        if not browser_cookies:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 构造登录态 cookies 失败 (token_id={token_key})"
            )
            return False

        try:
            await context.clear_cookies()
            scoped_cookies = []
            regular_cookies = []
            for cookie in browser_cookies:
                target = scoped_cookies if cookie.get("url") and "path" in cookie else regular_cookies
                target.append(cookie)
            if regular_cookies:
                await context.add_cookies(regular_cookies)
            if scoped_cookies:
                page = await context.new_page()
                session = None
                try:
                    session = await context.new_cdp_session(page)
                    await session.send("Network.setCookies", {"cookies": scoped_cookies})
                finally:
                    if session:
                        try:
                            await session.detach()
                        finally:
                            await page.close()
                    else:
                        await page.close()
            self._shared_bound_token_id = token_key
            self._shared_bound_cookie_signature = cookie_signature
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 已绑定业务 token 登录态到共享 context "
                f"(token_id={token_key}, cookies={len(browser_cookies)})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 绑定业务 token 登录态失败 "
                f"(token_id={token_key}): {type(e).__name__}: {str(e)[:200]}"
            )
            return False

    async def _prepare_flow_runtime_page(
        self,
        page,
        project_id: str,
        website_key: str,
        action: str,
        *,
        context_label: str = "",
    ) -> bool:
        """打开真实 Flow 页面并完成页面预热。

        页面级注册 route：捕获页面自身发出的 StreamChat 请求（harvest
        打码数据源，见 _execute_captcha），其余请求一律直通，不干预页面
        原生加载行为。route 挂在 page 上，页面关闭后自动清理，不影响
        共享 context。
        """
        primary_host = (
            "https://www.recaptcha.net"
            if self._browser_proxy_active
            else "https://www.google.com"
        )
        secondary_host = (
            "https://www.google.com"
            if primary_host == "https://www.recaptcha.net"
            else "https://www.recaptcha.net"
        )

        async def _harvest_stream_route(route):
            try:
                request = route.request
                if (
                    self._harvest_streamchat_post is None
                    and request.method == "POST"
                    and "StreamChat" in request.url
                ):
                    self._harvest_streamchat_post = request.post_data or ""
                    await route.abort()
                    return
                await route.continue_()
            except Exception:
                await route.continue_()

        try:
            await page.route(
                # 注意：Playwright glob 中单个 * 不跨越路径分隔符，
                # StreamChat 的路径有多层目录（/_/AiSandboxAngularFrontend/...），
                # 必须用 ** 才能匹配 flow.google.com 下的全部子路径。
                "https://flow.google.com/**", _harvest_stream_route
            )
        except Exception as route_error:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} harvest route 注册失败: "
                f"{type(route_error).__name__}: {str(route_error)[:150]}"
            )

        page_urls = [
            self._build_flow_project_url(project_id),
            "https://flow.google.com/about",
        ]
        label = f"{context_label} " if context_label else ""

        loaded = False
        last_error: Optional[str] = None
        for index, target_url in enumerate(page_urls):
            try:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} {label}打开真实 Flow 页面: {target_url} (action={action})"
                )
                await page.goto(
                    target_url, wait_until="domcontentloaded", timeout=45000
                )
                loaded = True
                break
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:200]}"
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} {label}Flow 页面打开失败[{index + 1}/{len(page_urls)}]: {last_error}"
                )

        if not loaded:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {label}无法打开真实 Flow 页面: {last_error or 'unknown'}"
            )
            return False

        page_loaded = False
        for _ in range(20):
            try:
                ready_state = await page.evaluate("document.readyState")
                if ready_state == "complete":
                    page_loaded = True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if not page_loaded:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {label}Flow 页面 readyState 未达到 complete，继续尝试预热"
            )

        try:
            await page.bring_to_front()
        except Exception:
            pass

        try:
            await page.mouse.move(320, 220)
            await page.mouse.move(560, 360, steps=16)
            await page.mouse.wheel(0, 260)
            await page.evaluate(
                """
                (() => {
                    try {
                        window.focus();
                        window.dispatchEvent(new Event('focus'));
                        document.dispatchEvent(new MouseEvent('mousemove', {
                            bubbles: true,
                            clientX: Math.max(32, Math.floor((window.innerWidth || 1280) * 0.42)),
                            clientY: Math.max(32, Math.floor((window.innerHeight || 720) * 0.36))
                        }));
                        window.scrollTo(0, Math.min(320, document.body?.scrollHeight || 320));
                    } catch (e) {}
                })()
                """
            )
        except Exception:
            pass

        warmup_seconds = _resolve_flow_page_warmup_seconds(
            getattr(config, "browser_flow_page_warmup_seconds", None)
        )
        if warmup_seconds > 0:
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} {label}真实页面预热 {warmup_seconds:.1f}s"
            )
            await asyncio.sleep(warmup_seconds)

        ready = await self._wait_for_enterprise_ready(
            page,
            website_key,
            primary_host,
            secondary_host,
            timeout_ms=15000,
            context_label=f"{label}真实页面",
        )
        if not ready:
            return False

        await self._capture_page_fingerprint(page)
        try:
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} {label}真实页面已就绪: url={page.url[:200]}"
            )
        except Exception:
            pass
        return True

    async def _resolve_proxy_runtime_config(
        self, token_proxy_url: Optional[str] = None
    ) -> tuple:
        """Resolve runtime proxy configuration."""
        proxy_option = None
        raw_proxy_url = None
        proxy_source = "none"
        self._browser_proxy_active = False
        try:
            candidate_proxy_url = None
            if token_proxy_url and token_proxy_url.strip():
                candidate_proxy_url = token_proxy_url.strip()
                proxy_source = "token"
            elif self.db:
                captcha_config = await self.db.get_captcha_config()
                if (
                    captcha_config.browser_proxy_enabled
                    and captcha_config.browser_proxy_url
                ):
                    candidate_proxy_url = captcha_config.browser_proxy_url.strip()
                    proxy_source = "global"

            if candidate_proxy_url:
                normalized_proxy_url, proxy_warning = normalize_browser_proxy_url(
                    candidate_proxy_url
                )
                if proxy_warning:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} {proxy_warning}"
                    )
                proxy_option = parse_proxy_url(normalized_proxy_url)
                if proxy_option:
                    raw_proxy_url = normalized_proxy_url
                    self._browser_proxy_active = True
                    debug_logger.log_info(
                        f"[BrowserCaptcha] Token-{self.token_id} using {proxy_source} proxy: {proxy_option['server']}"
                    )
                else:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} {proxy_source} proxy format is invalid and has been ignored"
                    )
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} failed to read proxy configuration: {e}"
            )

        return proxy_option, raw_proxy_url, proxy_source

    async def _create_browser(
        self, token_proxy_url: Optional[str] = None, manage_slot_pid: bool = True
    ) -> tuple:
        """Create a browser instance; shared-slot browsers track PIDs while temporary browsers do not."""
        width = self._profile_viewport["width"]
        height = self._profile_viewport["height"]
        viewport = {"width": width, "height": height}
        launch_in_background = bool(getattr(config, "browser_launch_background", True))

        if manage_slot_pid:
            await self._cleanup_stale_slot_process()
        playwright = await async_playwright().start()
        browser_executable_path = (
            os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
        )
        proxy_option, raw_proxy_url, _ = await self._resolve_proxy_runtime_config(
            token_proxy_url=token_proxy_url
        )

        # 先只记录代理，真实 UA/UA-CH 交给浏览器自己暴露，避免 user-agent 与 sec-ch-ua 版本错位。
        self._last_fingerprint = {
            "proxy_url": raw_proxy_url if raw_proxy_url else None,
        }

        try:
            browser_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-quic",
                "--disable-features=UseDnsHttpsSvcb",
                "--lang=en-US",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-zygote",
                f"--window-size={width},{height}",
                "--disable-infobars",
                "--hide-scrollbars",
                "--profile-directory=Default",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-default-apps",
                "--no-default-browser-check",
            ]

            if launch_in_background:
                browser_args.extend(
                    [
                        "--start-minimized",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                        "--disable-backgrounding-occluded-windows",
                        f"--flow2api-browser-slot={self.token_id}",
                    ]
                )
                if sys.platform.startswith("win"):
                    browser_args.append("--window-position=-32000,-32000")
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} headed browser will launch in background mode"
                )

            if browser_executable_path:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} using custom browser executable: {browser_executable_path}"
                )

            browser = await playwright.chromium.launch(
                headless=False,
                executable_path=browser_executable_path,
                proxy=proxy_option,
                args=browser_args,
            )
            context = await browser.new_context(
                viewport=viewport,
                locale="en-US",
            )
            await self._apply_browser_environment_patch(context, label="context")
            browser_pid = self._extract_browser_pid(browser)
            if manage_slot_pid:
                self._write_pid_file(browser_pid)
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} shared browser started (proxy={'yes' if raw_proxy_url else 'no'})"
            )
            return playwright, browser, context
        except Exception as e:
            debug_logger.log_error(
                f"[BrowserCaptcha] Token-{self.token_id} browser launch failed: {type(e).__name__}: {str(e)[:200]}"
            )
            try:
                if playwright:
                    await playwright.stop()
            except Exception:
                pass
            if manage_slot_pid:
                self._write_pid_file(None)
            raise

    async def _recycle_browser_locked(
        self, reason: str = "unknown", rotate_profile: bool = True
    ):
        """Recycle the shared browser instance and reset its state."""
        playwright = self._shared_playwright
        browser = self._shared_browser
        context = self._shared_context
        keepalive_page = self._shared_keepalive_page
        browser_pid = self._shared_browser_pid or self._read_pid_file()
        had_browser = bool(
            playwright or browser or context or keepalive_page or browser_pid
        )

        self._shared_playwright = None
        self._shared_browser = None
        self._shared_context = None
        self._shared_keepalive_page = None
        self._shared_browser_pid = None
        self._shared_proxy_url = None
        self._shared_bound_token_id = None
        self._shared_bound_cookie_signature = None
        self._runtime_generation = getattr(self, "_runtime_generation", 0) + 1
        self._last_fingerprint = None
        self._last_harvest_session_id = None
        self._harvest_streamchat_post = None
        self._consecutive_browser_failures = 0
        self._shared_reuse_count = 0

        if rotate_profile:
            self._refresh_browser_profile()

        if had_browser:
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} shared browser recycled, reason={reason}"
            )
        await self._close_browser(playwright, browser, context, browser_pid=browser_pid)

    async def recycle_browser(
        self, reason: str = "unknown", rotate_profile: bool = True
    ):
        """Recycle the current shared browser."""
        async with self._shared_browser_lock:
            await self._recycle_browser_locked(
                reason=reason, rotate_profile=rotate_profile
            )

    async def _get_or_create_shared_browser(
        self,
        token_proxy_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> tuple:
        """Get or create the shared browser for this slot."""
        _, expected_proxy_url, _ = await self._resolve_proxy_runtime_config(
            token_proxy_url=token_proxy_url
        )
        expected_token_key = self._normalize_token_key(token_id)

        async with self._shared_browser_lock:
            has_shared_browser = bool(
                self._shared_playwright
                and self._shared_browser
                and self._shared_context
            )

            if has_shared_browser:
                is_connected = True
                try:
                    checker = getattr(self._shared_browser, "is_connected", None)
                    if callable(checker):
                        is_connected = bool(checker())
                except Exception:
                    is_connected = False

                if not is_connected:
                    await self._recycle_browser_locked(
                        reason="browser_disconnected", rotate_profile=False
                    )
                    has_shared_browser = False

            if has_shared_browser and self._shared_proxy_url != expected_proxy_url:
                # If the proxy configuration changed, recycle the slot before reusing it.
                await self._recycle_browser_locked(
                    reason="proxy_changed", rotate_profile=False
                )
                has_shared_browser = False

            if has_shared_browser:
                current_bound_token = self._shared_bound_token_id
                token_changed = expected_token_key != current_bound_token and not (
                    expected_token_key is None and current_bound_token is None
                )
                if token_changed:
                    await self._recycle_browser_locked(
                        reason=f"token_binding_changed:{current_bound_token}->{expected_token_key}",
                        rotate_profile=False,
                    )
                    has_shared_browser = False

            if has_shared_browser:
                try:
                    await self._ensure_shared_keepalive_page()
                except Exception:
                    await self._recycle_browser_locked(
                        reason="keepalive_page_broken", rotate_profile=False
                    )
                    has_shared_browser = False

            if has_shared_browser:
                binding_ok = await self._ensure_shared_token_binding(
                    self._shared_context, token_id
                )
                if not binding_ok:
                    await self._recycle_browser_locked(
                        reason="token_binding_failed", rotate_profile=False
                    )
                    has_shared_browser = False

            if has_shared_browser:
                self._shared_reuse_count += 1
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} reusing shared browser (reuse={self._shared_reuse_count})"
                )
                return (
                    self._shared_playwright,
                    self._shared_browser,
                    self._shared_context,
                )

            playwright, browser, context = await self._create_browser(
                token_proxy_url=token_proxy_url
            )
            binding_ok = await self._ensure_shared_token_binding(context, token_id)
            if not binding_ok:
                await self._close_browser(
                    playwright,
                    browser,
                    context,
                    browser_pid=self._extract_browser_pid(browser),
                    clear_slot_pid=True,
                )
                raise RuntimeError("failed to bind token session context")
            self._shared_playwright = playwright
            self._shared_browser = browser
            self._shared_context = context
            await self._ensure_shared_keepalive_page()
            self._shared_proxy_url = (self._last_fingerprint or {}).get("proxy_url")
            self._shared_launch_count += 1
            self._shared_reuse_count = 0
            self.note_idle()
            return playwright, browser, context

    async def _capture_page_fingerprint(self, page):
        """从浏览器页面提取 UA 与客户端提示头，确保与打码浏览器一致。"""
        try:
            fingerprint = await page.evaluate("""
                () => {
                    const ua = navigator.userAgent || "";
                    const lang = navigator.language || "";
                    const uaData = navigator.userAgentData || null;
                    let secChUa = "";
                    let secChUaMobile = "";
                    let secChUaPlatform = "";

                    if (uaData) {
                        if (Array.isArray(uaData.brands) && uaData.brands.length > 0) {
                            secChUa = uaData.brands
                                .map((item) => `"${item.brand}";v="${item.version}"`)
                                .join(", ");
                        }
                        secChUaMobile = uaData.mobile ? "?1" : "?0";
                        if (uaData.platform) {
                            secChUaPlatform = `"${uaData.platform}"`;
                        }
                    }

                    return {
                        user_agent: ua,
                        accept_language: lang,
                        sec_ch_ua: secChUa,
                        sec_ch_ua_mobile: secChUaMobile,
                        sec_ch_ua_platform: secChUaPlatform,
                    };
                }
            """)

            if not isinstance(fingerprint, dict):
                return

            if self._last_fingerprint is None:
                self._last_fingerprint = {}

            for key in (
                "user_agent",
                "accept_language",
                "sec_ch_ua",
                "sec_ch_ua_mobile",
                "sec_ch_ua_platform",
            ):
                value = fingerprint.get(key)
                if isinstance(value, str) and value:
                    self._last_fingerprint[key] = value
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 提取浏览器指纹失败: {type(e).__name__}: {str(e)[:200]}"
            )

    async def _verify_score_in_page(
        self, page, token: str, verify_url: str
    ) -> Dict[str, Any]:
        """直接读取测试页面展示的分数，避免 verify.php 与页面显示口径不一致。"""
        _ = token
        _ = verify_url
        started_at = time.time()
        timeout_seconds = 25.0
        refresh_clicked = False
        last_snapshot: Dict[str, Any] = {}

        try:
            timeout_seconds = float(
                getattr(config, "browser_score_dom_wait_seconds", 25) or 25
            )
        except Exception:
            pass

        while (time.time() - started_at) < timeout_seconds:
            try:
                result = await page.evaluate(
                    """
                        () => {
                            const bodyText = ((document.body && document.body.innerText) || "")
                                .replace(/\\u00a0/g, " ")
                                .replace(/\\r/g, "");
                            const patterns = [
                                { source: "current_score", regex: /Your score is:\\s*([01](?:\\.\\d+)?)/i },
                                { source: "selected_score", regex: /Selected Score Test:[\\s\\S]{0,400}?Score:\\s*([01](?:\\.\\d+)?)/i },
                                { source: "history_score", regex: /(?:^|\\n)\\s*Score:\\s*([01](?:\\.\\d+)?)\\s*;/i },
                            ];
                            let score = null;
                            let source = "";
                            for (const item of patterns) {
                                const match = bodyText.match(item.regex);
                                if (!match) continue;
                                const parsed = Number(match[1]);
                                if (!Number.isNaN(parsed) && parsed >= 0 && parsed <= 1) {
                                    score = parsed;
                                    source = item.source;
                                    break;
                                }
                            }
                            const uaMatch = bodyText.match(/Current User Agent:\\s*([^\\n]+)/i);
                            const ipMatch = bodyText.match(/Current IP Address:\\s*([^\\n]+)/i);
                            return {
                                score,
                                source,
                                raw_text: bodyText.slice(0, 4000),
                                current_user_agent: uaMatch ? uaMatch[1].trim() : "",
                                current_ip_address: ipMatch ? ipMatch[1].trim() : "",
                                title: document.title || "",
                                url: location.href || "",
                            };
                        }
                    """
                )
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

            if isinstance(result, dict):
                last_snapshot = result
                score = result.get("score")
                if isinstance(score, (int, float)):
                    elapsed_ms = int((time.time() - started_at) * 1000)
                    return {
                        "verify_mode": "browser_page_dom",
                        "verify_elapsed_ms": elapsed_ms,
                        "verify_http_status": None,
                        "verify_result": {
                            "success": True,
                            "score": score,
                            "source": result.get("source") or "antcpt_dom",
                            "raw_text": result.get("raw_text") or "",
                            "current_user_agent": result.get("current_user_agent")
                            or "",
                            "current_ip_address": result.get("current_ip_address")
                            or "",
                            "page_title": result.get("title") or "",
                            "page_url": result.get("url") or "",
                        },
                    }

            if not refresh_clicked and (time.time() - started_at) >= 2:
                refresh_clicked = True
                try:
                    await page.evaluate(
                        """
                            () => {
                                const nodes = Array.from(
                                    document.querySelectorAll('button, input[type="button"], input[type="submit"], a')
                                );
                                const target = nodes.find((node) => {
                                    const text = (node.innerText || node.textContent || node.value || "").trim();
                                    return /Refresh score now!?/i.test(text);
                                });
                                if (target) {
                                    target.click();
                                    return true;
                                }
                                return false;
                            }
                        """
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        elapsed_ms = int((time.time() - started_at) * 1000)
        if not isinstance(last_snapshot, dict):
            last_snapshot = {"raw": last_snapshot}

        return {
            "verify_mode": "browser_page_dom",
            "verify_elapsed_ms": elapsed_ms,
            "verify_http_status": None,
            "verify_result": {
                "success": False,
                "score": None,
                "source": "antcpt_dom_timeout",
                "raw_text": last_snapshot.get("raw_text") or "",
                "current_user_agent": last_snapshot.get("current_user_agent") or "",
                "current_ip_address": last_snapshot.get("current_ip_address") or "",
                "page_title": last_snapshot.get("title") or "",
                "page_url": last_snapshot.get("url") or "",
                "error": last_snapshot.get("error") or "未在页面中读取到分数",
            },
        }
    
    async def _close_browser(
        self,
        playwright,
        browser,
        context,
        browser_pid: Optional[int] = None,
        clear_slot_pid: bool = True,
    ):
        """Close a browser instance and fall back to PID cleanup if needed."""
        is_shared_browser = any(
            [
            context is not None and context is self._shared_context,
            browser is not None and browser is self._shared_browser,
            playwright is not None and playwright is self._shared_playwright,
            ]
        )
        effective_pid = browser_pid or self._extract_browser_pid(browser)
        if clear_slot_pid and not effective_pid:
            effective_pid = self._shared_browser_pid or self._read_pid_file()
        if is_shared_browser:
            self._shared_playwright = None
            self._shared_browser = None
            self._shared_context = None
            self._shared_keepalive_page = None
            self._shared_browser_pid = None
            self._shared_proxy_url = None
            self._shared_bound_token_id = None
            self._shared_bound_cookie_signature = None
        try:
            if context:
                await asyncio.wait_for(context.close(), timeout=10)
        except Exception:
            pass
        try:
            if browser:
                await asyncio.wait_for(browser.close(), timeout=10)
        except Exception:
            pass
        try:
            if playwright:
                await asyncio.wait_for(playwright.stop(), timeout=10)
        except Exception:
            pass
        if effective_pid and not await self._wait_pid_exit(
            effective_pid, timeout_seconds=4
        ):
            self._kill_pid(effective_pid, reason="close_timeout_or_orphan")
            await self._wait_pid_exit(effective_pid, timeout_seconds=2)
        if clear_slot_pid:
            self._write_pid_file(None)

    async def _wait_and_close_after_request(
        self,
        request_ref: str,
        release_event: asyncio.Event,
        wait_timeout: int,
        playwright,
        browser,
        context,
        action: str,
    ):
        """等待上游请求结束后再关闭浏览器（超时兜底）。"""
        close_reason = "上游请求完成"
        try:
            await asyncio.wait_for(release_event.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            close_reason = f"等待上游请求完成超时({wait_timeout}s)"
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}，执行兜底关闭"
            )
        except Exception as e:
            close_reason = f"等待上游请求完成异常: {type(e).__name__}"
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}，执行兜底关闭"
            )
        finally:
            await self._close_browser(playwright, browser, context)
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}，浏览器已关闭 (action={action}, request_ref={request_ref[:8]})"
            )
            async with self._pending_release_lock:
                self._pending_release_entries.pop(request_ref, None)

    async def _defer_browser_close_until_request_done(
        self, playwright, browser, context, action: str
    ) -> str:
        """打码成功后延迟关闭浏览器，等待 Flow 请求结束通知。"""
        flow_timeout = int(getattr(config, "flow_timeout", 300) or 300)
        upsample_timeout = int(getattr(config, "upsample_timeout", 300) or 300)
        if action == "IMAGE_GENERATION":
            # 图片链路可能包含放大请求，等待上限至少覆盖 flow/upsample 超时
            base_timeout = max(flow_timeout, upsample_timeout)
            wait_timeout = max(base_timeout + 180, 900)
        else:
            # 视频请求默认超时更长，给更大的缓冲避免“请求未结束就关闭”
            wait_timeout = max(flow_timeout + 300, 1800)
        request_ref = uuid.uuid4().hex
        release_event = asyncio.Event()
        release_task = asyncio.create_task(
            self._wait_and_close_after_request(
                request_ref=request_ref,
                release_event=release_event,
                wait_timeout=wait_timeout,
                playwright=playwright,
                browser=browser,
                context=context,
                action=action,
            )
        )

        async with self._pending_release_lock:
            self._pending_release_entries[request_ref] = {
                "event": release_event,
                "task": release_task,
            }
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} 打码成功后进入延迟关闭，等待上游请求完成 "
            f"(action={action}, timeout={wait_timeout}s, request_ref={request_ref[:8]})"
        )
        return request_ref

    async def notify_generation_request_finished(
        self, request_ref: Optional[str] = None
    ):
        """通知当前 Token 对应的上游图片/视频请求已结束。"""
        async with self._pending_release_lock:
            release_event = None
            matched_ref = request_ref
            if matched_ref and matched_ref in self._pending_release_entries:
                entry = self._pending_release_entries.pop(matched_ref)
                release_event = entry.get("event")
            elif not matched_ref and self._pending_release_entries:
                # 兼容旧调用方（无 request_ref），仅回收最早待释放项，避免一次性影响全部请求。
                matched_ref = next(iter(self._pending_release_entries.keys()))
                entry = self._pending_release_entries.pop(matched_ref)
                release_event = entry.get("event")
        if release_event and not release_event.is_set():
            release_event.set()
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 收到上游请求完成通知，开始关闭浏览器 "
                f"(request_ref={(matched_ref or 'unknown')[:8]})"
            )

    async def force_close_pending_browser(
        self, request_ref: Optional[str] = None, close_all: bool = False
    ):
        """Force close pending browsers tracked by this slot."""
        async with self._pending_release_lock:
            entries: List[Dict[str, Any]] = []
            if close_all:
                entries = list(self._pending_release_entries.values())
                self._pending_release_entries.clear()
            elif request_ref and request_ref in self._pending_release_entries:
                entry = self._pending_release_entries.pop(request_ref)
                entries = [entry]
            elif self._pending_release_entries:
                first_ref = next(iter(self._pending_release_entries.keys()))
                entry = self._pending_release_entries.pop(first_ref)
                entries = [entry]

        release_events = [
            entry.get("event") for entry in entries if isinstance(entry, dict)
        ]
        release_tasks = [
            entry.get("task") for entry in entries if isinstance(entry, dict)
        ]

        for release_event in release_events:
            if not release_event:
                continue
            if not release_event.is_set():
                release_event.set()
        for release_task in release_tasks:
            if not release_task:
                continue
            try:
                await asyncio.wait_for(release_task, timeout=5)
            except Exception:
                pass

        if close_all:
            await self.recycle_browser(reason="force_close_all", rotate_profile=False)

    async def _execute_captcha(
        self, context, project_id: str, website_key: str, action: str
    ) -> Optional[str]:
        """Solve the requested action; native harvest is reserved for StreamChat."""
        normalized_action = str(action or "IMAGE_GENERATION").strip().upper()
        page = None
        self._harvest_streamchat_post = None
        self._last_harvest_session_id = None
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} harvest 开始: "
            f"project={project_id} action={action}"
        )
        try:
            page = await context.new_page()
            await self._apply_browser_environment_patch(page, label="captcha_page")
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            if normalized_action != "CHAT_GENERATION":
                await page.add_init_script(TRUSTED_RECAPTCHA_HOOK_SOURCE)
            ready = await self._prepare_flow_runtime_page(
                page,
                project_id,
                website_key,
                action,
                context_label="打码",
            )
            if not ready:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} harvest 页面未就绪: "
                    f"url={page.url[:80]}"
                )
                return None

            if normalized_action != "CHAT_GENERATION":
                result = await asyncio.wait_for(
                    page.evaluate(build_recaptcha_execute_script(website_key, normalized_action, 25000)),
                    timeout=40,
                )
                if isinstance(result, dict) and result.get("ok"):
                    return str(result.get("token") or "").strip() or None
                reason = str(result.get("error") or "empty token")[:240] if isinstance(result, dict) else "invalid result"
                debug_logger.log_warning(f"[BrowserCaptcha] reCAPTCHA execute 未返回有效 token: {reason}")
                return None

            harvest_result = await asyncio.wait_for(
                self._harvest_token_via_ui_submit(page),
                timeout=120,
            )
            if not harvest_result or not harvest_result.get("token"):
                return None
            self._last_harvest_session_id = harvest_result.get("session_id")
            return harvest_result.get("token")
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 打码失败: {msg[:200]}"
            )
            return None
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass

    HARVEST_PROMPT_POOL = (
        "a red apple on a wooden table",
        "a blue ceramic vase with sunflowers",
        "a cozy mountain cabin in snow",
        "a sailboat on a calm lake at sunrise",
        "a hummingbird hovering near a red flower",
    )

    async def _dismiss_flow_overlays(self, page, rounds: int = 8) -> None:
        """关闭项目页引导弹窗（Next/Continue 等），必要时移除无按钮的遮罩。"""
        for _ in range(rounds):
            clicked = False
            for label in (
                "Next",
                "Continue",
                "Start",
                "Got it",
                "Close",
                "Skip",
                "Not now",
                "Agree",
                "Accept",
            ):
                button = page.locator(
                    f".cdk-overlay-container button:has-text('{label}')"
                ).first
                try:
                    if await button.count() and await button.is_visible():
                        await button.click(timeout=2500)
                        debug_logger.log_info(
                            f"[BrowserCaptcha] Token-{self.token_id} 关闭弹窗: {label}"
                        )
                        clicked = True
                        await page.wait_for_timeout(1600)
                        break
                except Exception:
                    continue
            if clicked:
                continue
            try:
                if await page.locator(".cdk-overlay-backdrop").count():
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(800)
                    if await page.locator(".cdk-overlay-backdrop").count():
                        await page.evaluate(
                            "() => document.querySelectorAll('.cdk-overlay-backdrop')"
                            ".forEach(e => e.remove())"
                        )
                        await page.wait_for_timeout(600)
                    continue
            except Exception:
                pass
            return

    async def _harvest_token_via_ui_submit(self, page) -> Optional[Dict[str, Any]]:
        """驱动页面 UI 提交一次生成并从被拦截的请求中提取 token / 会话 id。

        全新浏览器 context 没有本地引导状态，项目页可能反复弹出多步
        引导弹窗（Previous/Continue 等）吞掉编辑器内容，因此输入与提交
        都需要校验并重试，提交后在等待窗口内持续关闭后出现的弹窗。
        """
        await self._dismiss_flow_overlays(page)
        await page.wait_for_timeout(2500)

        editor = page.locator(".ProseMirror").first
        try:
            await editor.wait_for(timeout=30000)
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 未找到 ProseMirror 输入框: "
                f"{type(e).__name__}: {str(e)[:150]} url={page.url[:80]}"
            )
            return None

        harvest_prompt = random.choice(self.HARVEST_PROMPT_POOL)
        prompt_head = harvest_prompt[:24]
        submitted = False
        for round_index in range(4):
            try:
                current_text = await editor.inner_text()
            except Exception:
                current_text = ""
            if prompt_head not in current_text:
                try:
                    await editor.click(timeout=4000)
                    await page.keyboard.press("Control+a")
                except Exception:
                    await self._dismiss_flow_overlays(page, rounds=4)
                    await page.wait_for_timeout(800)
                    continue
                try:
                    await editor.type(harvest_prompt, delay=30)
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 输入提示词失败: "
                        f"{type(e).__name__}: {str(e)[:150]}"
                    )
                    return None
                # 输入过程中可能再次弹出引导弹窗，关闭后确认文本仍在
                await self._dismiss_flow_overlays(page, rounds=2)
                try:
                    current_text = await editor.inner_text()
                except Exception:
                    current_text = ""
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} harvest 第{round_index + 1}轮"
                    f"输入确认: {'ok' if prompt_head in current_text else 'lost'}"
                )
                if prompt_head not in current_text:
                    continue

            submit_button = page.locator(
                "button[aria-label='Start generation']"
            ).first
            try:
                await submit_button.click(timeout=8000)
                submitted = True
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 点击 Start generation 失败"
                    f"（第{round_index + 1}轮）: {type(e).__name__}: {str(e)[:150]}"
                )
                await self._dismiss_flow_overlays(page, rounds=4)
                continue

            for tick in range(30):
                if self._harvest_streamchat_post:
                    break
                await page.wait_for_timeout(1000)
                if tick and tick % 5 == 0:
                    await self._dismiss_flow_overlays(page, rounds=1)
            if self._harvest_streamchat_post:
                break
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} harvest 第{round_index + 1}轮"
                "未捕获到请求，重试"
            )

        post_data = self._harvest_streamchat_post
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} harvest 捕获: "
            f"{'len=' + str(len(post_data)) if post_data else 'NONE'} submitted={submitted}"
        )
        if not post_data:
            try:
                dbg = await page.evaluate(
                    """() => ({
                        url: location.href,
                        editorText: (document.querySelector('.ProseMirror')||{}).innerText?.slice(0,60),
                        submitDisabled: (document.querySelector("button[aria-label='Start generation']")||{}).disabled,
                        overlayButtons: [...document.querySelectorAll('.cdk-overlay-container button')].map(b => b.innerText.slice(0,20)).slice(0,5),
                        generating: !!document.querySelector('stop-generation, [aria-label*=Stop]'),
                    })"""
                )
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 未捕获页面 StreamChat 请求，"
                    f"页面状态: {dbg}"
                )
            except Exception:
                pass
        if not post_data:
            return None

        harvest_result = self._parse_harvest_streamchat_post(post_data)
        if not harvest_result:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} StreamChat 请求体中未找到 token"
            )
            return None
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} harvest 成功: "
            f"token_len={len(harvest_result['token'])} "
            f"session={harvest_result.get('session_id') or 'n/a'}"
        )
        return harvest_result

    @staticmethod
    def _parse_harvest_streamchat_post(
        post_data: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Parse a native StreamChat form body into token and Flow session id."""
        if not post_data:
            return None

        try:
            freq_value = next(
                value
                for key, value in parse_qsl(post_data, keep_blank_values=True)
                if key == "f.req"
            )
        except StopIteration:
            return None

        token_match = re.search(r"(0cAFcW[A-Za-z0-9_\-]+)", freq_value)
        if not token_match:
            return None

        try:
            outer = json.loads(freq_value)
            inner = json.loads(outer[1]) if len(outer) > 1 else None
            session_id = None
            if isinstance(inner, list) and inner:
                session_id = str(inner[0] or "").strip() or None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

        return {"token": token_match.group(1), "session_id": session_id}

    def get_last_harvest_session_id(self) -> Optional[str]:
        """返回最近一次 harvest 得到的 Flow 会话 id（供主程序提交复用）。"""
        return self._last_harvest_session_id

    async def _execute_custom_captcha(
        self,
        context,
        website_url: str,
        website_key: str,
        action: str,
        verify_url: Optional[str] = None,
        enterprise: bool = False,
    ) -> Any:
        """在任意站点执行 reCAPTCHA，用于分数测试等非 Flow 场景。"""
        page = None
        try:
            page = await context.new_page()
            await self._apply_browser_environment_patch(
                page, label="custom_captcha_page"
            )
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            primary_host = (
                "https://www.recaptcha.net"
                if self._browser_proxy_active
                else "https://www.google.com"
            )
            secondary_host = (
                "https://www.google.com"
                if primary_host == "https://www.recaptcha.net"
                else "https://www.recaptcha.net"
            )
            script_path = (
                "recaptcha/enterprise.js" if enterprise else "recaptcha/api.js"
            )
            execute_target = (
                "grecaptcha.enterprise.execute" if enterprise else "grecaptcha.execute"
            )
            ready_target = (
                "grecaptcha.enterprise.ready" if enterprise else "grecaptcha.ready"
            )
            wait_expression = (
                (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
                "typeof grecaptcha.enterprise.execute === 'function'"
                )
                if enterprise
                else (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
            )
            )
            api_label = "enterprise.js" if enterprise else "api.js"

            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 加载真实自定义页面 {api_label}: primary={primary_host}, secondary={secondary_host}, url={website_url}"
            )

            def handle_request_failed(request):
                try:
                    failed_url = request.url or ""
                    if not any(
                        d in failed_url
                        for d in [
                            "google.com",
                            "gstatic.com",
                            "recaptcha.net",
                            "antcpt.com",
                        ]
                    ):
                        return
                    failure = request.failure or ""
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 自定义资源加载失败: url={failed_url[:200]}, error={failure}"
                    )
                except Exception:
                    pass

            page.on("requestfailed", handle_request_failed)

            try:
                await page.goto(
                    website_url, wait_until="domcontentloaded", timeout=30000
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 自定义 page.goto 失败: {type(e).__name__}: {str(e)[:200]}"
                )
                return None

            page_loaded = False
            for _ in range(20):
                try:
                    ready_state = await page.evaluate("document.readyState")
                    if ready_state == "complete":
                        page_loaded = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            if not page_loaded:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 自定义页面 readyState 未达到 complete，继续尝试预热"
                )

            # 模拟更自然的前台交互，避免冷启动空白上下文直接 execute。
            try:
                await page.mouse.move(320, 220)
                await page.mouse.move(520, 320, steps=12)
                await page.mouse.wheel(0, 240)
                await page.bring_to_front()
                await page.evaluate("""
                    (() => {
                        try {
                            window.focus();
                            window.dispatchEvent(new Event('focus'));
                            document.dispatchEvent(new MouseEvent('mousemove', {
                                bubbles: true,
                                clientX: Math.max(32, Math.floor((window.innerWidth || 1280) * 0.4)),
                                clientY: Math.max(32, Math.floor((window.innerHeight || 720) * 0.35))
                            }));
                            window.scrollTo(0, Math.min(280, document.body?.scrollHeight || 280));
                        } catch (e) {}
                    })()
                """)
            except Exception:
                pass

            warmup_seconds = float(
                getattr(config, "browser_score_test_warmup_seconds", 12) or 12
            )
            if warmup_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} 真实页面预热 {warmup_seconds:.1f}s 后再执行自定义打码"
                )
                await asyncio.sleep(warmup_seconds)

            if not await self._poll_enterprise_ready(page, 15000, wait_expression):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 自定义 grecaptcha 未就绪，尝试补注入脚本"
                )
                injected = False
                for script_host in (primary_host, secondary_host):
                    script_url = f"{script_host}/{script_path}?render={website_key}"
                    if await self._inject_recaptcha_script(
                        page, script_url, "/recaptcha/"
                    ):
                        injected = True
                    if await self._poll_enterprise_ready(
                        page, 15000, wait_expression
                    ):
                        injected = True
                        break
                if not injected:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 自定义 grecaptcha 最终未就绪"
                    )
                    return None

            await self._capture_page_fingerprint(page)

            token = await asyncio.wait_for(
                page.evaluate(
                    f"""
                        (actionName) => {{
                            return new Promise((resolve, reject) => {{
                                const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                                try {{
                                    {ready_target}(function() {{
                                        {execute_target}('{website_key}', {{action: actionName}})
                                            .then(t => {{
                                                clearTimeout(timeout);
                                                resolve(t);
                                            }})
                                            .catch(e => {{
                                                clearTimeout(timeout);
                                                reject(e);
                                            }});
                                    }});
                                }} catch (e) {{
                                    clearTimeout(timeout);
                                    reject(e);
                                }}
                            }});
                        }}
                    """,
                    action,
                ),
                timeout=30,
            )

            post_wait_seconds = float(
                getattr(config, "browser_recaptcha_settle_seconds", 3) or 3
            )
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} 自定义打码已完成，额外等待 {post_wait_seconds:.1f}s 后返回 token"
                )
                await asyncio.sleep(post_wait_seconds)

            if verify_url:
                verify_payload = await self._verify_score_in_page(
                    page, token, verify_url
                )
                return {
                    "token": token,
                    **verify_payload,
                }

            return token
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 自定义打码失败: {msg[:200]}"
            )
            return None
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass

    def is_busy(self) -> bool:
        return self._solve_inflight > 0

    def note_idle(self):
        if self._solve_inflight <= 0:
            self._last_idle_since = time.monotonic()

    def idle_seconds(self) -> float:
        if self.is_busy():
            return 0.0
        return max(0.0, time.monotonic() - self._last_idle_since)

    def has_shared_browser(self) -> bool:
        return bool(
            self._shared_browser or self._shared_context or self._shared_keepalive_page
        )

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        """返回最近一次打码浏览器的指纹快照。"""
        if not self._last_fingerprint:
            return None
        return dict(self._last_fingerprint)

    ENTERPRISE_READY_EXPRESSION = (
        "() => typeof grecaptcha !== 'undefined' && "
        "typeof grecaptcha.enterprise !== 'undefined' && "
        "typeof grecaptcha.enterprise.execute === 'function'"
    )

    # 新版 Flow 页面启用了 Trusted Types
    # (`require-trusted-types-for 'script'`)，直接给 script.src 赋值会抛
    # "This document requires 'TrustedScriptURL' assignment"。
    # 这里通过页面内创建的 Trusted Types policy 完成注入，避免使用
    # Playwright 的 add_script_tag（它在 Trusted Types 下必然失败）。
    INJECT_RECAPTCHA_SCRIPT_JS = r"""
    ({url, marker}) => {
        const scripts = Array.from(document.scripts || []);
        const existing = scripts.some((script) => {
            const src = script && script.src ? String(script.src) : '';
            return src.includes(marker);
        });
        if (existing) return 'existing';
        let policy = null;
        try {
            if (
                window.trustedTypes &&
                typeof window.trustedTypes.createPolicy === 'function'
            ) {
                policy = window.trustedTypes.createPolicy(
                    'flow2api-recaptcha-' + Math.random().toString(36).slice(2),
                    { createScriptURL: (value) => value }
                );
            }
        } catch (error) {}
        const script = document.createElement('script');
        script.async = true;
        try {
            script.src = policy ? policy.createScriptURL(url) : url;
        } catch (error) {
            script.src = url;
        }
        (document.head || document.documentElement).appendChild(script);
        return 'injected';
    }
    """

    async def _poll_enterprise_ready(
        self, page, timeout_ms: int, expression: Optional[str] = None
    ) -> bool:
        """用 page.evaluate 轮询 grecaptcha 就绪状态。

        不使用 ``page.wait_for_function``：新版 Flow 的 Trusted Types CSP 会让
        其中的字符串 eval 抛
        "Evaluating a string as JavaScript violates this document's Trusted Type
        assignment requirements"，导致就绪检测永远失败。
        """
        probe = expression or self.ENTERPRISE_READY_EXPRESSION
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        while True:
            try:
                if await page.evaluate(probe):
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.25)

    async def _inject_recaptcha_script(self, page, url: str, marker: str) -> bool:
        """在页面内注入 reCAPTCHA 脚本（兼容 Trusted Types CSP）。"""
        try:
            await page.evaluate(
                self.INJECT_RECAPTCHA_SCRIPT_JS,
                {"url": url, "marker": marker},
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 页面内注入脚本失败: "
                f"{type(e).__name__}: {str(e)[:150]}"
            )
            return False

    async def _wait_for_enterprise_ready(
        self,
        page,
        website_key: str,
        primary_host: str,
        secondary_host: str,
        *,
        timeout_ms: int = 15000,
        context_label: str = "",
        script_path: str = "recaptcha/enterprise.js",
        marker: str = "/recaptcha/enterprise.js",
    ) -> bool:
        """等待 grecaptcha.enterprise 就绪，必要时补注入脚本。"""
        label = f"{context_label} " if context_label else ""
        if await self._poll_enterprise_ready(page, timeout_ms):
            return True

        debug_logger.log_warning(
            f"[BrowserCaptcha] Token-{self.token_id} {label}grecaptcha 未就绪，尝试补注入脚本"
        )

        for script_host in (primary_host, secondary_host):
            script_url = f"{script_host}/{script_path}?render={website_key}"
            if not await self._inject_recaptcha_script(page, script_url, marker):
                continue
            if await self._poll_enterprise_ready(page, timeout_ms):
                return True
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {label}注入后仍未就绪: {script_url}"
            )
        return False

    @staticmethod
    def _inject_recaptcha_token(payload: Any, token: str):
        """递归更新 payload 内所有 reCAPTCHA token 字段。"""
        if isinstance(payload, dict):
            recaptcha_context = payload.get("recaptchaContext")
            if isinstance(recaptcha_context, dict):
                recaptcha_context["token"] = token
                recaptcha_context.setdefault(
                    "applicationType",
                    "RECAPTCHA_APPLICATION_TYPE_WEB",
                )
            for value in payload.values():
                TokenBrowser._inject_recaptcha_token(value, token)
        elif isinstance(payload, list):
            for item in payload:
                TokenBrowser._inject_recaptcha_token(item, token)

    async def submit_flow_request(
        self,
        project_id: str,
        website_key: str,
        action: str,
        url: str,
        at_token: str,
        json_data: Dict[str, Any],
        timeout: int,
        token_proxy_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """在同一浏览器上下文中完成打码并直接提交 Flow 请求。"""
        async with self._semaphore:
            self._solve_inflight += 1
            # Submission retries belong to FlowClient; replaying a timed-out POST
            # here can create another billable generation task.
            max_retries = 1

            try:
                for attempt in range(max_retries):
                    page = None
                    submit_started = False
                    try:
                        start_ts = time.time()
                        _, _, context = await self._get_or_create_shared_browser(
                            token_proxy_url=token_proxy_url,
                            token_id=token_id,
                        )

                        page = await context.new_page()
                        await self._apply_browser_environment_patch(
                            page, label="browser_submit_page"
                        )
                        await page.add_init_script(
                            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                        )

                        debug_logger.log_info(
                            f"[BrowserCaptcha] Token-{self.token_id} 浏览器内提交 Flow 请求: "
                            f"action={action}, project_id={project_id}"
                        )
                        ready = await self._prepare_flow_runtime_page(
                            page,
                            project_id,
                            website_key,
                            action,
                            context_label="浏览器内提交",
                        )
                        if not ready:
                            raise RuntimeError("grecaptcha.enterprise 未就绪")

                        payload_for_submit = deepcopy(json_data)
                        submit_started = True
                        response_payload = await asyncio.wait_for(
                            page.evaluate(
                                """
                                async ({ websiteKey, actionName, targetUrl, bearerToken, payload, timeoutMs }) => {
                                    const solveToken = () => new Promise((resolve, reject) => {
                                        const timer = setTimeout(() => reject(new Error('captcha_timeout')), 25000);
                                        try {
                                            grecaptcha.enterprise.execute(websiteKey, { action: actionName })
                                                .then((token) => {
                                                    clearTimeout(timer);
                                                    resolve(token);
                                                })
                                                .catch((error) => {
                                                    clearTimeout(timer);
                                                    reject(error);
                                                });
                                        } catch (error) {
                                            clearTimeout(timer);
                                            reject(error);
                                        }
                                    });

                                    const patchToken = (value, token) => {
                                        if (!value) return;
                                        if (Array.isArray(value)) {
                                            value.forEach((item) => patchToken(item, token));
                                            return;
                                        }
                                        if (typeof value !== 'object') return;
                                        if (value.recaptchaContext && typeof value.recaptchaContext === 'object') {
                                            value.recaptchaContext.token = token;
                                            if (!value.recaptchaContext.applicationType) {
                                                value.recaptchaContext.applicationType = 'RECAPTCHA_APPLICATION_TYPE_WEB';
                                            }
                                        }
                                        Object.values(value).forEach((item) => patchToken(item, token));
                                    };

                                    const token = await solveToken();
                                    const body = typeof structuredClone === 'function'
                                        ? structuredClone(payload)
                                        : JSON.parse(JSON.stringify(payload));
                                    patchToken(body, token);

                                    const controller = new AbortController();
                                    const abortTimer = setTimeout(() => controller.abort('flow_fetch_timeout'), timeoutMs);
                                    try {
                                        const response = await fetch(targetUrl, {
                                            method: 'POST',
                                            headers: {
                                                'authorization': `Bearer ${bearerToken}`,
                                                'content-type': 'text/plain;charset=UTF-8',
                                            },
                                            credentials: 'include',
                                            body: JSON.stringify(body),
                                            signal: controller.signal,
                                        });
                                        const text = await response.text();
                                        const headers = {};
                                        response.headers.forEach((value, key) => {
                                            headers[key] = value;
                                        });
                                        return {
                                            ok: response.ok,
                                            status: response.status,
                                            text,
                                            headers,
                                            token,
                                        };
                                    } finally {
                                        clearTimeout(abortTimer);
                                    }
                                }
                                """,
                                {
                                    "websiteKey": website_key,
                                    "actionName": action,
                                    "targetUrl": url,
                                    "bearerToken": at_token,
                                    "payload": payload_for_submit,
                                    "timeoutMs": max(5000, int(timeout * 1000)),
                                },
                            ),
                            timeout=max(35, timeout + 10),
                        )

                        if not isinstance(response_payload, dict):
                            raise RuntimeError("浏览器内提交返回格式异常")

                        self._solve_count += 1
                        self._consecutive_browser_failures = 0
                        debug_logger.log_info(
                            f"[BrowserCaptcha] Token-{self.token_id} 浏览器内提交完成 "
                            f"(status={response_payload.get('status')}, "
                            f"{(time.time() - start_ts) * 1000:.0f}ms, "
                            f"launches={self._shared_launch_count}, reuse={self._shared_reuse_count})"
                        )
                        return response_payload
                    except Exception as e:
                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        error_message = f"{type(e).__name__}: {str(e)}"
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} 浏览器内提交失败: "
                            f"{error_message[:240]}"
                        )
                        error_lower = error_message.lower()
                        if submit_started and not any(
                            marker in error_lower for marker in ("captcha_timeout", "grecaptcha.enterprise 未就绪")
                        ):
                            setattr(e, "submission_uncertain", True)
                        if any(
                            keyword in error_lower
                            for keyword in [
                                "context or browser has been closed",
                                "target closed",
                                "browser has been closed",
                                "connection closed",
                                "crash",
                                "closed",
                            ]
                        ):
                            await self.recycle_browser(
                                reason="browser_submit_runtime_error",
                                rotate_profile=False,
                            )
                        raise
                    finally:
                        if page:
                            try:
                                await page.close()
                            except Exception:
                                pass

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)

                raise RuntimeError("浏览器内提交 Flow 请求失败")
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()
    
    async def refresh_google_cookies(
        self, *, token_id: int, token_proxy_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Refresh the bound account's current frontend without solving a CAPTCHA."""
        if not token_id:
            raise ValueError("Cookie 刷新必须指定 Token ID")
        async with self._semaphore:
            self._solve_inflight += 1
            page = None
            try:
                _, _, context = await self._get_or_create_shared_browser(
                    token_proxy_url=token_proxy_url, token_id=token_id
                )
                page = await context.new_page()
                response = await page.goto(
                    "https://flow.google.com/projects", wait_until="domcontentloaded", timeout=30000
                )
                if urlparse(page.url).hostname != "flow.google.com" or not response or response.status != 200:
                    raise RuntimeError("Flow 登录已失效，请重新同步 Google Cookies")
                email = await page.evaluate("(window.WIZ_global_data || {}).oPEP7c || ''")
                cookies = [
                    cookie for cookie in await context.cookies()
                    if not is_partitioned_cookie(cookie) and (
                        cookie["domain"].lstrip(".") == "google.com"
                        or cookie["domain"].endswith(".google.com")
                    )
                ]
                return {
                    "google_cookies": json.dumps(cookies, ensure_ascii=False, separators=(",", ":")),
                    "email": email,
                }
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()

    async def get_token(
        self,
        project_id: str,
        website_key: str,
        action: str = "IMAGE_GENERATION",
        token_proxy_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """Get a token from the shared browser unless a fatal browser error occurs."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = max(
                1, int(getattr(config, "browser_captcha_max_retries", 5) or 5)
            )

            try:
                for attempt in range(max_retries):
                    try:
                        start_ts = time.time()
                        _, _, context = await self._get_or_create_shared_browser(
                            token_proxy_url=token_proxy_url,
                            token_id=token_id,
                        )

                        token = await self._execute_captcha(
                            context, project_id, website_key, action
                        )
                        if token:
                            self._solve_count += 1
                            self._consecutive_browser_failures = 0
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} token acquired ({(time.time()-start_ts)*1000:.0f}ms, launches={self._shared_launch_count}, reuse={self._shared_reuse_count})"
                            )
                            return token, self._last_harvest_session_id

                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} token attempt {attempt + 1}/{max_retries} failed"
                        )
                        if self._consecutive_browser_failures >= 2:
                            await self.recycle_browser(
                                reason=f"captcha_failed_{attempt + 1}",
                                rotate_profile=False,
                            )
                    except Exception as e:
                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        error_message = f"{type(e).__name__}: {str(e)}"
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} browser error: {error_message[:200]}"
                        )
                        error_lower = error_message.lower()
                        if any(
                            keyword in error_lower
                            for keyword in [
                            "context or browser has been closed",
                            "target closed",
                            "browser has been closed",
                            "connection closed",
                            "crash",
                            "closed",
                            ]
                        ):
                            await self.recycle_browser(
                                reason="browser_runtime_error", rotate_profile=False
                            )

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)

                return None, None
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """Get a custom reCAPTCHA token using a temporary browser."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = 3

            try:
                for attempt in range(max_retries):
                    playwright = None
                    browser = None
                    context = None
                    try:
                        start_ts = time.time()
                        playwright, browser, context = await self._create_browser(
                            manage_slot_pid=False
                        )
                        token = await self._execute_custom_captcha(
                            context=context,
                            website_url=website_url,
                            website_key=website_key,
                            action=action,
                            enterprise=enterprise,
                        )

                        if token:
                            self._solve_count += 1
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} custom token acquired ({(time.time()-start_ts)*1000:.0f}ms)"
                            )
                            return token

                        self._error_count += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} custom token attempt {attempt+1}/{max_retries} failed"
                        )
                    except Exception as e:
                        self._error_count += 1
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} custom browser error: {type(e).__name__}: {str(e)[:200]}"
                        )
                    finally:
                        await self._close_browser(
                            playwright,
                            browser,
                            context,
                            browser_pid=self._extract_browser_pid(browser),
                            clear_slot_pid=False,
                        )

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)

                return None
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        """Get a custom token and verify its score using a temporary browser."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = 3

            try:
                for attempt in range(max_retries):
                    playwright = None
                    browser = None
                    context = None
                    try:
                        started_at = time.time()
                        playwright, browser, context = await self._create_browser(
                            manage_slot_pid=False
                        )
                        payload = await self._execute_custom_captcha(
                            context=context,
                            website_url=website_url,
                            website_key=website_key,
                            action=action,
                            verify_url=verify_url,
                            enterprise=enterprise,
                        )

                        if isinstance(payload, dict) and payload.get("token"):
                            self._solve_count += 1
                            payload.setdefault(
                                "token_elapsed_ms",
                                int((time.time() - started_at) * 1000),
                            )
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} in-page score verification succeeded ({(time.time()-started_at)*1000:.0f}ms)"
                            )
                            return payload

                        self._error_count += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} in-page score attempt {attempt+1}/{max_retries} failed"
                        )
                    except Exception as e:
                        self._error_count += 1
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} in-page score browser error: {type(e).__name__}: {str(e)[:200]}"
                        )
                    finally:
                        await self._close_browser(
                            playwright,
                            browser,
                            context,
                            browser_pid=self._extract_browser_pid(browser),
                            clear_slot_pid=False,
                        )

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)

                return {
                    "token": None,
                    "verify_mode": "browser_page",
                    "verify_elapsed_ms": 0,
                    "verify_http_status": None,
                    "verify_result": {},
                }
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()


@dataclass
class _BrowserRequest:
    ref: str
    slot_id: int
    browser: Any
    token_id: Optional[int]
    generation: int
    owner: Any
    borrower: Any = None
    owner_callback: Any = None
    hold: bool = False
    failed: bool = False
    risk_reported: bool = False
    fingerprint: Optional[Dict[str, Any]] = None


@dataclass
class _SlotRecovery:
    phase: str = "ready"
    failure_streak: int = 0
    cooldown_until: float = 0.0
    recovery_count: int = 0
    reason: str = ""
    needs_recycle: bool = False
    retiring: bool = False
    task: Optional[asyncio.Task] = None


class BrowserCaptchaQueueTimeout(RuntimeError):
    """No browser slot became available before the admission deadline."""


class BrowserCaptchaService:
    """多浏览器轮询打码服务（单例模式）
    
    支持配置浏览器数量，每个浏览器只开 1 个标签页，请求轮询分配
    """
    
    _instance: Optional["BrowserCaptchaService"] = None
    _lock = asyncio.Lock()
    QUEUE_WAIT_SECONDS = 30.0
    DRAIN_WAIT_SECONDS = 120.0
    RECOVERY_TIMEOUT_SECONDS = 60.0
    RECOVERY_BACKOFF_SECONDS = (5.0, 15.0, 30.0, 60.0)
    MAINTENANCE_INTERVAL_SECONDS = 15.0
    
    def __init__(self, db=None):
        self.db = db
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.base_user_data_dir = os.path.join(os.getcwd(), "browser_data_rt")
        self._browsers: Dict[int, TokenBrowser] = {}
        self._browsers_lock = asyncio.Lock()
        self._slot_allocation_lock = asyncio.Lock()
        self._slot_reservations: Dict[int, int] = {}
        self._slot_condition = asyncio.Condition(self._slot_allocation_lock)
        self._slot_recovery: Dict[int, _SlotRecovery] = {}
        self._requests: Dict[str, _BrowserRequest] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._closed = False
        self._queue_timeouts = 0
        self._queue_wait_total_seconds = 0.0
        self._admitted_requests = 0
        
        # ???????
        self._browser_count = 1  # ?? 1 ?????????
        self._round_robin_index = 0  # ????
        # ????
        self._stats = {"req_total": 0, "gen_ok": 0, "gen_fail": 0, "api_403": 0}
        
        # ?????? _load_browser_count ???????
        self._token_semaphore = None
        self._idle_reaper_task: Optional[asyncio.Task] = None
    async def _ensure_idle_reaper(self):
        if self._closed:
            return
        if self._idle_reaper_task is None or self._idle_reaper_task.done():
            self._idle_reaper_task = asyncio.create_task(self._idle_reaper_loop())

    async def _idle_reaper_loop(self):
        while True:
            try:
                await asyncio.sleep(self.MAINTENANCE_INTERVAL_SECONDS)
                async with self._slot_condition:
                    if self._closed:
                        return
                    for slot_id, state in self._slot_recovery.items():
                        if (
                            state.retiring
                            and state.needs_recycle
                            and state.phase == "cooldown"
                        ):
                            self._resume_recovery_locked(slot_id)
                idle_ttl = int(getattr(config, "browser_idle_ttl_seconds", 600) or 600)
                browsers = []
                async with self._browsers_lock:
                    browsers = list(self._browsers.items())
                for slot_id, browser in browsers:
                    reserved = False
                    try:
                        if not browser.has_shared_browser():
                            continue
                        if browser.idle_seconds() < idle_ttl:
                            continue
                        async with self._slot_condition:
                            if self._is_slot_busy_for_allocation(slot_id):
                                continue
                            self._reserve_slot_locked(slot_id)
                            reserved = True
                        await browser.recycle_browser(
                            reason=f"idle_ttl_{idle_ttl}s", rotate_profile=False
                        )
                    except Exception as e:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] idle reaper failed: {e}"
                        )
                    finally:
                        if reserved:
                            await self._release_slot_reservation(slot_id)
            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] idle reaper loop error: {e}"
                )

    @classmethod
    async def get_instance(cls, db=None) -> "BrowserCaptchaService":
        async with cls._lock:
            instance = cls._instance
            if instance is not None:
                if instance._closed:
                    raise RuntimeError("Browser captcha service is closing")
                return instance

            instance = cls(db)
            try:
                await instance._load_browser_count()
                if instance._closed:
                    raise RuntimeError("Browser captcha service is closing")
                await instance._ensure_idle_reaper()
                if instance._closed:
                    raise RuntimeError("Browser captcha service is closing")
            except BaseException:
                instance._closed = True
                if instance._idle_reaper_task:
                    instance._idle_reaper_task.cancel()
                    await asyncio.gather(instance._idle_reaper_task, return_exceptions=True)
                raise
            # Publish only after initialization and background-task setup finish.
            cls._instance = instance
            return instance
    
    def _check_available(self):
        """检查服务是否可用"""
        if DOCKER_HEADED_BLOCKED:
            raise RuntimeError(
                "检测到 Docker 环境，默认禁用有头浏览器打码。"
                "如需启用请设置环境变量 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb。"
            )
        if IS_DOCKER and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Docker 有头浏览器打码已启用，但 DISPLAY 未设置。"
                "请设置 DISPLAY（例如 :99）并启动 Xvfb。"
            )
        if not PLAYWRIGHT_AVAILABLE or async_playwright is None:
            raise RuntimeError(
                "playwright 未安装或不可用。"
                "请手动安装: pip install playwright && python -m playwright install chromium"
            )
    
    async def _load_browser_count(self):
        """从数据库加载浏览器数量配置"""
        if self.db:
            try:
                captcha_config = await self.db.get_captcha_config()
                self._browser_count = max(1, captcha_config.browser_count)
                debug_logger.log_info(
                    f"[BrowserCaptcha] 浏览器数量配置: {self._browser_count}"
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 加载 browser_count 配置失败: {e}，使用默认值 1"
                )
                self._browser_count = 1
        # 并发限制 = 浏览器数量，不再硬编码限制
        self._token_semaphore = asyncio.Semaphore(self._browser_count)
        debug_logger.log_info(f"[BrowserCaptcha] 并发上限: {self._browser_count}")
    
    async def reload_browser_count(self):
        """Retire removed slots after their active submissions release them."""
        old_count = self._browser_count
        await self._load_browser_count()
        await self._ensure_idle_reaper()
        async with self._slot_condition:
            if self._browser_count < old_count:
                for slot_id in range(self._browser_count, old_count):
                    state = self._slot_recovery.setdefault(slot_id, _SlotRecovery())
                    state.phase = "draining"
                    state.needs_recycle = True
                    state.retiring = True
                    state.reason = "slot_removed"
                    state.cooldown_until = 0.0
                    self._resume_recovery_locked(slot_id)
            self._slot_condition.notify_all()
        if self._browser_count > old_count:
            await self.warmup_browser_slots()

    def _log_stats(self):
        total = self._stats["req_total"]
        gen_fail = self._stats["gen_fail"]
        api_403 = self._stats["api_403"]
        gen_ok = self._stats["gen_ok"]
        
        valid_success = gen_ok - api_403
        if valid_success < 0:
            valid_success = 0
        
        rate = (valid_success / total * 100) if total > 0 else 0.0

    async def _warmup_browser_slot(
        self,
        browser_id: int,
        *,
        token_id: Optional[int] = None,
        token_proxy_url: Optional[str] = None,
    ):
        async with self._slot_condition:
            if self._closed or self._is_slot_busy_for_allocation(browser_id):
                return
            self._reserve_slot_locked(browser_id)
        try:
            browser = await self._get_or_create_browser(browser_id)
            await browser._get_or_create_shared_browser(
                token_proxy_url=token_proxy_url,
                token_id=token_id,
            )
            debug_logger.log_info(
                f"[BrowserCaptcha] warmed browser slot {browser_id} "
                f"(token_id={token_id or '-'})"
            )
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] warmup for slot {browser_id} failed: {e}"
            )
        finally:
            await self._release_slot_reservation(browser_id)

    async def _get_warmup_token_specs(self) -> List[Tuple[int, Optional[str]]]:
        """Return account-bound warmup targets for the configured browser slots."""
        if not self.db or not hasattr(self.db, "get_active_tokens"):
            return []

        try:
            tokens = await self.db.get_active_tokens()
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 读取预热 token 列表失败: {e}"
            )
            return []

        specs: List[Tuple[int, Optional[str]]] = []
        for token in tokens:
            if getattr(token, "ban_reason", None):
                continue
            token_key = getattr(token, "id", None)
            if token_key is None:
                continue
            google_cookies = str(getattr(token, "google_cookies", "") or "").strip()
            session_token = str(getattr(token, "st", "") or "").strip()
            if not (google_cookies or session_token):
                continue
            proxy_url = str(getattr(token, "captcha_proxy_url", "") or "").strip()
            specs.append((int(token_key), proxy_url or None))
            if len(specs) >= self._browser_count:
                break
        return specs

    async def warmup_browser_slots(self):
        token_specs = await self._get_warmup_token_specs()
        token_spec_by_id = {
            token_id: (token_id, token_proxy_url)
            for token_id, token_proxy_url in token_specs
        }
        warmup_targets: List[Tuple[int, Optional[Tuple[int, Optional[str]]]]] = []

        async with self._browsers_lock:
            if token_spec_by_id:
                pending_specs = [
                    spec
                    for token_id, spec in token_spec_by_id.items()
                    if token_id
                    not in {
                        self._slot_bound_token_id(browser_id)
                        for browser_id in range(self._browser_count)
                    }
                ]
                for browser_id in range(self._browser_count):
                    browser = self._browsers.get(browser_id)
                    bound_token_id = self._slot_bound_token_id(browser_id)
                    if browser is not None and bound_token_id in token_spec_by_id:
                        continue
                    if not pending_specs:
                        continue
                    warmup_targets.append((browser_id, pending_specs.pop(0)))
            elif not any(
                self._has_warmed_browser_for_allocation(browser_id)
                for browser_id in range(self._browser_count)
            ):
                # Keep dependency/runtime probing useful even before an account exists.
                warmup_targets = [(0, None)]

        tasks = [
            self._warmup_browser_slot(
                browser_id,
                token_id=spec[0] if spec is not None else None,
                token_proxy_url=spec[1] if spec is not None else None,
            )
            for browser_id, spec in warmup_targets
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _is_slot_busy_for_allocation(self, slot_id: int) -> bool:
        state = self._slot_recovery.get(slot_id)
        if state and state.phase not in {"ready", "probe"}:
            return True
        if any(request.slot_id == slot_id for request in self._requests.values()):
            return True
        if self._slot_reservations.get(slot_id, 0) > 0:
            return True
        browser = self._browsers.get(slot_id)
        return bool(browser and getattr(browser, "is_busy", lambda: False)())

    def _has_warmed_browser_for_allocation(self, slot_id: int) -> bool:
        browser = self._browsers.get(slot_id)
        return bool(browser and getattr(browser, "has_shared_browser", lambda: False)())

    def _slot_bound_token_id(self, slot_id: int) -> Optional[int]:
        browser = self._browsers.get(slot_id)
        bound_token_id = getattr(browser, "_shared_bound_token_id", None)
        try:
            return int(bound_token_id) if bound_token_id is not None else None
        except (TypeError, ValueError):
            return None

    def _reserve_slot_locked(self, slot_id: int):
        self._slot_reservations[slot_id] = self._slot_reservations.get(slot_id, 0) + 1

    async def _release_slot_reservation(self, slot_id: Optional[int]):
        if slot_id is None:
            return
        async with self._slot_condition:
            current = self._slot_reservations.get(slot_id, 0)
            if current <= 1:
                self._slot_reservations.pop(slot_id, None)
            else:
                self._slot_reservations[slot_id] = current - 1
            self._resume_recovery_locked(slot_id)
            self._slot_condition.notify_all()

    def _resume_recovery_locked(self, slot_id: int):
        state = self._slot_recovery.get(slot_id)
        if not state or not state.needs_recycle or self._closed:
            return
        if state.task and not state.task.done():
            return
        if state.cooldown_until > time.monotonic():
            return
        state.phase = "draining"
        state.task = asyncio.create_task(self._recover_slot(slot_id))

    async def _recover_slot(self, slot_id: int):
        state = self._slot_recovery[slot_id]
        deadline = time.monotonic() + self.DRAIN_WAIT_SECONDS
        try:
            async with self._slot_condition:
                while (
                    self._slot_reservations.get(slot_id, 0)
                    or any(r.slot_id == slot_id for r in self._requests.values())
                    or (
                        self._browsers.get(slot_id)
                        and self._browsers[slot_id].is_busy()
                    )
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        state.reason = "drain_timeout"
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] slot={slot_id} 等待在途请求释放超时，保持隔离"
                        )
                        return
                    try:
                        await asyncio.wait_for(self._slot_condition.wait(), remaining)
                    except asyncio.TimeoutError:
                        continue
                state.phase = "recovering"
                browser = self._browsers.get(slot_id)

            if browser:
                await asyncio.wait_for(
                    browser.recycle_browser(
                        reason=state.reason,
                        rotate_profile=not state.retiring or state.failure_streak > 0,
                    ),
                    timeout=self.RECOVERY_TIMEOUT_SECONDS,
                )
            async with self._slot_condition:
                state.needs_recycle = False
                if state.retiring:
                    state.retiring = False
                    if slot_id >= self._browser_count:
                        self._browsers.pop(slot_id, None)
                        self._slot_recovery.pop(slot_id, None)
                        return
                    if not state.failure_streak:
                        state.phase = "ready"
                        return
                state.recovery_count += 1
                state.phase = "cooldown"
                state.cooldown_until = (
                    time.monotonic()
                    + self.RECOVERY_BACKOFF_SECONDS[
                        min(
                            max(state.failure_streak - 1, 0),
                            len(self.RECOVERY_BACKOFF_SECONDS) - 1,
                        )
                    ]
                )
                debug_logger.log_info(
                    f"[BrowserCaptcha] slot={slot_id} 已完成恢复，次数={state.recovery_count}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._slot_condition:
                state.phase = "cooldown"
                state.reason = "recovery_failed"
                state.cooldown_until = (
                    time.monotonic() + self.RECOVERY_BACKOFF_SECONDS[-1]
                )
            debug_logger.log_warning(
                f"[BrowserCaptcha] slot={slot_id} 恢复失败: {type(exc).__name__}"
            )
        finally:
            async with self._slot_condition:
                if state.task is asyncio.current_task():
                    state.task = None
                self._slot_condition.notify_all()

    async def _finish_request(self, browser_ref, *, success: Optional[bool] = None):
        async with self._slot_condition:
            request = self._requests.get(browser_ref)
            if not request:
                return
            if (
                request.borrower is not None
                and request.borrower is not asyncio.current_task()
                and self._slot_reservations.get(request.slot_id, 0)
            ):
                if success is False:
                    request.failed = True
                return
            self._requests.pop(browser_ref)
            request.owner.remove_done_callback(request.owner_callback)
            state = self._slot_recovery.get(request.slot_id)
            if (
                state
                and state.phase == "probe"
                and not request.failed
                and success is True
                and request.generation
                == getattr(request.browser, "_runtime_generation", 0)
            ):
                state.phase = "ready"
                state.failure_streak = 0
            self._resume_recovery_locked(request.slot_id)
            self._slot_condition.notify_all()

    def _on_request_owner_done(self, request: _BrowserRequest):
        if self._closed or request.ref not in self._requests:
            return
        task = asyncio.create_task(self._finish_request(request.ref, success=False))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    @asynccontextmanager
    async def _borrow_browser(
        self,
        project_id=None,
        token_id=None,
        *,
        browser_ref=None,
        wait_timeout=None,
    ):
        request = None
        async with self._slot_condition:
            candidate = self._requests.get(browser_ref)
            if (
                candidate
                and candidate.token_id == token_id
                and not candidate.owner.done()
            ):
                state = self._slot_recovery.get(candidate.slot_id)
                if (
                    candidate.generation
                    == getattr(candidate.browser, "_runtime_generation", 0)
                    and not candidate.failed
                    and (not state or state.phase in {"ready", "probe"})
                    and not self._slot_reservations.get(candidate.slot_id, 0)
                ):
                    request = candidate
                    request.hold = False
                    request.borrower = asyncio.current_task()
                    self._reserve_slot_locked(request.slot_id)
        if not request:
            if candidate and candidate.token_id == token_id:
                if self._slot_reservations.get(candidate.slot_id, 0):
                    raise RuntimeError("Browser captcha request is already submitting")
                await self._finish_request(candidate.ref, success=False)
            slot_id = await self._select_browser_id(
                project_id,
                token_id=token_id,
                timeout_seconds=wait_timeout,
            )
            try:
                browser = await self._get_or_create_browser(slot_id)
                request = _BrowserRequest(
                    ref=f"{slot_id}:lease-{uuid.uuid4().hex}/",
                    slot_id=slot_id,
                    browser=browser,
                    token_id=token_id,
                    generation=getattr(browser, "_runtime_generation", 0),
                    owner=asyncio.current_task(),
                    borrower=asyncio.current_task(),
                )
                request.owner_callback = lambda owner: self._on_request_owner_done(
                    request
                )
                async with self._slot_condition:
                    if self._closed:
                        raise RuntimeError("Browser captcha service is closed")
                    self._requests[request.ref] = request
                    request.owner.add_done_callback(request.owner_callback)
            except BaseException:
                await self._release_slot_reservation(slot_id)
                raise
        try:
            yield request
        except BaseException as exc:
            request.failed = True
            request.generation = getattr(request.browser, "_runtime_generation", 0)
            if isinstance(exc, Exception):
                setattr(exc, "browser_ref", request.ref)
                await self.report_error(request.ref, error_reason=str(exc))
            raise
        finally:
            request.borrower = None
            if not request.hold:
                await self._finish_request(request.ref, success=not request.failed)
            await self._release_slot_reservation(request.slot_id)

    async def _select_browser_id(
        self,
        project_id: Optional[str],
        token_id: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ) -> int:
        _ = project_id
        expected_token_id = TokenBrowser._normalize_token_key(token_id)
        wait_seconds = self.QUEUE_WAIT_SECONDS if timeout_seconds is None else max(0, float(timeout_seconds))
        queued_at = time.monotonic()
        deadline = queued_at + wait_seconds
        async with self._slot_condition:
            while True:
                if self._closed:
                    raise RuntimeError("Browser captcha service is closed")
                now = time.monotonic()
                for slot_id, state in self._slot_recovery.items():
                    if state.phase == "cooldown" and state.cooldown_until <= now:
                        if state.needs_recycle:
                            self._resume_recovery_locked(slot_id)
                        else:
                            state.phase = "probe"
                matching_idle_slot: Optional[int] = None
                unbound_warm_idle_slot: Optional[int] = None
                cold_idle_slot: Optional[int] = None
                idle_slot: Optional[int] = None

                for offset in range(self._browser_count):
                    slot_id = (self._round_robin_index + offset) % self._browser_count
                    if self._is_slot_busy_for_allocation(slot_id):
                        continue

                    if idle_slot is None:
                        idle_slot = slot_id

                    browser = self._browsers.get(slot_id)
                    if browser is None:
                        if cold_idle_slot is None:
                            cold_idle_slot = slot_id
                        continue

                    bound_token_id = self._slot_bound_token_id(slot_id)
                    if (
                        expected_token_id is not None
                        and bound_token_id == expected_token_id
                    ):
                        matching_idle_slot = slot_id
                        break
                    if (
                        bound_token_id is None
                        and self._has_warmed_browser_for_allocation(slot_id)
                    ):
                        if unbound_warm_idle_slot is None:
                            unbound_warm_idle_slot = slot_id

                if matching_idle_slot is not None:
                    selected_slot = matching_idle_slot
                elif expected_token_id is None and unbound_warm_idle_slot is not None:
                    selected_slot = unbound_warm_idle_slot
                elif cold_idle_slot is not None:
                    selected_slot = cold_idle_slot
                else:
                    selected_slot = idle_slot

                if selected_slot is not None:
                    self._round_robin_index = (selected_slot + 1) % self._browser_count
                    self._reserve_slot_locked(selected_slot)
                    self._admitted_requests += 1
                    self._queue_wait_total_seconds += time.monotonic() - queued_at
                    return selected_slot

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._queue_timeouts += 1
                    raise BrowserCaptchaQueueTimeout("Browser captcha queue wait timeout")
                wake_delays = [
                    state.cooldown_until - time.monotonic()
                    for state in self._slot_recovery.values()
                    if state.phase == "cooldown" and state.cooldown_until > time.monotonic()
                ]
                delay = min([remaining, *wake_delays])
                try:
                    await asyncio.wait_for(self._slot_condition.wait(), delay)
                except asyncio.TimeoutError:
                    pass

    async def _get_or_create_browser(self, browser_id: int) -> TokenBrowser:
        """获取或创建指定 ID 的浏览器实例"""
        async with self._browsers_lock:
            if self._closed:
                raise RuntimeError("Browser captcha service is closed")
            if browser_id not in self._browsers:
                user_data_dir = os.path.join(
                    self.base_user_data_dir, f"browser_{browser_id}"
                )
                browser = TokenBrowser(browser_id, user_data_dir, db=self.db)
                self._browsers[browser_id] = browser
                debug_logger.log_info(f"[BrowserCaptcha] 创建浏览器实例 {browser_id}")
            return self._browsers[browser_id]
    
    def _get_next_browser_id(self) -> int:
        """轮询获取下一个浏览器 ID"""
        browser_id = self._round_robin_index % self._browser_count
        self._round_robin_index += 1
        return browser_id

    @staticmethod
    def _compose_browser_ref(
        browser_id: int, request_ref: Optional[str]
    ) -> Union[int, str]:
        """将 browser_id 与 request_ref 合并为可回传的请求句柄。"""
        if request_ref:
            return f"{browser_id}:{request_ref}"
        return browser_id

    @staticmethod
    def _parse_browser_ref(
        browser_ref: Optional[Union[int, str]],
    ) -> tuple[Optional[int], Optional[str]]:
        """解析请求句柄，兼容旧的纯 int browser_id。"""
        if browser_ref is None:
            return None, None

        if isinstance(browser_ref, int):
            return browser_ref, None

        if isinstance(browser_ref, str):
            raw = browser_ref.strip()
            if raw.isdigit():
                return int(raw), None
            browser_id_part, sep, request_ref = raw.partition(":")
            if sep and browser_id_part.isdigit() and request_ref:
                return int(browser_id_part), request_ref

        return None, None

    async def _resolve_token_proxy_url(self, token_id: Optional[int]) -> Optional[str]:
        """读取 token 级打码代理，为空时回退全局配置。"""
        if not token_id or not self.db:
            return None
        try:
            token = await self.db.get_token(token_id)
            if token and token.captcha_proxy_url and token.captcha_proxy_url.strip():
                return token.captcha_proxy_url.strip()
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 读取 token({token_id}) 打码代理失败: {e}"
            )
        return None
    
    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: int = None,
    ) -> tuple[Optional[str], Union[int, str], Optional[Dict[str, Any]]]:
        """Reserve the account context until its protected submission completes."""
        self._check_available()
        self._stats["req_total"] += 1
        token_proxy_url = await self._resolve_token_proxy_url(token_id)
        async with self._borrow_browser(project_id, token_id) as request:
            token, session_id = await asyncio.wait_for(
                request.browser.get_token(
                    project_id,
                    self.website_key,
                    action,
                    token_proxy_url=token_proxy_url,
                    token_id=token_id,
                ),
                timeout=max(1, float(config.flow_timeout)),
            )
            request.generation = getattr(request.browser, "_runtime_generation", 0)
            request.fingerprint = (
                deepcopy(request.browser.get_last_fingerprint()) if token else None
            )
            if token:
                async with self._slot_condition:
                    self._requests.pop(request.ref)
                    request.ref += str(session_id or "")
                    self._requests[request.ref] = request
                    request.hold = True
                self._stats["gen_ok"] += 1
            else:
                self._stats["gen_fail"] += 1
        return token, request.ref, deepcopy(request.fingerprint)

    async def refresh_google_cookies(
        self, project_id: Optional[str], *, token_id: int
    ) -> Dict[str, Any]:
        self._check_available()
        if not token_id:
            raise ValueError("Cookie 刷新必须指定 Token ID")
        token_proxy_url = await self._resolve_token_proxy_url(token_id)
        async with self._borrow_browser(project_id, token_id) as request:
            return await request.browser.refresh_google_cookies(
                token_id=token_id, token_proxy_url=token_proxy_url
            )

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> tuple[Optional[str], int]:
        """Use the same admission policy for diagnostic solves and generation."""
        self._check_available()
        async with self._borrow_browser() as request:
            token = await asyncio.wait_for(
                request.browser.get_custom_token(
                    website_url=website_url,
                    website_key=website_key,
                    action=action,
                    enterprise=enterprise,
                ),
                timeout=max(1, float(config.flow_timeout)),
            )
            request.generation = getattr(request.browser, "_runtime_generation", 0)
            request.failed = True
            return token, request.slot_id

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> tuple[Dict[str, Any], int]:
        """Keep diagnostic pages away from slots that are draining or recovering."""
        self._check_available()
        async with self._borrow_browser() as request:
            payload = await asyncio.wait_for(
                request.browser.get_custom_score(
                    website_url=website_url,
                    website_key=website_key,
                    verify_url=verify_url,
                    action=action,
                    enterprise=enterprise,
                ),
                timeout=max(1, float(config.flow_timeout)),
            )
            request.generation = getattr(request.browser, "_runtime_generation", 0)
            # A diagnostic score is not evidence that Flow accepted a submission.
            request.failed = True
            return payload, request.slot_id

    async def get_fingerprint(
        self, browser_ref: Optional[Union[int, str]]
    ) -> Optional[Dict[str, Any]]:
        """获取指定浏览器最近一次打码时的指纹快照。"""
        browser_id, _ = self._parse_browser_ref(browser_ref)
        if browser_id is None:
            return None

        if isinstance(browser_ref, str) and ":lease-" in browser_ref:
            request = self._requests.get(browser_ref)
            return deepcopy(request.fingerprint) if request else None

        async with self._browsers_lock:
            browser = self._browsers.get(browser_id)
            if not browser:
                return None
            return browser.get_last_fingerprint()

    @staticmethod
    def _is_recaptcha_rejection(error_text: str) -> bool:
        normalized = str(error_text or "").lower()
        return "recaptcha" in normalized and any(
            marker in normalized
            for marker in (
                "evaluation failed",
                "verification failed",
                "验证失败",
                "extension_hijack_detected",
            )
        )

    async def submit_flow_request(
        self,
        project_id: str,
        action: str,
        token_id: Optional[int],
        url: str,
        at_token: str,
        json_data: Dict[str, Any],
        timeout: int,
        browser_ref: Optional[Union[int, str]] = None,
    ) -> tuple[Dict[str, Any], Union[int, str], Optional[Dict[str, Any]]]:
        """Reuse this request's account context and bound admission plus submission."""
        self._check_available()
        deadline = time.monotonic() + max(1, float(timeout))
        token_proxy_url = await self._resolve_token_proxy_url(token_id)
        async with self._borrow_browser(
            project_id,
            token_id,
            browser_ref=browser_ref,
            wait_timeout=min(
                self.QUEUE_WAIT_SECONDS, max(0, deadline - time.monotonic())
            ),
        ) as request:
            remaining = max(0.001, deadline - time.monotonic())
            try:
                response = await asyncio.wait_for(
                    request.browser.submit_flow_request(
                        project_id=project_id,
                        website_key=self.website_key,
                        action=action,
                        url=url,
                        at_token=at_token,
                        json_data=json_data,
                        timeout=remaining,
                        token_proxy_url=token_proxy_url,
                        token_id=token_id,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                error = RuntimeError(
                    "Flow browser submission timed out; submission status is unknown"
                )
                error.submission_uncertain = True
                raise error from exc
            request.generation = getattr(request.browser, "_runtime_generation", 0)
            request.fingerprint = deepcopy(request.browser.get_last_fingerprint())
            if int(response.get("status") or 0) >= 400:
                request.failed = True
                # Isolate the slot before releasing its reservation to the allocator.
                await self.report_error(
                    request.ref, error_reason=response.get("text") or ""
                )
            return response, request.ref, deepcopy(request.fingerprint)

    async def report_error(self, browser_ref=None, error_reason: Optional[str] = None):
        """Isolate only the runtime that produced this request's failed token."""
        slot_id, handle = self._parse_browser_ref(browser_ref)
        if slot_id is None:
            return
        async with self._slot_condition:
            request = self._requests.get(browser_ref)
            if handle and handle.startswith("lease-") and not request:
                return
            browser = self._browsers.get(slot_id)
            if not browser or (
                request
                and (
                    request.browser is not browser
                    or request.generation != getattr(browser, "_runtime_generation", 0)
                    or (
                        request.token_id is not None
                        and request.token_id
                        != getattr(browser, "_shared_bound_token_id", None)
                    )
                )
            ):
                return
            if request:
                request.failed = True
            if not self._is_recaptcha_rejection(error_reason):
                return
            if request and request.risk_reported:
                return
            if request:
                request.risk_reported = True
            state = self._slot_recovery.setdefault(slot_id, _SlotRecovery())
            if not state.needs_recycle or state.retiring:
                state.failure_streak += 1
                self._stats["api_403"] += 1
            state.needs_recycle = True
            state.reason = "recaptcha_rejected"
            state.phase = "draining"
            state.cooldown_until = 0.0
            self._resume_recovery_locked(slot_id)
            self._slot_condition.notify_all()
            debug_logger.log_warning(
                f"[BrowserCaptcha] slot={slot_id} 已隔离，等待请求释放后恢复"
            )

    async def report_request_finished(
        self, browser_ref=None, *, success: Optional[bool] = None
    ):
        """Release the submission lease; video result polling does not hold a slot."""
        await self._finish_request(browser_ref, success=success)

    async def remove_browser(self, browser_id: int):
        async with self._browsers_lock:
            if browser_id in self._browsers:
                self._browsers.pop(browser_id)

    async def close(self):
        async with self._slot_condition:
            self._closed = True
            owners = {
                owner
                for request in self._requests.values()
                for owner in (request.owner, request.borrower)
                if owner is not None
                and owner is not asyncio.current_task()
                and not owner.done()
            }
            tasks = list(self._cleanup_tasks) + [
                state.task for state in self._slot_recovery.values() if state.task
            ]
            for request in self._requests.values():
                request.owner.remove_done_callback(request.owner_callback)
            self._requests.clear()
            self._slot_reservations.clear()
            self._slot_condition.notify_all()
        for task in tasks:
            task.cancel()
        for owner in owners:
            owner.cancel()
        if tasks or owners:
            await asyncio.gather(*tasks, *owners, return_exceptions=True)
        async with self._browsers_lock:
            browsers = list(self._browsers.values())
            self._browsers.clear()

        if self._idle_reaper_task and not self._idle_reaper_task.done():
            self._idle_reaper_task.cancel()
            try:
                await self._idle_reaper_task
            except asyncio.CancelledError:
                pass

        for browser in browsers:
            try:
                await browser.force_close_pending_browser(close_all=True)
                await browser.recycle_browser(
                    reason="service_shutdown", rotate_profile=False
                )
            except Exception:
                pass
        async with type(self)._lock:
            if type(self)._instance is self:
                type(self)._instance = None
            
    async def open_login_browser(self):
        return {"success": False, "error": "Not implemented"}

    async def create_browser_for_token(self, t, s=None):
        pass

    def get_stats(self):
        now = time.monotonic()
        states = []
        slot_ids = sorted(set(range(self._browser_count)) | set(self._browsers))
        for slot_id in slot_ids:
            state = self._slot_recovery.get(slot_id, _SlotRecovery())
            active_count = sum(r.slot_id == slot_id for r in self._requests.values())
            busy = (
                active_count
                or self._slot_reservations.get(slot_id, 0)
                or bool(
                    self._browsers.get(slot_id) and self._browsers[slot_id].is_busy()
                )
            )
            phase = (
                "busy" if busy and state.phase in {"ready", "probe"} else state.phase
            )
            states.append(
                {
                    "browser_id": slot_id,
                    "state": phase,
                    "active_request_count": active_count,
                    "cooldown_remaining_seconds": round(
                        max(0, state.cooldown_until - now), 2
                    ),
                    "recovery_count": state.recovery_count,
                    "failure_streak": state.failure_streak,
                    "last_reason": state.reason,
                }
            )
        recovering = [
            item["browser_id"]
            for item in states
            if item["state"] in {"draining", "recovering"}
        ]
        return {
            "total_solve_count": self._stats["gen_ok"],
            "total_error_count": self._stats["gen_fail"],
            "risk_403_count": self._stats["api_403"],
            "browser_count": len(self._browsers),
            "configured_browser_count": self._browser_count,
            "busy_browser_count": sum(item["state"] == "busy" for item in states),
            "idle_browser_count": sum(
                item["state"] in {"ready", "probe"} for item in states
            ),
            "shared_browser_count": sum(
                browser.has_shared_browser() for browser in self._browsers.values()
            ),
            "recovering_browser_count": len(recovering),
            "recovering_slots": recovering,
            "queue_timeout_count": self._queue_timeouts,
            "average_queue_wait_ms": round(
                self._queue_wait_total_seconds * 1000 / max(1, self._admitted_requests), 2,
            ),
            "project_affinity_count": 0,
            "browsers": states,
        }
