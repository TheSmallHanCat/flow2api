"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
支持常驻模式：维护全局共享的常驻标签页池，即时生成 token
"""
import asyncio
import base64
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import gc
import inspect
import math
import random
import time
import os
import sys
import re
import signal
import json
import hashlib
import mimetypes
import shutil
import tempfile
import subprocess
import types
from pathlib import Path
from typing import Optional, Dict, Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

from ..core.logger import debug_logger
from ..core.config import config
from .browser_cookie_utils import (
    build_browser_cookie_targets,
    build_cookie_signature,
    merge_browser_cookie_payloads,
    normalize_cookie_storage_text,
)

# flow2api 缺少的配置常量和函数，内联定义
TOKEN_POOL_SIZE_MAX = 500
PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS = 50

def resolve_effective_browser_count(value) -> int:
    try:
        current = max(1, min(20, int(value or 1)))
    except Exception:
        current = 1
    return current

def resolve_effective_personal_max_resident_tabs(value) -> int:
    try:
        current = max(1, min(50, int(value or 1)))
    except Exception:
        current = 1
    return current

PERSONAL_COOKIE_PREBIND_URL = "about:blank"
PERSONAL_LABS_BOOTSTRAP_URL = "https://labs.google/fx/api/auth/providers"
PERSONAL_COOKIE_TARGET_URLS = (
    "https://labs.google/",
    "https://www.google.com/",
    "https://www.recaptcha.net/",
)
PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS = (
    "https://www.google.com/",
    "https://www.recaptcha.net/",
)
PERSONAL_HEADLESS_VISIBLE_SPOOF_SOURCE = r"""
(() => {
    const marker = "__personalHeadlessVisibleSpoofInstalled__";
    if (window[marker]) {
        return;
    }
    window[marker] = true;

    const defineGetter = (target, key, getter) => {
        if (!target) {
            return;
        }
        try {
            Object.defineProperty(target, key, {
                configurable: true,
                enumerable: true,
                get: getter,
            });
        } catch (e) {}
    };

    defineGetter(Document.prototype, "visibilityState", () => "visible");
    defineGetter(document, "visibilityState", () => "visible");
    defineGetter(Document.prototype, "webkitVisibilityState", () => "visible");
    defineGetter(document, "webkitVisibilityState", () => "visible");
    defineGetter(Document.prototype, "hidden", () => false);
    defineGetter(document, "hidden", () => false);
    defineGetter(Document.prototype, "webkitHidden", () => false);
    defineGetter(document, "webkitHidden", () => false);

    try {
        document.hasFocus = () => true;
    } catch (e) {}

    try {
        if (typeof window.focus === "function") {
            window.focus();
        }
    } catch (e) {}

    const emit = (target, type) => {
        try {
            target.dispatchEvent(new Event(type));
        } catch (e) {}
    };

    setTimeout(() => {
        emit(document, "visibilitychange");
        emit(window, "focus");
        emit(window, "pageshow");
    }, 0);
})();
"""
PERSONAL_FINGERPRINT_SURFACE_SPOOF_MARKER = "__personalFingerprintSurfaceSpoofInstalled__"
PERSONAL_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PERSONAL_RUNTIME_TMP_DIR = PERSONAL_RUNTIME_ROOT / "tmp"
PERSONAL_RUNTIME_DATA_DIR = PERSONAL_RUNTIME_ROOT / "data"


# ==================== Docker 环境检测 ====================
def _is_running_in_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    # 方法1: 检查 /.dockerenv 文件
    if os.path.exists('/.dockerenv'):
        return True
    # 方法2: 检查 cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except:
        pass
    # 方法3: 检查环境变量
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


def _is_truthy_env(name: str) -> bool:
    """判断环境变量是否为 true。"""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DOCKER_HEADED = (
    _is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA")
    or _is_truthy_env("ALLOW_DOCKER_BROWSER_CAPTCHA")
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED

RECAPTCHA_SCRIPT_CACHE_TTL_SECONDS = 86400
RECAPTCHA_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS = 20
RECAPTCHA_ASSET_CACHE_TTL_SECONDS = 86400
RECAPTCHA_CACHE_CLEANUP_MAX_AGE_SECONDS = max(
    RECAPTCHA_SCRIPT_CACHE_TTL_SECONDS,
    RECAPTCHA_ASSET_CACHE_TTL_SECONDS,
) * 3
PERSONAL_RUNTIME_PROFILE_STALE_TTL_SECONDS = 6 * 60 * 60
PERSONAL_PROXY_EXTENSION_STALE_TTL_SECONDS = 6 * 60 * 60
RECAPTCHA_REMOTE_URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\)]+", re.IGNORECASE)
RECAPTCHA_CSS_URL_PATTERN = re.compile(r"url\((.*?)\)", re.IGNORECASE)
RECAPTCHA_STATIC_EXTENSIONS = {
    ".js",
    ".css",
    ".png",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
}
RECAPTCHA_STATIC_HOST_ALIASES = {
    "www.gstatic.com": ("www.gstatic.com", "www.gstatic.cn"),
    "www.gstatic.cn": ("www.gstatic.com", "www.gstatic.cn"),
}


def _path_mtime_age_seconds(path: Path, now_value: float) -> Optional[float]:
    try:
        return max(0.0, now_value - float(path.stat().st_mtime))
    except Exception:
        return None


def _remove_path_quietly(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _cleanup_runtime_artifacts_sync(
    *,
    active_runtime_paths: set[str],
    active_proxy_extension_paths: set[str],
) -> dict[str, int]:
    stats = {
        "profiles_deleted": 0,
        "recaptcha_cache_deleted": 0,
        "proxy_extensions_deleted": 0,
    }
    now_value = time.time()
    normalized_active_runtime_paths = {
        os.path.normcase(os.path.normpath(item))
        for item in active_runtime_paths
        if str(item or "").strip()
    }
    normalized_active_proxy_paths = {
        os.path.normcase(os.path.normpath(item))
        for item in active_proxy_extension_paths
        if str(item or "").strip()
    }

    try:
        if PERSONAL_RUNTIME_TMP_DIR.exists():
            for child in PERSONAL_RUNTIME_TMP_DIR.iterdir():
                child_name = child.name
                normalized_child = os.path.normcase(os.path.normpath(str(child)))
                if child_name.startswith(("browser_profile_", "fresh_browser_profile_", "launch_retry_profile_")):
                    if normalized_child in normalized_active_runtime_paths:
                        continue
                    age_seconds = _path_mtime_age_seconds(child, now_value)
                    if age_seconds is not None and age_seconds >= PERSONAL_RUNTIME_PROFILE_STALE_TTL_SECONDS:
                        if _remove_path_quietly(child):
                            stats["profiles_deleted"] += 1
                elif child_name in {"recaptcha_js", "recaptcha_assets"} and child.is_dir():
                    for cache_file in child.iterdir():
                        if not cache_file.is_file():
                            continue
                        age_seconds = _path_mtime_age_seconds(cache_file, now_value)
                        if age_seconds is None or age_seconds < RECAPTCHA_CACHE_CLEANUP_MAX_AGE_SECONDS:
                            continue
                        if _remove_path_quietly(cache_file):
                            stats["recaptcha_cache_deleted"] += 1
    except Exception:
        pass

    temp_root = Path(tempfile.gettempdir())
    try:
        if temp_root.exists():
            for child in temp_root.iterdir():
                if not child.is_dir() or not child.name.startswith("nodriver_proxy_auth_"):
                    continue
                normalized_child = os.path.normcase(os.path.normpath(str(child)))
                if normalized_child in normalized_active_proxy_paths:
                    continue
                age_seconds = _path_mtime_age_seconds(child, now_value)
                if age_seconds is None or age_seconds < PERSONAL_PROXY_EXTENSION_STALE_TTL_SECONDS:
                    continue
                if _remove_path_quietly(child):
                    stats["proxy_extensions_deleted"] += 1
    except Exception:
        pass

    return stats


# ==================== nodriver 自动安装 ====================
def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """运行 pip install 命令
    
    Args:
        package: 包名
        use_mirror: 是否使用国内镜像
    
    Returns:
        是否安装成功
    """
    cmd = [sys.executable, '-m', 'pip', 'install', package]
    if use_mirror:
        cmd.extend(['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])
    
    try:
        debug_logger.log_info(f"[BrowserCaptcha] 正在安装 {package}...")
        print(f"[BrowserCaptcha] 正在安装 {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ {package} 安装成功")
            print(f"[BrowserCaptcha] ✅ {package} 安装成功")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装异常: {e}")
        return False


def _ensure_nodriver_installed() -> bool:
    """确保 nodriver 已安装
    
    Returns:
        是否安装成功/已安装
    """
    try:
        import nodriver
        debug_logger.log_info("[BrowserCaptcha] nodriver 已安装")
        return True
    except ImportError:
        pass
    
    debug_logger.log_info("[BrowserCaptcha] nodriver 未安装，开始自动安装...")
    print("[BrowserCaptcha] nodriver 未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_pip_install('nodriver', use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install('nodriver', use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ nodriver 自动安装失败，请手动安装: pip install nodriver")
    print("[BrowserCaptcha] ❌ nodriver 自动安装失败，请手动安装: pip install nodriver")
    return False


def _read_windows_app_path(executable_name: str) -> Optional[str]:
    """读取 Windows App Paths 中注册的浏览器路径。"""
    if os.name != "nt":
        return None

    try:
        import winreg
    except Exception:
        return None

    key_candidates = [
        rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}",
        rf"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}",
    ]

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_path in key_candidates:
            try:
                with winreg.OpenKey(root, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    resolved = str(value or "").strip().strip('"')
                    if resolved and os.path.exists(resolved):
                        return os.path.normpath(resolved)
            except Exception:
                continue
    return None


def _detect_real_browser_executable_path() -> Optional[str]:
    """尽量探测本机已安装的真实 Chromium 浏览器，避免交给 nodriver 自行弹选择。"""
    if os.name != "nt":
        linux_browser_candidates = [
            (
                "Google Chrome",
                [
                    shutil.which("google-chrome"),
                    shutil.which("google-chrome-stable"),
                    "/usr/bin/google-chrome",
                    "/usr/bin/google-chrome-stable",
                ],
            ),
            (
                "Microsoft Edge",
                [
                    shutil.which("microsoft-edge"),
                    shutil.which("microsoft-edge-stable"),
                    "/usr/bin/microsoft-edge",
                    "/usr/bin/microsoft-edge-stable",
                ],
            ),
            (
                "Brave",
                [
                    shutil.which("brave-browser"),
                    shutil.which("brave"),
                    "/usr/bin/brave-browser",
                    "/usr/bin/brave",
                ],
            ),
            (
                "Chromium",
                [
                    shutil.which("chromium"),
                    shutil.which("chromium-browser"),
                    "/usr/bin/chromium",
                    "/usr/bin/chromium-browser",
                ],
            ),
        ]
        for browser_name, candidates in linux_browser_candidates:
            for candidate in candidates:
                resolved = str(candidate or "").strip().strip('"')
                if not resolved or not os.path.exists(resolved):
                    continue
                normalized = os.path.normpath(resolved)
                debug_logger.log_info(
                    f"[BrowserCaptcha] 自动检测到真实浏览器 {browser_name}: {normalized}"
                )
                return normalized
        return None

    browser_candidates = [
        (
            "Google Chrome",
            "chrome.exe",
            [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            ],
        ),
        (
            "Microsoft Edge",
            "msedge.exe",
            [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            ],
        ),
        (
            "Brave",
            "brave.exe",
            [
                os.path.join(
                    os.environ.get("LOCALAPPDATA", ""),
                    "BraveSoftware",
                    "Brave-Browser",
                    "Application",
                    "brave.exe",
                ),
                os.path.join(
                    os.environ.get("PROGRAMFILES", ""),
                    "BraveSoftware",
                    "Brave-Browser",
                    "Application",
                    "brave.exe",
                ),
                os.path.join(
                    os.environ.get("PROGRAMFILES(X86)", ""),
                    "BraveSoftware",
                    "Brave-Browser",
                    "Application",
                    "brave.exe",
                ),
            ],
        ),
        (
            "Chromium",
            "chrome.exe",
            [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Chromium", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Chromium", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Chromium", "Application", "chrome.exe"),
            ],
        ),
    ]

    for browser_name, executable_name, candidate_paths in browser_candidates:
        detected_candidates = [
            shutil.which(executable_name),
            _read_windows_app_path(executable_name),
            *candidate_paths,
        ]
        for candidate in detected_candidates:
            resolved = str(candidate or "").strip().strip('"')
            if not resolved or not os.path.exists(resolved):
                continue
            normalized = os.path.normpath(resolved)
            debug_logger.log_info(
                f"[BrowserCaptcha] 自动检测到真实浏览器 {browser_name}: {normalized}"
            )
            return normalized

    return None


def _resolve_browser_executable_path() -> tuple[Optional[str], str]:
    """解析浏览器优先级：环境变量 > auto。"""
    browser_executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
    if browser_executable_path and not os.path.exists(browser_executable_path):
        debug_logger.log_warning(
            f"[BrowserCaptcha] 指定浏览器不存在，改回 nodriver 默认浏览器解析: {browser_executable_path}"
        )
        browser_executable_path = None

    if browser_executable_path:
        normalized = os.path.normpath(browser_executable_path)
        debug_logger.log_info(f"[BrowserCaptcha] 使用环境变量指定浏览器: {normalized}")
        return normalized, "configured"

    return None, "auto"


def _build_personal_browser_args(
    *,
    headless: bool,
    proxy_server_arg: Optional[str] = None,
    proxy_extension_dir: Optional[str] = None,
) -> list[str]:
    """构建 personal 模式浏览器启动参数。

    说明：
    - 始终依赖独立临时 user-data-dir，避免污染系统真实资料。
    - 显式去掉 `--profile-directory=Default` 这种容易误导的配置。
    - 显式加 `--no-startup-window`，避免 Chrome 先弹一个默认普通窗口。
    - 仅在未加载代理认证扩展时附加 `--incognito`，避免扩展在无痕窗口中失效。
    """
    browser_args = [
        '--disable-quic',
        '--disable-features=UseDnsHttpsSvcb,OptimizationHints,AutofillServerCommunication,CertificateTransparencyComponentUpdater,MediaRouter,GlobalMediaControls',
        '--disable-dev-shm-usage',
        '--disable-setuid-sandbox',
        '--disable-breakpad',
        '--disable-client-side-phishing-detection',
        '--disable-gpu',
        '--disable-infobars',
        '--hide-scrollbars',
        '--window-size=1280,720',
        '--disable-background-networking',
        '--disable-component-update',
        '--disable-domain-reliability',
        '--disable-sync',
        '--disable-translate',
        '--disable-default-apps',
        '--metrics-recording-only',
        '--mute-audio',
        '--safebrowsing-disable-auto-update',
        '--no-first-run',
        '--no-default-browser-check',
        '--no-zygote',
    ]

    if headless:
        browser_args.append('--no-startup-window')
        browser_args.append('--window-position=3000,3000')
    else:
        browser_args.append('--window-position=80,80')

    if proxy_server_arg:
        browser_args.append(proxy_server_arg)

    if proxy_extension_dir:
        # 代理认证扩展在 bwsi/incognito 风格会话下容易失效，保持临时 profile 即可满足隔离需求。
        browser_args.append(f'--load-extension={proxy_extension_dir}')
    else:
        browser_args.append('--bwsi')
        browser_args.append('--disable-extensions')
        browser_args.append('--incognito')

    return browser_args


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_browser_launch_parallelism_limit() -> int:
    default_limit = 1 if os.name == "nt" else 2
    raw_value = os.environ.get("PERSONAL_BROWSER_LAUNCH_PARALLELISM", "").strip()
    try:
        return max(1, min(4, int(raw_value or default_limit)))
    except Exception:
        return default_limit


def _resolve_personal_browser_sandbox_enabled() -> bool:
    """尽量沿用真实浏览器默认沙箱；仅在 root/显式禁用时关闭。"""
    if _env_truthy("PERSONAL_BROWSER_DISABLE_SANDBOX"):
        return False
    if _env_truthy("PERSONAL_BROWSER_FORCE_SANDBOX"):
        return True
    if os.name != "posix":
        return True
    if hasattr(os, "geteuid"):
        try:
            return os.geteuid() != 0
        except Exception:
            return False
    return False


def _normalize_personal_browser_args_for_launch(
    browser_args: list[str],
    *,
    sandbox_enabled: bool,
) -> list[str]:
    normalized_args: list[str] = []
    for arg in browser_args:
        if sandbox_enabled and arg in {"--disable-setuid-sandbox", "--no-sandbox"}:
            continue
        normalized_args.append(arg)
    return normalized_args


def _tune_personal_browser_args_for_docker_headed(
    browser_args: list[str],
) -> list[str]:
    """Make Docker headed Chromium look closer to a regular desktop session."""
    removable_exact = {
        '--disable-dev-shm-usage',
        '--disable-setuid-sandbox',
        '--disable-gpu',
        '--disable-infobars',
        '--hide-scrollbars',
        '--disable-background-networking',
        '--disable-sync',
        '--disable-translate',
        '--disable-default-apps',
        '--bwsi',
        '--incognito',
        '--disable-extensions',
        '--no-zygote',
    }
    removable_prefixes = (
        '--window-size=',
        '--window-position=',
        '--lang=',
        '--use-gl=',
        '--ozone-platform=',
        '--password-store=',
    )

    tuned_args: list[str] = []
    for arg in browser_args:
        if arg in removable_exact:
            continue
        if any(arg.startswith(prefix) for prefix in removable_prefixes):
            continue
        tuned_args.append(arg)

    tuned_args.extend([
        '--window-size=1366,768',
        '--window-position=40,40',
        '--lang=zh-CN',
        '--password-store=basic',
        '--ozone-platform=x11',
        '--use-gl=swiftshader',
    ])
    return tuned_args


# 尝试导入 nodriver
uc = None
NODRIVER_AVAILABLE = False
_NODRIVER_RUNTIME_PATCHED = False

if DOCKER_HEADED_BLOCKED:
    debug_logger.log_warning(
        "[BrowserCaptcha] 检测到 Docker 环境，默认禁用内置浏览器打码。"
        "如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true。"
        "personal 模式默认支持无头，不强制依赖 DISPLAY/虚拟显示。"
    )
    print("[BrowserCaptcha] ⚠️ 检测到 Docker 环境，默认禁用内置浏览器打码")
    print("[BrowserCaptcha] 如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true")
else:
    if IS_DOCKER and ALLOW_DOCKER_HEADED:
        debug_logger.log_warning(
            "[BrowserCaptcha] Docker 内置浏览器打码白名单已启用，personal 模式将按 headless 配置决定是否需要 DISPLAY/虚拟显示"
        )
        print("[BrowserCaptcha] ✅ Docker 内置浏览器打码白名单已启用")
    if _ensure_nodriver_installed():
        try:
            import nodriver as uc
            NODRIVER_AVAILABLE = True
        except ImportError as e:
            debug_logger.log_error(f"[BrowserCaptcha] nodriver 导入失败: {e}")
            print(f"[BrowserCaptcha] ❌ nodriver 导入失败: {e}")


_RUNTIME_ERROR_KEYWORDS = (
    "has been closed",
    "browser has been closed",
    "target closed",
    "has no attribute \"closed\"",
    "has no attribute 'closed'",
    "connection closed",
    "connection lost",
    "connection refused",
    "connection reset",
    "broken pipe",
    "session closed",
    "not attached to an active page",
    "no session with given id",
    "cannot find context with specified id",
    "websocket is not open",
    "websocket unavailable",
    "'nonetype' object has no attribute 'send'",
    '"nonetype" object has no attribute "send"',
    "no close frame received or sent",
    "cannot call write to closing transport",
    "cannot write to closing transport",
    "cannot call send once a close message has been sent",
    "connectionclosederror",
    "connectionrefusederror",
    "disconnected",
    "errno 111",
)

_NORMAL_CLOSE_KEYWORDS = (
    "connectionclosedok",
    "normal closure",
    "normal_closure",
    "sent 1000 (ok)",
    "received 1000 (ok)",
    "close(code=1000",
)


def _flatten_exception_text(error: Any) -> str:
    """拼接异常链文本，便于统一识别 nodriver 运行态断连。"""
    visited: set[int] = set()
    pending = [error]
    parts: list[str] = []

    while pending:
        current = pending.pop()
        if current is None:
            continue

        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)

        parts.append(type(current).__name__)

        message = str(current or "").strip()
        if message:
            parts.append(message)

        args = getattr(current, "args", None)
        if isinstance(args, tuple):
            for arg in args:
                arg_text = str(arg or "").strip()
                if arg_text:
                    parts.append(arg_text)

        pending.append(getattr(current, "__cause__", None))
        pending.append(getattr(current, "__context__", None))

    return " | ".join(parts).lower()


def _is_runtime_disconnect_error(error: Any) -> bool:
    """识别浏览器 / websocket 运行态断连。"""
    error_text = _flatten_exception_text(error)
    if not error_text:
        return False
    return any(keyword in error_text for keyword in _RUNTIME_ERROR_KEYWORDS) or any(
        keyword in error_text for keyword in _NORMAL_CLOSE_KEYWORDS
    )


def _is_runtime_normal_close_error(error: Any) -> bool:
    """识别 websocket 正常关闭（1000）这类预期退场。"""
    error_text = _flatten_exception_text(error)
    if not error_text:
        return False
    return any(keyword in error_text for keyword in _NORMAL_CLOSE_KEYWORDS)


def _finalize_nodriver_send_task(connection, transaction, tx_id: int, task: asyncio.Task):
    """回收 nodriver websocket.send 的后台异常，避免事件循环打印未检索 task 错误。"""
    try:
        task.result()
    except asyncio.CancelledError:
        connection.mapper.pop(tx_id, None)
        if not transaction.done():
            transaction.cancel()
    except Exception as e:
        connection.mapper.pop(tx_id, None)
        if not transaction.done():
            try:
                transaction.set_exception(e)
            except Exception:
                pass

        if _is_runtime_normal_close_error(e):
            debug_logger.log_info(
                f"[BrowserCaptcha] nodriver websocket 在正常关闭后退出: {type(e).__name__}: {e}"
            )
        elif _is_runtime_disconnect_error(e):
            debug_logger.log_warning(
                f"[BrowserCaptcha] nodriver websocket 发送在断连后退出: {type(e).__name__}: {e}"
            )
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] nodriver websocket 发送异常: {type(e).__name__}: {e}"
            )


def _is_nodriver_connection_closed(connection_instance) -> bool:
    """兼容不同 nodriver 版本的连接状态判断。"""
    try:
        return bool(getattr(connection_instance, "closed"))
    except AttributeError:
        pass
    except Exception:
        return True

    websocket = getattr(connection_instance, "websocket", None)
    if websocket is None:
        return True

    try:
        return bool(getattr(websocket, "close_code", None))
    except Exception:
        return True


def _patch_nodriver_connection_instance(connection_instance):
    """在连接实例级别收口 websocket.send 的后台异常。"""
    if not connection_instance or getattr(connection_instance, "_flow2api_send_patched", False):
        return
    if (
        not callable(getattr(connection_instance, "send", None))
        or not callable(getattr(connection_instance, "connect", None))
        or not callable(getattr(connection_instance, "_register_handlers", None))
        or not hasattr(connection_instance, "mapper")
        or not hasattr(connection_instance, "__count__")
    ):
        return

    try:
        from nodriver.core import connection as nodriver_connection_module
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] 加载 nodriver.connection 失败，跳过连接补丁: {e}")
        return

    async def patched_send(self, cdp_obj, _is_update=False):
        if _is_nodriver_connection_closed(self):
            await self.connect()
        if not _is_update:
            await self._register_handlers()

        transaction = nodriver_connection_module.Transaction(cdp_obj)
        tx_id = next(self.__count__)
        transaction.id = tx_id
        self.mapper[tx_id] = transaction

        websocket = getattr(self, "websocket", None)
        if websocket is None:
            self.mapper.pop(tx_id, None)
            if not transaction.done():
                transaction.cancel()
            raise ConnectionError("nodriver websocket unavailable after connect")

        send_task = asyncio.create_task(websocket.send(transaction.message))
        send_task.add_done_callback(
            lambda task, connection=self, tx=transaction, current_tx_id=tx_id:
            _finalize_nodriver_send_task(connection, tx, current_tx_id, task)
        )
        return await transaction

    connection_instance.send = types.MethodType(patched_send, connection_instance)
    connection_instance._flow2api_send_patched = True


def _patch_nodriver_browser_instance(browser_instance):
    """在浏览器实例级别收口 update_targets，并补齐新 target 的连接补丁。"""
    if not browser_instance:
        return

    _patch_nodriver_connection_instance(getattr(browser_instance, "connection", None))
    for target in list(getattr(browser_instance, "targets", []) or []):
        _patch_nodriver_connection_instance(target)

    if getattr(browser_instance, "_flow2api_update_targets_patched", False):
        return

    original_update_targets = browser_instance.update_targets

    async def patched_update_targets(self, *args, **kwargs):
        try:
            result = await original_update_targets(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
                if _is_runtime_disconnect_error(e):
                    try:
                        setattr(self, "_flow2api_runtime_disconnected", True)
                    except Exception:
                        pass
                    try:
                        self.targets = []
                    except Exception:
                        pass
                    log_message = (
                        f"[BrowserCaptcha] nodriver.update_targets 在浏览器断连后退出: "
                        f"{type(e).__name__}: {e}"
                    )
                    if _is_runtime_normal_close_error(e):
                        debug_logger.log_info(log_message)
                    else:
                        debug_logger.log_warning(log_message)
                    return []
                raise

        _patch_nodriver_connection_instance(getattr(self, "connection", None))
        for target in list(getattr(self, "targets", []) or []):
            _patch_nodriver_connection_instance(target)
        try:
            setattr(self, "_flow2api_runtime_disconnected", False)
        except Exception:
            pass
        return result

    browser_instance.update_targets = types.MethodType(patched_update_targets, browser_instance)
    browser_instance._flow2api_update_targets_patched = True


def _patch_nodriver_runtime(browser_instance=None):
    """给 nodriver 当前浏览器实例补一层断连降噪与异常透传。"""
    global _NODRIVER_RUNTIME_PATCHED

    if not NODRIVER_AVAILABLE or uc is None:
        return

    if browser_instance is not None:
        _patch_nodriver_browser_instance(browser_instance)

    if not _NODRIVER_RUNTIME_PATCHED:
        _NODRIVER_RUNTIME_PATCHED = True
        debug_logger.log_info("[BrowserCaptcha] 已启用 nodriver 运行态安全补丁")


def _parse_proxy_url(proxy_url: str):
    """Parse a proxy URL into (protocol, host, port, username, password)."""
    if not proxy_url:
        return None, None, None, None, None
    url = proxy_url.strip()
    if not re.match(r'^(http|https|socks5h?|socks5)://', url):
        url = f"http://{url}"
    m = re.match(r'^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$', url)
    if not m:
        return None, None, None, None, None
    protocol, username, password, host, port = m.groups()
    if protocol == "socks5h":
        protocol = "socks5"
    return protocol, host, port, username, password


def _compose_proxy_url(
    protocol: Optional[str],
    host: Optional[str],
    port: Optional[str],
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[str]:
    """Compose a proxy URL from parsed proxy parts."""
    if not protocol or not host or not port:
        return None

    auth = ""
    if username and password:
        auth = f"{username}:{password}@"

    return f"{protocol}://{auth}{host}:{port}"


def _parse_windows_proxy_server_candidates(proxy_server: str) -> list[str]:
    """Parse Windows Internet Settings ProxyServer into normalized proxy URLs."""
    normalized_candidates: list[str] = []
    seen: set[str] = set()

    for raw_item in str(proxy_server or "").split(";"):
        item = str(raw_item or "").strip()
        if not item:
            continue
        if "=" in item:
            _, item = item.split("=", 1)
            item = item.strip()
        if not item:
            continue
        normalized = item if re.match(r"^[a-z]+://", item, re.I) else f"http://{item}"
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized_candidates.append(normalized)

    return normalized_candidates


def _read_windows_internet_settings_proxy_candidates() -> list[str]:
    """Read ProxyServer candidates from Windows Internet Settings."""
    if os.name != "nt":
        return []

    try:
        import winreg
    except Exception:
        return []

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key_path) as key:
                proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except Exception:
            continue

        candidates = _parse_windows_proxy_server_candidates(str(proxy_server or ""))
        if candidates:
            return candidates

    return []


def _get_recaptcha_script_cache_dir() -> Path:
    """Return the persistent cache directory for reCAPTCHA bootstrap scripts."""
    cache_dir = PERSONAL_RUNTIME_TMP_DIR / "recaptcha_js"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_recaptcha_script_cache_path(cache_dir: Path, remote_url: str) -> Path:
    """Map a bootstrap script URL to a stable local cache path."""
    digest = hashlib.md5(remote_url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.js"


def _write_text_cache(cache_path: Path, content: str):
    """Atomically write UTF-8 text content into the cache."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(f"{cache_path.suffix}.part")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(temp_path, cache_path)


def _get_recaptcha_asset_cache_dir() -> Path:
    """Return the persistent cache directory for local reCAPTCHA static assets."""
    cache_dir = PERSONAL_RUNTIME_TMP_DIR / "recaptcha_assets"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _guess_recaptcha_asset_mime_type(remote_url: str, response_mime: Optional[str] = None) -> str:
    """Best-effort MIME type detection for cached reCAPTCHA assets."""
    normalized = (response_mime or "").split(";", 1)[0].strip().lower()
    if normalized:
        return normalized

    guessed, _ = mimetypes.guess_type(urlparse(remote_url).path)
    if guessed:
        return guessed

    suffix = Path(urlparse(remote_url).path).suffix.lower()
    if suffix == ".js":
        return "text/javascript"
    if suffix == ".css":
        return "text/css"
    if suffix == ".woff2":
        return "font/woff2"
    if suffix == ".woff":
        return "font/woff"
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".ico":
        return "image/x-icon"
    return "application/octet-stream"


def _get_recaptcha_asset_cache_path(cache_dir: Path, remote_url: str) -> Path:
    """Map any reCAPTCHA static asset URL to a stable local cache path."""
    digest = hashlib.md5(remote_url.encode("utf-8")).hexdigest()
    suffix = Path(urlparse(remote_url).path).suffix.lower() or ".bin"
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    return cache_dir / f"{digest}{suffix}"


def _write_binary_cache(cache_path: Path, content: bytes):
    """Atomically write binary content into the cache."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(f"{cache_path.suffix}.part")
    with open(temp_path, "wb") as f:
        f.write(content)
    os.replace(temp_path, cache_path)


def _extract_remote_urls_from_text(source: str) -> list[str]:
    """Extract absolute remote URLs from JavaScript/CSS text."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in RECAPTCHA_REMOTE_URL_PATTERN.findall(source or ""):
        normalized = match.strip().rstrip("),;\"'")
        if not normalized.startswith(("http://", "https://")) or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def _extract_remote_urls_from_css(css_source: str, base_url: str) -> list[str]:
    """Extract absolute asset URLs referenced from a CSS source."""
    urls: list[str] = []
    seen: set[str] = set()
    for raw_value in RECAPTCHA_CSS_URL_PATTERN.findall(css_source or ""):
        normalized = raw_value.strip().strip("\"'")
        if not normalized or normalized.startswith(("data:", "blob:", "javascript:")):
            continue
        absolute = urljoin(base_url, normalized)
        if not absolute.startswith(("http://", "https://")) or absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
    return urls


def _rewrite_css_urls_with_local_assets(
    css_source: str,
    base_url: str,
    replacements: Dict[str, str],
) -> str:
    """Rewrite CSS url(...) references to their local data URLs."""

    def _replace(match: re.Match[str]) -> str:
        raw_value = match.group(1).strip()
        normalized = raw_value.strip("\"'")
        if not normalized or normalized.startswith(("data:", "blob:", "javascript:")):
            return match.group(0)

        absolute = urljoin(base_url, normalized)
        local_value = replacements.get(absolute)
        if not local_value:
            return match.group(0)
        return f"url('{local_value}')"

    return RECAPTCHA_CSS_URL_PATTERN.sub(_replace, css_source or "")


def _rewrite_text_urls_with_local_assets(
    source_text: str,
    replacements: Dict[str, str],
) -> str:
    """Rewrite literal remote URLs in JS/text content to local data URLs."""
    localized = source_text or ""
    for remote_url in sorted(replacements.keys(), key=len, reverse=True):
        localized = localized.replace(remote_url, replacements[remote_url])
    return localized


def _build_data_url(content: bytes, mime_type: str) -> str:
    """Encode bytes as a data: URL."""
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _is_localizable_recaptcha_asset_url(remote_url: str) -> bool:
    """Check whether the remote URL is a static asset suitable for local mirroring."""
    try:
        parsed = urlparse(remote_url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower()
    path = parsed.path.lower()
    suffix = Path(path).suffix.lower()

    if host in {"www.gstatic.com", "www.gstatic.cn", "fonts.gstatic.com"}:
        return suffix in RECAPTCHA_STATIC_EXTENSIONS or "/recaptcha/" in path or "/api2/" in path

    if host in {"www.google.com", "www.recaptcha.net"}:
        return path.startswith("/recaptcha/") and suffix in {".js", ".css"}

    if host == "labs.google":
        return suffix in RECAPTCHA_STATIC_EXTENSIONS

    return False


def _iter_recaptcha_asset_url_aliases(remote_url: str) -> list[str]:
    """Return equivalent host aliases for gstatic-hosted assets."""
    try:
        parsed = urlparse(remote_url)
    except Exception:
        return [remote_url]

    aliases: list[str] = []
    seen: set[str] = set()
    candidate_hosts = RECAPTCHA_STATIC_HOST_ALIASES.get(parsed.netloc.lower(), (parsed.netloc,))
    for host in candidate_hosts:
        candidate = urlunparse(parsed._replace(netloc=host))
        if candidate in seen:
            continue
        seen.add(candidate)
        aliases.append(candidate)
    return aliases


def _iter_recaptcha_release_companion_urls(remote_url: str) -> list[str]:
    """Derive same-release companion assets from a locale JS URL."""
    try:
        parsed = urlparse(remote_url)
    except Exception:
        return []

    match = re.search(r"/recaptcha/releases/([^/]+)/recaptcha__[^/]+\.js$", parsed.path)
    if not match:
        return []

    release_id = match.group(1)
    locale_match = re.search(r"/recaptcha__([^/]+)\.js$", parsed.path)
    locale = (locale_match.group(1) if locale_match else "en").replace("_", "-")
    companions: list[str] = []
    for css_name in ("styles__ltr.css", "styles__rtl.css"):
        companions.append(
            urlunparse(
                parsed._replace(
                    path=f"/recaptcha/releases/{release_id}/{css_name}",
                    query="",
                    fragment="",
                )
            )
        )
    for host in ("www.recaptcha.net", "www.google.com"):
        companions.append(
            urlunparse(
                parsed._replace(
                    scheme="https",
                    netloc=host,
                    path="/recaptcha/enterprise/webworker.js",
                    query=f"hl={locale}&v={release_id}",
                    fragment="",
                )
            )
        )
    return companions


def _create_proxy_auth_extension(protocol: str, host: str, port: str, username: str, password: str) -> str:
    """Create a temporary Chrome extension directory for proxy authentication.
    Returns the path to the extension directory."""
    ext_dir = tempfile.mkdtemp(prefix="nodriver_proxy_auth_")

    scheme_map = {"http": "http", "https": "https", "socks5": "socks5"}
    scheme = scheme_map.get(protocol, "http")

    manifest = {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Proxy Auth Helper",
        "permissions": [
            "proxy",
            "storage",
            "webRequest",
            "webRequestAuthProvider",
        ],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "108.0.0.0",
    }
    background_js = (
        "const config = {\n"
        '    mode: "fixed_servers",\n'
        "    rules: {\n"
        "        singleProxy: {\n"
        f"            scheme: {json.dumps(scheme)},\n"
        f"            host: {json.dumps(host)},\n"
        f"            port: parseInt({port})\n"
        "        },\n"
        '        bypassList: ["localhost", "127.0.0.1"]\n'
        "    }\n"
        "};\n"
        "function applyProxyConfig() {\n"
        '    chrome.proxy.settings.set({value: config, scope: "regular"}, () => {\n'
        "        if (chrome.runtime.lastError) {\n"
        '            console.warn("proxy.settings.set failed", chrome.runtime.lastError.message);\n'
        "        }\n"
        "    });\n"
        "}\n"
        "chrome.runtime.onInstalled.addListener(applyProxyConfig);\n"
        "chrome.runtime.onStartup.addListener(applyProxyConfig);\n"
        "applyProxyConfig();\n"
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "    (details, callback) => {\n"
        "        if (!details.isProxy) {\n"
        "            callback({});\n"
        "            return;\n"
        "        }\n"
        "        callback({\n"
        "            authCredentials: {\n"
        f"                username: {json.dumps(username)},\n"
        f"                password: {json.dumps(password)}\n"
        "            }\n"
        "        });\n"
        "    },\n"
        '    {urls: ["<all_urls>"]},\n'
        "    ['asyncBlocking']\n"
        ");\n"
    )
    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)
    return ext_dir


class ResidentTabInfo:
    """常驻标签页信息结构"""
    def __init__(
        self,
        tab,
        slot_id: str,
        project_id: Optional[str] = None,
        *,
        token_id: Optional[int] = None,
        browser_context_id: Any = None,
    ):
        self.tab = tab
        self.slot_id = slot_id
        self.project_id = project_id or slot_id
        self.token_id = token_id
        self.browser_context_id = browser_context_id
        self.recaptcha_ready = False
        self.created_at = time.time()
        self.last_used_at = time.time()  # 最后使用时间
        self.use_count = 0  # 使用次数
        self.fingerprint: Optional[Dict[str, Any]] = None
        self.cookie_signature: Optional[str] = None
        self.solve_lock = asyncio.Lock()  # 串行化同一标签页上的执行，降低并发冲突
        self.pending_assignment_count = 0  # 选中但尚未真正进入 solve_lock 的请求数


@dataclass
class TokenPoolLease:
    bucket_key: str
    token: str
    project_id: str
    action: str
    token_id: Optional[int]
    slot_id: Optional[str]
    worker_index: Optional[int]
    created_at: float
    expires_at: float


class TokenPoolTimeoutError(TimeoutError):
    """严格 token 池模式下，请求在等待可用 token 时超时。"""


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（nodriver 有头模式）
    
    支持两种模式：
    1. 常驻模式 (Resident Mode): 维护全局共享常驻标签页池，谁抢到空闲页谁执行
    2. 传统模式 (Legacy Mode): 每次请求创建新标签页 (fallback)
    """

    _instance: Optional['BrowserCaptchaService'] = None
    _pool_instance: Optional['_PersonalBrowserPoolService'] = None
    _lock = asyncio.Lock()
    _launch_gate: Optional[asyncio.Semaphore] = None
    _launch_gate_loop: Optional[asyncio.AbstractEventLoop] = None
    _launch_gate_limit: int = 0

    @classmethod
    def _get_global_browser_launch_gate(cls) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        limit = _resolve_browser_launch_parallelism_limit()
        if (
            cls._launch_gate is None
            or cls._launch_gate_loop is not loop
            or cls._launch_gate_limit != limit
        ):
            cls._launch_gate = asyncio.Semaphore(limit)
            cls._launch_gate_loop = loop
            cls._launch_gate_limit = limit
        return cls._launch_gate

    def __init__(
        self,
        db=None,
        *,
        browser_instance_id: int = 0,
        max_resident_tabs_override: Optional[int] = None,
    ):
        """初始化服务"""
        self.headless = bool(getattr(config, "personal_headless", False))  # 是否无头由配置控制
        self.browser = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        self._browser_instance_id = 0
        self._slot_id_prefix = ""
        self._max_resident_tabs_override: Optional[int] = None
        self._apply_browser_instance_identity(browser_instance_id)
        self.apply_pool_worker_settings(
            browser_instance_id=browser_instance_id,
            max_resident_tabs_override=max_resident_tabs_override,
        )
        self._runtime_ephemeral_user_data_dir: Optional[str] = None
        self._managed_runtime_profile_dirs: set[str] = set()
        self._browser_process_pid: Optional[int] = None
        self.user_data_dir = self._resolve_user_data_dir(self.headless)
        self._visible_startup_target_id: Optional[str] = None
        self._headless_host_target_id: Optional[str] = None
        self._runtime_fingerprint_spoof_seed = ""
        self._runtime_surface_profile: Dict[str, Any] = {}
        self._refresh_runtime_fingerprint_spoof_seed()

        # 常驻模式相关属性
        self._resident_tabs: dict[str, 'ResidentTabInfo'] = {}  # slot_id -> 常驻标签页信息
        self._token_resident_affinity: dict[str, str] = {}  # token_id -> slot_id（优先保证 token 独占 context）
        self._project_resident_affinity: dict[str, str] = {}  # project_id -> slot_id（最近一次使用）
        self._resident_slot_seq = 0
        self._resident_pick_index = 0
        self._resident_lock = asyncio.Lock()  # 保护常驻标签页操作
        self._browser_lock = asyncio.Lock()  # 保护浏览器初始化/关闭/重启，避免重复拉起实例
        self._runtime_recover_lock = asyncio.Lock()  # 串行化浏览器级恢复，避免并发重启风暴
        self._tab_build_lock = asyncio.Lock()  # 串行化冷启动/重建，降低 nodriver 抖动
        self._legacy_lock = asyncio.Lock()  # 避免 legacy fallback 并发失控创建临时标签页
        configured_total_tabs = getattr(config, "personal_max_resident_tabs", 5)
        self._max_resident_tabs = self._resolve_personal_max_resident_tabs(configured_total_tabs)
        self._idle_tab_ttl_seconds = max(
            60,
            int(getattr(config, "personal_idle_tab_ttl_seconds", 600) or 600),
        )
        self._idle_reaper_task: Optional[asyncio.Task] = None  # 空闲回收任务
        self._command_timeout_seconds = 8.0
        self._navigation_timeout_seconds = 20.0
        self._solve_timeout_seconds = 45.0
        self._session_refresh_timeout_seconds = 45.0
        self._health_probe_ttl_seconds = max(
            0.0,
            float(getattr(config, "browser_personal_health_probe_ttl_seconds", 10.0) or 10.0),
        )
        self._last_health_probe_at = 0.0
        self._last_health_probe_ok = False
        self._fingerprint_cache_ttl_seconds = max(
            0.0,
            float(getattr(config, "browser_personal_fingerprint_ttl_seconds", 300.0) or 300.0),
        )
        self._last_fingerprint_at = 0.0

        # 兼容旧 API（保留 single resident 属性作为别名）
        self.resident_project_id: Optional[str] = None  # 向后兼容
        self.resident_tab = None                         # 向后兼容
        self._running = False                            # 向后兼容
        self._recaptcha_ready = False                    # 向后兼容
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self._resident_error_streaks: dict[str, int] = {}
        self._resident_unavailable_slots: set[str] = set()
        self._resident_warmup_task: Optional[asyncio.Task] = None
        self._resident_rebuild_tasks: dict[str, asyncio.Task] = {}
        self._resident_recovery_tasks: dict[str, asyncio.Task] = {}
        self._last_runtime_restart_at = 0.0
        self._runtime_last_active_at = time.time()
        self._successful_solves_since_browser_start = 0
        self._fresh_profile_restart_pending = False
        self._fresh_profile_restart_task: Optional[asyncio.Task] = None
        self._fresh_profile_restart_force_pending = False
        self._browser_launch_failure_streak = 0
        self._browser_launch_cooldown_until = 0.0
        self._browser_launch_last_error = ""
        self._fresh_profile_restart_pending_reason = ""
        self._proxy_url: Optional[str] = None
        self._proxy_ext_dir: Optional[str] = None
        self._proxy_config_signature: str = ""
        self._recaptcha_script_cache_dir = _get_recaptcha_script_cache_dir()
        self._recaptcha_script_cache_lock = asyncio.Lock()
        self._recaptcha_asset_cache_dir = _get_recaptcha_asset_cache_dir()
        self._recaptcha_asset_cache_lock = asyncio.Lock()
        self._recaptcha_asset_data_url_cache: dict[str, str] = {}
        self._recaptcha_asset_bundle_signature: Optional[str] = None
        self._recaptcha_asset_bundle: Optional[Dict[str, Any]] = None
        self._recaptcha_asset_hook_source: Optional[str] = None
        # 自定义站点打码常驻页（用于 score-test）
        self._custom_tabs: dict[str, Dict[str, Any]] = {}
        self._custom_lock = asyncio.Lock()
        self._refresh_runtime_tunables()

    def _apply_browser_instance_identity(self, browser_instance_id: int) -> None:
        normalized_instance_id = max(0, int(browser_instance_id or 0))
        self._browser_instance_id = normalized_instance_id
        self._slot_id_prefix = f"b{normalized_instance_id}-" if normalized_instance_id > 0 else ""

    def apply_pool_worker_settings(
        self,
        *,
        browser_instance_id: Optional[int] = None,
        max_resident_tabs_override: Optional[int] = None,
    ) -> None:
        if browser_instance_id is not None:
            self._apply_browser_instance_identity(browser_instance_id)

        if max_resident_tabs_override is None:
            self._max_resident_tabs_override = None
        else:
            self._max_resident_tabs_override = max(1, min(50, int(max_resident_tabs_override)))

        # pool 调整分片配额时，worker 需要立即更新本地有效 resident 上限；
        # 否则新创建的 worker 会沿用旧值，导致明明配置了多浏览器/多标签，
        # 实际每个实例仍只跑 1 个 resident slot。
        configured_total_tabs = getattr(config, "personal_max_resident_tabs", 5)
        self._max_resident_tabs = self._resolve_personal_max_resident_tabs(configured_total_tabs)

    def _create_fresh_runtime_profile_dir(self, *, prefix: str = "fresh_browser_profile_") -> str:
        PERSONAL_RUNTIME_TMP_DIR.mkdir(parents=True, exist_ok=True)
        fresh_profile_dir = tempfile.mkdtemp(
            prefix=prefix,
            dir=str(PERSONAL_RUNTIME_TMP_DIR),
        )
        normalized_dir = os.path.normpath(str(fresh_profile_dir))
        self._managed_runtime_profile_dirs.add(normalized_dir)
        self._runtime_ephemeral_user_data_dir = normalized_dir
        self.user_data_dir = normalized_dir
        return normalized_dir

    def _resolve_user_data_dir(self, headless: Optional[bool] = None) -> Optional[str]:
        _ = self.headless if headless is None else bool(headless)
        existing_runtime_profile = str(getattr(self, "_runtime_ephemeral_user_data_dir", "") or "").strip()
        if existing_runtime_profile:
            return os.path.normpath(existing_runtime_profile)

        profile_override = os.environ.get("PERSONAL_BROWSER_USER_DATA_DIR", "").strip()
        if profile_override:
            return os.path.normpath(profile_override)

        return self._create_fresh_runtime_profile_dir(prefix="browser_profile_")

    def _default_runtime_profile_dir(self) -> Path:
        return (PERSONAL_RUNTIME_DATA_DIR / "browser_profile").resolve()

    def _is_runtime_managed_profile_dir(self, path_value: Optional[str]) -> bool:
        normalized_path = str(path_value or "").strip()
        if not normalized_path:
            return False

        try:
            resolved_path = Path(normalized_path).resolve()
            runtime_data_dir = PERSONAL_RUNTIME_DATA_DIR.resolve()
            runtime_tmp_dir = PERSONAL_RUNTIME_TMP_DIR.resolve()
        except Exception:
            return False

        return (
            resolved_path == runtime_data_dir
            or runtime_data_dir in resolved_path.parents
            or resolved_path == runtime_tmp_dir
            or runtime_tmp_dir in resolved_path.parents
        )

    def _collect_runtime_profile_cleanup_targets(self) -> list[Path]:
        targets: list[Path] = []
        seen_targets: set[str] = set()

        for raw_path in (
            self.user_data_dir,
            self._runtime_ephemeral_user_data_dir,
            str(self._default_runtime_profile_dir()),
            *list(getattr(self, "_managed_runtime_profile_dirs", set()) or set()),
        ):
            normalized_path = str(raw_path or "").strip()
            if not normalized_path or not self._is_runtime_managed_profile_dir(normalized_path):
                continue
            try:
                resolved_path = Path(normalized_path).resolve()
            except Exception:
                continue
            target_key = os.path.normcase(str(resolved_path))
            if target_key in seen_targets:
                continue
            seen_targets.add(target_key)
            targets.append(resolved_path)

        return targets

    async def _purge_runtime_profile_dirs(self, reason: str) -> None:
        current_user_data_dir = str(self.user_data_dir or "").strip()
        cleanup_targets = self._collect_runtime_profile_cleanup_targets()
        if current_user_data_dir and not self._is_runtime_managed_profile_dir(current_user_data_dir):
            next_profile_dir = self._create_fresh_runtime_profile_dir()
            debug_logger.log_warning(
                "[BrowserCaptcha] 当前 user_data_dir 不在运行时目录下，"
                f"本次恢复改用全新临时 profile: {next_profile_dir} (reason={reason})"
            )
            return

        if not cleanup_targets:
            next_profile_dir = self._create_fresh_runtime_profile_dir()
            debug_logger.log_warning(
                "[BrowserCaptcha] 未找到可清理的运行时 profile，"
                f"改用新的临时无状态 profile: {next_profile_dir} (reason={reason})"
            )
            return

        for target_dir in cleanup_targets:
            try:
                if target_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, str(target_dir), True)
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 已删除浏览器 profile 目录以执行全新冷启动: {target_dir}"
                    )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 删除浏览器 profile 目录失败 (reason={reason}, path={target_dir}): {e}"
                )

        next_profile_dir = self._create_fresh_runtime_profile_dir()
        debug_logger.log_warning(
            f"[BrowserCaptcha] profile 清理完成，下一次启动将使用全新临时 profile: {next_profile_dir} (reason={reason})"
        )

    async def _cleanup_runtime_profile_dirs_after_shutdown(self, *, reason: str) -> bool:
        current_user_data_dir = str(self.user_data_dir or "").strip()
        cleanup_targets = self._collect_runtime_profile_cleanup_targets()
        if current_user_data_dir and not self._is_runtime_managed_profile_dir(current_user_data_dir):
            next_profile_dir = self._create_fresh_runtime_profile_dir()
            debug_logger.log_info(
                "[BrowserCaptcha] 关闭后检测到自定义 profile 路径，"
                f"下一次启动改用全新临时 profile: {next_profile_dir} (reason={reason})"
            )
            return False

        if not cleanup_targets:
            next_profile_dir = self._create_fresh_runtime_profile_dir()
            debug_logger.log_info(
                f"[BrowserCaptcha] 关闭后未发现可复用 profile，已准备新的临时 profile: {next_profile_dir} (reason={reason})"
            )
            return False

        for target_dir in cleanup_targets:
            try:
                if target_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, str(target_dir), True)
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 已清理关闭后的运行时 profile 目录: {target_dir} (reason={reason})"
                    )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 清理关闭后的运行时 profile 目录失败 (reason={reason}, path={target_dir}): {e}"
                )

        next_profile_dir = self._create_fresh_runtime_profile_dir()
        debug_logger.log_info(
            f"[BrowserCaptcha] 关闭后已切换到新的临时 profile: {next_profile_dir} (reason={reason})"
        )
        return True

    def _resolve_personal_max_resident_tabs(self, configured_tabs: Optional[int] = None) -> int:
        """计算当前模式下的有效 resident tab 上限。"""
        try:
            resolved_tabs = (
                self._max_resident_tabs_override
                if self._max_resident_tabs_override is not None
                else configured_tabs
            )
            return max(1, min(50, int(resolved_tabs if resolved_tabs is not None else 5)))
        except Exception:
            return 5

    def _reset_local_recaptcha_asset_caches(self, *, purge_disk: bool = False) -> None:
        """重置本地 reCAPTCHA 资源缓存，必要时删除磁盘缓存以强制刷新。"""
        self._recaptcha_asset_data_url_cache.clear()
        self._recaptcha_asset_bundle_signature = None
        self._recaptcha_asset_bundle = None
        self._recaptcha_asset_hook_source = None

        if not purge_disk:
            return

        for cache_dir in (self._recaptcha_script_cache_dir, self._recaptcha_asset_cache_dir):
            try:
                if not cache_dir.exists():
                    continue
                for cache_file in cache_dir.iterdir():
                    if cache_file.is_file():
                        cache_file.unlink(missing_ok=True)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 清理本地 reCAPTCHA 资源缓存失败: dir={cache_dir}, error={e}"
                )

    @classmethod
    def _resolve_configured_browser_count(cls) -> int:
        try:
            configured_value = getattr(config, "browser_count", None)
            if configured_value is None:
                configured_value = config.get_raw_config().get("captcha", {}).get("browser_count", 1)
        except Exception:
            configured_value = 1

        return resolve_effective_browser_count(configured_value)

    @classmethod
    async def get_instance(cls, db=None):
        """获取单例实例"""
        close_single_instance = None
        close_pool_instance = None
        async with cls._lock:
            use_pool = cls._resolve_configured_browser_count() > 1 or bool(
                getattr(config, "token_pool_enabled", False)
            )

            if use_pool:
                if cls._instance is not None:
                    close_single_instance = cls._instance
                    cls._instance = None
                if cls._pool_instance is None:
                    cls._pool_instance = _PersonalBrowserPoolService(db)
                elif db is not None:
                    cls._pool_instance.db = db
                service = cls._pool_instance
            else:
                if cls._pool_instance is not None:
                    close_pool_instance = cls._pool_instance
                    cls._pool_instance = None
                if cls._instance is None:
                    cls._instance = cls(db)
                    cls._instance._idle_reaper_task = asyncio.create_task(
                        cls._instance._idle_tab_reaper_loop()
                    )
                elif db is not None:
                    cls._instance.db = db
                service = cls._instance

        if close_single_instance is not None:
            try:
                await close_single_instance.close()
            except Exception:
                pass
        if close_pool_instance is not None:
            try:
                await close_pool_instance.close()
            except Exception:
                pass

        if isinstance(service, _PersonalBrowserPoolService):
            await service.reload_config()
        return service

    @classmethod
    async def cleanup_stale_runtime_artifacts(cls, *, reason: str = "manual") -> dict[str, int]:
        async with cls._lock:
            instances: list[Any] = []
            if cls._instance is not None:
                instances.append(cls._instance)
            if cls._pool_instance is not None:
                instances.append(cls._pool_instance)

        active_runtime_paths: set[str] = set()
        active_proxy_extension_paths: set[str] = set()
        for instance in instances:
            if isinstance(instance, _PersonalBrowserPoolService):
                workers = list(getattr(instance, "_workers", []) or [])
            else:
                workers = [instance]
            for worker in workers:
                for raw_path in (
                    str(getattr(worker, "user_data_dir", "") or "").strip(),
                    str(getattr(worker, "_runtime_ephemeral_user_data_dir", "") or "").strip(),
                ):
                    if raw_path:
                        active_runtime_paths.add(raw_path)
                proxy_ext_dir = str(getattr(worker, "_proxy_ext_dir", "") or "").strip()
                if proxy_ext_dir:
                    active_proxy_extension_paths.add(proxy_ext_dir)

        stats = await asyncio.to_thread(
            _cleanup_runtime_artifacts_sync,
            active_runtime_paths=active_runtime_paths,
            active_proxy_extension_paths=active_proxy_extension_paths,
        )
        if any(int(value or 0) > 0 for value in stats.values()):
            debug_logger.log_info(
                f"[BrowserCaptcha] 运行时临时文件清理完成 ({reason}): {stats}"
            )
        return stats

    @classmethod
    async def reset_shared_instances(cls) -> None:
        single_instance = None
        pool_instance = None
        async with cls._lock:
            single_instance = cls._instance
            pool_instance = cls._pool_instance
            cls._instance = None
            cls._pool_instance = None

        if pool_instance is not None:
            try:
                await pool_instance.close()
            except Exception:
                pass
        if single_instance is not None:
            try:
                await single_instance.close()
            except Exception:
                pass

    async def reload_config(self):
        """热更新配置（从数据库重新加载）"""
        old_headless = self.headless
        old_max_tabs = self._max_resident_tabs
        old_idle_ttl = self._idle_tab_ttl_seconds
        old_probe_ttl = self._health_probe_ttl_seconds
        old_fingerprint_ttl = self._fingerprint_cache_ttl_seconds
        old_fresh_restart_every = self._fresh_profile_restart_every_n_solves
        old_user_data_dir = self.user_data_dir
        old_runtime_config_signature = self._proxy_config_signature

        self.headless = bool(getattr(config, "personal_headless", False))
        configured_max_tabs = config.personal_max_resident_tabs
        self._max_resident_tabs = self._resolve_personal_max_resident_tabs(configured_max_tabs)
        self._idle_tab_ttl_seconds = config.personal_idle_tab_ttl_seconds
        self._refresh_runtime_tunables()
        self.user_data_dir = self._resolve_user_data_dir(self.headless)
        self._proxy_config_signature = await self._build_proxy_config_signature()
        runtime_config_changed = old_runtime_config_signature != self._proxy_config_signature

        debug_logger.log_info(
            f"[BrowserCaptcha] Personal 配置已热更新: "
            f"headless {old_headless}->{self.headless}, "
            f"max_tabs {old_max_tabs}->{self._max_resident_tabs}, "
            f"idle_ttl {old_idle_ttl}s->{self._idle_tab_ttl_seconds}s, "
            f"probe_ttl {old_probe_ttl}s->{self._health_probe_ttl_seconds}s, "
            f"fingerprint_ttl {old_fingerprint_ttl}s->{self._fingerprint_cache_ttl_seconds}s, "
            f"fresh_restart_every {old_fresh_restart_every}->{self._fresh_profile_restart_every_n_solves}, "
            f"profile {old_user_data_dir or '<isolated-temp>'}->{self.user_data_dir or '<isolated-temp>'}, "
            f"runtime_changed={runtime_config_changed}"
        )
        if (
            (
                old_headless != self.headless
                or old_user_data_dir != self.user_data_dir
                or runtime_config_changed
            )
            and (self._initialized or self.browser)
        ):
            async with self._browser_lock:
                await self._shutdown_browser_runtime_locked(
                    reason="reload_config_runtime_changed"
                )
            debug_logger.log_info(
                "[BrowserCaptcha] personal 运行参数发生变化，已重置浏览器运行态，后续请求将按新 profile/代理/模式重启"
            )
        elif old_max_tabs > self._max_resident_tabs:
            await self._trim_resident_tabs_to_limit()

    async def _trim_resident_tabs_to_limit(self) -> None:
        """在配额缩小时立即裁掉多余的空闲 resident tab，避免内存长期不回落。"""
        while True:
            async with self._resident_lock:
                overflow = len(self._resident_tabs) - max(1, int(self._max_resident_tabs or 1))
                if overflow <= 0:
                    return

                lru_slot_id = None
                lru_last_used = float("inf")
                for slot_id, resident_info in self._resident_tabs.items():
                    if resident_info.solve_lock.locked():
                        continue
                    if int(getattr(resident_info, "pending_assignment_count", 0) or 0) > 0:
                        continue
                    if resident_info.last_used_at < lru_last_used:
                        lru_last_used = resident_info.last_used_at
                        lru_slot_id = slot_id

            if not lru_slot_id:
                debug_logger.log_warning(
                    "[BrowserCaptcha] max_tabs 已缩小，但当前没有可安全裁剪的空闲 resident tab，"
                    f"当前数量={len(self._resident_tabs)}, target={self._max_resident_tabs}"
                )
                return

            await self._close_resident_tab(lru_slot_id)

    async def _build_proxy_config_signature(self) -> str:
        """基于当前数据库配置构建稳定签名，用于判断是否需要重启浏览器 runtime。"""
        if not self.db:
            return ""

        try:
            captcha_cfg = await self.db.get_captcha_config()
        except Exception:
            captcha_cfg = None

        try:
            proxy_cfg = await self.db.get_proxy_config()
        except Exception:
            proxy_cfg = None

        def normalize_pool(value: Any) -> str:
            return "\n".join(
                item.strip()
                for item in re.split(r"[\r\n,]+", str(value or ""))
                if item.strip()
            )

        startup_cookie_enabled = bool(
            getattr(captcha_cfg, "browser_startup_cookie_enabled", False)
        )
        startup_cookie_text = (
            getattr(captcha_cfg, "browser_startup_cookie", "")
            if startup_cookie_enabled
            else ""
        )

        signature_payload = {
            "captcha_browser_proxy_enabled": bool(getattr(captcha_cfg, "browser_proxy_enabled", False)),
            "captcha_browser_proxy_url": str(getattr(captcha_cfg, "browser_proxy_url", "") or "").strip(),
            "captcha_browser_proxy_pool": normalize_pool(getattr(captcha_cfg, "browser_proxy_pool", "")),
            "captcha_browser_startup_cookie_enabled": startup_cookie_enabled,
            "captcha_browser_startup_cookie_signature": build_cookie_signature(startup_cookie_text),
            "request_proxy_enabled": bool(getattr(proxy_cfg, "enabled", False)),
            "request_proxy_url": str(getattr(proxy_cfg, "proxy_url", "") or "").strip(),
            "request_proxy_pool": normalize_pool(getattr(proxy_cfg, "proxy_pool", "")),
            "request_rotation_mode": str(getattr(proxy_cfg, "rotation_mode", "") or "").strip(),
        }
        return json.dumps(signature_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _refresh_runtime_tunables(self):
        """刷新运行时调优参数，缺省时使用保守的低开销默认值。"""
        try:
            self._health_probe_ttl_seconds = max(
                0.2,
                float(getattr(config, "browser_personal_health_probe_ttl_seconds", 10.0) or 10.0),
            )
        except Exception:
            self._health_probe_ttl_seconds = 10.0

        try:
            self._fingerprint_cache_ttl_seconds = max(
                0.0,
                float(getattr(config, "browser_personal_fingerprint_cache_ttl_seconds", 3600.0) or 3600.0),
            )
        except Exception:
            self._fingerprint_cache_ttl_seconds = 3600.0

        self._fresh_profile_restart_every_n_solves = self._resolve_fresh_profile_restart_every_n_solves()

    def _resolve_fresh_profile_restart_every_n_solves(self) -> int:
        """解析浏览器 fresh profile 轮换阈值，0 表示禁用。"""
        raw_value: Any = None
        env_value = os.environ.get("PERSONAL_BROWSER_FRESH_RESTART_EVERY_N_SOLVES", "").strip()
        if env_value:
            raw_value = env_value
        else:
            try:
                raw_value = config.get_raw_config().get("captcha", {}).get(
                    "browser_personal_fresh_restart_every_n_solves",
                    10,
                )
            except Exception:
                raw_value = 10

        try:
            return max(0, int(raw_value))
        except Exception:
            return 10

    def _reset_browser_rotation_budget(self) -> None:
        self._successful_solves_since_browser_start = 0
        self._fresh_profile_restart_pending = False
        self._fresh_profile_restart_force_pending = False
        self._fresh_profile_restart_pending_reason = ""

    def _mark_runtime_active(self) -> None:
        self._runtime_last_active_at = time.time()

    def _get_runtime_idle_seconds(self) -> float:
        last_active_at = float(getattr(self, "_runtime_last_active_at", 0.0) or 0.0)
        if last_active_at <= 0.0:
            return 0.0
        return max(0.0, time.time() - last_active_at)

    def _record_browser_solve_success(self, *, source: str, project_id: Optional[str] = None) -> int:
        self._successful_solves_since_browser_start = max(
            0,
            int(self._successful_solves_since_browser_start or 0),
        ) + 1

        threshold = max(0, int(self._fresh_profile_restart_every_n_solves or 0))
        current_count = self._successful_solves_since_browser_start
        if threshold > 0 and current_count >= threshold and not self._fresh_profile_restart_pending:
            self._fresh_profile_restart_pending = True
            self._fresh_profile_restart_pending_reason = (
                f"{source}:{project_id or 'global'}:{current_count}/{threshold}"
            )
            debug_logger.log_warning(
                "[BrowserCaptcha] 浏览器成功打码次数达到 fresh profile 轮换阈值，"
                f"后续新取码会先等待当前并发清空并完成全新无状态浏览器重启 "
                f"(count={current_count}, threshold={threshold}, reason={self._fresh_profile_restart_pending_reason})"
            )
        return current_count

    def _mark_fresh_profile_restart_pending(self, *, reason: str, force: bool = False) -> None:
        normalized_reason = str(reason or "manual").strip() or "manual"
        already_pending = bool(self._fresh_profile_restart_pending)
        self._fresh_profile_restart_pending = True
        if force:
            self._fresh_profile_restart_force_pending = True
        self._fresh_profile_restart_pending_reason = normalized_reason
        if not already_pending:
            debug_logger.log_warning(
                "[BrowserCaptcha] 已请求 fresh profile 轮换，"
                f"后续新取码会等待当前并发清空并完成重启 (force={force}, reason={normalized_reason})"
            )

    async def _has_active_browser_work(self) -> bool:
        if (
            self._legacy_lock.locked()
            or self._custom_lock.locked()
            or self._tab_build_lock.locked()
            or self._browser_lock.locked()
        ):
            return True

        async with self._resident_lock:
            for slot_id, resident_info in self._resident_tabs.items():
                if self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info):
                    return True
        return False

    async def _wait_for_browser_work_to_drain(self, *, source: str) -> None:
        warned = False
        while await self._has_active_browser_work():
            if not warned:
                warned = True
                debug_logger.log_warning(
                    f"[BrowserCaptcha] fresh profile 轮换等待当前浏览器任务 drain 完成 (source={source})"
                )
            await asyncio.sleep(0.2)

    async def _maybe_execute_pending_fresh_profile_restart(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        *,
        source: str,
    ) -> bool:
        if not self._fresh_profile_restart_pending:
            return False

        threshold = max(0, int(self._fresh_profile_restart_every_n_solves or 0))
        force_restart = bool(getattr(self, "_fresh_profile_restart_force_pending", False))
        if threshold <= 0 and not force_restart:
            self._fresh_profile_restart_pending = False
            self._fresh_profile_restart_force_pending = False
            self._fresh_profile_restart_pending_reason = ""
            return False

        existing_task = getattr(self, "_fresh_profile_restart_task", None)
        if existing_task is not None and not existing_task.done():
            return False

        async def _runner() -> bool:
            try:
                await self._wait_for_browser_work_to_drain(source=source)

                async with self._runtime_recover_lock:
                    if not self._fresh_profile_restart_pending:
                        return False
                    if await self._has_active_browser_work():
                        debug_logger.log_info(
                            "[BrowserCaptcha] fresh profile 后台轮换发现新任务活跃，延后到下一轮 "
                            f"(project_id={project_id}, source={source})"
                        )
                        return False

                    debug_logger.log_warning(
                        "[BrowserCaptcha] 执行计划中的 fresh profile 轮换重启 "
                        f"(project_id={project_id}, source={source}, reason={self._fresh_profile_restart_pending_reason})"
                    )
                    restarted = await self._restart_browser_for_project_unlocked(
                        project_id,
                        token_id=token_id,
                        fresh_profile=True,
                    )
                    if restarted:
                        self._mark_runtime_restart()
                    return restarted
            except asyncio.CancelledError:
                raise
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] fresh profile 后台轮换失败 (project_id={project_id}, source={source}): {e}"
                )
                return False
            finally:
                if self._fresh_profile_restart_task is asyncio.current_task():
                    self._fresh_profile_restart_task = None

        self._fresh_profile_restart_task = asyncio.create_task(_runner())
        debug_logger.log_info(
            f"[BrowserCaptcha] fresh profile 轮换已计划执行 (project_id={project_id}, source={source})"
        )
        return False

    async def _wait_for_pending_fresh_profile_restart_before_solve(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        *,
        source: str,
    ) -> bool:
        """达到 fresh 轮换阈值后，阻止新取码继续复用旧 resident tab。"""
        waited = False
        current_task = asyncio.current_task()

        while True:
            existing_task = getattr(self, "_fresh_profile_restart_task", None)
            if existing_task is not None and not existing_task.done() and existing_task is not current_task:
                if not waited:
                    waited = True
                    debug_logger.log_warning(
                        "[BrowserCaptcha] fresh profile 轮换正在执行/等待，"
                        f"当前取码先等待重启完成再分配标签页 (project_id={project_id}, source={source})"
                    )
                try:
                    await asyncio.shield(existing_task)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 等待 fresh profile 轮换任务异常 (project_id={project_id}, source={source}): {e}"
                    )
                if not self._fresh_profile_restart_pending:
                    return True
                await asyncio.sleep(0)
                continue

            if not self._fresh_profile_restart_pending:
                return waited

            threshold = max(0, int(self._fresh_profile_restart_every_n_solves or 0))
            force_restart = bool(getattr(self, "_fresh_profile_restart_force_pending", False))
            if threshold <= 0 and not force_restart:
                self._fresh_profile_restart_pending = False
                self._fresh_profile_restart_force_pending = False
                self._fresh_profile_restart_pending_reason = ""
                return waited

            if not waited:
                waited = True
                debug_logger.log_warning(
                    "[BrowserCaptcha] fresh profile 轮换已到阈值，"
                    f"当前取码先触发并等待重启完成 (project_id={project_id}, source={source}, "
                    f"reason={self._fresh_profile_restart_pending_reason})"
                )

            await self._maybe_execute_pending_fresh_profile_restart(
                project_id,
                token_id=token_id,
                source=source,
            )

            scheduled_task = getattr(self, "_fresh_profile_restart_task", None)
            if scheduled_task is None or scheduled_task.done() or scheduled_task is current_task:
                await asyncio.sleep(0.05)
                continue

            try:
                await asyncio.shield(scheduled_task)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] fresh profile 轮换任务执行异常 (project_id={project_id}, source={source}): {e}"
                )

            if not self._fresh_profile_restart_pending:
                return True
            await asyncio.sleep(0)

    def _requires_virtual_display(self) -> bool:
        """仅在显式有头模式下要求 Docker/Linux 提供 DISPLAY/虚拟显示。"""
        return bool(IS_DOCKER and os.name == "posix" and not self.headless)

    def _check_available(self):
        """检查服务是否可用"""
        if DOCKER_HEADED_BLOCKED:
            raise RuntimeError(
                "检测到 Docker 环境，默认禁用内置浏览器打码。"
                "如需启用请设置环境变量 ALLOW_DOCKER_HEADED_CAPTCHA=true。"
            )
        if self._requires_virtual_display() and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Docker 内置浏览器打码已启用，但 DISPLAY 未设置。"
                "请设置 DISPLAY（例如 :99）并启动 Xorg/Xdummy 等虚拟显示。"
            )
        if not NODRIVER_AVAILABLE or uc is None:
            raise RuntimeError(
                "nodriver 未安装或不可用。"
                "请手动安装: pip install nodriver"
            )

    async def _run_with_timeout(self, awaitable, timeout_seconds: float, label: str):
        """统一收口 nodriver 操作超时，避免单次卡死拖住整条请求链路。"""
        effective_timeout = max(0.5, float(timeout_seconds or 0))
        try:
            return await asyncio.wait_for(awaitable, timeout=effective_timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"{label} 超时 ({effective_timeout:.1f}s)") from e

    async def _wait_for_display_ready(self, display_value: str, timeout_seconds: float = 5.0):
        """Docker 有头模式下等待 X display socket 就绪，避免容器重启后立刻拉起浏览器失败。"""
        if not (IS_DOCKER and display_value and display_value.startswith(":") and os.name == "posix"):
            return

        display_suffix = display_value.split(".", 1)[0].lstrip(":")
        if not display_suffix.isdigit():
            return

        socket_path = f"/tmp/.X11-unix/X{display_suffix}"
        deadline = time.monotonic() + max(0.5, float(timeout_seconds or 0))
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                return
            await asyncio.sleep(0.1)

        raise RuntimeError(
            f"DISPLAY={display_value} 对应的 X display socket 未就绪: {socket_path}"
        )

    def _mark_browser_health(self, healthy: bool):
        self._last_health_probe_at = time.monotonic()
        self._last_health_probe_ok = bool(healthy)

    def _is_browser_health_fresh(self) -> bool:
        if not (self._initialized and self.browser and self._last_health_probe_ok):
            return False
        try:
            if self.browser.stopped or getattr(self.browser, "_flow2api_runtime_disconnected", False):
                return False
        except Exception:
            return False
        ttl_seconds = max(0.0, float(self._health_probe_ttl_seconds or 0.0))
        if ttl_seconds <= 0:
            return False
        return (time.monotonic() - self._last_health_probe_at) < ttl_seconds

    def _is_fingerprint_cache_fresh(self) -> bool:
        if not self._last_fingerprint:
            return False
        ttl_seconds = max(0.0, float(self._fingerprint_cache_ttl_seconds or 0.0))
        if ttl_seconds <= 0:
            return False
        return (time.monotonic() - self._last_fingerprint_at) < ttl_seconds

    def _invalidate_browser_health(self):
        self._last_health_probe_at = 0.0
        self._last_health_probe_ok = False

    def _mark_runtime_restart(self):
        self._last_runtime_restart_at = time.time()
        self._mark_runtime_active()

    def _was_runtime_restarted_recently(self, window_seconds: float = 5.0) -> bool:
        if self._last_runtime_restart_at <= 0.0:
            return False
        return (time.time() - self._last_runtime_restart_at) <= max(0.0, window_seconds)

    def _get_browser_launch_cooldown_remaining_seconds(self) -> float:
        return max(0.0, float(self._browser_launch_cooldown_until or 0.0) - time.monotonic())

    def _is_browser_launch_cooldown_active(self) -> bool:
        return self._get_browser_launch_cooldown_remaining_seconds() > 0.0

    def _reset_browser_launch_failure_state(self) -> None:
        self._browser_launch_failure_streak = 0
        self._browser_launch_cooldown_until = 0.0
        self._browser_launch_last_error = ""

    def _mark_browser_launch_failure(self, error: Any) -> None:
        self._browser_launch_failure_streak = min(
            8,
            max(0, int(self._browser_launch_failure_streak or 0)) + 1,
        )
        error_text = str(error or "").strip()
        error_lower = error_text.lower()
        base_cooldown_seconds = 2.0
        if isinstance(error, PermissionError) or "winerror 5" in error_lower:
            base_cooldown_seconds = 5.0
        elif any(keyword in error_lower for keyword in ("address already in use", "only one usage", "port")):
            base_cooldown_seconds = 8.0
        cooldown_seconds = min(
            45.0,
            base_cooldown_seconds * (2 ** min(4, self._browser_launch_failure_streak - 1)),
        )
        self._browser_launch_cooldown_until = time.monotonic() + cooldown_seconds
        self._browser_launch_last_error = f"{type(error).__name__}: {error_text or '<empty>'}"

    def _raise_if_browser_launch_cooling_down(self) -> None:
        remaining_seconds = self._get_browser_launch_cooldown_remaining_seconds()
        if remaining_seconds <= 0.0:
            return
        suffix = f", last_error={self._browser_launch_last_error}" if self._browser_launch_last_error else ""
        raise RuntimeError(
            f"浏览器启动冷却中，请 {remaining_seconds:.1f}s 后重试{suffix}"
        )

    @staticmethod
    def _should_use_explicit_no_sandbox_retry(error: Any) -> bool:
        if os.name != "posix":
            return False
        error_text = str(error or "").lower()
        return any(
            keyword in error_text
            for keyword in (
                "no_sandbox",
                "no usable sandbox",
                "setuid sandbox",
                "namespace",
                "running as root",
                "you are running as root",
            )
        )

    @staticmethod
    def _is_retryable_browser_launch_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return any(
            keyword in error_text
            for keyword in (
                "failed to connect to browser",
                "connection refused",
                "connection reset",
                "connection closed",
                "websocket is not open",
                "chrome not reachable",
                "browser has been closed",
                "target closed",
            )
        )

    @staticmethod
    def _is_memory_pressure_browser_launch_error(error: Any) -> bool:
        error_text = _flatten_exception_text(error)
        return any(
            keyword in error_text
            for keyword in (
                "0xc000012d",
                "status_commitment_limit",
                "commitment limit",
                "paging file",
                "not enough memory",
                "insufficient system resources",
                "not enough storage is available",
                "out of memory",
                "cannot allocate memory",
            )
        )

    @staticmethod
    def _is_invalid_browser_context_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return (
            "failed to find browser context" in error_text
            or "cannot find context with specified id" in error_text
            or "browser context" in error_text and "-32602" in error_text
        )

    async def shutdown_idle_runtime_if_needed(
        self,
        *,
        idle_ttl_seconds: Optional[int] = None,
        reason: str = "idle_runtime_ttl",
    ) -> bool:
        if self._fresh_profile_restart_pending:
            return False

        try:
            ttl_seconds = max(
                60,
                int(self._idle_tab_ttl_seconds if idle_ttl_seconds is None else idle_ttl_seconds),
            )
        except Exception:
            ttl_seconds = 600

        browser_instance = self.browser
        if not (self._initialized and browser_instance) or getattr(browser_instance, "stopped", False):
            return False
        if self._get_runtime_idle_seconds() < ttl_seconds:
            return False
        if await self._has_active_browser_work():
            return False

        async with self._resident_lock:
            if self._resident_tabs:
                return False

        async with self._custom_lock:
            if self._custom_tabs:
                return False

        await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=reason)
        return True

    async def _collect_reclaimable_resident_slot_ids(self) -> list[str]:
        current_time = time.time()
        async with self._resident_lock:
            reclaimable_slot_ids = []
            for slot_id, resident_info in list(self._resident_tabs.items()):
                if resident_info is None:
                    continue
                if resident_info.solve_lock.locked():
                    continue
                if int(getattr(resident_info, "pending_assignment_count", 0) or 0) > 0:
                    continue
                reclaimable_slot_ids.append(
                    (
                        current_time - float(getattr(resident_info, "last_used_at", current_time) or current_time),
                        slot_id,
                    )
                )

        reclaimable_slot_ids.sort(reverse=True)
        return [slot_id for _, slot_id in reclaimable_slot_ids]

    async def reclaim_runtime_memory(
        self,
        *,
        reason: str = "manual",
        aggressive: bool = False,
    ) -> dict[str, int]:
        stats = {
            "resident_tabs_closed": 0,
            "runtime_shutdown": 0,
            "profiles_deleted": 0,
            "recaptcha_cache_deleted": 0,
            "proxy_extensions_deleted": 0,
            "python_gc_collected": 0,
        }

        await self._cancel_background_runtime_tasks(reason=f"memory_reclaim:{reason}")

        reclaimable_slot_ids = await self._collect_reclaimable_resident_slot_ids()
        if not aggressive and reclaimable_slot_ids:
            reclaimable_slot_ids = reclaimable_slot_ids[:1]

        for slot_id in reclaimable_slot_ids:
            try:
                await self._close_resident_tab(slot_id)
                stats["resident_tabs_closed"] += 1
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 关闭可回收 resident 失败 (slot={slot_id}, reason={reason}): {e}"
                )

        should_shutdown_runtime = False
        browser_instance = self.browser
        if self._initialized and browser_instance and not getattr(browser_instance, "stopped", False):
            has_active_work = await self._has_active_browser_work()
            async with self._resident_lock:
                has_resident_tabs = bool(self._resident_tabs)
            async with self._custom_lock:
                has_custom_tabs = bool(self._custom_tabs)
            should_shutdown_runtime = not has_active_work and not has_resident_tabs and not has_custom_tabs

        if should_shutdown_runtime:
            await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=f"memory_reclaim:{reason}")
            stats["runtime_shutdown"] += 1

        stale_stats = await self.cleanup_stale_runtime_artifacts(reason=f"memory_reclaim:{reason}")
        for key in ("profiles_deleted", "recaptcha_cache_deleted", "proxy_extensions_deleted"):
            stats[key] = int(stale_stats.get(key, 0) or 0)

        try:
            stats["python_gc_collected"] = max(0, int(gc.collect()))
        except Exception:
            stats["python_gc_collected"] = 0

        if any(int(value or 0) > 0 for value in stats.values()):
            debug_logger.log_info(f"[BrowserCaptcha] 内存回收完成 ({reason}): {stats}")
        return stats

    def _is_browser_runtime_error(self, error: Any) -> bool:
        """识别浏览器运行态已损坏/已关闭的典型异常。"""
        return _is_runtime_disconnect_error(error) or self._is_no_browser_window_error(error)

    @staticmethod
    def _is_no_browser_window_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return "no browser is open" in error_text or "failed to open new tab" in error_text

    def _decode_nodriver_object_entries(self, value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, list):
            return None

        result: Dict[str, Any] = {}
        for entry in value:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                return None
            key, entry_value = entry
            if not isinstance(key, str):
                return None
            result[key] = self._normalize_nodriver_evaluate_result(entry_value)
        return result

    def _normalize_nodriver_evaluate_result(self, value: Any) -> Any:
        if value is None:
            return None

        deep_serialized_value = getattr(value, "deep_serialized_value", None)
        if deep_serialized_value is not None:
            return self._normalize_nodriver_evaluate_result(deep_serialized_value)

        type_name = getattr(value, "type_", None)
        if type_name is not None and hasattr(value, "value"):
            raw_value = getattr(value, "value", None)
            if type_name == "object":
                object_entries = self._decode_nodriver_object_entries(raw_value)
                if object_entries is not None:
                    return object_entries
            if raw_value is not None:
                return self._normalize_nodriver_evaluate_result(raw_value)
            unserializable_value = getattr(value, "unserializable_value", None)
            if unserializable_value is not None:
                return str(unserializable_value)
            return value

        if isinstance(value, dict):
            typed_value_keys = {"type", "value", "objectId", "weakLocalObjectReference"}
            if "type" in value and set(value.keys()).issubset(typed_value_keys):
                raw_value = value.get("value")
                if value.get("type") == "object":
                    object_entries = self._decode_nodriver_object_entries(raw_value)
                    if object_entries is not None:
                        return object_entries
                return self._normalize_nodriver_evaluate_result(raw_value)
            return {
                key: self._normalize_nodriver_evaluate_result(item)
                for key, item in value.items()
            }

        if isinstance(value, list):
            object_entries = self._decode_nodriver_object_entries(value)
            if object_entries is not None:
                return object_entries
            return [self._normalize_nodriver_evaluate_result(item) for item in value]

        return value

    async def _probe_browser_runtime(self) -> bool:
        """轻量探测当前 nodriver 连接是否仍可用。"""
        if not self.browser:
            self._invalidate_browser_health()
            return False
        if getattr(self.browser, "_flow2api_runtime_disconnected", False):
            self._invalidate_browser_health()
            return False
        if self._is_browser_health_fresh():
            return True

        try:
            from nodriver import cdp

            await self._run_with_timeout(
                self.browser.connection.send(cdp.browser.get_version()),
                timeout_seconds=3.0,
                label="browser.health_probe",
            )
            if self._requires_virtual_display() and not await self._browser_has_page_targets():
                await self._ensure_browser_host_page(
                    label="browser_health_probe",
                    timeout_seconds=3.0,
                )
            self._mark_browser_health(True)
            return True
        except Exception as e:
            self._mark_browser_health(False)
            debug_logger.log_warning(f"[BrowserCaptcha] 浏览器健康检查失败: {e}")
            return False

    async def _recover_browser_runtime(self, project_id: Optional[str] = None, reason: str = "runtime_error") -> bool:
        """浏览器运行态损坏时，优先整颗浏览器重启并恢复 resident 池。"""
        normalized_project_id = str(project_id or "").strip()
        async with self._runtime_recover_lock:
            if self.browser and self._initialized and not getattr(self.browser, "stopped", False):
                try:
                    if await self._probe_browser_runtime():
                        debug_logger.log_info(
                            f"[BrowserCaptcha] 浏览器运行态已被并发协程恢复，直接复用 (project_id={normalized_project_id or '<empty>'}, reason={reason})"
                        )
                        return True
                except Exception:
                    pass

            self._invalidate_browser_health()

            if normalized_project_id:
                try:
                    if await self._restart_browser_for_project_unlocked(normalized_project_id):
                        self._mark_runtime_restart()
                        return True
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 浏览器重启恢复失败 (project_id={normalized_project_id}, reason={reason}): {e}"
                    )

            try:
                await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=f"recover:{reason}")
                await self.initialize()
                self._mark_runtime_restart()
                return True
            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] 浏览器运行态恢复失败 ({reason}): {e}")
                return False

    async def _tab_evaluate(
        self,
        tab,
        script: str,
        label: str,
        timeout_seconds: Optional[float] = None,
        *,
        await_promise: bool = False,
        return_by_value: bool = False,
    ):
        result = await self._run_with_timeout(
            tab.evaluate(
                script,
                await_promise=await_promise,
                return_by_value=return_by_value,
            ),
            timeout_seconds or self._command_timeout_seconds,
            label,
        )
        if return_by_value:
            return self._normalize_nodriver_evaluate_result(result)
        return result

    async def _tab_get(self, tab, url: str, label: str, timeout_seconds: Optional[float] = None):
        return await self._run_with_timeout(
            tab.get(url),
            timeout_seconds or self._navigation_timeout_seconds,
            label,
        )

    async def _browser_get(
        self,
        url: str,
        label: str,
        new_tab: bool = False,
        new_window: bool = False,
        timeout_seconds: Optional[float] = None,
    ):
        target_url = str(url or "").strip() or PERSONAL_COOKIE_PREBIND_URL
        prebind_url = (
            PERSONAL_COOKIE_PREBIND_URL
            if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL
            else target_url
        )
        tab = await self._run_with_timeout(
            self.browser.get(prebind_url, new_tab=new_tab, new_window=new_window),
            timeout_seconds or self._navigation_timeout_seconds,
            label,
        )
        await self._apply_tab_startup_spoofs(
            tab,
            label=label,
            target_url=target_url,
        )
        if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL:
            await self._tab_get(
                tab,
                target_url,
                label=f"{label}:navigate_target",
                timeout_seconds=timeout_seconds,
            )
        return tab

    def _refresh_runtime_fingerprint_spoof_seed(
        self,
        *,
        user_agent: Optional[str] = None,
        product: Optional[str] = None,
    ) -> None:
        self._runtime_fingerprint_spoof_seed = hashlib.sha256(
            (
                f"runtime:{time.time_ns()}:{os.getpid()}:"
                f"{self._browser_instance_id}:{self.user_data_dir or '<isolated-temp>'}"
            ).encode("utf-8")
        ).hexdigest()
        self._runtime_surface_profile = self._build_runtime_surface_profile(
            user_agent=user_agent,
            product=product,
        )

    async def _get_live_browser_runtime_identity(self) -> tuple[Optional[str], Optional[str]]:
        if not self.browser:
            return None, None

        try:
            from nodriver import cdp

            version_info = await self._run_with_timeout(
                self.browser.connection.send(cdp.browser.get_version()),
                timeout_seconds=5.0,
                label="browser.get_version:runtime_profile",
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取浏览器运行态版本失败，回退默认 runtime profile: {e}")
            return None, None

        user_agent = None
        product = None
        if isinstance(version_info, (list, tuple)):
            if len(version_info) >= 4:
                product = version_info[1]
                user_agent = version_info[3]
        elif isinstance(version_info, dict):
            product = version_info.get("product")
            user_agent = version_info.get("userAgent")
        else:
            product = getattr(version_info, "product", None)
            user_agent = getattr(version_info, "userAgent", None) or getattr(version_info, "user_agent", None)

        normalized_user_agent = str(user_agent or "").strip() or None
        normalized_product = str(product or "").strip() or None
        return normalized_user_agent, normalized_product

    def _get_runtime_surface_profile(self) -> Dict[str, Any]:
        return dict(self._runtime_surface_profile or {})

    @staticmethod
    def _format_runtime_client_hint_brands(items: Iterable[Dict[str, Any]]) -> str:
        formatted: list[str] = []
        for item in items or ():
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").strip()
            version = str(item.get("version") or "").strip()
            if not brand or not version:
                continue
            formatted.append(f'"{brand}";v="{version}"')
        return ", ".join(formatted)

    def _build_runtime_extra_http_headers(self) -> Dict[str, str]:
        runtime_profile = self._get_runtime_surface_profile()
        headers = {
            str(key): str(value)
            for key, value in dict(runtime_profile.get("httpHeaders") or {}).items()
            if str(key or "").strip() and value not in (None, "")
        }
        metadata = dict(runtime_profile.get("userAgentMetadata") or {})
        brands = metadata.get("brands") or []
        full_version_list = metadata.get("fullVersionList") or []
        sec_ch_ua = self._format_runtime_client_hint_brands(brands)
        sec_ch_ua_full_version_list = self._format_runtime_client_hint_brands(full_version_list)
        if sec_ch_ua:
            headers["Sec-CH-UA"] = sec_ch_ua
        headers["Sec-CH-UA-Mobile"] = "?1" if metadata.get("mobile") else "?0"
        for header_name, value in (
            ("Sec-CH-UA-Platform", metadata.get("platform")),
            ("Sec-CH-UA-Platform-Version", metadata.get("platformVersion")),
            ("Sec-CH-UA-Full-Version", metadata.get("fullVersion")),
            ("Sec-CH-UA-Arch", metadata.get("architecture")),
            ("Sec-CH-UA-Bitness", metadata.get("bitness")),
        ):
            normalized = str(value or "").strip()
            if normalized:
                headers[header_name] = f'"{normalized}"'
        if sec_ch_ua_full_version_list:
            headers["Sec-CH-UA-Full-Version-List"] = sec_ch_ua_full_version_list
        return headers

    def _build_runtime_user_agent_metadata(self):
        runtime_profile = self._get_runtime_surface_profile()
        metadata_profile = dict(runtime_profile.get("userAgentMetadata") or {})
        if not metadata_profile:
            return None

        try:
            from nodriver import cdp

            def _build_brand_items(items: Iterable[Dict[str, Any]]) -> list[Any]:
                result = []
                for item in items or ():
                    if not isinstance(item, dict):
                        continue
                    brand = str(item.get("brand") or "").strip()
                    version = str(item.get("version") or "").strip()
                    if not brand or not version:
                        continue
                    result.append(cdp.emulation.UserAgentBrandVersion(brand=brand, version=version))
                return result

            return cdp.emulation.UserAgentMetadata(
                platform=str(metadata_profile.get("platform") or "Windows"),
                platform_version=str(metadata_profile.get("platformVersion") or "10.0.0"),
                architecture=str(metadata_profile.get("architecture") or "x86"),
                model=str(metadata_profile.get("model") or ""),
                mobile=bool(metadata_profile.get("mobile")),
                brands=_build_brand_items(metadata_profile.get("brands") or []),
                full_version_list=_build_brand_items(metadata_profile.get("fullVersionList") or []),
                full_version=str(metadata_profile.get("fullVersion") or ""),
                bitness=str(metadata_profile.get("bitness") or "64"),
                wow64=bool(metadata_profile.get("wow64")),
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 构建 UserAgentMetadata 失败，将跳过 UA-CH runtime 注入: {e}")
            return None

    @staticmethod
    def _normalize_permission_origin(url: Optional[str]) -> Optional[str]:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    def _build_runtime_permission_origins(self, target_url: Optional[str] = None) -> list[str]:
        seen: set[str] = set()
        origins: list[str] = []
        candidate_urls = [
            target_url,
            PERSONAL_LABS_BOOTSTRAP_URL,
            *PERSONAL_COOKIE_TARGET_URLS,
        ]
        for candidate in candidate_urls:
            origin = self._normalize_permission_origin(candidate)
            if not origin or origin in seen:
                continue
            seen.add(origin)
            origins.append(origin)
        return origins

    async def _apply_runtime_profile_permissions(
        self,
        *,
        label: str,
        browser_context_id: Any = None,
        target_url: Optional[str] = None,
    ) -> bool:
        if not self.browser:
            return False

        runtime_profile = self._get_runtime_surface_profile()
        permissions_profile = dict(runtime_profile.get("permissions") or {})
        if not permissions_profile:
            return False

        try:
            from nodriver import cdp

            permission_mapping = {
                "geolocation": "geolocation",
                "notifications": "notifications",
                "camera": "camera",
                "microphone": "microphone",
                "display-capture": "display-capture",
            }
            permission_settings = {
                "granted": cdp.browser.PermissionSetting.GRANTED,
                "denied": cdp.browser.PermissionSetting.DENIED,
                "prompt": cdp.browser.PermissionSetting.PROMPT,
            }
            configured_permissions = [
                (
                    permission_mapping[key],
                    permission_settings[str(value or "").strip().lower()],
                )
                for key, value in permissions_profile.items()
                if key in permission_mapping and str(value or "").strip().lower() in permission_settings
            ]
            if not configured_permissions:
                return False

            normalized_browser_context_id = browser_context_id
            if (
                normalized_browser_context_id is not None
                and not hasattr(normalized_browser_context_id, "to_json")
            ):
                normalized_browser_context_id = cdp.browser.BrowserContextID(
                    str(normalized_browser_context_id)
                )

            applied = False
            for origin in self._build_runtime_permission_origins(target_url=target_url):
                for permission_name, permission_setting in configured_permissions:
                    try:
                        await self._run_with_timeout(
                            self.browser.connection.send(
                                cdp.browser.set_permission(
                                    permission=cdp.browser.PermissionDescriptor(name=permission_name),
                                    setting=permission_setting,
                                    origin=origin,
                                    browser_context_id=normalized_browser_context_id,
                                )
                            ),
                            timeout_seconds=5.0,
                            label=f"browser.set_permission:{label}:{origin}:{permission_name}",
                        )
                    except Exception as permission_error:
                        if (
                            normalized_browser_context_id is None
                            or not self._is_invalid_browser_context_error(permission_error)
                        ):
                            raise
                        await self._run_with_timeout(
                            self.browser.connection.send(
                                cdp.browser.set_permission(
                                    permission=cdp.browser.PermissionDescriptor(name=permission_name),
                                    setting=permission_setting,
                                    origin=origin,
                                )
                            ),
                            timeout_seconds=5.0,
                            label=f"browser.set_permission:{label}:{origin}:{permission_name}:fallback_default_context",
                        )
                    applied = True
            return applied
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 应用 runtime 权限画像失败 ({label}): {e}")
            return False

    async def _apply_runtime_profile_to_tab(
        self,
        tab,
        *,
        label: str,
        browser_context_id: Any = None,
        target_url: Optional[str] = None,
    ) -> bool:
        if tab is None:
            return False

        runtime_profile = self._get_runtime_surface_profile()
        runtime_signature = str(runtime_profile.get("signature") or "").strip()
        target_context_id = browser_context_id if browser_context_id is not None else self._extract_tab_browser_context_id(tab)
        applied_marker = {
            "signature": runtime_signature,
            "browser_context_id": str(target_context_id or ""),
        }
        existing_marker = getattr(tab, "_personal_runtime_profile_marker", None)
        if existing_marker == applied_marker:
            await self._apply_runtime_profile_permissions(
                label=label,
                browser_context_id=target_context_id,
                target_url=target_url,
            )
            return True

        try:
            from nodriver import cdp

            navigator_profile = dict(runtime_profile.get("navigator") or {})
            locale_profile = dict(runtime_profile.get("locale") or {})
            timezone_profile = dict(runtime_profile.get("timezone") or {})
            geolocation_profile = dict(runtime_profile.get("geolocation") or {})
            screen_profile = dict(runtime_profile.get("screen") or {})
            window_profile = dict(runtime_profile.get("window") or {})

            await self._run_with_timeout(
                tab.send(cdp.network.enable()),
                timeout_seconds=5.0,
                label=f"network.enable:{label}",
            )
            await self._run_with_timeout(
                tab.send(
                    cdp.emulation.set_user_agent_override(
                        user_agent=str(runtime_profile.get("userAgent") or navigator_profile.get("userAgent") or ""),
                        accept_language=str(runtime_profile.get("acceptLanguage") or locale_profile.get("code") or ""),
                        platform=str(navigator_profile.get("platform") or ""),
                        user_agent_metadata=self._build_runtime_user_agent_metadata(),
                    )
                ),
                timeout_seconds=5.0,
                label=f"emulation.set_user_agent_override:{label}",
            )
            await self._run_with_timeout(
                tab.send(
                    cdp.network.set_extra_http_headers(
                        cdp.network.Headers(self._build_runtime_extra_http_headers())
                    )
                ),
                timeout_seconds=5.0,
                label=f"network.set_extra_http_headers:{label}",
            )
            await self._run_with_timeout(
                tab.send(
                    cdp.emulation.set_device_metrics_override(
                        width=int(window_profile.get("innerWidth") or screen_profile.get("width") or 1280),
                        height=int(window_profile.get("innerHeight") or screen_profile.get("height") or 720),
                        device_scale_factor=float(window_profile.get("devicePixelRatio") or 1.0),
                        mobile=bool((runtime_profile.get("userAgentMetadata") or {}).get("mobile")),
                        screen_width=int(screen_profile.get("width") or 1280),
                        screen_height=int(screen_profile.get("height") or 720),
                    )
                ),
                timeout_seconds=5.0,
                label=f"emulation.set_device_metrics_override:{label}",
            )
            timezone_id = str(timezone_profile.get("id") or "").strip()
            if timezone_id:
                await self._run_with_timeout(
                    tab.send(cdp.emulation.set_timezone_override(timezone_id=timezone_id)),
                    timeout_seconds=5.0,
                    label=f"emulation.set_timezone_override:{label}",
                )
            locale_code = str(locale_profile.get("code") or "").strip()
            if locale_code:
                await self._run_with_timeout(
                    tab.send(cdp.emulation.set_locale_override(locale=locale_code)),
                    timeout_seconds=5.0,
                    label=f"emulation.set_locale_override:{label}",
                )
            if all(key in geolocation_profile for key in ("latitude", "longitude", "accuracy")):
                await self._run_with_timeout(
                    tab.send(
                        cdp.emulation.set_geolocation_override(
                            latitude=float(geolocation_profile["latitude"]),
                            longitude=float(geolocation_profile["longitude"]),
                            accuracy=float(geolocation_profile["accuracy"]),
                        )
                    ),
                    timeout_seconds=5.0,
                    label=f"emulation.set_geolocation_override:{label}",
                )
            await self._apply_runtime_profile_permissions(
                label=label,
                browser_context_id=target_context_id,
                target_url=target_url,
            )
            try:
                tab._personal_runtime_profile_marker = applied_marker
            except Exception:
                pass
            return True
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 应用 runtime profile 失败 ({label}): {e}")
            return False

    @staticmethod
    def _parse_runtime_browser_version(user_agent: Optional[str], product: Optional[str] = None) -> str:
        candidates = [
            str(user_agent or "").strip(),
            str(product or "").strip(),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            match = re.search(r"(?:Chrome|Chromium)/(\d+\.\d+\.\d+\.\d+)", candidate)
            if match:
                return match.group(1)
            match = re.search(r"/(\d+\.\d+\.\d+\.\d+)", candidate)
            if match:
                return match.group(1)
        return "135.0.0.0"

    @classmethod
    def _derive_runtime_os_profile(cls, user_agent: Optional[str]) -> Dict[str, Any]:
        ua_text = str(user_agent or "")
        if "Mac OS X" in ua_text or "Macintosh" in ua_text:
            return {
                "ua_ch_platform": "macOS",
                "ua_ch_platform_version": "14.0.0",
                "js_platform": "MacIntel",
                "vendor": "Google Inc.",
                "architecture": "x86",
                "bitness": "64",
                "wow64": False,
            }
        if "Linux" in ua_text and "Android" not in ua_text:
            return {
                "ua_ch_platform": "Linux",
                "ua_ch_platform_version": "6.1.0",
                "js_platform": "Linux x86_64",
                "vendor": "Google Inc.",
                "architecture": "x86",
                "bitness": "64",
                "wow64": False,
            }
        return {
            "ua_ch_platform": "Windows",
            "ua_ch_platform_version": "10.0.0",
            "js_platform": "Win32",
            "vendor": "Google Inc.",
            "architecture": "x86",
            "bitness": "64",
            "wow64": False,
        }

    def _build_runtime_surface_profile(
        self,
        *,
        user_agent: Optional[str] = None,
        product: Optional[str] = None,
    ) -> Dict[str, Any]:
        seed_material = (
            f"{self._runtime_fingerprint_spoof_seed}:{self._browser_instance_id}:runtime-surface"
        ).encode("utf-8")
        digest = hashlib.sha256(seed_material).digest()
        full_version = self._parse_runtime_browser_version(user_agent, product)
        major_version = full_version.split(".", 1)[0]
        effective_user_agent = str(user_agent or "").strip() or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{full_version} Safari/537.36"
        )
        os_profile = self._derive_runtime_os_profile(effective_user_agent)

        locale_profiles = (
            {
                "locale": "zh-CN",
                "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
                "languages": ["zh-CN", "zh", "en"],
                "timezoneId": "Asia/Shanghai",
                "geolocation": {"latitude": 31.2304, "longitude": 121.4737, "accuracy": 18.0},
            },
            {
                "locale": "en-US",
                "acceptLanguage": "en-US,en;q=0.9",
                "languages": ["en-US", "en"],
                "timezoneId": "America/New_York",
                "geolocation": {"latitude": 40.7128, "longitude": -74.0060, "accuracy": 20.0},
            },
            {
                "locale": "en-US",
                "acceptLanguage": "en-US,en;q=0.9",
                "languages": ["en-US", "en"],
                "timezoneId": "America/Los_Angeles",
                "geolocation": {"latitude": 34.0522, "longitude": -118.2437, "accuracy": 20.0},
            },
            {
                "locale": "en-GB",
                "acceptLanguage": "en-GB,en;q=0.9",
                "languages": ["en-GB", "en"],
                "timezoneId": "Europe/London",
                "geolocation": {"latitude": 51.5074, "longitude": -0.1278, "accuracy": 18.0},
            },
            {
                "locale": "ja-JP",
                "acceptLanguage": "ja-JP,ja;q=0.9,en;q=0.7",
                "languages": ["ja-JP", "ja", "en"],
                "timezoneId": "Asia/Tokyo",
                "geolocation": {"latitude": 35.6762, "longitude": 139.6503, "accuracy": 18.0},
            },
        )
        locale_profile = dict(locale_profiles[digest[2] % len(locale_profiles)])
        desktop_profiles = (
            {"width": 1366, "height": 768, "hardwareConcurrency": 4, "deviceMemory": 4},
            {"width": 1440, "height": 900, "hardwareConcurrency": 8, "deviceMemory": 8},
            {"width": 1536, "height": 864, "hardwareConcurrency": 8, "deviceMemory": 8},
            {"width": 1600, "height": 900, "hardwareConcurrency": 10, "deviceMemory": 8},
            {"width": 1680, "height": 1050, "hardwareConcurrency": 12, "deviceMemory": 8},
            {"width": 1920, "height": 1080, "hardwareConcurrency": 12, "deviceMemory": 8},
        )
        base_profile = dict(desktop_profiles[digest[0] % len(desktop_profiles)])
        gpu_profiles = (
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
        )
        gpu_profile = dict(gpu_profiles[digest[11] % len(gpu_profiles)])
        gpu_arch = "turing"
        if "Intel" in str(gpu_profile.get("unmaskedVendor") or gpu_profile.get("vendor") or ""):
            gpu_arch = "gen-9"
        elif "ATI" in str(gpu_profile.get("unmaskedVendor") or "") or "AMD" in str(gpu_profile.get("vendor") or ""):
            gpu_arch = "rdna"
        width = int(base_profile["width"])
        height = int(base_profile["height"])
        taskbar_height = 40 + int(digest[1] % 32)
        avail_width = width
        avail_height = max(640, height - taskbar_height)
        viewport_width = min(1280, max(1100, width - (72 + int(digest[3] % 40))))
        viewport_height = min(720, max(620, avail_height - (52 + int(digest[4] % 28))))
        outer_width = min(width, viewport_width + 16)
        outer_height = min(height, viewport_height + 88)
        device_scale_factor = 1.0
        seed_prefix = hashlib.md5(seed_material).hexdigest()

        runtime_profile = {
            "seed": seed_prefix[:16],
            "userAgent": effective_user_agent,
            "acceptLanguage": str(locale_profile["acceptLanguage"]),
            "locale": {
                "code": str(locale_profile["locale"]),
                "languages": list(locale_profile["languages"]),
            },
            "timezone": {
                "id": str(locale_profile["timezoneId"]),
            },
            "geolocation": {
                "latitude": float(locale_profile["geolocation"]["latitude"]),
                "longitude": float(locale_profile["geolocation"]["longitude"]),
                "accuracy": float(locale_profile["geolocation"]["accuracy"]),
            },
            "permissions": {
                "geolocation": "granted",
                "notifications": "denied",
                "camera": "denied",
                "microphone": "denied",
                "display-capture": "denied",
            },
            "navigator": {
                "userAgent": effective_user_agent,
                "appVersion": effective_user_agent.replace("Mozilla/", "", 1) if effective_user_agent.startswith("Mozilla/") else effective_user_agent,
                "platform": str(os_profile["js_platform"]),
                "vendor": str(os_profile["vendor"]),
                "language": str(locale_profile["locale"]),
                "languages": list(locale_profile["languages"]),
                "hardwareConcurrency": int(base_profile["hardwareConcurrency"]),
                "deviceMemory": int(base_profile["deviceMemory"]),
                "maxTouchPoints": 0,
                "cookieEnabled": True,
                "onLine": True,
                "pdfViewerEnabled": True,
                "doNotTrack": "1",
                "webdriver": False,
            },
            "screen": {
                "width": width,
                "height": height,
                "availWidth": avail_width,
                "availHeight": avail_height,
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "window": {
                "innerWidth": viewport_width,
                "innerHeight": viewport_height,
                "outerWidth": outer_width,
                "outerHeight": outer_height,
                "devicePixelRatio": device_scale_factor,
                "visualViewport": {
                    "width": viewport_width,
                    "height": viewport_height,
                    "scale": device_scale_factor,
                    "offsetLeft": 0,
                    "offsetTop": 0,
                    "pageLeft": 0,
                    "pageTop": 0,
                },
            },
            "network": {
                "type": "wifi",
                "effectiveType": "4g",
                "rtt": 45 + int(digest[12] % 75),
                "downlink": round(6.0 + (int(digest[13] % 95) / 10.0), 1),
                "saveData": False,
            },
            "performance": {
                "navigationType": "navigate",
                "redirectCount": 0,
                "paintStart": 90 + int(digest[14] % 70),
                "paintEnd": 150 + int(digest[15] % 95),
            },
            "graphics": {
                **gpu_profile,
                "version": "WebGL 1.0 (OpenGL ES 2.0 Chromium)",
                "shadingLanguageVersion": "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)",
                "maxTextureSize": 16384,
                "maxRenderbufferSize": 16384,
                "maxCombinedTextureImageUnits": 32,
                "maxCubeMapTextureSize": 16384,
                "maxTextureImageUnits": 16,
                "maxVertexTextureImageUnits": 16,
                "maxVertexAttribs": 16,
                "maxVertexUniformVectors": 4096,
                "maxFragmentUniformVectors": 1024,
                "aliasedLineWidthRange": [1, 1],
                "aliasedPointSizeRange": [1, 1024],
                "supportedExtensions": [
                    "ANGLE_instanced_arrays",
                    "EXT_blend_minmax",
                    "EXT_color_buffer_half_float",
                    "EXT_float_blend",
                    "EXT_frag_depth",
                    "EXT_shader_texture_lod",
                    "EXT_sRGB",
                    "OES_element_index_uint",
                    "OES_fbo_render_mipmap",
                    "OES_standard_derivatives",
                    "OES_texture_float",
                    "OES_texture_float_linear",
                    "OES_texture_half_float",
                    "OES_texture_half_float_linear",
                    "OES_vertex_array_object",
                    "WEBGL_color_buffer_float",
                    "WEBGL_compressed_texture_s3tc",
                    "WEBGL_debug_renderer_info",
                    "WEBGL_debug_shaders",
                    "WEBGL_depth_texture",
                    "WEBGL_draw_buffers",
                    "WEBGL_lose_context",
                ],
                "shaderPrecision": {
                    "highFloat": {"rangeMin": 127, "rangeMax": 127, "precision": 23},
                    "mediumFloat": {"rangeMin": 127, "rangeMax": 127, "precision": 23},
                    "lowFloat": {"rangeMin": 127, "rangeMax": 127, "precision": 23},
                    "highInt": {"rangeMin": 31, "rangeMax": 30, "precision": 0},
                    "mediumInt": {"rangeMin": 31, "rangeMax": 30, "precision": 0},
                    "lowInt": {"rangeMin": 31, "rangeMax": 30, "precision": 0},
                },
            },
            "webgpu": {
                "vendor": str(gpu_profile.get("unmaskedVendor") or gpu_profile.get("vendor") or ""),
                "architecture": gpu_arch,
                "device": str(gpu_profile.get("unmaskedRenderer") or gpu_profile.get("renderer") or ""),
                "description": str(gpu_profile.get("renderer") or ""),
                "isFallbackAdapter": False,
                "preferredCanvasFormat": "bgra8unorm",
                "features": [
                    "depth-clip-control",
                    "texture-compression-bc",
                    "timestamp-query",
                ],
                "limits": {
                    "maxTextureDimension1D": 8192,
                    "maxTextureDimension2D": 8192,
                    "maxTextureDimension3D": 2048,
                    "maxTextureArrayLayers": 256,
                    "maxBindGroups": 4,
                    "maxBindingsPerBindGroup": 1000,
                    "maxBufferSize": 268435456,
                    "maxStorageBufferBindingSize": 134217728,
                    "maxUniformBufferBindingSize": 65536,
                    "maxVertexBuffers": 8,
                    "maxVertexAttributes": 16,
                    "maxVertexBufferArrayStride": 2048,
                    "maxInterStageShaderComponents": 60,
                    "maxComputeWorkgroupStorageSize": 16384,
                    "maxComputeInvocationsPerWorkgroup": 256,
                    "maxComputeWorkgroupSizeX": 256,
                    "maxComputeWorkgroupSizeY": 256,
                    "maxComputeWorkgroupSizeZ": 64,
                    "maxComputeWorkgroupsPerDimension": 65535,
                },
            },
            "font": {
                "families": [
                    "Arial",
                    "Calibri",
                    "Cambria",
                    "Consolas",
                    "Courier New",
                    "Georgia",
                    "Microsoft YaHei",
                    "Segoe UI",
                    "Times New Roman",
                    "Verdana",
                ],
            },
            "userAgentMetadata": {
                "platform": str(os_profile["ua_ch_platform"]),
                "platformVersion": str(os_profile["ua_ch_platform_version"]),
                "architecture": str(os_profile["architecture"]),
                "model": "",
                "mobile": False,
                "fullVersion": full_version,
                "bitness": str(os_profile["bitness"]),
                "wow64": bool(os_profile["wow64"]),
                "brands": [
                    {"brand": "Not.A/Brand", "version": "8"},
                    {"brand": "Chromium", "version": major_version},
                    {"brand": "Google Chrome", "version": major_version},
                ],
                "fullVersionList": [
                    {"brand": "Not.A/Brand", "version": "8.0.0.0"},
                    {"brand": "Chromium", "version": full_version},
                    {"brand": "Google Chrome", "version": full_version},
                ],
            },
            "httpHeaders": {
                "Accept-Language": str(locale_profile["acceptLanguage"]),
                "DNT": "1",
            },
            "mediaQueries": {
                "prefersColorScheme": "light",
                "prefersReducedMotion": "no-preference",
                "prefersContrast": "no-preference",
                "forcedColors": "none",
                "hover": "hover",
                "anyHover": "hover",
                "pointer": "fine",
                "anyPointer": "fine",
                "orientation": "landscape" if viewport_width >= viewport_height else "portrait",
            },
            "storage": {
                "quota": 120000000000 + int(digest[16] % 20) * 1000000000,
                "usage": 8000000 + int(digest[17] % 20) * 250000,
                "usageDetails": {
                    "indexedDB": 1000000 + int(digest[18] % 8) * 100000,
                    "caches": 1000000 + int(digest[19] % 8) * 100000,
                    "serviceWorkerRegistrations": 0,
                },
                "persisted": False,
            },
            "behavior": {
                "documentHidden": False,
                "visibilityState": "visible",
                "hasFocus": True,
                "userActivation": {
                    "hasBeenActive": True,
                    "isActive": False,
                },
            },
            "mediaDevices": {
                "devices": [
                    {
                        "kind": "audioinput",
                        "deviceId": f"aid-{seed_prefix[0:12]}",
                        "groupId": f"grp-{seed_prefix[12:20]}",
                        "label": "",
                    },
                    {
                        "kind": "videoinput",
                        "deviceId": f"vid-{seed_prefix[20:32]}",
                        "groupId": f"grp-{seed_prefix[12:20]}",
                        "label": "",
                    },
                    {
                        "kind": "audiooutput",
                        "deviceId": f"aod-{seed_prefix[32:44]}",
                        "groupId": f"grp-{seed_prefix[44:52]}",
                        "label": "Default Audio Output",
                    },
                ],
            },
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
            "webrtc": {
                "candidateMaskIp": "0.0.0.0",
            },
        }
        runtime_profile["signature"] = hashlib.sha256(
            json.dumps(runtime_profile, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return runtime_profile

    def _build_tab_fingerprint_spoof_config(self, tab) -> Dict[str, Any]:
        target_id = str(getattr(tab, "target_id", "") or "").strip() or "unknown-target"
        seed_material = (
            f"{self._runtime_fingerprint_spoof_seed}:{self._browser_instance_id}:{target_id}"
        ).encode("utf-8")
        digest = hashlib.sha256(seed_material).digest()

        def non_zero_byte_delta(index: int) -> int:
            value = (digest[index] % 5) - 2
            return value if value != 0 else 1

        def signed_unit(index: int, scale: float) -> float:
            return round((((digest[index] / 255.0) * 2.0) - 1.0) * scale, 8)

        return {
            "seed": hashlib.md5(seed_material).hexdigest()[:16],
            "runtime": dict(self._runtime_surface_profile or {}),
            "canvas": {
                "rgba": [
                    non_zero_byte_delta(0),
                    non_zero_byte_delta(1),
                    non_zero_byte_delta(2),
                    0,
                ],
                "pixelStep": 13 + (digest[3] % 11),
                "lineShiftX": signed_unit(4, 0.35),
                "lineShiftY": signed_unit(5, 0.35),
            },
            "webgl": {
                "delta": non_zero_byte_delta(6),
                "stride": 19 + (digest[7] % 17),
            },
            "audio": {
                "floatDelta": signed_unit(8, 0.00003),
                "byteDelta": non_zero_byte_delta(9),
                "stride": 17 + (digest[10] % 13),
            },
        }

    def _build_tab_fingerprint_spoof_source(self, tab) -> str:
        config_json = json.dumps(
            self._build_tab_fingerprint_spoof_config(tab),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return (
            """
(() => {
    const marker = __MARKER_JSON__;
    if (window[marker]) {
        return;
    }

    const config = __CONFIG_JSON__;
    window[marker] = config.seed;
    const runtimeProfile = config.runtime || {};

    const setValue = (target, key, value) => {
        try {
            Object.defineProperty(target, key, {
                configurable: true,
                enumerable: false,
                writable: true,
                value,
            });
        } catch (e) {}
    };

    const defineGetter = (target, key, getter) => {
        if (!target) {
            return;
        }
        try {
            Object.defineProperty(target, key, {
                configurable: true,
                enumerable: true,
                get: getter,
            });
        } catch (e) {}
    };

    const defineMethod = (target, key, value) => {
        if (!target || typeof value !== "function") {
            return;
        }
        try {
            Object.defineProperty(target, key, {
                configurable: true,
                enumerable: false,
                writable: true,
                value,
            });
        } catch (e) {
            try {
                target[key] = value;
            } catch (err) {}
        }
    };

    const cloneValue = (value) => {
        if (value === null || value === undefined) {
            return value;
        }
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (e) {
            return value;
        }
    };

    const stableNow = () => {
        try {
            return Math.max(0, Number(performance && performance.now && performance.now()) || 0);
        } catch (e) {
            return Date.now() % 100000;
        }
    };

    const makeEventTargetLike = (target) => {
        const listeners = {};
        defineMethod(target, "addEventListener", (type, listener) => {
            if (typeof listener !== "function") {
                return;
            }
            const name = String(type || "");
            listeners[name] = listeners[name] || [];
            if (!listeners[name].includes(listener)) {
                listeners[name].push(listener);
            }
        });
        defineMethod(target, "removeEventListener", (type, listener) => {
            const name = String(type || "");
            listeners[name] = (listeners[name] || []).filter((item) => item !== listener);
        });
        defineMethod(target, "dispatchEvent", (event) => {
            const name = String(event && event.type || "");
            for (const listener of listeners[name] || []) {
                try {
                    listener.call(target, event);
                } catch (e) {}
            }
            return true;
        });
        return target;
    };

    const makeArrayLike = (items, namedKey, proto) => {
        const sourceItems = Array.isArray(items) ? items : [];
        const target = {};
        try {
            if (proto) {
                Object.setPrototypeOf(target, proto);
            }
        } catch (e) {}
        defineGetter(target, "length", () => sourceItems.length);
        sourceItems.forEach((item, index) => {
            defineGetter(target, String(index), () => item);
            const namedValue = item && item[namedKey];
            if (namedValue) {
                defineGetter(target, String(namedValue), () => item);
            }
        });
        defineMethod(target, "item", (index) => {
            const normalizedIndex = Number(index);
            return Number.isFinite(normalizedIndex) ? (sourceItems[normalizedIndex] || null) : null;
        });
        defineMethod(target, "namedItem", (name) => {
            const normalizedName = String(name || "");
            return sourceItems.find((item) => String(item && item[namedKey] || "") === normalizedName) || null;
        });
        if (typeof Symbol !== "undefined" && Symbol.iterator) {
            defineMethod(target, Symbol.iterator, function* () {
                for (const item of sourceItems) {
                    yield item;
                }
            });
        }
        return target;
    };

    const navigatorProfile = runtimeProfile.navigator || {};
    const localeProfile = runtimeProfile.locale || {};
    const permissionsProfile = runtimeProfile.permissions || {};
    const timezoneProfile = runtimeProfile.timezone || {};
    const uaMetadataProfile = runtimeProfile.userAgentMetadata || {};
    const windowProfile = runtimeProfile.window || {};
    const mediaDevicesProfile = runtimeProfile.mediaDevices || {};
    const webRtcProfile = runtimeProfile.webrtc || {};
    const networkProfile = runtimeProfile.network || {};
    const performanceProfile = runtimeProfile.performance || {};
    const graphicsProfile = runtimeProfile.graphics || {};
    const webgpuProfile = runtimeProfile.webgpu || {};
    const fontProfile = runtimeProfile.font || {};
    const storageProfile = runtimeProfile.storage || {};
    const mediaQueryProfile = runtimeProfile.mediaQueries || {};
    const behaviorProfile = runtimeProfile.behavior || {};
    const mimeTypesProfile = Array.isArray(runtimeProfile.mimeTypes) ? runtimeProfile.mimeTypes : [];
    const pluginsProfile = Array.isArray(runtimeProfile.plugins) ? runtimeProfile.plugins : [];
    const maskedIp = String(webRtcProfile.candidateMaskIp || "0.0.0.0");
    const sanitizeIpText = (input) => String(input || "").replace(/\\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b/g, maskedIp);

    const patchNavigatorMetric = (key) => {
        if (navigatorProfile[key] === undefined || navigatorProfile[key] === null) {
            return;
        }
        defineGetter(Navigator.prototype, key, () => cloneValue(navigatorProfile[key]));
        defineGetter(navigator, key, () => cloneValue(navigatorProfile[key]));
    };

    patchNavigatorMetric("userAgent");
    patchNavigatorMetric("appVersion");
    patchNavigatorMetric("platform");
    patchNavigatorMetric("vendor");
    patchNavigatorMetric("language");
    patchNavigatorMetric("languages");
    patchNavigatorMetric("hardwareConcurrency");
    patchNavigatorMetric("deviceMemory");
    patchNavigatorMetric("maxTouchPoints");
    patchNavigatorMetric("cookieEnabled");
    patchNavigatorMetric("onLine");
    patchNavigatorMetric("pdfViewerEnabled");
    patchNavigatorMetric("doNotTrack");
    if (navigatorProfile.webdriver !== undefined) {
        defineGetter(Navigator.prototype, "webdriver", () => false);
        defineGetter(navigator, "webdriver", () => false);
    }

    if (uaMetadataProfile && Object.keys(uaMetadataProfile).length > 0) {
        const clonedBrands = cloneValue(uaMetadataProfile.brands || []);
        const highEntropyPayload = {
            architecture: String(uaMetadataProfile.architecture || ""),
            bitness: String(uaMetadataProfile.bitness || ""),
            brands: cloneValue(uaMetadataProfile.brands || []),
            fullVersionList: cloneValue(uaMetadataProfile.fullVersionList || []),
            mobile: Boolean(uaMetadataProfile.mobile),
            model: String(uaMetadataProfile.model || ""),
            platform: String(uaMetadataProfile.platform || ""),
            platformVersion: String(uaMetadataProfile.platformVersion || ""),
            uaFullVersion: String(uaMetadataProfile.fullVersion || ""),
            wow64: Boolean(uaMetadataProfile.wow64),
        };
        const userAgentData = {
            brands: clonedBrands,
            mobile: Boolean(uaMetadataProfile.mobile),
            platform: String(uaMetadataProfile.platform || ""),
            getHighEntropyValues: async (hints) => {
                const result = {};
                const normalizedHints = Array.isArray(hints) ? hints : [];
                for (const hint of normalizedHints) {
                    if (Object.prototype.hasOwnProperty.call(highEntropyPayload, hint)) {
                        result[hint] = cloneValue(highEntropyPayload[hint]);
                    }
                }
                return result;
            },
            toJSON: () => ({
                brands: cloneValue(clonedBrands),
                mobile: Boolean(uaMetadataProfile.mobile),
                platform: String(uaMetadataProfile.platform || ""),
            }),
        };
        defineGetter(Navigator.prototype, "userAgentData", () => userAgentData);
        defineGetter(navigator, "userAgentData", () => userAgentData);
    }

    if (pluginsProfile.length || mimeTypesProfile.length) {
        const pluginObjects = [];
        const mimeObjects = [];
        const pluginByName = {};
        for (const pluginProfile of pluginsProfile) {
            const pluginObject = {
                name: String(pluginProfile.name || ""),
                filename: String(pluginProfile.filename || ""),
                description: String(pluginProfile.description || ""),
            };
            try {
                if (window.Plugin && window.Plugin.prototype) {
                    Object.setPrototypeOf(pluginObject, window.Plugin.prototype);
                }
            } catch (e) {}
            pluginObjects.push(pluginObject);
            if (pluginObject.name) {
                pluginByName[pluginObject.name] = pluginObject;
            }
        }
        for (const mimeProfile of mimeTypesProfile) {
            const enabledPlugin = pluginByName[String(mimeProfile.pluginName || "")] || pluginObjects[0] || null;
            const mimeObject = {
                type: String(mimeProfile.type || ""),
                suffixes: String(mimeProfile.suffixes || ""),
                description: String(mimeProfile.description || ""),
                enabledPlugin,
            };
            try {
                if (window.MimeType && window.MimeType.prototype) {
                    Object.setPrototypeOf(mimeObject, window.MimeType.prototype);
                }
            } catch (e) {}
            mimeObjects.push(mimeObject);
        }
        for (const pluginObject of pluginObjects) {
            const pluginProfile = pluginsProfile.find((item) => String(item.name || "") === pluginObject.name) || {};
            const pluginMimeTypes = (Array.isArray(pluginProfile.mimeTypes) ? pluginProfile.mimeTypes : [])
                .map((type) => mimeObjects.find((item) => item.type === String(type || "")))
                .filter(Boolean);
            defineGetter(pluginObject, "length", () => pluginMimeTypes.length);
            pluginMimeTypes.forEach((mimeObject, index) => {
                defineGetter(pluginObject, String(index), () => mimeObject);
                if (mimeObject.type) {
                    defineGetter(pluginObject, mimeObject.type, () => mimeObject);
                }
            });
            defineMethod(pluginObject, "item", (index) => pluginMimeTypes[Number(index)] || null);
            defineMethod(pluginObject, "namedItem", (name) => {
                const normalizedName = String(name || "");
                return pluginMimeTypes.find((item) => item.type === normalizedName) || null;
            });
        }
        const pluginArray = makeArrayLike(pluginObjects, "name", window.PluginArray && window.PluginArray.prototype);
        const mimeTypeArray = makeArrayLike(mimeObjects, "type", window.MimeTypeArray && window.MimeTypeArray.prototype);
        defineGetter(Navigator.prototype, "plugins", () => pluginArray);
        defineGetter(navigator, "plugins", () => pluginArray);
        defineGetter(Navigator.prototype, "mimeTypes", () => mimeTypeArray);
        defineGetter(navigator, "mimeTypes", () => mimeTypeArray);
    }

    const screenProfile = runtimeProfile.screen || {};
    const patchScreenMetric = (key) => {
        if (typeof screenProfile[key] !== "number") {
            return;
        }
        defineGetter(Screen.prototype, key, () => screenProfile[key]);
        if (window.screen) {
            defineGetter(window.screen, key, () => screenProfile[key]);
        }
    };
    patchScreenMetric("width");
    patchScreenMetric("height");
    patchScreenMetric("availWidth");
    patchScreenMetric("availHeight");
    patchScreenMetric("colorDepth");
    patchScreenMetric("pixelDepth");

    const patchWindowMetric = (key) => {
        if (typeof windowProfile[key] !== "number") {
            return;
        }
        defineGetter(window, key, () => windowProfile[key]);
        if (window.Window && window.Window.prototype) {
            defineGetter(window.Window.prototype, key, () => windowProfile[key]);
        }
    };
    patchWindowMetric("innerWidth");
    patchWindowMetric("innerHeight");
    patchWindowMetric("outerWidth");
    patchWindowMetric("outerHeight");
    patchWindowMetric("devicePixelRatio");

    const ensureVisualViewportEnvironment = () => {
        const viewportProfile = windowProfile.visualViewport || {};
        const visualViewport = makeEventTargetLike(window.visualViewport || {});
        const patchViewportMetric = (key, fallbackValue) => {
            const value = viewportProfile[key] !== undefined ? viewportProfile[key] : fallbackValue;
            defineGetter(visualViewport, key, () => Number(value || 0));
        };
        patchViewportMetric("width", windowProfile.innerWidth || 1280);
        patchViewportMetric("height", windowProfile.innerHeight || 720);
        patchViewportMetric("scale", windowProfile.devicePixelRatio || 1);
        patchViewportMetric("offsetLeft", 0);
        patchViewportMetric("offsetTop", 0);
        patchViewportMetric("pageLeft", 0);
        patchViewportMetric("pageTop", 0);
        defineGetter(visualViewport, "onresize", () => null);
        defineGetter(visualViewport, "onscroll", () => null);
        defineGetter(window, "visualViewport", () => visualViewport);
    };
    ensureVisualViewportEnvironment();

    const ensureMatchMediaEnvironment = () => {
        const originalMatchMedia = typeof window.matchMedia === "function"
            ? window.matchMedia.bind(window)
            : null;
        const normalizeQuery = (query) => String(query || "").replace(/\\s+/g, " ").trim().toLowerCase();
        const parsePx = (text) => {
            const match = String(text || "").match(/(-?\\d+(?:\\.\\d+)?)px/);
            return match ? Number(match[1]) : null;
        };
        const resolveMediaMatch = (query) => {
            const normalized = normalizeQuery(query);
            const width = Number(windowProfile.innerWidth || 1280);
            const height = Number(windowProfile.innerHeight || 720);
            const dpr = Number(windowProfile.devicePixelRatio || 1);
            if (!normalized) {
                return false;
            }
            if (normalized.includes("prefers-color-scheme")) {
                const expected = String(mediaQueryProfile.prefersColorScheme || "light").toLowerCase();
                return normalized.includes(expected);
            }
            if (normalized.includes("prefers-reduced-motion")) {
                const expected = String(mediaQueryProfile.prefersReducedMotion || "no-preference").toLowerCase();
                return normalized.includes(expected);
            }
            if (normalized.includes("prefers-contrast")) {
                const expected = String(mediaQueryProfile.prefersContrast || "no-preference").toLowerCase();
                return normalized.includes(expected);
            }
            if (normalized.includes("forced-colors")) {
                const expected = String(mediaQueryProfile.forcedColors || "none").toLowerCase();
                return normalized.includes(expected);
            }
            if (normalized.includes("(hover:")) {
                return normalized.includes(String(mediaQueryProfile.hover || "hover").toLowerCase());
            }
            if (normalized.includes("(any-hover:")) {
                return normalized.includes(String(mediaQueryProfile.anyHover || "hover").toLowerCase());
            }
            if (normalized.includes("(pointer:")) {
                return normalized.includes(String(mediaQueryProfile.pointer || "fine").toLowerCase());
            }
            if (normalized.includes("(any-pointer:")) {
                return normalized.includes(String(mediaQueryProfile.anyPointer || "fine").toLowerCase());
            }
            if (normalized.includes("orientation")) {
                return normalized.includes(String(mediaQueryProfile.orientation || (width >= height ? "landscape" : "portrait")).toLowerCase());
            }
            if (normalized.includes("min-width")) {
                const value = parsePx(normalized);
                return value === null ? false : width >= value;
            }
            if (normalized.includes("max-width")) {
                const value = parsePx(normalized);
                return value === null ? false : width <= value;
            }
            if (normalized.includes("min-height")) {
                const value = parsePx(normalized);
                return value === null ? false : height >= value;
            }
            if (normalized.includes("max-height")) {
                const value = parsePx(normalized);
                return value === null ? false : height <= value;
            }
            if (normalized.includes("resolution") || normalized.includes("device-pixel-ratio")) {
                const match = normalized.match(/(-?\\d+(?:\\.\\d+)?)(?:dppx|x)/);
                return match ? dpr >= Number(match[1]) : dpr >= 1;
            }
            if (originalMatchMedia) {
                try {
                    return Boolean(originalMatchMedia(query).matches);
                } catch (e) {}
            }
            return false;
        };
        defineMethod(window, "matchMedia", (query) => {
            const media = String(query || "");
            const target = makeEventTargetLike({
                media,
                matches: resolveMediaMatch(media),
                onchange: null,
            });
            if (window.MediaQueryList && window.MediaQueryList.prototype) {
                try {
                    Object.setPrototypeOf(target, window.MediaQueryList.prototype);
                } catch (e) {}
            }
            defineMethod(target, "addListener", (listener) => target.addEventListener("change", listener));
            defineMethod(target, "removeListener", (listener) => target.removeEventListener("change", listener));
            return target;
        });
    };
    ensureMatchMediaEnvironment();

    const localeCode = String(localeProfile.code || navigatorProfile.language || "");
    const timezoneId = String(timezoneProfile.id || "");
    const patchIntlResolvedOptions = (ctor) => {
        if (!ctor || !ctor.prototype || typeof ctor.prototype.resolvedOptions !== "function") {
            return;
        }
        const original = ctor.prototype.resolvedOptions;
        ctor.prototype.resolvedOptions = function(...args) {
            const resolved = original.apply(this, args) || {};
            if (localeCode) {
                resolved.locale = localeCode;
            }
            if (timezoneId) {
                resolved.timeZone = timezoneId;
            }
            return resolved;
        };
    };
    patchIntlResolvedOptions(window.Intl && window.Intl.DateTimeFormat);
    patchIntlResolvedOptions(window.Intl && window.Intl.NumberFormat);
    patchIntlResolvedOptions(window.Intl && window.Intl.Collator);
    patchIntlResolvedOptions(window.Intl && window.Intl.PluralRules);
    patchIntlResolvedOptions(window.Intl && window.Intl.RelativeTimeFormat);
    patchIntlResolvedOptions(window.Intl && window.Intl.ListFormat);
    patchIntlResolvedOptions(window.Intl && window.Intl.DisplayNames);
    patchIntlResolvedOptions(window.Intl && window.Intl.Segmenter);

    const buildPermissionStatus = (state) => {
        const status = makeEventTargetLike({
            state,
            onchange: null,
        });
        try {
            if (window.PermissionStatus && window.PermissionStatus.prototype) {
                Object.setPrototypeOf(status, window.PermissionStatus.prototype);
            }
        } catch (e) {}
        return status;
    };
    if (navigator.permissions) {
        const permissionsTarget = navigator.permissions;
        const permissionsProto = Object.getPrototypeOf(permissionsTarget);
        const originalQuery = typeof permissionsTarget.query === "function"
            ? permissionsTarget.query.bind(permissionsTarget)
            : null;
        const patchedQuery = function(descriptor) {
            const name = String(descriptor && descriptor.name || "").toLowerCase();
            if (name && Object.prototype.hasOwnProperty.call(permissionsProfile, name)) {
                return Promise.resolve(buildPermissionStatus(String(permissionsProfile[name] || "prompt")));
            }
            if (!originalQuery) {
                return Promise.resolve(buildPermissionStatus("prompt"));
            }
            return originalQuery(descriptor);
        };
        defineMethod(permissionsProto, "query", patchedQuery);
        defineMethod(permissionsTarget, "query", patchedQuery);
    }

    if (networkProfile && Object.keys(networkProfile).length > 0) {
        const networkInfo = makeEventTargetLike(navigator.connection || {});
        const patchNetworkMetric = (key, fallbackValue) => {
            const value = networkProfile[key] !== undefined ? networkProfile[key] : fallbackValue;
            defineGetter(networkInfo, key, () => cloneValue(value));
        };
        patchNetworkMetric("type", "wifi");
        patchNetworkMetric("effectiveType", "4g");
        patchNetworkMetric("rtt", 75);
        patchNetworkMetric("downlink", 10);
        patchNetworkMetric("saveData", false);
        defineGetter(networkInfo, "onchange", () => null);
        defineGetter(Navigator.prototype, "connection", () => networkInfo);
        defineGetter(navigator, "connection", () => networkInfo);
        defineGetter(Navigator.prototype, "mozConnection", () => networkInfo);
        defineGetter(navigator, "mozConnection", () => networkInfo);
        defineGetter(Navigator.prototype, "webkitConnection", () => networkInfo);
        defineGetter(navigator, "webkitConnection", () => networkInfo);
    }

    const ensureMediaDevices = () => {
        let target = navigator.mediaDevices || null;
        if (!target) {
            target = {};
        }
        const devices = (Array.isArray(mediaDevicesProfile.devices) ? mediaDevicesProfile.devices : []).map((device) => {
            const mediaDevice = {
                deviceId: String(device.deviceId || ""),
                groupId: String(device.groupId || ""),
                kind: String(device.kind || ""),
                label: String(device.label || ""),
                toJSON() {
                    return {
                        deviceId: this.deviceId,
                        groupId: this.groupId,
                        kind: this.kind,
                        label: this.label,
                    };
                },
            };
            try {
                if (window.MediaDeviceInfo && window.MediaDeviceInfo.prototype) {
                    Object.setPrototypeOf(mediaDevice, window.MediaDeviceInfo.prototype);
                }
            } catch (e) {}
            return mediaDevice;
        });
        const deniedMedia = () => Promise.reject(new DOMException("Permission denied", "NotAllowedError"));
        defineGetter(target, "ondevicechange", () => null);
        defineMethod(target, "enumerateDevices", async () => devices.slice());
        defineMethod(target, "getUserMedia", deniedMedia);
        defineMethod(target, "getDisplayMedia", deniedMedia);
        defineMethod(target, "getSupportedConstraints", () => ({
            aspectRatio: true,
            autoGainControl: true,
            channelCount: true,
            deviceId: true,
            echoCancellation: true,
            facingMode: true,
            frameRate: true,
            groupId: true,
            height: true,
            latency: true,
            noiseSuppression: true,
            sampleRate: true,
            sampleSize: true,
            width: true,
        }));
        defineGetter(Navigator.prototype, "mediaDevices", () => target);
        defineGetter(navigator, "mediaDevices", () => target);
    };
    ensureMediaDevices();

    const ensureFontFaceEnvironment = () => {
        const knownFonts = Array.isArray(fontProfile.families) ? fontProfile.families : [];
        if (!window.FontFace) {
            class PersonalFontFace {
                constructor(family, source, descriptors) {
                    this.family = String(family || "");
                    this.source = source;
                    this.descriptors = descriptors || {};
                    this.status = "unloaded";
                    this.loaded = Promise.resolve(this);
                }
                load() {
                    this.status = "loaded";
                    this.loaded = Promise.resolve(this);
                    return this.loaded;
                }
            }
            setValue(window, "FontFace", PersonalFontFace);
        }
        const existingFontSet = document.fonts || null;
        const fontSet = existingFontSet || makeEventTargetLike({
                status: "loaded",
                ready: Promise.resolve(),
                size: knownFonts.length,
            });
        const originalFontCheck = typeof fontSet.check === "function" ? fontSet.check.bind(fontSet) : null;
        const originalFontLoad = typeof fontSet.load === "function" ? fontSet.load.bind(fontSet) : null;
        defineMethod(fontSet, "check", (font, text) => {
            const normalizedFont = String(font || "").toLowerCase();
            if (knownFonts.some((family) => normalizedFont.includes(String(family).toLowerCase()))) {
                return true;
            }
            return originalFontCheck ? Boolean(originalFontCheck(font, text)) : false;
        });
        defineMethod(fontSet, "load", async (font, text) => {
            const normalizedFont = String(font || "").toLowerCase();
            if (knownFonts.some((family) => normalizedFont.includes(String(family).toLowerCase()))) {
                return [];
            }
            return originalFontLoad ? originalFontLoad(font, text) : [];
        });
        if (typeof fontSet.add !== "function") {
            defineMethod(fontSet, "add", () => fontSet);
        }
        if (typeof fontSet.delete !== "function") {
            defineMethod(fontSet, "delete", () => false);
        }
        if (typeof fontSet.clear !== "function") {
            defineMethod(fontSet, "clear", () => undefined);
        }
        if (typeof fontSet.forEach !== "function") {
            defineMethod(fontSet, "forEach", () => undefined);
        }
        if (typeof Symbol !== "undefined" && Symbol.iterator && typeof fontSet[Symbol.iterator] !== "function") {
            defineMethod(fontSet, Symbol.iterator, function* () {});
        }
        defineGetter(Document.prototype, "fonts", () => fontSet);
        defineGetter(document, "fonts", () => fontSet);
    };
    ensureFontFaceEnvironment();

    const patchRtcSessionDescription = (ctor) => {
        if (!ctor || !ctor.prototype) {
            return;
        }
        const descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, "sdp");
        if (!descriptor || typeof descriptor.get !== "function") {
            return;
        }
        try {
            Object.defineProperty(ctor.prototype, "sdp", {
                configurable: true,
                enumerable: descriptor.enumerable,
                get() {
                    return sanitizeIpText(descriptor.get.call(this));
                },
            });
        } catch (e) {}
    };
    patchRtcSessionDescription(window.RTCSessionDescription);

    if (window.RTCIceCandidate && window.RTCIceCandidate.prototype) {
        const candidateDescriptor = Object.getOwnPropertyDescriptor(window.RTCIceCandidate.prototype, "candidate");
        if (candidateDescriptor && typeof candidateDescriptor.get === "function") {
            try {
                Object.defineProperty(window.RTCIceCandidate.prototype, "candidate", {
                    configurable: true,
                    enumerable: candidateDescriptor.enumerable,
                    get() {
                        return sanitizeIpText(candidateDescriptor.get.call(this));
                    },
                });
            } catch (e) {}
        }
        defineGetter(window.RTCIceCandidate.prototype, "address", () => maskedIp);
        defineGetter(window.RTCIceCandidate.prototype, "relatedAddress", () => maskedIp);
    }

    if (window.RTCPeerConnection && window.RTCPeerConnection.prototype) {
        const wrapAsyncDescriptionMethod = (methodName) => {
            const original = window.RTCPeerConnection.prototype[methodName];
            if (typeof original !== "function") {
                return;
            }
            window.RTCPeerConnection.prototype[methodName] = function(...args) {
                return Promise.resolve(original.apply(this, args)).then((description) => {
                    if (!description || typeof description.sdp !== "string") {
                        return description;
                    }
                    return Object.assign({}, description, {
                        sdp: sanitizeIpText(description.sdp),
                    });
                });
            };
        };
        wrapAsyncDescriptionMethod("createOffer");
        wrapAsyncDescriptionMethod("createAnswer");

        const wrapDescriptionSetter = (methodName) => {
            const original = window.RTCPeerConnection.prototype[methodName];
            if (typeof original !== "function") {
                return;
            }
            window.RTCPeerConnection.prototype[methodName] = function(description, ...args) {
                let nextDescription = description;
                if (description && typeof description.sdp === "string") {
                    nextDescription = Object.assign({}, description, {
                        sdp: sanitizeIpText(description.sdp),
                    });
                }
                return original.call(this, nextDescription, ...args);
            };
        };
        wrapDescriptionSetter("setLocalDescription");
        wrapDescriptionSetter("setRemoteDescription");

        const originalAddIceCandidate = window.RTCPeerConnection.prototype.addIceCandidate;
        if (typeof originalAddIceCandidate === "function") {
            window.RTCPeerConnection.prototype.addIceCandidate = function(candidate, ...args) {
                let nextCandidate = candidate;
                if (candidate && typeof candidate.candidate === "string") {
                    nextCandidate = Object.assign({}, candidate, {
                        candidate: sanitizeIpText(candidate.candidate),
                    });
                }
                return originalAddIceCandidate.call(this, nextCandidate, ...args);
            };
        }
    }

    const applyCanvasNoise = (canvas) => {
        try {
            if (!canvas || !canvas.width || !canvas.height) {
                return canvas;
            }
            const clone = document.createElement("canvas");
            clone.width = canvas.width;
            clone.height = canvas.height;
            const ctx = clone.getContext("2d", { willReadFrequently: true });
            if (!ctx) {
                return canvas;
            }
            ctx.drawImage(canvas, 0, 0);
            const x = Math.max(0, (canvas.width - 1) % config.canvas.pixelStep);
            const y = Math.max(0, (canvas.height - 1) % config.canvas.pixelStep);
            const imageData = ctx.getImageData(x, y, 1, 1);
            const data = imageData.data;
            for (let i = 0; i < 4; i += 1) {
                const nextValue = Number(data[i] || 0) + Number(config.canvas.rgba[i] || 0);
                data[i] = Math.max(0, Math.min(255, nextValue));
            }
            ctx.putImageData(imageData, x, y);
            ctx.save();
            ctx.globalAlpha = 0.01;
            ctx.fillStyle = "rgba(0,0,0,0.01)";
            ctx.fillRect(
                Math.max(0, canvas.width * 0.5 + config.canvas.lineShiftX),
                Math.max(0, canvas.height * 0.5 + config.canvas.lineShiftY),
                1,
                1
            );
            ctx.restore();
            return clone;
        } catch (e) {
            return canvas;
        }
    };

    const patchCanvasExport = (proto, methodName) => {
        if (!proto || typeof proto[methodName] !== "function") {
            return;
        }
        const original = proto[methodName];
        proto[methodName] = function(...args) {
            return original.apply(applyCanvasNoise(this), args);
        };
    };

    patchCanvasExport(HTMLCanvasElement.prototype, "toDataURL");
    patchCanvasExport(HTMLCanvasElement.prototype, "toBlob");

    if (CanvasRenderingContext2D && CanvasRenderingContext2D.prototype) {
        const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        if (typeof originalGetImageData === "function") {
            CanvasRenderingContext2D.prototype.getImageData = function(...args) {
                const result = originalGetImageData.apply(this, args);
                try {
                    if (result && result.data && result.data.length >= 4) {
                        for (let offset = 0; offset < result.data.length; offset += Math.max(4, config.canvas.pixelStep * 4)) {
                            result.data[offset] = Math.max(0, Math.min(255, result.data[offset] + config.canvas.rgba[0]));
                        }
                    }
                } catch (e) {}
                return result;
            };
        }
        const originalMeasureText = CanvasRenderingContext2D.prototype.measureText;
        if (typeof originalMeasureText === "function") {
            CanvasRenderingContext2D.prototype.measureText = function(text) {
                const result = originalMeasureText.apply(this, arguments);
                try {
                    const knownFonts = Array.isArray(fontProfile.families) ? fontProfile.families : [];
                    const fontText = String(this.font || "").toLowerCase();
                    const hasKnownFont = knownFonts.some((family) => fontText.includes(String(family).toLowerCase()));
                    if (hasKnownFont && result && typeof result.width === "number") {
                        const delta = (String(text || "").length % 7) * 0.003;
                        Object.defineProperty(result, "width", {
                            configurable: true,
                            enumerable: true,
                            value: result.width + delta,
                        });
                    }
                } catch (e) {}
                return result;
            };
        }
    }

    const patchWebGL = (proto) => {
        if (!proto) {
            return;
        }
        if (typeof proto.getParameter === "function") {
            const originalGetParameter = proto.getParameter;
            proto.getParameter = function(parameter) {
                try {
                    const normalizedParameter = Number(parameter);
                    if (normalizedParameter === 37445) {
                        return String(graphicsProfile.unmaskedVendor || graphicsProfile.vendor || "Google Inc.");
                    }
                    if (normalizedParameter === 37446) {
                        return String(graphicsProfile.unmaskedRenderer || graphicsProfile.renderer || "ANGLE");
                    }
                    if (normalizedParameter === 7936 && graphicsProfile.vendor) {
                        return String(graphicsProfile.vendor);
                    }
                    if (normalizedParameter === 7937 && graphicsProfile.renderer) {
                        return String(graphicsProfile.renderer);
                    }
                    if (normalizedParameter === 7938 && graphicsProfile.version) {
                        return String(graphicsProfile.version);
                    }
                    if (normalizedParameter === 35724 && graphicsProfile.shadingLanguageVersion) {
                        return String(graphicsProfile.shadingLanguageVersion);
                    }
                    if (normalizedParameter === 3379 && graphicsProfile.maxTextureSize) {
                        return Number(graphicsProfile.maxTextureSize);
                    }
                    if (normalizedParameter === 34024 && graphicsProfile.maxRenderbufferSize) {
                        return Number(graphicsProfile.maxRenderbufferSize);
                    }
                    if (normalizedParameter === 35661 && graphicsProfile.maxCombinedTextureImageUnits) {
                        return Number(graphicsProfile.maxCombinedTextureImageUnits);
                    }
                    if (normalizedParameter === 34076 && graphicsProfile.maxCubeMapTextureSize) {
                        return Number(graphicsProfile.maxCubeMapTextureSize);
                    }
                    if (normalizedParameter === 34930 && graphicsProfile.maxTextureImageUnits) {
                        return Number(graphicsProfile.maxTextureImageUnits);
                    }
                    if (normalizedParameter === 35660 && graphicsProfile.maxVertexTextureImageUnits) {
                        return Number(graphicsProfile.maxVertexTextureImageUnits);
                    }
                    if (normalizedParameter === 34921 && graphicsProfile.maxVertexAttribs) {
                        return Number(graphicsProfile.maxVertexAttribs);
                    }
                    if (normalizedParameter === 36347 && graphicsProfile.maxVertexUniformVectors) {
                        return Number(graphicsProfile.maxVertexUniformVectors);
                    }
                    if (normalizedParameter === 36349 && graphicsProfile.maxFragmentUniformVectors) {
                        return Number(graphicsProfile.maxFragmentUniformVectors);
                    }
                    if (normalizedParameter === 33902 && Array.isArray(graphicsProfile.aliasedPointSizeRange)) {
                        return new Float32Array(graphicsProfile.aliasedPointSizeRange.map(Number));
                    }
                    if (normalizedParameter === 33901 && Array.isArray(graphicsProfile.aliasedLineWidthRange)) {
                        return new Float32Array(graphicsProfile.aliasedLineWidthRange.map(Number));
                    }
                } catch (e) {}
                return originalGetParameter.apply(this, arguments);
            };
        }
        if (typeof proto.getShaderPrecisionFormat === "function") {
            const originalGetShaderPrecisionFormat = proto.getShaderPrecisionFormat;
            proto.getShaderPrecisionFormat = function(shaderType, precisionType) {
                try {
                    const precisionProfile = graphicsProfile.shaderPrecision || {};
                    const normalizedPrecision = Number(precisionType);
                    let selected = null;
                    if (normalizedPrecision === 36338) {
                        selected = precisionProfile.highFloat;
                    } else if (normalizedPrecision === 36337) {
                        selected = precisionProfile.mediumFloat;
                    } else if (normalizedPrecision === 36336) {
                        selected = precisionProfile.lowFloat;
                    } else if (normalizedPrecision === 36341) {
                        selected = precisionProfile.highInt;
                    } else if (normalizedPrecision === 36340) {
                        selected = precisionProfile.mediumInt;
                    } else if (normalizedPrecision === 36339) {
                        selected = precisionProfile.lowInt;
                    }
                    if (selected) {
                        return {
                            rangeMin: Number(selected.rangeMin || 0),
                            rangeMax: Number(selected.rangeMax || 0),
                            precision: Number(selected.precision || 0),
                        };
                    }
                } catch (e) {}
                return originalGetShaderPrecisionFormat.apply(this, arguments);
            };
        }
        if (typeof proto.readPixels === "function") {
            const originalReadPixels = proto.readPixels;
            proto.readPixels = function(...args) {
                const output = args.find((item) => item && typeof item.length === "number" && typeof item.BYTES_PER_ELEMENT === "number");
                const result = originalReadPixels.apply(this, args);
                try {
                    if (output && output.length) {
                        const stride = Math.max(1, Number(config.webgl.stride || 23));
                        const delta = Number(config.webgl.delta || 1);
                        for (let i = 0; i < output.length; i += stride) {
                            const nextValue = Number(output[i] || 0) + delta;
                            output[i] = Math.max(0, Math.min(255, nextValue));
                        }
                    }
                } catch (e) {}
                return result;
            };
        }
        if (typeof proto.getSupportedExtensions === "function") {
            const originalGetSupportedExtensions = proto.getSupportedExtensions;
            proto.getSupportedExtensions = function(...args) {
                const result = originalGetSupportedExtensions.apply(this, args);
                const next = Array.isArray(result) ? result.slice() : [];
                for (const extensionName of (Array.isArray(graphicsProfile.supportedExtensions) ? graphicsProfile.supportedExtensions : [])) {
                    if (extensionName && !next.includes(extensionName)) {
                        next.push(extensionName);
                    }
                }
                return next;
            };
        }
        if (typeof proto.getExtension === "function") {
            const originalGetExtension = proto.getExtension;
            proto.getExtension = function(name) {
                const extensionName = String(name || "");
                if (extensionName === "WEBGL_debug_renderer_info") {
                    return {
                        UNMASKED_VENDOR_WEBGL: 37445,
                        UNMASKED_RENDERER_WEBGL: 37446,
                    };
                }
                const result = originalGetExtension.apply(this, arguments);
                if (result) {
                    return result;
                }
                const supported = Array.isArray(graphicsProfile.supportedExtensions)
                    ? graphicsProfile.supportedExtensions
                    : [];
                if (supported.includes(extensionName)) {
                    return {};
                }
                return result;
            };
        }
    };

    patchWebGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
    patchWebGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);

    const ensureGeometryEnvironment = () => {
        const patchRectCtor = (ctor) => {
            if (!ctor || !ctor.prototype) {
                return;
            }
            if (typeof ctor.fromRect !== "function") {
                defineMethod(ctor, "fromRect", (rect) => {
                    const source = rect || {};
                    return new ctor(
                        Number(source.x || source.left || 0),
                        Number(source.y || source.top || 0),
                        Number(source.width || 0),
                        Number(source.height || 0)
                    );
                });
            }
            if (typeof ctor.prototype.toJSON !== "function") {
                defineMethod(ctor.prototype, "toJSON", function() {
                    return {
                        x: Number(this.x || 0),
                        y: Number(this.y || 0),
                        width: Number(this.width || 0),
                        height: Number(this.height || 0),
                        top: Number(this.top || this.y || 0),
                        right: Number(this.right || ((this.x || 0) + (this.width || 0))),
                        bottom: Number(this.bottom || ((this.y || 0) + (this.height || 0))),
                        left: Number(this.left || this.x || 0),
                    };
                });
            }
        };
        patchRectCtor(window.DOMRect);
        patchRectCtor(window.DOMRectReadOnly);
        if (window.DOMPoint && window.DOMPoint.prototype && typeof window.DOMPoint.prototype.toJSON !== "function") {
            defineMethod(window.DOMPoint.prototype, "toJSON", function() {
                return { x: Number(this.x || 0), y: Number(this.y || 0), z: Number(this.z || 0), w: Number(this.w || 1) };
            });
        }
    };
    ensureGeometryEnvironment();

    const ensureCssEnvironment = () => {
        if (!window.CSS) {
            setValue(window, "CSS", {});
        }
        if (window.CSS) {
            if (typeof window.CSS.supports !== "function") {
                defineMethod(window.CSS, "supports", (...args) => {
                    const property = args[0];
                    const value = args[1];
                    if (args.length === 1) {
                        return typeof property === "string" && property.length > 0;
                    }
                    return typeof property === "string" && typeof value === "string";
                });
            }
            if (typeof window.CSS.escape !== "function") {
                defineMethod(window.CSS, "escape", (value) => String(value || "").replace(/[^a-zA-Z0-9_-]/g, "\\\\$&"));
            }
            if (!window.CSS.highlights) {
                setValue(window.CSS, "highlights", new Map());
            }
        }
    };
    ensureCssEnvironment();

    const ensurePerformanceEnvironment = () => {
        if (!window.performance) {
            return;
        }
        const makeEntry = (entry) => ({
            ...entry,
            toJSON() {
                return { ...entry };
            },
        });
        const paintStart = Number(performanceProfile.paintStart || 110);
        const paintEnd = Number(performanceProfile.paintEnd || 180);
        const fallbackPaintEntries = [
            makeEntry({ name: "first-paint", entryType: "paint", startTime: paintStart, duration: 0 }),
            makeEntry({ name: "first-contentful-paint", entryType: "paint", startTime: paintEnd, duration: 0 }),
        ];
        const fallbackNavigationEntry = makeEntry({
            name: location.href,
            entryType: "navigation",
            startTime: 0,
            duration: Math.max(paintEnd + 80, stableNow()),
            initiatorType: "navigation",
            type: String(performanceProfile.navigationType || "navigate"),
            redirectCount: Number(performanceProfile.redirectCount || 0),
        });
        if (typeof performance.getEntriesByType === "function") {
            const originalGetEntriesByType = performance.getEntriesByType.bind(performance);
            defineMethod(performance, "getEntriesByType", (type) => {
                const normalizedType = String(type || "");
                const entries = originalGetEntriesByType(normalizedType) || [];
                if (entries.length > 0) {
                    return entries;
                }
                if (normalizedType === "paint") {
                    return fallbackPaintEntries.slice();
                }
                if (normalizedType === "navigation") {
                    return [fallbackNavigationEntry];
                }
                return entries;
            });
        }
        if (typeof performance.getEntriesByName === "function") {
            const originalGetEntriesByName = performance.getEntriesByName.bind(performance);
            defineMethod(performance, "getEntriesByName", (name, type) => {
                const entries = originalGetEntriesByName(name, type) || [];
                if (entries.length > 0) {
                    return entries;
                }
                const normalizedName = String(name || "");
                const normalizedType = String(type || "");
                if ((!normalizedType || normalizedType === "paint") && normalizedName === "first-contentful-paint") {
                    return [fallbackPaintEntries[1]];
                }
                if ((!normalizedType || normalizedType === "paint") && normalizedName === "first-paint") {
                    return [fallbackPaintEntries[0]];
                }
                return entries;
            });
        }
    };
    ensurePerformanceEnvironment();

    const ensureNavigationEnvironment = () => {
        if (window.navigation) {
            return;
        }
        const currentEntry = makeEventTargetLike({
            id: "current",
            key: "current",
            index: 0,
            sameDocument: true,
            url: location.href,
            getState: () => null,
        });
        const navigation = makeEventTargetLike({
            currentEntry,
            transition: null,
            activation: { from: null, entry: currentEntry, navigationType: "push", activationStart: 0 },
            canGoBack: history.length > 1,
            canGoForward: false,
            onnavigate: null,
            onnavigatesuccess: null,
            onnavigateerror: null,
            entries: () => [currentEntry],
        });
        const navigationResult = () => ({
            committed: Promise.resolve(currentEntry),
            finished: Promise.resolve(currentEntry),
        });
        defineMethod(navigation, "navigate", navigationResult);
        defineMethod(navigation, "reload", navigationResult);
        defineMethod(navigation, "back", navigationResult);
        defineMethod(navigation, "forward", navigationResult);
        defineMethod(navigation, "traverseTo", navigationResult);
        defineMethod(navigation, "updateCurrentEntry", () => undefined);
        setValue(window, "navigation", navigation);
    };
    ensureNavigationEnvironment();

    const ensureCapabilityEnvironment = () => {
        const chromeObject = window.chrome || {};
        if (!chromeObject.app) {
            chromeObject.app = {
                isInstalled: false,
                InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" },
                RunningState: { CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" },
                getDetails: () => null,
                getIsInstalled: () => false,
                runningState: () => "cannot_run",
            };
        }
        if (typeof chromeObject.csi !== "function") {
            chromeObject.csi = () => ({
                startE: Date.now() - Math.floor(stableNow()),
                onloadT: Date.now(),
                pageT: Math.floor(stableNow()),
                tran: 15,
            });
        }
        if (typeof chromeObject.loadTimes !== "function") {
            chromeObject.loadTimes = () => {
                const nowSeconds = Date.now() / 1000;
                return {
                    requestTime: nowSeconds - 1.2,
                    startLoadTime: nowSeconds - 1.1,
                    commitLoadTime: nowSeconds - 0.8,
                    finishDocumentLoadTime: nowSeconds - 0.2,
                    finishLoadTime: nowSeconds - 0.1,
                    firstPaintTime: nowSeconds - 0.6,
                    firstPaintAfterLoadTime: 0,
                    navigationType: String(performanceProfile.navigationType || "Other"),
                    wasFetchedViaSpdy: true,
                    wasNpnNegotiated: true,
                    npnNegotiatedProtocol: "h2",
                    wasAlternateProtocolAvailable: false,
                    connectionInfo: "h2",
                };
            };
        }
        if (!chromeObject.runtime) {
            chromeObject.runtime = {
                PlatformOs: { MAC: "mac", WIN: "win", ANDROID: "android", CROS: "cros", LINUX: "linux", OPENBSD: "openbsd" },
                PlatformArch: { ARM: "arm", ARM64: "arm64", X86_32: "x86-32", X86_64: "x86-64" },
                PlatformNaclArch: { ARM: "arm", X86_32: "x86-32", X86_64: "x86-64" },
                RequestUpdateCheckStatus: { THROTTLED: "throttled", NO_UPDATE: "no_update", UPDATE_AVAILABLE: "update_available" },
            };
        }
        if (!window.chrome) {
            setValue(window, "chrome", chromeObject);
        }

        const storage = navigator.storage || {};
        defineMethod(storage, "estimate", async () => ({
            quota: Number(storageProfile.quota || 120000000000),
            usage: Number(storageProfile.usage || 0),
            usageDetails: cloneValue(storageProfile.usageDetails || {}),
        }));
        defineMethod(storage, "persist", async () => Boolean(storageProfile.persisted));
        defineMethod(storage, "persisted", async () => Boolean(storageProfile.persisted));
        defineGetter(Navigator.prototype, "storage", () => storage);
        defineGetter(navigator, "storage", () => storage);

        if (!window.caches) {
            const cacheStore = new Map();
            const cacheStorage = {
                async keys() {
                    return Array.from(cacheStore.keys());
                },
                async has(name) {
                    return cacheStore.has(String(name || ""));
                },
                async delete(name) {
                    return cacheStore.delete(String(name || ""));
                },
                async match() {
                    return undefined;
                },
                async open(name) {
                    const key = String(name || "");
                    if (!cacheStore.has(key)) {
                        cacheStore.set(key, {
                            async match() { return undefined; },
                            async matchAll() { return []; },
                            async add() { return undefined; },
                            async addAll() { return undefined; },
                            async put() { return undefined; },
                            async delete() { return false; },
                            async keys() { return []; },
                        });
                    }
                    return cacheStore.get(key);
                },
            };
            setValue(window, "caches", cacheStorage);
        }

        if (!window.indexedDB) {
            const makeRequest = () => {
                const request = makeEventTargetLike({
                    result: undefined,
                    error: null,
                    source: null,
                    transaction: null,
                    readyState: "done",
                    onsuccess: null,
                    onerror: null,
                    onblocked: null,
                    onupgradeneeded: null,
                });
                setTimeout(() => {
                    try {
                        if (typeof request.onsuccess === "function") {
                            request.onsuccess.call(request, { type: "success", target: request });
                        }
                        request.dispatchEvent({ type: "success", target: request });
                    } catch (e) {}
                }, 0);
                return request;
            };
            setValue(window, "indexedDB", {
                open: () => makeRequest(),
                deleteDatabase: () => makeRequest(),
                databases: async () => [],
                cmp: (first, second) => (first === second ? 0 : (first > second ? 1 : -1)),
            });
        }
        if (!navigator.credentials) {
            const credentials = {
                create: async () => null,
                get: async () => null,
                store: async (credential) => credential || null,
                preventSilentAccess: async () => undefined,
            };
            defineGetter(Navigator.prototype, "credentials", () => credentials);
            defineGetter(navigator, "credentials", () => credentials);
        }
        if (!navigator.locks) {
            const locks = {
                query: async () => ({ held: [], pending: [] }),
                request: async (name, options, callback) => {
                    const cb = typeof options === "function" ? options : callback;
                    return typeof cb === "function" ? cb({ name: String(name || ""), mode: "exclusive" }) : undefined;
                },
            };
            defineGetter(Navigator.prototype, "locks", () => locks);
            defineGetter(navigator, "locks", () => locks);
        }
        if (!navigator.keyboard) {
            const keyboard = {
                getLayoutMap: async () => new Map([
                    ["KeyA", "a"],
                    ["KeyS", "s"],
                    ["KeyD", "d"],
                    ["KeyF", "f"],
                    ["Digit1", "1"],
                    ["Digit2", "2"],
                ]),
                lock: async () => undefined,
                unlock: () => undefined,
            };
            defineGetter(Navigator.prototype, "keyboard", () => keyboard);
            defineGetter(navigator, "keyboard", () => keyboard);
        }
        if (!navigator.scheduling) {
            const scheduling = { isInputPending: () => false };
            defineGetter(Navigator.prototype, "scheduling", () => scheduling);
            defineGetter(navigator, "scheduling", () => scheduling);
        }
        if (!window.scheduler) {
            setValue(window, "scheduler", {
                postTask: (callback) => Promise.resolve().then(() => (typeof callback === "function" ? callback() : undefined)),
                yield: () => Promise.resolve(),
            });
        }
        if (!window.trustedTypes) {
            const trustedTypes = {
                emptyHTML: "",
                emptyScript: "",
                createPolicy: (name, rules) => ({ name: String(name || ""), ...Object(rules || {}) }),
                getAttributeType: () => null,
                getPropertyType: () => null,
                isHTML: () => false,
                isScript: () => false,
                isScriptURL: () => false,
            };
            setValue(window, "trustedTypes", trustedTypes);
        }
    };
    ensureCapabilityEnvironment();

    const ensureWebGpuEnvironment = () => {
        if (!webgpuProfile || Object.keys(webgpuProfile).length <= 0) {
            return;
        }
        const adapterInfo = {
            vendor: String(webgpuProfile.vendor || graphicsProfile.unmaskedVendor || ""),
            architecture: String(webgpuProfile.architecture || ""),
            device: String(webgpuProfile.device || graphicsProfile.unmaskedRenderer || ""),
            description: String(webgpuProfile.description || graphicsProfile.renderer || ""),
            subgroupMinSize: 4,
            subgroupMaxSize: 128,
        };
        const features = new Set(Array.isArray(webgpuProfile.features) ? webgpuProfile.features : []);
        const limits = cloneValue(webgpuProfile.limits || {});
        const device = makeEventTargetLike({
            features,
            limits,
            lost: new Promise(() => {}),
            label: "",
            onuncapturederror: null,
            pushErrorScope: () => undefined,
            popErrorScope: async () => null,
            createBuffer: () => ({}),
            createTexture: () => ({}),
            createSampler: () => ({}),
            createBindGroupLayout: () => ({}),
            createPipelineLayout: () => ({}),
            createBindGroup: () => ({}),
            createShaderModule: () => ({}),
            createComputePipeline: () => ({}),
            createRenderPipeline: () => ({}),
            createCommandEncoder: () => ({}),
            createQuerySet: () => ({}),
            destroy: () => undefined,
        });
        const adapter = {
            features,
            limits,
            isFallbackAdapter: Boolean(webgpuProfile.isFallbackAdapter),
            info: adapterInfo,
            requestAdapterInfo: async () => cloneValue(adapterInfo),
            requestDevice: async () => device,
        };
        const gpu = navigator.gpu || {};
        defineGetter(gpu, "wgslLanguageFeatures", () => new Set(["readonly_and_readwrite_storage_textures", "packed_4x8_integer_dot_product"]));
        defineMethod(gpu, "getPreferredCanvasFormat", () => String(webgpuProfile.preferredCanvasFormat || "bgra8unorm"));
        defineMethod(gpu, "requestAdapter", async () => adapter);
        defineGetter(Navigator.prototype, "gpu", () => gpu);
        defineGetter(navigator, "gpu", () => gpu);
    };
    ensureWebGpuEnvironment();

    const ensureBehaviorEnvironment = () => {
        const visibilityState = String(behaviorProfile.visibilityState || "visible");
        const documentHidden = Boolean(behaviorProfile.documentHidden);
        defineGetter(Document.prototype, "visibilityState", () => visibilityState);
        defineGetter(document, "visibilityState", () => visibilityState);
        defineGetter(Document.prototype, "hidden", () => documentHidden);
        defineGetter(document, "hidden", () => documentHidden);
        if (typeof document.hasFocus === "function") {
            defineMethod(document, "hasFocus", () => Boolean(behaviorProfile.hasFocus !== false));
        }
        const activationProfile = behaviorProfile.userActivation || {};
        const userActivation = {
            hasBeenActive: activationProfile.hasBeenActive !== false,
            isActive: Boolean(activationProfile.isActive),
        };
        defineGetter(Navigator.prototype, "userActivation", () => userActivation);
        defineGetter(navigator, "userActivation", () => userActivation);
        if (!window.IdleDetector) {
            class PersonalIdleDetector extends EventTarget {
                constructor() {
                    super();
                    this.userState = "active";
                    this.screenState = "unlocked";
                    this.onchange = null;
                }
                static requestPermission() {
                    return Promise.resolve("denied");
                }
                start() {
                    return Promise.resolve();
                }
            }
            setValue(window, "IdleDetector", PersonalIdleDetector);
        }
    };
    ensureBehaviorEnvironment();

    const ensureXrEnvironment = () => {
        if (navigator.xr) {
            return;
        }
        const xrSystem = makeEventTargetLike({
            ondevicechange: null,
        });
        defineMethod(xrSystem, "isSessionSupported", async () => false);
        defineMethod(xrSystem, "supportsSession", async () => false);
        defineMethod(xrSystem, "requestSession", async () => {
            throw new DOMException("The specified session configuration is not supported.", "NotSupportedError");
        });
        defineGetter(Navigator.prototype, "xr", () => xrSystem);
        defineGetter(navigator, "xr", () => xrSystem);
    };
    ensureXrEnvironment();

    const sanitizeStackText = (value) => String(value || "")
        .split("\\n")
        .filter((line) => !/personalFingerprint|HeadlessVisible|evaluate_on_new_document|nodriver|cdp|__puppeteer|debugger eval/i.test(line))
        .join("\\n");
    if (window.Error && typeof Error.captureStackTrace === "function") {
        const originalCaptureStackTrace = Error.captureStackTrace;
        defineMethod(Error, "captureStackTrace", function(targetObject, constructorOpt) {
            const result = originalCaptureStackTrace.call(this, targetObject, constructorOpt);
            try {
                if (targetObject && typeof targetObject.stack === "string") {
                    setValue(targetObject, "stack", sanitizeStackText(targetObject.stack));
                }
            } catch (e) {}
            return result;
        });
    }

    if (window.AudioBuffer && AudioBuffer.prototype && typeof AudioBuffer.prototype.getChannelData === "function") {
        const originalGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function(...args) {
            const channelData = originalGetChannelData.apply(this, args);
            try {
                const stamp = "__personalAudioSpoof_" + config.seed;
                if (channelData && channelData.length && !channelData[stamp]) {
                    const stride = Math.max(1, Number(config.audio.stride || 23));
                    const delta = Number(config.audio.floatDelta || 0);
                    for (let i = 0; i < channelData.length; i += stride) {
                        channelData[i] = channelData[i] + delta;
                    }
                    setValue(channelData, stamp, true);
                }
            } catch (e) {}
            return channelData;
        };
    }

    if (window.AnalyserNode && AnalyserNode.prototype) {
        const patchAnalyserMethod = (methodName, deltaValue) => {
            const original = AnalyserNode.prototype[methodName];
            if (typeof original !== "function") {
                return;
            }
            AnalyserNode.prototype[methodName] = function(array) {
                const result = original.call(this, array);
                try {
                    if (array && array.length) {
                        const stride = Math.max(1, Number(config.audio.stride || 23));
                        for (let i = 0; i < array.length; i += stride) {
                            array[i] = array[i] + deltaValue;
                        }
                    }
                } catch (e) {}
                return result;
            };
        };

        patchAnalyserMethod("getFloatFrequencyData", Number(config.audio.floatDelta || 0));
        patchAnalyserMethod("getFloatTimeDomainData", Number(config.audio.floatDelta || 0));
        patchAnalyserMethod("getByteFrequencyData", Number(config.audio.byteDelta || 1));
        patchAnalyserMethod("getByteTimeDomainData", Number(config.audio.byteDelta || 1));
    }
})();
"""
            .replace("__MARKER_JSON__", json.dumps(PERSONAL_FINGERPRINT_SURFACE_SPOOF_MARKER))
            .replace("__CONFIG_JSON__", config_json)
        )

    async def _apply_fingerprint_surface_spoof(self, tab, *, label: str) -> bool:
        if tab is None:
            return False

        runtime_signature = str(self._get_runtime_surface_profile().get("signature") or "").strip()
        if getattr(tab, "_personal_fingerprint_surface_spoof_signature", None) == runtime_signature:
            return True

        try:
            from nodriver import cdp

            await self._run_with_timeout(
                tab.send(
                    cdp.page.add_script_to_evaluate_on_new_document(
                        self._build_tab_fingerprint_spoof_source(tab),
                        run_immediately=True,
                    )
                ),
                timeout_seconds=5.0,
                label=f"page.add_script_to_evaluate_on_new_document:fingerprint:{label}",
            )
            debug_logger.log_info(
                f"[BrowserCaptcha] 已注入 Canvas/WebGL/Audio 与浏览器环境补齐脚本 "
                f"(label={label}, target={getattr(tab, 'target_id', None) or '<none>'})"
            )
            try:
                tab._personal_fingerprint_surface_spoof_signature = runtime_signature
            except Exception:
                pass
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 注入浏览器环境补齐脚本失败 ({label}): {e}"
            )
            return False

    async def _apply_tab_startup_spoofs(
        self,
        tab,
        *,
        label: str,
        browser_context_id: Any = None,
        target_url: Optional[str] = None,
    ) -> None:
        await self._apply_runtime_profile_to_tab(
            tab,
            label=label,
            browser_context_id=browser_context_id,
            target_url=target_url,
        )
        await self._apply_headless_visibility_spoof(tab, label=label)
        await self._apply_fingerprint_surface_spoof(tab, label=label)

    async def _apply_headless_visibility_spoof(self, tab, *, label: str) -> bool:
        if not self.headless or tab is None:
            return False

        if getattr(tab, "_personal_headless_visibility_spoof_applied", None) is True:
            return True

        try:
            from nodriver import cdp

            await self._run_with_timeout(
                tab.send(
                    cdp.page.add_script_to_evaluate_on_new_document(
                        PERSONAL_HEADLESS_VISIBLE_SPOOF_SOURCE,
                        run_immediately=True,
                    )
                ),
                timeout_seconds=5.0,
                label=f"page.add_script_to_evaluate_on_new_document:{label}",
            )
            debug_logger.log_info(
                f"[BrowserCaptcha] 已注入无头可见态伪装脚本 (label={label}, target={getattr(tab, 'target_id', None) or '<none>'})"
            )
            try:
                tab._personal_headless_visibility_spoof_applied = True
            except Exception:
                pass
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 注入无头可见态伪装脚本失败 ({label}): {e}"
            )
            return False

    async def _dispatch_input_command(self, tab, command: Any, *, label: str, timeout_seconds: float = 2.0):
        return await self._run_with_timeout(
            tab.send(command),
            timeout_seconds=timeout_seconds,
            label=label,
        )

    async def _sleep_with_deadline(self, deadline: float, preferred_seconds: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(max(0.0, min(preferred_seconds, remaining)))

    @staticmethod
    def _cubic_bezier_point(
        start: tuple[float, float],
        control_a: tuple[float, float],
        control_b: tuple[float, float],
        end: tuple[float, float],
        t: float,
    ) -> tuple[float, float]:
        omt = max(0.0, 1.0 - t)
        x = (
            (omt ** 3) * start[0]
            + 3.0 * (omt ** 2) * t * control_a[0]
            + 3.0 * omt * (t ** 2) * control_b[0]
            + (t ** 3) * end[0]
        )
        y = (
            (omt ** 3) * start[1]
            + 3.0 * (omt ** 2) * t * control_a[1]
            + 3.0 * omt * (t ** 2) * control_b[1]
            + (t ** 3) * end[1]
        )
        return x, y

    @staticmethod
    def _ease_human_progress(t: float) -> float:
        normalized = max(0.0, min(1.0, t))
        return 0.5 - 0.5 * math.cos(math.pi * normalized)

    def _build_bezier_mouse_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        viewport_width: float,
        viewport_height: float,
        steps: int,
        rng: random.Random,
    ) -> list[tuple[float, float]]:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = max(1.0, math.hypot(dx, dy))
        normal_x = -dy / distance
        normal_y = dx / distance
        curvature = min(distance * 0.25, max(24.0, distance * rng.uniform(0.08, 0.18)))
        control_a = (
            start[0] + dx * rng.uniform(0.20, 0.35) + normal_x * curvature * rng.uniform(-1.0, 1.0),
            start[1] + dy * rng.uniform(0.18, 0.32) + normal_y * curvature * rng.uniform(-1.0, 1.0),
        )
        control_b = (
            start[0] + dx * rng.uniform(0.62, 0.82) + normal_x * curvature * rng.uniform(-1.0, 1.0),
            start[1] + dy * rng.uniform(0.60, 0.84) + normal_y * curvature * rng.uniform(-1.0, 1.0),
        )

        path: list[tuple[float, float]] = []
        clamped_steps = max(6, int(steps or 0))
        for step_index in range(1, clamped_steps + 1):
            raw_t = step_index / clamped_steps
            eased_t = self._ease_human_progress(raw_t)
            x, y = self._cubic_bezier_point(start, control_a, control_b, end, eased_t)
            jitter_scale = max(0.6, min(2.8, distance / 180.0))
            jitter_x = rng.gauss(0.0, 0.8 * jitter_scale)
            jitter_y = rng.gauss(0.0, 0.8 * jitter_scale)
            clamped_x = min(max(2.0, x + jitter_x), max(4.0, viewport_width - 2.0))
            clamped_y = min(max(2.0, y + jitter_y), max(4.0, viewport_height - 2.0))
            path.append((clamped_x, clamped_y))
        return path

    async def _simulate_startup_human_warmup(
        self,
        tab,
        *,
        label: str,
        duration_seconds: float = 1.0,
    ) -> bool:
        if tab is None:
            return False

        deadline = time.monotonic() + max(0.25, float(duration_seconds or 0.0))
        try:
            metrics = await self._tab_evaluate(
                tab,
                """
                (() => ({
                    width: Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0, 1280),
                    height: Math.max(window.innerHeight || 0, document.documentElement?.clientHeight || 0, 720),
                    scrollHeight: Math.max(
                        document.documentElement?.scrollHeight || 0,
                        document.body?.scrollHeight || 0,
                        window.innerHeight || 0
                    ),
                }))()
                """,
                label=f"startup_human_metrics:{label}",
                timeout_seconds=2.0,
                return_by_value=True,
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 启动预热读取 viewport 失败 ({label}): {e}")
            metrics = {}

        if not isinstance(metrics, dict):
            try:
                metrics = dict(metrics or {})
            except Exception:
                metrics = {}

        viewport_width = max(640.0, float((metrics or {}).get("width") or 1280.0))
        viewport_height = max(480.0, float((metrics or {}).get("height") or 720.0))
        scroll_height = max(viewport_height, float((metrics or {}).get("scrollHeight") or viewport_height))

        rng = random.Random(
            f"{time.time_ns()}:{getattr(self, '_browser_instance_id', 0)}:{label}:{viewport_width:.0f}x{viewport_height:.0f}"
        )

        left = max(28.0, viewport_width * rng.uniform(0.10, 0.16))
        upper = max(22.0, viewport_height * rng.uniform(0.10, 0.18))
        reading_targets = [
            (left, upper),
            (viewport_width * rng.uniform(0.34, 0.42), viewport_height * rng.uniform(0.24, 0.34)),
            (viewport_width * rng.uniform(0.54, 0.66), viewport_height * rng.uniform(0.42, 0.54)),
            (viewport_width * rng.uniform(0.72, 0.84), viewport_height * rng.uniform(0.60, 0.76)),
        ]
        current_point = (
            viewport_width * rng.uniform(0.08, 0.16),
            viewport_height * rng.uniform(0.08, 0.18),
        )

        try:
            from nodriver import cdp

            await self._dispatch_input_command(
                tab,
                cdp.input_.dispatch_mouse_event(
                    "mouseMoved",
                    x=current_point[0],
                    y=current_point[1],
                    pointer_type="mouse",
                ),
                label=f"startup_human_init_move:{label}",
                timeout_seconds=1.5,
            )

            for index, target in enumerate(reading_targets):
                if time.monotonic() >= deadline:
                    break

                steps = rng.randint(5, 8)
                path = self._build_bezier_mouse_path(
                    current_point,
                    target,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    steps=steps,
                    rng=rng,
                )
                for point_index, (x, y) in enumerate(path):
                    if time.monotonic() >= deadline:
                        break
                    await self._dispatch_input_command(
                        tab,
                        cdp.input_.dispatch_mouse_event(
                            "mouseMoved",
                            x=x,
                            y=y,
                            pointer_type="mouse",
                        ),
                        label=f"startup_human_move:{label}:{index}:{point_index}",
                        timeout_seconds=1.5,
                    )
                    if point_index == max(1, len(path) // 2):
                        await self._sleep_with_deadline(
                            deadline,
                            min(0.08, 0.018 + rng.expovariate(18.0)),
                        )
                    else:
                        await self._sleep_with_deadline(
                            deadline,
                            min(0.05, 0.006 + rng.expovariate(32.0)),
                        )
                current_point = target

                if time.monotonic() >= deadline:
                    break

                if index in {1, 2}:
                    wheel_delta = min(
                        180.0,
                        max(36.0, viewport_height * rng.uniform(0.08, 0.18)),
                    )
                    await self._dispatch_input_command(
                        tab,
                        cdp.input_.dispatch_mouse_event(
                            "mouseWheel",
                            x=current_point[0],
                            y=current_point[1],
                            delta_x=rng.uniform(-6.0, 6.0),
                            delta_y=wheel_delta,
                            pointer_type="mouse",
                        ),
                        label=f"startup_human_wheel:{label}:{index}",
                        timeout_seconds=1.5,
                    )
                    await self._sleep_with_deadline(
                        deadline,
                        min(0.06, 0.012 + rng.expovariate(24.0)),
                    )

                if index == 1 or (index == 2 and rng.random() < 0.45):
                    await self._dispatch_input_command(
                        tab,
                        cdp.input_.dispatch_key_event(
                            "keyDown",
                            key="Tab",
                            code="Tab",
                            windows_virtual_key_code=9,
                            native_virtual_key_code=9,
                        ),
                        label=f"startup_human_key_down:{label}:{index}",
                        timeout_seconds=1.5,
                    )
                    await self._sleep_with_deadline(deadline, 0.014 + rng.uniform(0.004, 0.022))
                    await self._dispatch_input_command(
                        tab,
                        cdp.input_.dispatch_key_event(
                            "keyUp",
                            key="Tab",
                            code="Tab",
                            windows_virtual_key_code=9,
                            native_virtual_key_code=9,
                        ),
                        label=f"startup_human_key_up:{label}:{index}",
                        timeout_seconds=1.5,
                    )

            final_scroll = int(min(max(0.0, scroll_height - viewport_height), viewport_height * rng.uniform(0.08, 0.20)))
            if final_scroll > 0 and time.monotonic() < deadline:
                try:
                    await self._tab_evaluate(
                        tab,
                        f"window.scrollTo({{top:{final_scroll},behavior:'auto'}})",
                        label=f"startup_human_scroll:{label}",
                        timeout_seconds=1.5,
                    )
                except Exception:
                    pass

            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 0.08))

            debug_logger.log_info(
                "[BrowserCaptcha] 已完成 fresh browser 启动人类化预热 "
                f"(label={label}, duration_ms={int(max(0.0, duration_seconds) * 1000)})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] fresh browser 启动人类化预热失败 ({label}): {e}"
            )
            return False

    async def _browser_has_page_targets(self) -> bool:
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            return False
        try:
            await browser.update_targets()
        except Exception:
            pass
        return any(
            getattr(item, "type_", None) == "page"
            for item in getattr(browser, "targets", [])
        )

    @staticmethod
    def _is_reusable_startup_page_url(url: Optional[str]) -> bool:
        normalized_url = str(url or "").strip().lower()
        if not normalized_url:
            return True
        return normalized_url in {
            "about:blank",
            "chrome://newtab/",
            "chrome://new-tab-page/",
            "chrome://new-tab-page",
            "chrome://new-tab-page-third-party/",
        }

    async def _capture_visible_startup_page(self):
        """记录浏览器启动后自带的首个 page target，避免后续先关空页再开业务页。"""
        browser = self.browser
        self._visible_startup_target_id = None
        if browser is None or getattr(browser, "stopped", False):
            return None

        try:
            await browser.update_targets()
        except Exception:
            pass

        for item in getattr(browser, "targets", []):
            if getattr(item, "type_", None) != "page":
                continue
            target_id = str(getattr(item, "target_id", "") or "").strip()
            if not target_id:
                continue
            self._visible_startup_target_id = target_id
            try:
                item._browser = browser
            except Exception:
                pass
            return item
        return None

    async def _take_headless_host_page(self):
        target_id = str(self._headless_host_target_id or "").strip()
        if not target_id:
            return None

        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            self._headless_host_target_id = None
            return None

        try:
            await browser.update_targets()
        except Exception:
            pass

        for item in getattr(browser, "targets", []):
            if getattr(item, "type_", None) != "page":
                continue
            current_target_id = str(getattr(item, "target_id", "") or "").strip()
            if current_target_id != target_id:
                continue
            if getattr(getattr(item, "target", None), "browser_context_id", None) is not None:
                break
            try:
                item._browser = browser
            except Exception:
                pass
            return item

        self._headless_host_target_id = None
        return None

    async def _take_visible_startup_page(self):
        target_id = str(self._visible_startup_target_id or "").strip()
        if not target_id:
            return None

        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            self._visible_startup_target_id = None
            return None

        try:
            await browser.update_targets()
        except Exception:
            pass

        for item in getattr(browser, "targets", []):
            if getattr(item, "type_", None) != "page":
                continue
            current_target_id = str(getattr(item, "target_id", "") or "").strip()
            if current_target_id != target_id:
                continue
            current_url = str(getattr(item, "url", "") or "").strip()
            if not self._is_reusable_startup_page_url(current_url):
                debug_logger.log_info(
                    f"[BrowserCaptcha] 启动页已被其他逻辑占用，放弃复用 (target={target_id}, url={current_url or '<empty>'})"
                )
                self._visible_startup_target_id = None
                return None
            self._visible_startup_target_id = None
            try:
                item._browser = browser
            except Exception:
                pass
            return item

        self._visible_startup_target_id = None
        return None

    async def _ensure_browser_host_page(
        self,
        *,
        label: str,
        timeout_seconds: Optional[float] = None,
    ):
        """确保当前浏览器存在至少一个可复用的 page target。"""
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            raise RuntimeError("browser runtime unavailable")

        try:
            await browser.update_targets()
        except Exception:
            pass

        if self.headless:
            tracked_host_page = await self._take_headless_host_page()
            if tracked_host_page is not None:
                await self._apply_tab_startup_spoofs(tracked_host_page, label=f"{label}:tracked_host_page")
                return tracked_host_page

        for item in getattr(browser, "targets", []):
            if getattr(item, "type_", None) == "page":
                if self.headless and getattr(getattr(item, "target", None), "browser_context_id", None) is not None:
                    continue
                if self.headless:
                    self._headless_host_target_id = str(getattr(item, "target_id", "") or "").strip() or None
                try:
                    item._browser = browser
                except Exception:
                    pass
                await self._apply_tab_startup_spoofs(item, label=f"{label}:existing_host_page")
                return item

        debug_logger.log_info(f"[BrowserCaptcha] 当前无可用 page target，创建宿主页 ({label})")
        tab = await self._browser_get(
            PERSONAL_COOKIE_PREBIND_URL,
            label=f"{label}:host_page",
            new_tab=False,
            new_window=True,
            timeout_seconds=timeout_seconds,
        )
        try:
            tab._browser = browser
        except Exception:
            pass
        if self.headless:
            self._headless_host_target_id = str(getattr(tab, "target_id", "") or "").strip() or None
        await self._apply_tab_startup_spoofs(tab, label=f"{label}:host_page")
        return tab

    async def _create_default_context_target_tab(
        self,
        url: str,
        *,
        label: str,
        timeout_seconds: Optional[float] = None,
        prefer_new_tab: bool = True,
    ):
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            raise RuntimeError("browser runtime unavailable")

        from nodriver import cdp

        timeout = timeout_seconds or self._navigation_timeout_seconds
        target_url = str(url or "").strip() or PERSONAL_COOKIE_PREBIND_URL
        initial_url = (
            PERSONAL_COOKIE_PREBIND_URL
            if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL
            else target_url
        )
        attempts = [False, True] if prefer_new_tab else [True, False]
        last_error = None

        for new_window in attempts:
            try:
                target_id = await self._run_with_timeout(
                    browser.connection.send(
                        cdp.target.create_target(
                            initial_url,
                            new_window=new_window,
                            enable_begin_frame_control=True,
                        )
                    ),
                    timeout_seconds=timeout,
                    label=f"{label}:create_target:{'window' if new_window else 'tab'}",
                )
            except Exception as create_error:
                last_error = create_error
                if self._is_no_browser_window_error(create_error) and not new_window:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] CDP 新标签创建提示无宿主窗口，改用新窗口重试 ({label}): {create_error}"
                    )
                    continue
                raise

            for _ in range(20):
                try:
                    await browser.update_targets()
                except Exception as update_error:
                    if self._is_browser_runtime_error(update_error):
                        raise

                tab = next(
                    (
                        item
                        for item in getattr(browser, "targets", []) or []
                        if getattr(item, "type_", None) == "page"
                        and getattr(item, "target_id", None) == target_id
                    ),
                    None,
                )
                if tab is not None:
                    try:
                        tab._browser = browser
                    except Exception:
                        pass
                    await self._apply_tab_startup_spoofs(
                        tab,
                        label=f"{label}:default_context_tab",
                        target_url=target_url,
                    )
                    if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL:
                        await self._tab_get(
                            tab,
                            target_url,
                            label=f"{label}:navigate_target",
                            timeout_seconds=timeout,
                        )
                    return tab

                await asyncio.sleep(0.15)

            last_error = RuntimeError(f"target not found after create_target (target_id={target_id})")

        raise last_error or RuntimeError("failed to create browser target")

    async def _open_visible_browser_tab(
        self,
        url: str,
        *,
        label: str,
        timeout_seconds: Optional[float] = None,
    ):
        """有头模式下复用唯一浏览器窗口。

        规则：
        - 当前没有任何 page target 时，先新建一个窗口；
        - 一旦已有窗口，后续统一只开新标签页，避免继续弹第二个浏览器窗口。
        """
        reusable_startup_tab = await self._take_visible_startup_page()
        if reusable_startup_tab is not None:
            debug_logger.log_info(f"[BrowserCaptcha] 复用浏览器启动页打开目标标签 ({label})")
            await self._apply_tab_startup_spoofs(
                reusable_startup_tab,
                label=f"{label}:reuse_startup_page",
                target_url=url,
            )
            await self._tab_get(
                reusable_startup_tab,
                url,
                label=f"{label}:reuse_startup_page",
                timeout_seconds=timeout_seconds,
            )
            return reusable_startup_tab

        has_page_targets = await self._browser_has_page_targets()
        if not has_page_targets and self._requires_virtual_display():
            try:
                await self._ensure_browser_host_page(
                    label=f"{label}:ensure_host_window",
                    timeout_seconds=timeout_seconds,
                )
                has_page_targets = await self._browser_has_page_targets()
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建有头宿主窗口失败，将直接尝试打开目标标签页 ({label}): {e}"
                )
        return await self._create_default_context_target_tab(
            url,
            label=label,
            timeout_seconds=timeout_seconds,
            prefer_new_tab=has_page_targets,
        )

    async def _cleanup_startup_browser_pages(self):
        """关闭浏览器启动时自动弹出的默认页面，避免有头模式出现额外普通窗口。"""
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            return

        try:
            await browser.update_targets()
        except Exception:
            pass

        page_tabs = [
            item
            for item in getattr(browser, "targets", [])
            if getattr(item, "type_", None) == "page"
        ]
        if not page_tabs:
            return

        for tab in page_tabs:
            try:
                target_id = getattr(tab, "target_id", None)
                tab_url = str(getattr(tab, "url", "") or "")
                debug_logger.log_info(
                    f"[BrowserCaptcha] 清理浏览器启动残留页 "
                    f"(target={target_id}, url={tab_url or '<empty>'})"
                )
                await self._close_tab_quietly(tab)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 清理启动残留页失败: {e}")

    async def _tab_reload(self, tab, label: str, timeout_seconds: Optional[float] = None):
        return await self._run_with_timeout(
            tab.reload(),
            timeout_seconds or self._navigation_timeout_seconds,
            label,
        )

    async def _create_isolated_context_tab(
        self,
        url: str,
        *,
        label: str,
        create_timeout_seconds: Optional[float] = None,
    ) -> tuple[Any, Any]:
        """通过 CDP 手动创建独立 browser context 与 target，绕过 nodriver.create_context 的 StopIteration 缺陷。"""
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            raise RuntimeError("browser runtime unavailable")

        timeout_seconds = create_timeout_seconds or self._navigation_timeout_seconds
        target_url = str(url or "").strip() or PERSONAL_COOKIE_PREBIND_URL
        initial_url = (
            PERSONAL_COOKIE_PREBIND_URL
            if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL
            else target_url
        )
        if not self.headless:
            # 有头模式下不再为内部打码页创建独立可见 browser context 窗口。
            # 直接复用默认 context 的单一浏览器窗口，通过新标签页承载 resident/legacy 页面。
            tab = await self._open_visible_browser_tab(
                target_url,
                label=f"{label}:headed_tab",
                timeout_seconds=timeout_seconds,
            )
            try:
                tab._browser = browser
            except Exception:
                pass
            return tab, None

        from nodriver import cdp

        if not await self._browser_has_page_targets():
            try:
                await self._ensure_browser_host_page(
                    label=f"{label}:ensure_host_page",
                    timeout_seconds=timeout_seconds,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建独立 context 前补宿主页失败 ({label}): {e}"
                )

        browser_context_id = await self._run_with_timeout(
            browser.connection.send(
                cdp.target.create_browser_context(
                    dispose_on_detach=True,
                )
            ),
            timeout_seconds=timeout_seconds,
            label=f"{label}:create_browser_context",
        )
        target_id = None

        try:
            async def _send_create_target():
                return await self._run_with_timeout(
                    browser.connection.send(
                        cdp.target.create_target(
                            initial_url,
                            browser_context_id=browser_context_id,
                            new_window=True,
                        )
                    ),
                    timeout_seconds=timeout_seconds,
                    label=f"{label}:create_target",
                )

            try:
                target_id = await _send_create_target()
            except Exception as create_target_error:
                if not self._is_no_browser_window_error(create_target_error):
                    raise

                debug_logger.log_warning(
                    f"[BrowserCaptcha] create_target 命中无宿主窗口错误，补宿主页后重试 ({label}): {create_target_error}"
                )
                await self._ensure_browser_host_page(
                    label=f"{label}:recover_host_page",
                    timeout_seconds=timeout_seconds,
                )
                target_id = await _send_create_target()

            for attempt in range(20):
                try:
                    await browser.update_targets()
                except Exception:
                    pass

                tab = next(
                    (
                        item
                        for item in getattr(browser, "targets", [])
                        if getattr(item, "type_", None) == "page"
                        and getattr(item, "target_id", None) == target_id
                    ),
                    None,
                )
                if tab is not None:
                    try:
                        tab._browser = browser
                    except Exception:
                        pass
                    await self._apply_tab_startup_spoofs(
                        tab,
                        label=f"{label}:isolated_context_tab",
                        browser_context_id=browser_context_id,
                        target_url=target_url,
                    )
                    await self._apply_configured_browser_startup_cookie(
                        label=f"{label}:startup_cookie",
                        browser_context_id=browser_context_id,
                        tab=tab,
                    )
                    if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL:
                        await self._tab_get(
                            tab,
                            target_url,
                            label=f"{label}:navigate_target",
                            timeout_seconds=timeout_seconds,
                        )
                    return tab, browser_context_id

                await asyncio.sleep(0.25)

            raise RuntimeError(
                f"target not found after create_target (target_id={target_id}, context_id={browser_context_id})"
            )
        except Exception:
            if browser_context_id is not None:
                await self._dispose_browser_context_quietly(browser_context_id)
            raise

    async def _get_browser_cookies(
        self,
        label: str,
        timeout_seconds: Optional[float] = None,
        browser_context_id: Any = None,
    ):
        if browser_context_id is not None and self.browser:
            try:
                from nodriver import cdp

                return await self._run_with_timeout(
                    self.browser.connection.send(
                        cdp.storage.get_cookies(browser_context_id=browser_context_id)
                    ),
                    timeout_seconds or self._command_timeout_seconds,
                    label,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 按 context 读取 cookies 失败，回退全局 cookie jar ({label}): {e}"
                )

        return await self._run_with_timeout(
            self.browser.cookies.get_all(),
            timeout_seconds or self._command_timeout_seconds,
            label,
        )

    async def _browser_send_command(
        self,
        command: Any,
        label: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ):
        return await self._run_with_timeout(
            self.browser.connection.send(command),
            timeout_seconds or self._command_timeout_seconds,
            label or "browser.command",
        )

    async def _idle_tab_reaper_loop(self):
        """空闲标签页回收循环"""
        while True:
            try:
                await asyncio.sleep(30)  # 每30秒检查一次
                current_time = time.time()
                tabs_to_close = []

                async with self._resident_lock:
                    for slot_id, resident_info in list(self._resident_tabs.items()):
                        if resident_info.solve_lock.locked():
                            continue
                        idle_seconds = current_time - resident_info.last_used_at
                        if idle_seconds >= self._idle_tab_ttl_seconds:
                            tabs_to_close.append(slot_id)
                            debug_logger.log_info(
                                f"[BrowserCaptcha] slot={slot_id} 空闲 {idle_seconds:.0f}s，准备回收"
                            )

                for slot_id in tabs_to_close:
                    await self._close_resident_tab(slot_id)

            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 空闲标签页回收异常: {e}")

    async def _evict_lru_tab_if_needed(self) -> bool:
        """如果达到共享池上限，使用 LRU 策略淘汰最久未使用的空闲标签页。"""
        async with self._resident_lock:
            if len(self._resident_tabs) < self._max_resident_tabs:
                return True

            lru_slot_id = None
            lru_project_hint = None
            lru_last_used = float('inf')

            for slot_id, resident_info in self._resident_tabs.items():
                if resident_info.solve_lock.locked():
                    continue
                if resident_info.last_used_at < lru_last_used:
                    lru_last_used = resident_info.last_used_at
                    lru_slot_id = slot_id
                    lru_project_hint = resident_info.project_id

        if lru_slot_id:
            debug_logger.log_info(
                f"[BrowserCaptcha] 标签页数量达到上限({self._max_resident_tabs})，"
                f"淘汰最久未使用的 slot={lru_slot_id}, project_hint={lru_project_hint}"
            )
            await self._close_resident_tab(lru_slot_id)
            return True

        debug_logger.log_warning(
            f"[BrowserCaptcha] 标签页数量达到上限({self._max_resident_tabs})，"
            "但当前没有可安全淘汰的空闲标签页"
        )
        return False

    async def _get_reserved_tab_ids(self) -> set[int]:
        """收集当前被 resident/custom 池占用的标签页，legacy 模式不得复用。"""
        reserved_tab_ids: set[int] = set()

        async with self._resident_lock:
            for resident_info in self._resident_tabs.values():
                if resident_info and resident_info.tab:
                    reserved_tab_ids.add(id(resident_info.tab))

        async with self._custom_lock:
            for item in self._custom_tabs.values():
                tab = item.get("tab") if isinstance(item, dict) else None
                if tab:
                    reserved_tab_ids.add(id(tab))

        return reserved_tab_ids

    def _next_resident_slot_id(self) -> str:
        self._resident_slot_seq += 1
        return f"{self._slot_id_prefix}slot-{self._resident_slot_seq}"

    @staticmethod
    def _normalize_token_key(token_id: Optional[int]) -> str:
        try:
            normalized = int(token_id or 0)
        except Exception:
            normalized = 0
        return str(normalized) if normalized > 0 else ""

    @staticmethod
    def _normalize_cookie_signature(cookie_text: Optional[str]) -> Optional[str]:
        signature = build_cookie_signature(cookie_text)
        return signature or None

    @staticmethod
    def _extract_cookie_name_domain(cookie: Any) -> tuple[str, str]:
        """兼容 nodriver cookie 对象与 dict 结构，提取 name/domain 用于日志。"""
        if isinstance(cookie, dict):
            return (
                str(cookie.get("name") or "").strip(),
                str(cookie.get("domain") or "").strip(),
            )
        return (
            str(getattr(cookie, "name", "") or "").strip(),
            str(getattr(cookie, "domain", "") or "").strip(),
        )

    @staticmethod
    def _extract_cookie_scope_host(cookie: Dict[str, Any]) -> str:
        url = str(cookie.get("url") or "").strip()
        if url:
            try:
                parsed = urlparse(url)
                return str(parsed.hostname or "").strip().lower()
            except Exception:
                return ""
        return str(cookie.get("domain") or "").strip().lstrip(".").lower()

    @staticmethod
    def _is_google_family_cookie_host(host: str) -> bool:
        normalized = str(host or "").strip().lower()
        if not normalized:
            return True
        return (
            normalized == "google.com"
            or normalized == "www.google.com"
            or normalized.endswith(".google.com")
            or normalized == "recaptcha.net"
            or normalized == "www.recaptcha.net"
            or normalized.endswith(".recaptcha.net")
        )

    @classmethod
    def _build_personal_cookie_targets(cls, raw_cookie: Optional[str]) -> list[Dict[str, Any]]:
        """为 personal 内置浏览器构建 cookie 注入列表。

        说明：
        - 原始 Cookie 头没有 domain 元数据时，直接扩展到 labs/google/recaptcha 三个目标。
        - 即使 token.cookie 已经带有显式的 google.com 域，也额外镜像一份到
          `www.recaptcha.net`，保证 enterprise reload 首轮请求也能命中 cookie。
        - 对 google/recaptcha 镜像副本强制使用 `SameSite=None`，避免 labs.google
          场景下第三方 anchor/reload 请求继续丢 cookie。
        """
        browser_cookies = build_browser_cookie_targets(
            raw_cookie,
            fallback_urls=list(PERSONAL_COOKIE_TARGET_URLS),
        )
        if not browser_cookies:
            return []

        expanded: list[Dict[str, Any]] = []
        seen: set[str] = set()

        def append_cookie(cookie: Dict[str, Any]) -> None:
            stable_key = json.dumps(cookie, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            if stable_key in seen:
                return
            seen.add(stable_key)
            expanded.append(cookie)

        for cookie in browser_cookies:
            scope_host = cls._extract_cookie_scope_host(cookie)
            explicit_domain = str(cookie.get("domain") or "").strip()
            google_family_scope = cls._is_google_family_cookie_host(scope_host)

            if explicit_domain or not google_family_scope:
                append_cookie(cookie)

            if not google_family_scope:
                continue

            mirrored_cookie = dict(cookie)
            mirrored_cookie.pop("domain", None)
            mirrored_cookie["path"] = str(mirrored_cookie.get("path") or "/").strip() or "/"
            mirrored_cookie["sameSite"] = "None"
            mirrored_cookie["secure"] = True
            for target_url in PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS:
                append_cookie({
                    **mirrored_cookie,
                    "url": target_url,
                })

        return expanded

    def _get_configured_browser_startup_cookie_text(self) -> Optional[str]:
        if not bool(getattr(config, "browser_startup_cookie_enabled", False)):
            return None
        cookie_text = normalize_cookie_storage_text(
            getattr(config, "browser_startup_cookie", "")
        )
        return cookie_text or None

    @classmethod
    def _build_configured_browser_cookie_targets(cls, raw_cookie: Optional[str]) -> list[Dict[str, Any]]:
        """构建系统级浏览器启动 cookie，确保 Google / reCAPTCHA 首跳都能命中。"""
        browser_cookies = build_browser_cookie_targets(
            raw_cookie,
            fallback_urls=list(PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS),
        )
        if not browser_cookies:
            return []

        expanded: list[Dict[str, Any]] = []
        seen: set[str] = set()

        def append_cookie(cookie: Dict[str, Any]) -> None:
            stable_key = json.dumps(cookie, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            if stable_key in seen:
                return
            seen.add(stable_key)
            expanded.append(cookie)

        for cookie in browser_cookies:
            scope_host = cls._extract_cookie_scope_host(cookie)
            explicit_domain = str(cookie.get("domain") or "").strip()
            google_family_scope = cls._is_google_family_cookie_host(scope_host) or scope_host == "labs.google"

            if explicit_domain or not google_family_scope:
                append_cookie(cookie)

            if not google_family_scope:
                continue

            mirrored_cookie = dict(cookie)
            mirrored_cookie.pop("domain", None)
            mirrored_cookie["path"] = str(mirrored_cookie.get("path") or "/").strip() or "/"
            mirrored_cookie["sameSite"] = "None"
            mirrored_cookie["secure"] = True
            for target_url in PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS:
                append_cookie({
                    **mirrored_cookie,
                    "url": target_url,
                })

        return expanded

    def _forget_token_affinity_for_slot_locked(
        self,
        slot_id: Optional[str],
        preserve_token_key: Optional[str] = None,
    ):
        if not slot_id:
            return
        stale_tokens = [
            token_key
            for token_key, mapped_slot_id in self._token_resident_affinity.items()
            if mapped_slot_id == slot_id and token_key != preserve_token_key
        ]
        for token_key in stale_tokens:
            self._token_resident_affinity.pop(token_key, None)

    def _forget_project_affinity_for_slot_locked(
        self,
        slot_id: Optional[str],
        preserve_project_id: Optional[str] = None,
    ):
        if not slot_id:
            return
        stale_projects = [
            project_id
            for project_id, mapped_slot_id in self._project_resident_affinity.items()
            if mapped_slot_id == slot_id and project_id != preserve_project_id
        ]
        for project_id in stale_projects:
            self._project_resident_affinity.pop(project_id, None)

    def _resident_slot_has_pending_assignment_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return False
        current = resident_info or self._resident_tabs.get(normalized_slot_id)
        if current is None:
            return False
        return int(getattr(current, "pending_assignment_count", 0) or 0) > 0

    def _is_resident_slot_busy_for_allocation_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return False
        current = resident_info or self._resident_tabs.get(normalized_slot_id)
        if current is None:
            return False
        return current.solve_lock.locked() or self._resident_slot_has_pending_assignment_locked(
            normalized_slot_id,
            current,
        )

    def _reserve_resident_slot_for_solve_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return False
        current = resident_info or self._resident_tabs.get(normalized_slot_id)
        if current is None or not current.tab:
            return False
        if self._is_resident_slot_busy_for_allocation_locked(normalized_slot_id, current):
            return False
        current.pending_assignment_count = int(
            getattr(current, "pending_assignment_count", 0) or 0
        ) + 1
        return True

    def _release_resident_slot_reservation_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        current = resident_info or self._resident_tabs.get(normalized_slot_id)
        if current is None:
            return
        pending_count = int(getattr(current, "pending_assignment_count", 0) or 0)
        current.pending_assignment_count = max(0, pending_count - 1)

    async def _release_resident_slot_reservation(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        async with self._resident_lock:
            self._release_resident_slot_reservation_locked(
                normalized_slot_id,
                resident_info=resident_info,
            )

    async def _consume_resident_slot_reservation(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> None:
        await self._release_resident_slot_reservation(
            slot_id,
            resident_info=resident_info,
        )

    def _resolve_token_affinity_slot_locked(
        self,
        token_id: Optional[int],
        *,
        available_only: bool = False,
    ) -> Optional[str]:
        token_key = self._normalize_token_key(token_id)
        if not token_key:
            return None
        slot_id = self._token_resident_affinity.get(token_key)
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if (
                resident_info
                and resident_info.tab
                and slot_id not in self._resident_unavailable_slots
                and resident_info.token_id == int(token_key)
            ):
                if available_only and self._is_resident_slot_busy_for_allocation_locked(
                    slot_id,
                    resident_info,
                ):
                    return None
                return slot_id
            if slot_id not in self._resident_tabs or (
                resident_info is not None and resident_info.token_id != int(token_key)
            ):
                self._token_resident_affinity.pop(token_key, None)
        return None

    def _resolve_affinity_slot_locked(
        self,
        project_id: Optional[str],
        *,
        available_only: bool = False,
    ) -> Optional[str]:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return None
        slot_id = self._project_resident_affinity.get(normalized_project_id)
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if (
                resident_info
                and resident_info.tab
                and slot_id not in self._resident_unavailable_slots
                and resident_info.project_id == normalized_project_id
            ):
                if available_only and self._is_resident_slot_busy_for_allocation_locked(
                    slot_id,
                    resident_info,
                ):
                    return None
                return slot_id
            if slot_id not in self._resident_tabs or (
                resident_info is not None and resident_info.project_id != normalized_project_id
            ):
                self._project_resident_affinity.pop(normalized_project_id, None)
        return None

    def _remember_project_affinity(self, project_id: Optional[str], slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id or not slot_id or resident_info is None:
            return
        self._forget_project_affinity_for_slot_locked(slot_id, preserve_project_id=normalized_project_id)
        self._project_resident_affinity[normalized_project_id] = slot_id
        resident_info.project_id = normalized_project_id

    def _remember_token_affinity(self, token_id: Optional[int], slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
        token_key = self._normalize_token_key(token_id)
        if not token_key or not slot_id or resident_info is None:
            return
        self._forget_token_affinity_for_slot_locked(slot_id, preserve_token_key=token_key)
        self._token_resident_affinity[token_key] = slot_id
        resident_info.token_id = int(token_key)

    def _mark_resident_slot_unavailable_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        self._resident_unavailable_slots.add(normalized_slot_id)
        current = self._resident_tabs.get(normalized_slot_id)
        if current is not None:
            current.recaptcha_ready = False
        elif resident_info is not None:
            resident_info.recaptcha_ready = False

    def _clear_resident_slot_unavailable_locked(self, slot_id: Optional[str]) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        self._resident_unavailable_slots.discard(normalized_slot_id)

    async def _mark_resident_slot_unavailable(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
        *,
        reason: str,
    ) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        async with self._resident_lock:
            self._mark_resident_slot_unavailable_locked(normalized_slot_id, resident_info=resident_info)
        debug_logger.log_warning(
            f"[BrowserCaptcha] slot={normalized_slot_id} 已标记为不可复用，等待恢复或重建 (reason={reason})"
        )

    async def _wait_for_active_resident_rebuild(
        self,
        slot_id: Optional[str] = None,
        *,
        timeout_seconds: float = 20.0,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        async with self._resident_lock:
            target_task = None
            if normalized_slot_id:
                candidate = self._resident_rebuild_tasks.get(normalized_slot_id)
                if candidate and not candidate.done():
                    target_task = candidate
            if target_task is None:
                for candidate in self._resident_rebuild_tasks.values():
                    if candidate and not candidate.done():
                        target_task = candidate
                        break

        if target_task is None:
            return False

        try:
            await self._run_with_timeout(
                asyncio.shield(target_task),
                timeout_seconds=timeout_seconds,
                label=f"wait_resident_rebuild:{normalized_slot_id or 'any'}",
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 等待共享标签页重建完成失败 (slot={normalized_slot_id or 'any'}): {e}"
            )
            return False

    @staticmethod
    def _extract_tab_browser_context_id(tab) -> Any:
        target = getattr(tab, "target", None)
        return getattr(target, "browser_context_id", None) if target else None

    async def _dispose_browser_context_quietly(self, browser_context_id: Any, browser_instance=None):
        target_browser = browser_instance or self.browser
        if browser_context_id is None or not target_browser:
            return
        try:
            from nodriver import cdp

            await self._run_with_timeout(
                target_browser.connection.send(
                    cdp.target.dispose_browser_context(browser_context_id)
                ),
                timeout_seconds=5.0,
                label="target.dispose_browser_context",
            )
        except Exception:
            pass

    async def _load_token_cookie(self, token_id: Optional[int]) -> Optional[str]:
        token_key = self._normalize_token_key(token_id)
        if not token_key or not self.db:
            return None
        try:
            token = await self.db.get_token(int(token_key))
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取 token cookie 失败 (token_id={token_key}): {e}")
            return None
        cookie_text = str(getattr(token, "cookie", "") or "").strip() if token else ""
        return cookie_text or None

    def _build_cdp_cookie_params(self, browser_cookies: Iterable[Dict[str, Any]]) -> list[Any]:
        from nodriver import cdp

        cookie_params: list[Any] = []
        for cookie in browser_cookies or []:
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue

            cookie_kwargs: Dict[str, Any] = {
                "name": name,
                "value": str(cookie.get("value") or ""),
            }

            url = str(cookie.get("url") or "").strip()
            domain = str(cookie.get("domain") or "").strip()
            path = str(cookie.get("path") or "/").strip() or "/"

            if url:
                cookie_kwargs["url"] = url
            elif domain:
                cookie_kwargs["domain"] = domain
                cookie_kwargs["path"] = path
            else:
                cookie_kwargs["url"] = "https://labs.google/"
                cookie_kwargs["path"] = path

            if "secure" in cookie:
                cookie_kwargs["secure"] = bool(cookie.get("secure"))
            if "httpOnly" in cookie:
                cookie_kwargs["http_only"] = bool(cookie.get("httpOnly"))

            same_site = self._normalize_cdp_same_site(cookie.get("sameSite"))
            if same_site is not None:
                cookie_kwargs["same_site"] = same_site

            expires = cookie.get("expires")
            if expires not in (None, ""):
                try:
                    cookie_kwargs["expires"] = cdp.network.TimeSinceEpoch.from_json(float(expires))
                except Exception:
                    pass

            cookie_params.append(cdp.network.CookieParam(**cookie_kwargs))

        return cookie_params

    async def _set_browser_cookie_targets(
        self,
        browser_cookies: Iterable[Dict[str, Any]],
        *,
        label: str,
        browser_context_id: Any = None,
        timeout_seconds: float = 8.0,
    ) -> int:
        if not self.browser:
            raise RuntimeError("browser runtime unavailable")

        cookie_params = self._build_cdp_cookie_params(browser_cookies)
        if not cookie_params:
            return 0

        from nodriver import cdp

        if browser_context_id is None:
            cookie_command = cdp.storage.set_cookies(cookie_params)
        else:
            cookie_command = cdp.storage.set_cookies(
                cookie_params,
                browser_context_id=browser_context_id,
            )

        await self._run_with_timeout(
            self.browser.connection.send(cookie_command),
            timeout_seconds=timeout_seconds,
            label=label,
        )
        return len(cookie_params)

    async def _apply_configured_browser_startup_cookie(
        self,
        *,
        label: str,
        browser_context_id: Any = None,
        tab=None,
    ) -> bool:
        cookie_text = self._get_configured_browser_startup_cookie_text()
        if not cookie_text:
            return False

        browser_cookies = self._build_configured_browser_cookie_targets(cookie_text)
        if not browser_cookies:
            raise RuntimeError("browser startup cookie enabled but no valid cookie targets were produced")

        cookie_count = await self._set_browser_cookie_targets(
            browser_cookies,
            label=f"storage.set_cookies:{label}:startup_cookie",
            browser_context_id=browser_context_id,
            timeout_seconds=8.0,
        )
        if cookie_count <= 0:
            raise RuntimeError("browser startup cookie enabled but no valid cookie params were produced")

        target_id = getattr(tab, "target_id", None)
        debug_logger.log_info(
            "[BrowserCaptcha] 已注入系统浏览器启动 Cookie "
            f"(label={label}, context={browser_context_id is not None}, target={target_id or '<none>'}, "
            f"cookies={cookie_count})"
        )
        return True

    @staticmethod
    def _normalize_cdp_same_site(value: Any):
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        try:
            from nodriver import cdp

            mapping = {
                "strict": cdp.network.CookieSameSite.STRICT,
                "lax": cdp.network.CookieSameSite.LAX,
                "none": cdp.network.CookieSameSite.NONE,
            }
            return mapping.get(raw)
        except Exception:
            return None

    @staticmethod
    def _normalize_cookie_same_site_text(value: Any) -> Optional[str]:
        raw = str(getattr(value, "name", value) or "").strip()
        if not raw:
            return None
        raw = raw.split(".")[-1].strip().lower()
        if raw == "strict":
            return "Strict"
        if raw == "lax":
            return "Lax"
        if raw == "none":
            return "None"
        return None

    @classmethod
    def _serialize_browser_cookie_for_storage(cls, cookie: Any) -> Optional[Dict[str, Any]]:
        if isinstance(cookie, dict):
            source = dict(cookie)
        else:
            source = {}
            for field in (
                "name",
                "value",
                "domain",
                "url",
                "path",
                "secure",
                "httpOnly",
                "http_only",
                "sameSite",
                "same_site",
                "expires",
            ):
                if hasattr(cookie, field):
                    source[field] = getattr(cookie, field)

        name = str(source.get("name") or "").strip()
        if not name:
            return None

        serialized: Dict[str, Any] = {
            "name": name,
            "value": str(source.get("value") or ""),
        }

        domain = str(source.get("domain") or "").strip()
        url = str(source.get("url") or "").strip()
        path = str(source.get("path") or "/").strip() or "/"

        if url:
            serialized["url"] = url
        elif domain:
            serialized["domain"] = domain
            serialized["path"] = path
        else:
            serialized["url"] = "https://labs.google/"
            serialized["path"] = path

        same_site = cls._normalize_cookie_same_site_text(
            source.get("sameSite", source.get("same_site"))
        )
        if same_site:
            serialized["sameSite"] = same_site

        expires = source.get("expires")
        if expires not in (None, ""):
            if hasattr(expires, "to_json"):
                try:
                    expires = expires.to_json()
                except Exception:
                    pass
            elif hasattr(expires, "value"):
                expires = getattr(expires, "value", expires)
            try:
                serialized["expires"] = float(expires)
            except Exception:
                pass

        secure = source.get("secure")
        if secure is not None:
            serialized["secure"] = bool(secure)

        http_only = source.get("httpOnly", source.get("http_only"))
        if http_only is not None:
            serialized["httpOnly"] = bool(http_only)

        if name.startswith("__Secure-") or name.startswith("__Host-"):
            serialized["secure"] = True

        if name.startswith("__Host-"):
            serialized.pop("domain", None)
            serialized["path"] = "/"
            if "url" not in serialized:
                serialized["url"] = "https://labs.google/"

        return serialized

    async def _persist_context_cookies_to_token(
        self,
        resident_info: Optional[ResidentTabInfo],
        token_id: Optional[int],
        *,
        label: str,
    ) -> bool:
        token_key = self._normalize_token_key(token_id)
        if resident_info is None or not resident_info.tab or not token_key or not self.db:
            return False

        browser_context_id = resident_info.browser_context_id or self._extract_tab_browser_context_id(
            resident_info.tab
        )
        resident_info.browser_context_id = browser_context_id
        if browser_context_id is None:
            return False

        try:
            current_cookies = await self._get_browser_cookies(
                label=f"context_cookie_persist_get:{label}",
                browser_context_id=browser_context_id,
            )
            serialized_cookies = [
                normalized_cookie
                for normalized_cookie in (
                    self._serialize_browser_cookie_for_storage(cookie)
                    for cookie in (current_cookies or [])
                )
                if normalized_cookie
            ]
            if not serialized_cookies:
                return False

            previous_cookie_text = await self._load_token_cookie(int(token_key))
            merged_cookie_text = merge_browser_cookie_payloads(previous_cookie_text, serialized_cookies)
            if not merged_cookie_text:
                return False

            previous_storage_text = normalize_cookie_storage_text(previous_cookie_text)
            merged_signature = self._normalize_cookie_signature(merged_cookie_text)

            if previous_storage_text != merged_cookie_text:
                await self.db.update_token(int(token_key), cookie=merged_cookie_text)
                debug_logger.log_info(
                    f"[BrowserCaptcha] 已回填 context cookies 到 token.cookie "
                    f"(slot={resident_info.slot_id}, token_id={token_key}, cookies={len(serialized_cookies)})"
                )
            else:
                debug_logger.log_info(
                    f"[BrowserCaptcha] context cookies 与 token.cookie 一致，跳过写回 "
                    f"(slot={resident_info.slot_id}, token_id={token_key}, cookies={len(serialized_cookies)})"
                )

            if resident_info.token_id == int(token_key):
                resident_info.cookie_signature = merged_signature

            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 回填 context cookies 到 token.cookie 失败 "
                f"(slot={resident_info.slot_id}, token_id={token_key}): {e}"
            )
            return False

    async def _apply_token_cookie_binding(
        self,
        resident_info: Optional[ResidentTabInfo],
        token_id: Optional[int],
        *,
        label: str,
        force: bool = False,
        acquire_lock: bool = True,
    ) -> bool:
        token_key = self._normalize_token_key(token_id)
        if resident_info is None or not resident_info.tab or not token_key:
            return False

        cookie_text = await self._load_token_cookie(int(token_key))
        cookie_signature = self._normalize_cookie_signature(cookie_text)

        if not cookie_signature:
            self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
            resident_info.cookie_signature = None
            return False

        if (
            not force
            and resident_info.token_id == int(token_key)
            and resident_info.cookie_signature == cookie_signature
        ):
            return True

        browser_cookies = self._build_personal_cookie_targets(cookie_text)
        if not browser_cookies:
            return False

        try:
            browser_context_id = resident_info.browser_context_id or self._extract_tab_browser_context_id(resident_info.tab)
            resident_info.browser_context_id = browser_context_id

            async def apply_cookie_update():
                return await self._set_browser_cookie_targets(
                    browser_cookies,
                    label=f"storage.set_cookies:{label}:{token_key}",
                    browser_context_id=browser_context_id,
                    timeout_seconds=8.0,
                )

            cookie_count = 0
            if acquire_lock:
                async with resident_info.solve_lock:
                    cookie_count = await apply_cookie_update()
                    await self._tab_reload(
                        resident_info.tab,
                        label=f"resident_cookie_reload:{label}:{token_key}",
                    )
            else:
                cookie_count = await apply_cookie_update()
                await self._tab_reload(
                    resident_info.tab,
                    label=f"resident_cookie_reload:{label}:{token_key}",
                )

            if cookie_count <= 0:
                return False

            resident_info.token_id = int(token_key)
            resident_info.cookie_signature = cookie_signature
            self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
            debug_logger.log_info(
                f"[BrowserCaptcha] 已向 context 注入 cookie (slot={resident_info.slot_id}, token_id={token_key}, cookies={cookie_count})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 注入 token cookie 失败 (slot={resident_info.slot_id}, token_id={token_key}): {e}"
            )
            return False

    @staticmethod
    def _is_labs_bootstrap_url(url: str) -> bool:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            return False

        try:
            parsed = urlparse(normalized_url)
        except Exception:
            return False

        host = str(parsed.netloc or "").strip().lower()
        path = str(parsed.path or "").strip()
        return host == "labs.google" and path.rstrip("/") == "/fx/api/auth/providers"

    async def _open_labs_bootstrap_page(self, tab, *, label: str) -> bool:
        """在 cookie 绑定之后再首跳 labs.google，避免首轮 anchor/reload 丢 cookie。"""
        async def _describe_surface(stage: str) -> tuple[str, str]:
            current_url = ""
            ready_state = ""

            try:
                current_url = str(
                    await self._tab_evaluate(
                        tab,
                        "location.href || ''",
                        label=f"labs_bootstrap_surface_url:{label}:{stage}",
                        timeout_seconds=2.0,
                    )
                    or ""
                ).strip()
            except Exception:
                current_url = ""

            try:
                ready_state = str(
                    await self._tab_evaluate(
                        tab,
                        "document.readyState",
                        label=f"labs_bootstrap_surface_ready:{label}:{stage}",
                        timeout_seconds=2.0,
                    )
                    or ""
                ).strip().lower()
            except Exception:
                ready_state = ""

            return current_url, ready_state

        async def _confirm_labs_surface(reason: str, *, stage: str) -> bool:
            current_url, ready_state = await _describe_surface(stage)
            if self._is_labs_bootstrap_url(current_url) and ready_state in {"interactive", "complete"}:
                debug_logger.log_warning(
                    "[BrowserCaptcha] labs 引导页命令超时，但页面已落到目标地址 "
                    f"(label={label}, reason={reason}, url={current_url}, "
                    f"ready_state={ready_state or '<empty>'})"
                )
                return True

            debug_logger.log_warning(
                "[BrowserCaptcha] labs 引导页失败，页面未落到目标地址 "
                f"(label={label}, reason={reason}, url={current_url or '<empty>'}, "
                f"ready_state={ready_state or '<empty>'})"
            )
            return False

        try:
            await self._tab_get(
                tab,
                PERSONAL_LABS_BOOTSTRAP_URL,
                label=f"labs_bootstrap_get:{label}",
                timeout_seconds=self._navigation_timeout_seconds,
            )
        except Exception as e:
            if self._is_browser_runtime_error(e):
                self._mark_browser_health(False)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 打开 labs 引导页时浏览器运行态断开 ({label}): {e}"
                )
                raise
            debug_logger.log_warning(f"[BrowserCaptcha] 打开 labs 引导页失败 ({label}): {e}")
            return await _confirm_labs_surface(str(e), stage="navigate_timeout")

        if not await self._wait_for_document_ready(tab, retries=20, interval_seconds=0.5):
            debug_logger.log_warning(f"[BrowserCaptcha] labs 引导页未按时 ready ({label})")
            return await _confirm_labs_surface("document_not_ready", stage="document_not_ready")

        current_url, ready_state = await _describe_surface("document_ready")
        if self._is_labs_bootstrap_url(current_url):
            debug_logger.log_info(
                "[BrowserCaptcha] 已进入 labs 引导页 "
                f"(label={label}, url={current_url}, ready_state={ready_state or '<empty>'})"
            )
            return True

        debug_logger.log_warning(
            "[BrowserCaptcha] labs 引导页 ready 后落点异常 "
            f"(label={label}, url={current_url or '<empty>'}, ready_state={ready_state or '<empty>'})"
        )
        return False

    async def _warmup_google_context_cookies(
        self,
        resident_info: Optional[ResidentTabInfo],
        *,
        label: str,
    ) -> bool:
        """访问一次 Google 首页，让当前 browser context 自行拿到额外站点 cookie。"""
        if resident_info is None or not resident_info.tab:
            return False

        browser_context_id = resident_info.browser_context_id or self._extract_tab_browser_context_id(resident_info.tab)
        resident_info.browser_context_id = browser_context_id
        if browser_context_id is None:
            return False

        warmup_url = "https://www.google.com/"
        return_url = "https://labs.google/fx/api/auth/providers"

        try:
            before_cookies = await self._get_browser_cookies(
                label=f"google_warmup_get_cookies_before:{label}",
                browser_context_id=browser_context_id,
            )
            before_pairs = {
                self._extract_cookie_name_domain(cookie)
                for cookie in (before_cookies or [])
            }

            await self._tab_get(
                resident_info.tab,
                warmup_url,
                label=f"google_warmup_get:{label}",
                timeout_seconds=self._navigation_timeout_seconds,
            )
            if not await self._wait_for_document_ready(
                resident_info.tab,
                retries=20,
                interval_seconds=0.5,
            ):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Google 预热页面未按时 ready (slot={resident_info.slot_id}, label={label})"
                )
            await resident_info.tab.sleep(1.0)

            after_cookies = await self._get_browser_cookies(
                label=f"google_warmup_get_cookies_after:{label}",
                browser_context_id=browser_context_id,
            )
            after_pairs = {
                self._extract_cookie_name_domain(cookie)
                for cookie in (after_cookies or [])
            }
            added_pairs = sorted(pair for pair in after_pairs if pair not in before_pairs)
            added_preview = ", ".join(
                f"{name}@{domain or '<host-only>'}"
                for name, domain in added_pairs[:6]
                if name
            )

            await self._tab_get(
                resident_info.tab,
                return_url,
                label=f"google_warmup_back_to_labs:{label}",
                timeout_seconds=self._navigation_timeout_seconds,
            )
            if not await self._wait_for_document_ready(
                resident_info.tab,
                retries=20,
                interval_seconds=0.5,
            ):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Google 预热返回 labs 页面未按时 ready (slot={resident_info.slot_id}, label={label})"
                )

            await self._persist_context_cookies_to_token(
                resident_info,
                resident_info.token_id,
                label=f"{label}:persist",
            )

            debug_logger.log_info(
                f"[BrowserCaptcha] Google 预热完成 "
                f"(slot={resident_info.slot_id}, label={label}, cookies_before={len(before_pairs)}, "
                f"cookies_after={len(after_pairs)}, added={len(added_pairs)}, preview={added_preview or '<none>'})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Google 预热失败 (slot={resident_info.slot_id}, label={label}): {e}"
            )
            try:
                await self._tab_get(
                    resident_info.tab,
                    return_url,
                    label=f"google_warmup_recover_labs:{label}",
                    timeout_seconds=self._navigation_timeout_seconds,
                )
            except Exception:
                pass
            return False

    async def _ensure_resident_token_binding(
        self,
        resident_info: Optional[ResidentTabInfo],
        token_id: Optional[int],
        *,
        label: str,
    ) -> bool:
        token_key = self._normalize_token_key(token_id)
        if resident_info is None or not resident_info.tab:
            return False
        if not token_key:
            return True

        desired_cookie_text = await self._load_token_cookie(int(token_key))
        desired_cookie_signature = self._normalize_cookie_signature(desired_cookie_text)
        current_token_id = resident_info.token_id
        current_cookie_signature = resident_info.cookie_signature

        if (
            current_token_id == int(token_key)
            and current_cookie_signature == desired_cookie_signature
        ):
            self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
            return True

        if not desired_cookie_signature:
            if current_token_id == int(token_key) and not current_cookie_signature:
                self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
                resident_info.cookie_signature = None
                return True

            try:
                from nodriver import cdp

                browser_context_id = resident_info.browser_context_id or self._extract_tab_browser_context_id(resident_info.tab)
                resident_info.browser_context_id = browser_context_id

                async with resident_info.solve_lock:
                    if browser_context_id is None:
                        clear_cookie_command = cdp.storage.clear_cookies()
                    else:
                        clear_cookie_command = cdp.storage.clear_cookies(browser_context_id=browser_context_id)
                    await self._run_with_timeout(
                        self.browser.connection.send(
                            clear_cookie_command
                        ),
                        timeout_seconds=8.0,
                        label=f"storage.clear_cookies:{label}:{token_key}",
                    )
                    await self._tab_reload(
                        resident_info.tab,
                        label=f"resident_cookie_clear_reload:{label}:{token_key}",
                    )
                    resident_info.recaptcha_ready = False
                    if not await self._wait_for_document_ready(
                        resident_info.tab,
                        retries=30,
                        interval_seconds=0.5,
                    ):
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] token_id={token_key} 清空 context cookies 后页面未能按时 ready (slot={resident_info.slot_id})"
                        )
                        return False

                    resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
                    if not resident_info.recaptcha_ready:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] token_id={token_key} 清空 context cookies 后 reCAPTCHA 未恢复就绪 (slot={resident_info.slot_id})"
                        )
                        return False
            except Exception as e:
                resident_info.recaptcha_ready = False
                debug_logger.log_warning(
                    f"[BrowserCaptcha] token_id={token_key} 清空 context cookies 失败 (slot={resident_info.slot_id}): {e}"
                )
                return False

            self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
            resident_info.cookie_signature = None
            return True

        async with resident_info.solve_lock:
            binding_ok = await self._apply_token_cookie_binding(
                resident_info,
                int(token_key),
                label=label,
                force=True,
                acquire_lock=False,
            )
            if not binding_ok:
                resident_info.recaptcha_ready = False
                return False

            resident_info.recaptcha_ready = False
            if not await self._wait_for_document_ready(
                resident_info.tab,
                retries=30,
                interval_seconds=0.5,
            ):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] token_id={token_key} cookie 注入后页面未能按时 ready (slot={resident_info.slot_id})"
                )
                return False

            resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
            if not resident_info.recaptcha_ready:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] token_id={token_key} cookie 注入后 reCAPTCHA 未恢复就绪 (slot={resident_info.slot_id})"
                )
                return False

        self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
        return True

    def _resolve_resident_slot_for_project_locked(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        *,
        available_only: bool = False,
    ) -> tuple[Optional[str], Optional[ResidentTabInfo]]:
        """优先走 token 级映射，其次 project 级映射；没有映射时退化到共享池全局挑选。"""
        slot_id = self._resolve_token_affinity_slot_locked(
            token_id,
            available_only=available_only,
        )
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if resident_info and resident_info.tab:
                return slot_id, resident_info
        slot_id = self._resolve_affinity_slot_locked(
            project_id,
            available_only=available_only,
        )
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if resident_info and resident_info.tab:
                return slot_id, resident_info
        return self._select_resident_slot_locked(
            project_id,
            token_id=token_id,
            available_only=available_only,
        )

    def _resolve_specific_resident_slot_locked(
        self,
        slot_id: Optional[str],
        *,
        reserve_for_solve: bool = False,
    ) -> tuple[Optional[str], Optional[ResidentTabInfo]]:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return None, None
        resident_info = self._resident_tabs.get(normalized_slot_id)
        if (
            resident_info is None
            or not resident_info.tab
            or normalized_slot_id in self._resident_unavailable_slots
        ):
            return None, None
        if reserve_for_solve and not self._reserve_resident_slot_for_solve_locked(
            normalized_slot_id,
            resident_info,
        ):
            return None, None
        return normalized_slot_id, resident_info

    async def _wait_for_resident_slot_available(
        self,
        slot_id: Optional[str],
        *,
        timeout_seconds: float = 0.8,
        poll_interval_seconds: float = 0.05,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return False

        deadline = time.monotonic() + max(0.05, float(timeout_seconds or 0.0))
        poll_interval = max(0.02, float(poll_interval_seconds or 0.0))
        while time.monotonic() < deadline:
            async with self._resident_lock:
                resident_info = self._resident_tabs.get(normalized_slot_id)
                if (
                    resident_info
                    and resident_info.tab
                    and normalized_slot_id not in self._resident_unavailable_slots
                    and not self._is_resident_slot_busy_for_allocation_locked(
                        normalized_slot_id,
                        resident_info,
                    )
                ):
                    return True
            await asyncio.sleep(poll_interval)
        return False

    def _select_resident_slot_locked(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        *,
        available_only: bool = False,
    ) -> tuple[Optional[str], Optional[ResidentTabInfo]]:
        candidates = [
            (slot_id, resident_info)
            for slot_id, resident_info in self._resident_tabs.items()
            if resident_info and resident_info.tab and slot_id not in self._resident_unavailable_slots
        ]
        if not candidates:
            return None, None

        normalized_token_key = self._normalize_token_key(token_id)
        if normalized_token_key:
            token_candidates = [
                (slot_id, resident_info)
                for slot_id, resident_info in candidates
                if resident_info.token_id == int(normalized_token_key)
            ]
            if available_only:
                token_candidates = [
                    (slot_id, resident_info)
                    for slot_id, resident_info in token_candidates
                    if not self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info)
                ]
            if token_candidates:
                token_candidates.sort(
                    key=lambda item: (
                        item[1].last_used_at,
                        item[1].use_count,
                        item[1].created_at,
                        item[0],
                    )
                )
                pick_index = self._resident_pick_index % len(token_candidates)
                self._resident_pick_index = (self._resident_pick_index + 1) % max(len(candidates), 1)
                return token_candidates[pick_index]

        # 共享打码池不再按 project_id 绑定；这里只根据“是否就绪 / 是否空闲 / 使用历史”
        # 做全局选择，避免 4 token/4 project 时把请求硬绑定到固定 tab。
        ready_idle = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if resident_info.recaptcha_ready
            and not self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info)
        ]
        ready_busy = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if resident_info.recaptcha_ready
            and self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info)
        ]
        cold_idle = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if not resident_info.recaptcha_ready
            and not self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info)
        ]

        if available_only:
            pool = ready_idle or cold_idle
        else:
            pool = ready_idle or ready_busy or cold_idle or candidates
        if not pool:
            return None, None
        pool.sort(key=lambda item: (item[1].last_used_at, item[1].use_count, item[1].created_at, item[0]))

        pick_index = self._resident_pick_index % len(pool)
        self._resident_pick_index = (self._resident_pick_index + 1) % max(len(candidates), 1)
        return pool[pick_index]

    async def _ensure_resident_tab(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        *,
        force_create: bool = False,
        reserve_for_solve: bool = False,
        return_slot_key: bool = False,
    ):
        """确保共享打码标签页池中有可用 tab。

        逻辑：
        - 优先复用空闲 tab
        - 如果所有 tab 都忙且未到上限，继续扩容
        - 到达上限后允许请求排队等待已有 tab
        """
        def wrap(slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
            return wrap_with_state(slot_id, resident_info, already_reserved=False)

        def wrap_with_state(
            slot_id: Optional[str],
            resident_info: Optional[ResidentTabInfo],
            *,
            already_reserved: bool = False,
        ):
            if reserve_for_solve and slot_id and resident_info:
                if not already_reserved and not self._reserve_resident_slot_for_solve_locked(slot_id, resident_info):
                    slot_id = None
                    resident_info = None
            if return_slot_key:
                return slot_id, resident_info
            return resident_info

        preferred_wait_slot_id: Optional[str] = None

        async with self._resident_lock:
            slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                project_id,
                token_id=token_id,
                available_only=reserve_for_solve,
            )
            at_capacity = len(self._resident_tabs) >= self._max_resident_tabs
            if reserve_for_solve and not force_create and at_capacity and (resident_info is None or not slot_id):
                preferred_wait_slot_id = self._resolve_token_affinity_slot_locked(
                    token_id,
                    available_only=False,
                ) or self._resolve_affinity_slot_locked(
                    project_id,
                    available_only=False,
                )
            available_infos = [
                (candidate_slot_id, info)
                for candidate_slot_id, info in self._resident_tabs.items()
                if candidate_slot_id not in self._resident_unavailable_slots
            ]
            if available_infos:
                all_busy = all(
                    self._is_resident_slot_busy_for_allocation_locked(candidate_slot_id, info)
                    for candidate_slot_id, info in available_infos
                )
            else:
                all_busy = True
            token_key = self._normalize_token_key(token_id)
            token_slot_matched = bool(
                token_key and resident_info and resident_info.token_id == int(token_key)
            )

            should_create = (
                force_create
                or not resident_info
                or (token_key and not token_slot_matched)
                or (all_busy and len(self._resident_tabs) < self._max_resident_tabs)
            )
            if not should_create:
                return wrap(slot_id, resident_info)

            if at_capacity:
                if not token_key:
                    return wrap(slot_id, resident_info)

        if preferred_wait_slot_id:
            waited = await self._wait_for_resident_slot_available(
                preferred_wait_slot_id,
                timeout_seconds=0.8,
                poll_interval_seconds=0.05,
            )
            if waited:
                async with self._resident_lock:
                    slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                        project_id,
                        token_id=token_id,
                        available_only=reserve_for_solve,
                    )
                    if slot_id and resident_info:
                        debug_logger.log_info(
                            "[BrowserCaptcha] affinity slot 短等待命中，跳过扩容新 tab "
                            f"(project_id={project_id or '<empty>'}, token_id={token_id}, slot={slot_id})"
                        )
                        return wrap(slot_id, resident_info)

        if self._normalize_token_key(token_id):
            await self._evict_lru_tab_if_needed()

        deferred_wait_slot_id: Optional[str] = None
        created_slot_id: Optional[str] = None
        created_resident_info: Optional[ResidentTabInfo] = None
        async with self._tab_build_lock:
            async with self._resident_lock:
                slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                    project_id,
                    token_id=token_id,
                    available_only=reserve_for_solve,
                )
                available_infos = [
                    (candidate_slot_id, info)
                    for candidate_slot_id, info in self._resident_tabs.items()
                    if candidate_slot_id not in self._resident_unavailable_slots
                ]
                if available_infos:
                    all_busy = all(
                        self._is_resident_slot_busy_for_allocation_locked(candidate_slot_id, info)
                        for candidate_slot_id, info in available_infos
                    )
                else:
                    all_busy = True
                token_key = self._normalize_token_key(token_id)
                token_slot_matched = bool(
                    token_key and resident_info and resident_info.token_id == int(token_key)
                )

                should_create = (
                    force_create
                    or not resident_info
                    or (token_key and not token_slot_matched)
                    or (all_busy and len(self._resident_tabs) < self._max_resident_tabs)
                )
                if not should_create:
                    return wrap(slot_id, resident_info)

                if len(self._resident_tabs) >= self._max_resident_tabs:
                    return wrap(slot_id, resident_info)

                created_slot_id = self._next_resident_slot_id()

            if created_slot_id is not None:
                created_resident_info = await self._create_resident_tab(
                    created_slot_id,
                    project_id=project_id,
                    token_id=token_id,
                )

            if created_slot_id is not None and created_resident_info is None:
                async with self._resident_lock:
                    slot_id, fallback_info = self._resolve_resident_slot_for_project_locked(
                        project_id,
                        token_id=token_id,
                        available_only=reserve_for_solve,
                    )
                return wrap(slot_id, fallback_info)

            if created_slot_id is not None and created_resident_info is not None:
                async with self._resident_lock:
                    self._resident_tabs[created_slot_id] = created_resident_info
                    self._clear_resident_slot_unavailable_locked(created_slot_id)
                    self._remember_token_affinity(token_id, created_slot_id, created_resident_info)
                    self._remember_project_affinity(project_id, created_slot_id, created_resident_info)
                    self._sync_compat_resident_state()
                    return wrap(created_slot_id, created_resident_info)

        if deferred_wait_slot_id:
            waited = await self._wait_for_resident_slot_available(
                deferred_wait_slot_id,
                timeout_seconds=8.0,
                poll_interval_seconds=0.05,
            )
            if waited:
                async with self._resident_lock:
                    slot_id, resident_info = self._resolve_specific_resident_slot_locked(
                        deferred_wait_slot_id,
                        reserve_for_solve=reserve_for_solve,
                    )
                    if slot_id and resident_info:
                        debug_logger.log_info(
                            "[BrowserCaptcha] 热 slot 长等待命中，避免新增 resident tab "
                            f"(project_id={project_id or '<empty>'}, token_id={token_id}, slot={slot_id})"
                        )
                        return wrap_with_state(slot_id, resident_info, already_reserved=True)

            async with self._tab_build_lock:
                async with self._resident_lock:
                    slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                        project_id,
                        token_id=token_id,
                        available_only=reserve_for_solve,
                    )
                    if slot_id and resident_info:
                        return wrap(slot_id, resident_info)
                    new_slot_id = self._next_resident_slot_id()

                resident_info = await self._create_resident_tab(new_slot_id, project_id=project_id, token_id=token_id)
                if resident_info is None:
                    async with self._resident_lock:
                        slot_id, fallback_info = self._resolve_resident_slot_for_project_locked(
                            project_id,
                            token_id=token_id,
                            available_only=reserve_for_solve,
                        )
                    return wrap(slot_id, fallback_info)

                async with self._resident_lock:
                    self._resident_tabs[new_slot_id] = resident_info
                    self._clear_resident_slot_unavailable_locked(new_slot_id)
                    self._remember_token_affinity(token_id, new_slot_id, resident_info)
                    self._remember_project_affinity(project_id, new_slot_id, resident_info)
                    self._sync_compat_resident_state()
                    return wrap(new_slot_id, resident_info)

    async def _rebuild_resident_tab(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        *,
        slot_id: Optional[str] = None,
        reserve_for_solve: bool = False,
        return_slot_key: bool = False,
    ):
        """重建共享池中的一个标签页。优先重建当前项目最近使用的 slot。"""
        def wrap(actual_slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
            if return_slot_key:
                return actual_slot_id, resident_info
            return resident_info

        async def finalize(actual_slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
            resolved_slot_id = actual_slot_id
            resolved_resident = resident_info
            if reserve_for_solve and resolved_slot_id and resolved_resident:
                async with self._resident_lock:
                    current_resident = self._resident_tabs.get(resolved_slot_id)
                    if current_resident and current_resident.tab:
                        resolved_resident = current_resident
                    if not self._reserve_resident_slot_for_solve_locked(
                        resolved_slot_id,
                        resolved_resident,
                    ):
                        resolved_slot_id = None
                        resolved_resident = None
            return wrap(resolved_slot_id, resolved_resident)
        pending_task = None
        async with self._resident_lock:
            actual_slot_id = slot_id
            if actual_slot_id is None:
                actual_slot_id, _ = self._resolve_resident_slot_for_project_locked(project_id, token_id=token_id)
            if actual_slot_id:
                existing_task = self._resident_rebuild_tasks.get(actual_slot_id)
                if existing_task and not existing_task.done():
                    pending_task = existing_task
                else:
                    self._mark_resident_slot_unavailable_locked(actual_slot_id)

        if pending_task is not None:
            debug_logger.log_info(
                f"[BrowserCaptcha] slot={actual_slot_id} 已有重建任务，等待复用其结果"
            )
            result = await asyncio.shield(pending_task)
            return await finalize(*result)

        async def _runner(resolved_slot_id: Optional[str]):
            async with self._tab_build_lock:
                async with self._resident_lock:
                    old_resident = self._resident_tabs.pop(resolved_slot_id, None) if resolved_slot_id else None
                    self._forget_token_affinity_for_slot_locked(resolved_slot_id)
                    self._forget_project_affinity_for_slot_locked(resolved_slot_id)
                    if resolved_slot_id:
                        self._resident_error_streaks.pop(resolved_slot_id, None)
                    self._sync_compat_resident_state()

                if old_resident:
                    try:
                        await self._dispose_browser_context_quietly(old_resident.browser_context_id)
                        async with old_resident.solve_lock:
                            await self._close_tab_quietly(old_resident.tab)
                    except Exception:
                        await self._close_tab_quietly(old_resident.tab)

                next_slot_id = resolved_slot_id or self._next_resident_slot_id()
                resident_info = await self._create_resident_tab(next_slot_id, project_id=project_id, token_id=token_id)
                if resident_info is None:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] slot={next_slot_id}, project_id={project_id}, token_id={token_id} 重建共享标签页失败"
                    )
                    return next_slot_id, None

                async with self._resident_lock:
                    self._resident_tabs[next_slot_id] = resident_info
                    self._clear_resident_slot_unavailable_locked(next_slot_id)
                    self._remember_token_affinity(token_id, next_slot_id, resident_info)
                    self._remember_project_affinity(project_id, next_slot_id, resident_info)
                    self._sync_compat_resident_state()
                    return next_slot_id, resident_info

        if actual_slot_id:
            async with self._resident_lock:
                existing_task = self._resident_rebuild_tasks.get(actual_slot_id)
                if existing_task and not existing_task.done():
                    rebuild_task = existing_task
                    created_task = False
                else:
                    rebuild_task = asyncio.create_task(_runner(actual_slot_id))
                    self._resident_rebuild_tasks[actual_slot_id] = rebuild_task
                    created_task = True
            if created_task:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 开始重建共享标签页 (slot={actual_slot_id}, project={project_id}, token_id={token_id})"
                )
            else:
                debug_logger.log_info(
                    f"[BrowserCaptcha] slot={actual_slot_id} 已在重建中，等待复用现有结果"
                )
            try:
                result = await asyncio.shield(rebuild_task)
                if created_task:
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 共享标签页重建结束 (slot={actual_slot_id}, project={project_id}, token_id={token_id})"
                    )
            finally:
                if created_task:
                    async with self._resident_lock:
                        if self._resident_rebuild_tasks.get(actual_slot_id) is rebuild_task:
                            self._resident_rebuild_tasks.pop(actual_slot_id, None)
            return await finalize(*result)

        result = await _runner(actual_slot_id)
        return await finalize(*result)

    async def _run_resident_recovery_task(
        self,
        slot_id: str,
        task_factory,
        *,
        project_id: str,
        error_reason: str,
    ):
        """同一 slot 的上游异常恢复任务去重，避免并发重复清缓存/重建。"""
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return await task_factory()

        async with self._resident_lock:
            existing_task = self._resident_recovery_tasks.get(normalized_slot_id)
            if existing_task and not existing_task.done():
                recovery_task = existing_task
                created_task = False
            else:
                recovery_task = asyncio.create_task(task_factory())
                self._resident_recovery_tasks[normalized_slot_id] = recovery_task
                created_task = True

        if not created_task:
            debug_logger.log_info(
                f"[BrowserCaptcha] project_id={project_id}, slot={normalized_slot_id} "
                f"检测到并发恢复任务，等待复用已有恢复结果: {error_reason}"
            )

        try:
            return await asyncio.shield(recovery_task)
        finally:
            if created_task:
                async with self._resident_lock:
                    if self._resident_recovery_tasks.get(normalized_slot_id) is recovery_task:
                        self._resident_recovery_tasks.pop(normalized_slot_id, None)

    def _sync_compat_resident_state(self):
        """同步旧版单 resident 兼容属性。"""
        first_resident = next(iter(self._resident_tabs.values()), None)
        if first_resident:
            self.resident_project_id = first_resident.project_id
            self.resident_tab = first_resident.tab
            self._running = True
            self._recaptcha_ready = bool(first_resident.recaptcha_ready)
        else:
            self.resident_project_id = None
            self.resident_tab = None
            self._running = False
            self._recaptcha_ready = False

    async def _close_tab_quietly(self, tab):
        if not tab:
            return
        try:
            await self._run_with_timeout(
                tab.close(),
                timeout_seconds=5.0,
                label="tab.close",
            )
        except Exception:
            pass
        await self._disconnect_connection_quietly(tab, reason="tab_close")

    async def _disconnect_connection_quietly(self, connection, *, reason: str):
        """尽量关闭任意 nodriver 连接对象，回收 listener task 与未完成 transaction。"""
        disconnect_method = getattr(connection, "disconnect", None) if connection else None
        if disconnect_method is None:
            return

        listener_task = getattr(connection, "_listener_task", None)
        try:
            result = disconnect_method()
            if inspect.isawaitable(result):
                await self._run_with_timeout(
                    result,
                    timeout_seconds=5.0,
                    label=f"browser.disconnect:{reason}",
                )
        except Exception as e:
            if self._is_browser_runtime_error(e):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 浏览器连接关闭时检测到已断连状态 ({reason}): {e}"
                )
            else:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 浏览器连接关闭异常 ({reason}): {type(e).__name__}: {e}"
                )
        finally:
            mapper = getattr(connection, "mapper", None)
            if isinstance(mapper, dict):
                for transaction in list(mapper.values()):
                    try:
                        if not transaction.done():
                            transaction.cancel()
                    except Exception:
                        pass
                mapper.clear()

            handlers = getattr(connection, "handlers", None)
            clear_handlers = getattr(handlers, "clear", None)
            if callable(clear_handlers):
                try:
                    clear_handlers()
                except Exception:
                    pass

            if isinstance(listener_task, asyncio.Task) and listener_task is not asyncio.current_task():
                if not listener_task.done():
                    try:
                        await self._run_with_timeout(
                            asyncio.shield(listener_task),
                            timeout_seconds=1.0,
                            label=f"browser.listener_drain:{reason}",
                        )
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        listener_task.cancel()
                        try:
                            await listener_task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass

                if listener_task.done():
                    try:
                        listener_task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        if not (
                            isinstance(e, asyncio.InvalidStateError)
                            or self._is_browser_runtime_error(e)
                        ):
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 浏览器监听任务收尾异常 ({reason}): "
                                f"{type(e).__name__}: {e}"
                            )

            try:
                connection._listener_task = None
            except Exception:
                pass

            await asyncio.sleep(0)

    async def _disconnect_browser_connection_quietly(self, browser_instance, reason: str):
        """尽量先关闭 DevTools websocket，减少 nodriver 后台任务在浏览器退场时炸栈。"""
        if not browser_instance:
            return

        await self._disconnect_connection_quietly(
            getattr(browser_instance, "connection", None),
            reason=reason,
        )

    @staticmethod
    def _get_process_pid(process: Any) -> Optional[int]:
        try:
            pid = int(getattr(process, "pid", 0) or 0)
        except Exception:
            pid = 0
        return pid if pid > 0 else None

    def _get_browser_process_pid(self, browser_instance) -> Optional[int]:
        if not browser_instance:
            return None
        return self._get_process_pid(getattr(browser_instance, "_process", None))

    def _is_pid_running(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        try:
            if sys.platform.startswith("win"):
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                return str(int(pid)) in (result.stdout or "")
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False

    def _terminate_pid_tree(self, pid: Optional[int], *, reason: str) -> bool:
        if not pid:
            return False
        try:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 浏览器进程仍未退出，强制回收进程树 PID={pid} ({reason})"
            )
            if sys.platform.startswith("win"):
                result = subprocess.run(
                    ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                return result.returncode == 0 or not self._is_pid_running(pid)

            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                return True
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if not self._is_pid_running(pid):
                    return True
                time.sleep(0.1)
            os.kill(int(pid), signal.SIGKILL)
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 强制回收浏览器进程失败 PID={pid} ({reason}): {e}"
            )
            return False

    def _collect_runtime_profile_process_targets(self) -> list[str]:
        profile_dirs: list[str] = []
        seen: set[str] = set()
        candidates = [
            self.user_data_dir,
            self._runtime_ephemeral_user_data_dir,
            *list(getattr(self, "_managed_runtime_profile_dirs", set()) or set()),
        ]

        for raw_path in candidates:
            normalized_path = str(raw_path or "").strip()
            if not normalized_path or not self._is_runtime_managed_profile_dir(normalized_path):
                continue
            try:
                resolved = os.path.normcase(os.path.normpath(str(Path(normalized_path).resolve())))
            except Exception:
                resolved = os.path.normcase(os.path.normpath(normalized_path))
            if resolved in seen:
                continue
            seen.add(resolved)
            profile_dirs.append(resolved)

        return profile_dirs

    def _find_browser_pids_for_profile_dirs(self, profile_dirs: Iterable[str]) -> list[int]:
        normalized_profile_dirs = [
            os.path.normcase(os.path.normpath(str(item or "").strip()))
            for item in profile_dirs
            if str(item or "").strip()
        ]
        if not normalized_profile_dirs:
            return []

        found_pids: set[int] = set()
        browser_names = {"chrome.exe", "chromium.exe", "msedge.exe", "chrome", "chromium", "msedge"}

        if sys.platform.startswith("win"):
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            "Get-CimInstance Win32_Process | "
                            "Where-Object { $_.Name -match '^(chrome|chromium|msedge)\\.exe$' } | "
                            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                output = (result.stdout or "").strip()
                if not output:
                    return []
                payload = json.loads(output)
                if isinstance(payload, dict):
                    payload = [payload]
                for item in payload if isinstance(payload, list) else []:
                    try:
                        pid = int(item.get("ProcessId") or 0)
                    except Exception:
                        continue
                    command_line = os.path.normcase(
                        os.path.normpath(str(item.get("CommandLine") or ""))
                    )
                    if pid > 0 and any(profile_dir in command_line for profile_dir in normalized_profile_dirs):
                        found_pids.add(pid)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 扫描浏览器残留进程失败: {e}")
            return sorted(found_pids)

        proc_dir = Path("/proc")
        if not proc_dir.exists():
            return []
        for child in proc_dir.iterdir():
            if not child.name.isdigit():
                continue
            try:
                pid = int(child.name)
                comm = (child / "comm").read_text(encoding="utf-8", errors="ignore").strip()
                if comm not in browser_names:
                    continue
                command_line = (child / "cmdline").read_bytes().decode(
                    "utf-8",
                    errors="ignore",
                ).replace("\x00", " ")
                normalized_command_line = os.path.normcase(os.path.normpath(command_line))
                if any(profile_dir in normalized_command_line for profile_dir in normalized_profile_dirs):
                    found_pids.add(pid)
            except Exception:
                continue
        return sorted(found_pids)

    def _terminate_browser_processes_for_profile_dirs(self, profile_dirs: Iterable[str], *, reason: str) -> int:
        pids = self._find_browser_pids_for_profile_dirs(profile_dirs)
        killed_count = 0
        for pid in pids:
            if self._terminate_pid_tree(pid, reason=reason):
                killed_count += 1
        if killed_count > 0:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 已按 profile 路径兜底回收浏览器进程 ({reason}): {killed_count}/{len(pids)}"
            )
        return killed_count

    async def _stop_browser_process(self, browser_instance, reason: str = "browser_stop"):
        """兼容 nodriver 同步 stop API，安全停止浏览器进程。"""
        if not browser_instance:
            return

        process = getattr(browser_instance, "_process", None)
        browser_pid = self._get_browser_process_pid(browser_instance) or self._browser_process_pid
        profile_dirs = self._collect_runtime_profile_process_targets()
        connection = getattr(browser_instance, "connection", None)
        await self._disconnect_browser_connection_quietly(browser_instance, reason=reason)

        if connection is not None:
            async def _noop_disconnect(_self):
                return None

            try:
                connection.disconnect = types.MethodType(_noop_disconnect, connection)
            except Exception:
                pass
            try:
                connection._listener_task = None
            except Exception:
                pass

        stop_method = getattr(browser_instance, "stop", None)
        if stop_method is not None:
            try:
                result = stop_method()
                if inspect.isawaitable(result):
                    await self._run_with_timeout(
                        result,
                        timeout_seconds=10.0,
                        label="browser.stop",
                    )
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] browser.stop 异常 ({reason}): {e}")

        if process is not None:
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                close_method = getattr(stream, "close", None)
                if callable(close_method):
                    try:
                        close_method()
                    except Exception:
                        pass
            try:
                await self._run_with_timeout(
                    process.wait(),
                    timeout_seconds=5.0,
                    label=f"browser.process.wait:{reason}",
                )
            except Exception:
                pass
        if browser_pid and self._is_pid_running(browser_pid):
            self._terminate_pid_tree(browser_pid, reason=reason)
        self._terminate_browser_processes_for_profile_dirs(profile_dirs, reason=reason)
        self._browser_process_pid = None
        await asyncio.sleep(0.3)

    async def _cancel_background_runtime_tasks(self, *, reason: str) -> None:
        current_task = asyncio.current_task()
        tasks_to_cancel: list[asyncio.Task] = []

        async with self._resident_lock:
            candidate_tasks = []
            resident_warmup_task = getattr(self, "_resident_warmup_task", None)
            if resident_warmup_task is not None:
                candidate_tasks.append(resident_warmup_task)
            fresh_restart_task = getattr(self, "_fresh_profile_restart_task", None)
            if fresh_restart_task is not None:
                candidate_tasks.append(fresh_restart_task)
            candidate_tasks.extend(self._resident_rebuild_tasks.values())
            candidate_tasks.extend(self._resident_recovery_tasks.values())

            for task in candidate_tasks:
                if task is None or task.done() or task is current_task:
                    continue
                tasks_to_cancel.append(task)

            self._resident_rebuild_tasks.clear()
            self._resident_recovery_tasks.clear()
            self._resident_warmup_task = None
            if fresh_restart_task is not current_task:
                self._fresh_profile_restart_task = None

        if not tasks_to_cancel:
            return

        for task in tasks_to_cancel:
            task.cancel()

        for task in tasks_to_cancel:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 取消后台任务异常 ({reason}): {type(e).__name__}: {e}"
                )

    async def _shutdown_browser_runtime_locked(self, reason: str):
        """在持有 _browser_lock 的前提下，彻底清理当前浏览器运行态。"""
        browser_instance = self.browser
        self.browser = None
        self._initialized = False
        self._visible_startup_target_id = None
        self._headless_host_target_id = None
        self._refresh_runtime_fingerprint_spoof_seed()
        self._last_fingerprint = None
        self._last_fingerprint_at = 0.0
        self._mark_browser_health(False)
        self._reset_browser_rotation_budget()
        self._reset_local_recaptcha_asset_caches(purge_disk=False)
        self._cleanup_proxy_extension()
        self._proxy_url = None
        await self._cancel_background_runtime_tasks(reason=reason)

        async with self._resident_lock:
            resident_items = list(self._resident_tabs.values())
            self._resident_tabs.clear()
            self._project_resident_affinity.clear()
            self._token_resident_affinity.clear()
            self._resident_error_streaks.clear()
            self._resident_unavailable_slots.clear()
            self._resident_rebuild_tasks.clear()
            self._resident_recovery_tasks.clear()
            self._sync_compat_resident_state()

        custom_items = list(self._custom_tabs.values())
        self._custom_tabs.clear()

        closed_tabs = set()

        async def close_once(tab):
            if not tab:
                return
            tab_key = id(tab)
            if tab_key in closed_tabs:
                return
            closed_tabs.add(tab_key)
            await self._close_tab_quietly(tab)

        for resident_info in resident_items:
            await self._dispose_browser_context_quietly(
                getattr(resident_info, "browser_context_id", None),
                browser_instance=browser_instance,
            )
            await close_once(resident_info.tab)

        for item in custom_items:
            tab = item.get("tab") if isinstance(item, dict) else None
            await close_once(tab)

        if browser_instance:
            for target in list(getattr(browser_instance, "targets", []) or []):
                await self._disconnect_connection_quietly(
                    target,
                    reason=f"{reason}:target_disconnect",
                )
            try:
                await self._stop_browser_process(browser_instance, reason=reason)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 停止浏览器实例失败 ({reason}): {e}"
                )
        await self._cleanup_runtime_profile_dirs_after_shutdown(reason=reason)

    async def _resolve_personal_proxy(self):
        """Read proxy config for personal captcha browser.
        Priority: captcha browser_proxy > request proxy."""
        if not self.db:
            return None, None, None, None, None
        try:
            captcha_cfg = await self.db.get_captcha_config()
            browser_proxy_pool = str(getattr(captcha_cfg, "browser_proxy_pool", "") or "").strip()
            if browser_proxy_pool:
                pooled_proxy = await self.db.pick_browser_proxy_from_pool()
                if pooled_proxy:
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 使用验证码代理池: {pooled_proxy}")
                    return _parse_proxy_url(pooled_proxy)
            if getattr(captcha_cfg, "browser_proxy_enabled", False) and getattr(captcha_cfg, "browser_proxy_url", None):
                url = str(getattr(captcha_cfg, "browser_proxy_url", "") or "").strip()
                if url:
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 使用验证码代理: {url}")
                    return _parse_proxy_url(url)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取验证码代理配置失败: {e}")
        try:
            proxy_cfg = await self.db.get_proxy_config()
            proxy_pool_text = str(getattr(proxy_cfg, "proxy_pool", "") or "")
            proxy_pool_candidates = [item.strip() for item in re.split(r"[\r\n,]+", proxy_pool_text) if item.strip()]
            if proxy_cfg and proxy_cfg.enabled and proxy_pool_candidates:
                pooled_proxy = proxy_pool_candidates[0]
                debug_logger.log_info(f"[BrowserCaptcha] Personal 回退使用请求代理池: {pooled_proxy}")
                return _parse_proxy_url(pooled_proxy)
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                url = proxy_cfg.proxy_url.strip()
                if url:
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 回退使用请求代理: {url}")
                    return _parse_proxy_url(url)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取请求代理配置失败: {e}")

        for candidate_url in _read_windows_internet_settings_proxy_candidates():
            protocol, host, port, username, password = _parse_proxy_url(candidate_url)
            if not protocol or not host or not port:
                continue
            if str(host).strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
                continue
            if not await self._is_tcp_endpoint_reachable(str(host), int(port), timeout_seconds=0.5):
                continue
            debug_logger.log_info(
                f"[BrowserCaptcha] Personal 自动接管本机可用代理: {candidate_url}"
            )
            return protocol, host, port, username, password

        return None, None, None, None, None

    async def _is_tcp_endpoint_reachable(
        self,
        host: str,
        port: int,
        *,
        timeout_seconds: float = 0.5,
    ) -> bool:
        try:
            connection = asyncio.open_connection(host, int(port))
            reader, writer = await asyncio.wait_for(connection, timeout_seconds)
        except Exception:
            return False

        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        finally:
            del reader

        return True

    def _cleanup_proxy_extension(self):
        """Remove temporary proxy auth extension directory."""
        if self._proxy_ext_dir and os.path.isdir(self._proxy_ext_dir):
            try:
                shutil.rmtree(self._proxy_ext_dir, ignore_errors=True)
            except Exception:
                pass
            self._proxy_ext_dir = None

    def _get_recaptcha_bootstrap_candidate_urls(
        self,
        script_path: str,
        website_key: Optional[str] = None,
    ) -> list[str]:
        """Build the candidate bootstrap URLs for the requested reCAPTCHA script."""
        normalized_path = script_path.lstrip("/")
        # 默认优先 recaptcha.net；google.com 仅作为回退。
        hosts = ["https://www.recaptcha.net", "https://www.google.com"]
        suffix = f"?render={website_key}" if website_key else ""
        return [f"{host}/{normalized_path}{suffix}" for host in hosts]

    async def _resolve_personal_proxy_download_url(self) -> Optional[str]:
        """Resolve the full proxy URL for downloading bootstrap scripts."""
        protocol, host, port, username, password = await self._resolve_personal_proxy()
        return _compose_proxy_url(protocol, host, port, username, password)

    async def _download_recaptcha_asset_bytes(self, remote_url: str) -> tuple[bytes, str]:
        """Download a reCAPTCHA-related static asset through the same proxy path."""
        proxy_url = await self._resolve_personal_proxy_download_url()
        async with AsyncSession() as session:
            response = await session.get(
                remote_url,
                timeout=RECAPTCHA_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS,
                proxy=proxy_url,
                headers={"Accept": "*/*"},
                impersonate="chrome120",
                verify=False,
            )

        if response.status_code != 200 or not response.content:
            raise RuntimeError(f"HTTP {response.status_code}")

        mime_type = _guess_recaptcha_asset_mime_type(
            remote_url,
            str(response.headers.get("content-type") or ""),
        )
        return bytes(response.content), mime_type

    async def _load_recaptcha_asset_bytes(self, remote_url: str) -> tuple[bytes, str]:
        """Load a static asset from local cache first, then refresh from upstream."""
        cache_path = _get_recaptcha_asset_cache_path(self._recaptcha_asset_cache_dir, remote_url)

        async with self._recaptcha_asset_cache_lock:
            if cache_path.exists():
                try:
                    cached_content = cache_path.read_bytes()
                    cache_age = max(0.0, time.time() - cache_path.stat().st_mtime)
                    if cached_content and cache_age <= RECAPTCHA_ASSET_CACHE_TTL_SECONDS:
                        return cached_content, _guess_recaptcha_asset_mime_type(remote_url)
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 读取本地静态资源缓存失败: path={cache_path.name}, error={e}"
                    )

            try:
                content, mime_type = await self._download_recaptcha_asset_bytes(remote_url)
                _write_binary_cache(cache_path, content)
                return content, mime_type
            except Exception as e:
                if cache_path.exists():
                    try:
                        cached_content = cache_path.read_bytes()
                        if cached_content:
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 静态资源下载失败，回退使用本地缓存: url={remote_url}, error={e}"
                            )
                            return cached_content, _guess_recaptcha_asset_mime_type(remote_url)
                    except Exception:
                        pass
                raise

    async def _discover_dynamic_recaptcha_static_urls(self, bootstrap_source: str) -> list[str]:
        """Recursively discover reCAPTCHA static assets from bootstrap/JS/CSS content."""
        discovered: list[str] = []
        seen: set[str] = set()
        pending: list[str] = []

        def _queue(remote_url: str):
            normalized = str(remote_url or "").strip()
            if not normalized or not _is_localizable_recaptcha_asset_url(normalized) or normalized in seen:
                return
            seen.add(normalized)
            pending.append(normalized)
            discovered.append(normalized)

        def _queue_text_urls(source_text: str):
            for remote_url in _extract_remote_urls_from_text(source_text):
                _queue(remote_url)
                for companion_url in _iter_recaptcha_release_companion_urls(remote_url):
                    _queue(companion_url)

        _queue_text_urls(bootstrap_source)

        while pending:
            remote_url = pending.pop(0)
            try:
                content, mime_type = await self._load_recaptcha_asset_bytes(remote_url)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 动态发现静态资源失败: url={remote_url}, error={e}"
                )
                continue

            normalized_mime_type = _guess_recaptcha_asset_mime_type(remote_url, mime_type)
            if normalized_mime_type == "text/css":
                css_source = content.decode("utf-8", errors="ignore")
                for child_url in _extract_remote_urls_from_css(css_source, remote_url):
                    _queue(child_url)
            elif normalized_mime_type in {"text/javascript", "application/javascript"}:
                js_source = content.decode("utf-8", errors="ignore")
                _queue_text_urls(js_source)

        return discovered

    async def _build_recaptcha_asset_data_url(self, remote_url: str) -> str:
        """Build a data URL backed by the local asset cache."""
        cached_data_url = self._recaptcha_asset_data_url_cache.get(remote_url)
        if cached_data_url:
            return cached_data_url

        content, mime_type = await self._load_recaptcha_asset_bytes(remote_url)
        normalized_mime_type = _guess_recaptcha_asset_mime_type(remote_url, mime_type)

        if normalized_mime_type == "text/css":
            css_source = content.decode("utf-8", errors="ignore")
            replacements: dict[str, str] = {}
            for child_url in _extract_remote_urls_from_css(css_source, remote_url):
                if not _is_localizable_recaptcha_asset_url(child_url):
                    continue
                replacements[child_url] = await self._build_recaptcha_asset_data_url(child_url)
            localized_css = _rewrite_css_urls_with_local_assets(css_source, remote_url, replacements)
            data_url = _build_data_url(localized_css.encode("utf-8"), "text/css;charset=utf-8")
        else:
            if normalized_mime_type in {"text/javascript", "application/javascript"}:
                js_source = content.decode("utf-8", errors="ignore")
                replacements: dict[str, str] = {}
                for child_url in _extract_remote_urls_from_text(js_source):
                    if not _is_localizable_recaptcha_asset_url(child_url):
                        continue
                    replacements[child_url] = await self._build_recaptcha_asset_data_url(child_url)
                localized_js = _rewrite_text_urls_with_local_assets(js_source, replacements)
                data_url = _build_data_url(localized_js.encode("utf-8"), "text/javascript;charset=utf-8")
            else:
                data_url = _build_data_url(content, normalized_mime_type)

        self._recaptcha_asset_data_url_cache[remote_url] = data_url
        return data_url

    async def _build_recaptcha_local_asset_bundle(
        self,
        bootstrap_source: str,
        bootstrap_candidate_urls: Iterable[str],
    ) -> Dict[str, Any]:
        """Build a compact rewrite bundle for local reCAPTCHA static assets."""
        candidate_url_list = [url for url in bootstrap_candidate_urls if url]
        signature_input = "||".join(candidate_url_list) + "||" + hashlib.md5(
            bootstrap_source.encode("utf-8")
        ).hexdigest()
        signature = hashlib.md5(signature_input.encode("utf-8")).hexdigest()
        if (
            self._recaptcha_asset_bundle_signature == signature
            and self._recaptcha_asset_bundle is not None
        ):
            return self._recaptcha_asset_bundle

        full_map: dict[str, str] = {}
        path_map: dict[str, str] = {}
        data_map: dict[str, str] = {}

        def _register_aliases(remote_url: str, data_key: str):
            for alias_url in _iter_recaptcha_asset_url_aliases(remote_url):
                full_map[alias_url] = data_key
                parsed = urlparse(alias_url)
                if parsed.path:
                    path_key = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                    path_map[path_key] = data_key
                    if not parsed.query:
                        path_map[parsed.path] = data_key

        bootstrap_key = "bootstrap"
        data_map[bootstrap_key] = _build_data_url(
            bootstrap_source.encode("utf-8"),
            "text/javascript;charset=utf-8",
        )
        for candidate_url in candidate_url_list:
            _register_aliases(candidate_url, bootstrap_key)

        static_urls = await self._discover_dynamic_recaptcha_static_urls(bootstrap_source)

        asset_index = 0
        for remote_url in static_urls:
            data_key = f"a{asset_index}"
            data_map[data_key] = await self._build_recaptcha_asset_data_url(remote_url)
            _register_aliases(remote_url, data_key)
            asset_index += 1

        bundle = {
            "full": full_map,
            "path": path_map,
            "data": data_map,
        }
        self._recaptcha_asset_bundle_signature = signature
        self._recaptcha_asset_bundle = bundle
        self._recaptcha_asset_hook_source = None
        return bundle

    def _build_recaptcha_local_asset_hook_source(self, bundle: Dict[str, Any]) -> str:
        """Build the browser-side rewrite hook for reCAPTCHA static assets."""
        if self._recaptcha_asset_hook_source is not None:
            return self._recaptcha_asset_hook_source

        hook_source = f"""
            (() => {{
                const bundle = {json.dumps(bundle, separators=(",", ":"))};
                const fullMap = bundle.full || Object.create(null);
                const pathMap = bundle.path || Object.create(null);
                const dataMap = bundle.data || Object.create(null);

                const resolveLocalAsset = (value) => {{
                    if (!value || typeof value !== 'string') return value;
                    if (value.startsWith('data:') || value.startsWith('blob:')) return value;

                    let key = fullMap[value];
                    if (key && dataMap[key]) return dataMap[key];

                    try {{
                        const parsed = new URL(value, window.location.href);
                        key = fullMap[parsed.href]
                            || pathMap[parsed.pathname + parsed.search]
                            || pathMap[parsed.pathname];
                        if (key && dataMap[key]) return dataMap[key];
                    }} catch (error) {{}}

                    return value;
                }};

                const clearIntegrity = (node) => {{
                    if (!node || !node.tagName) return;
                    const tagName = String(node.tagName).toUpperCase();
                    if (tagName !== 'SCRIPT' && tagName !== 'LINK') return;
                    try {{ node.integrity = ''; }} catch (error) {{}}
                    try {{ node.removeAttribute('integrity'); }} catch (error) {{}}
                    try {{ node.crossOrigin = ''; }} catch (error) {{}}
                    try {{ node.removeAttribute('crossorigin'); }} catch (error) {{}}
                }};

                const applyNode = (node) => {{
                    if (!node || typeof node !== 'object') return node;
                    try {{
                        if (typeof node.src === 'string' && node.src) {{
                            const nextSrc = resolveLocalAsset(node.src);
                            if (nextSrc !== node.src) {{
                                clearIntegrity(node);
                                node.src = nextSrc;
                            }}
                        }}
                    }} catch (error) {{}}
                    try {{
                        if (typeof node.href === 'string' && node.href) {{
                            const nextHref = resolveLocalAsset(node.href);
                            if (nextHref !== node.href) {{
                                clearIntegrity(node);
                                node.href = nextHref;
                            }}
                        }}
                    }} catch (error) {{}}
                    return node;
                }};

                window.__flow2apiRecaptchaLocalAssets = bundle;
                if (window.__flow2apiRecaptchaLocalAssetsPatched) {{
                    return Object.keys(fullMap).length;
                }}
                window.__flow2apiRecaptchaLocalAssetsPatched = true;

                const originalSetAttribute = Element.prototype.setAttribute;
                Element.prototype.setAttribute = function(name, value) {{
                    let nextValue = value;
                    if ((name === 'src' || name === 'href') && typeof value === 'string') {{
                        nextValue = resolveLocalAsset(value);
                        if (nextValue !== value) {{
                            clearIntegrity(this);
                        }}
                    }}
                    return originalSetAttribute.call(this, name, nextValue);
                }};

                const patchUrlProperty = (ctorName, propertyName) => {{
                    const ctor = window[ctorName];
                    if (!ctor || !ctor.prototype) return;
                    const descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, propertyName);
                    if (!descriptor || typeof descriptor.set !== 'function' || typeof descriptor.get !== 'function') {{
                        return;
                    }}
                    Object.defineProperty(ctor.prototype, propertyName, {{
                        configurable: true,
                        enumerable: descriptor.enumerable,
                        get() {{
                            return descriptor.get.call(this);
                        }},
                        set(value) {{
                            const nextValue = typeof value === 'string' ? resolveLocalAsset(value) : value;
                            if (nextValue !== value) {{
                                clearIntegrity(this);
                            }}
                            return descriptor.set.call(this, nextValue);
                        }},
                    }});
                }};

                patchUrlProperty('HTMLScriptElement', 'src');
                patchUrlProperty('HTMLLinkElement', 'href');
                patchUrlProperty('HTMLImageElement', 'src');
                patchUrlProperty('HTMLIFrameElement', 'src');

                const originalAppendChild = Node.prototype.appendChild;
                Node.prototype.appendChild = function(node) {{
                    return originalAppendChild.call(this, applyNode(node));
                }};

                const originalInsertBefore = Node.prototype.insertBefore;
                Node.prototype.insertBefore = function(node, referenceNode) {{
                    return originalInsertBefore.call(this, applyNode(node), referenceNode);
                }};

                if (typeof window.fetch === 'function') {{
                    const originalFetch = window.fetch.bind(window);
                    window.fetch = function(input, init) {{
                        if (typeof input === 'string') {{
                            input = resolveLocalAsset(input);
                        }} else if (input && typeof input.url === 'string') {{
                            const nextInput = resolveLocalAsset(input.url);
                            if (nextInput !== input.url) {{
                                input = nextInput;
                            }}
                        }}
                        return originalFetch(input, init);
                    }};
                }}

                if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {{
                    const originalOpen = window.XMLHttpRequest.prototype.open;
                    window.XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
                        return originalOpen.call(this, method, resolveLocalAsset(url), ...rest);
                    }};
                }}

                const workerObjectUrlCache = Object.create(null);
                const toWorkerObjectUrl = (value) => {{
                    if (typeof value !== 'string' || !value.startsWith('data:')) {{
                        return value;
                    }}
                    if (workerObjectUrlCache[value]) {{
                        return workerObjectUrlCache[value];
                    }}

                    try {{
                        const commaIndex = value.indexOf(',');
                        if (commaIndex < 0) return value;
                        const meta = value.slice(5, commaIndex);
                        const payload = value.slice(commaIndex + 1);
                        const mimeType = meta || 'text/javascript;charset=utf-8';
                        const binary = meta.includes(';base64')
                            ? atob(payload)
                            : decodeURIComponent(payload);
                        const bytes = new Uint8Array(binary.length);
                        for (let index = 0; index < binary.length; index += 1) {{
                            bytes[index] = binary.charCodeAt(index);
                        }}
                        const objectUrl = URL.createObjectURL(new Blob([bytes], {{ type: mimeType }}));
                        workerObjectUrlCache[value] = objectUrl;
                        return objectUrl;
                    }} catch (error) {{
                        return value;
                    }}
                }};

                if (typeof window.Worker === 'function') {{
                    const NativeWorker = window.Worker;
                    window.Worker = function(scriptURL, options) {{
                        const nextScriptURL = typeof scriptURL === 'string'
                            ? toWorkerObjectUrl(resolveLocalAsset(scriptURL))
                            : scriptURL;
                        return new NativeWorker(nextScriptURL, options);
                    }};
                    window.Worker.prototype = NativeWorker.prototype;
                }}

                if (typeof window.SharedWorker === 'function') {{
                    const NativeSharedWorker = window.SharedWorker;
                    window.SharedWorker = function(scriptURL, options) {{
                        const nextScriptURL = typeof scriptURL === 'string'
                            ? toWorkerObjectUrl(resolveLocalAsset(scriptURL))
                            : scriptURL;
                        return new NativeSharedWorker(nextScriptURL, options);
                    }};
                    window.SharedWorker.prototype = NativeSharedWorker.prototype;
                }}

                return Object.keys(fullMap).length;
            }})()
        """
        self._recaptcha_asset_hook_source = hook_source
        return hook_source

    async def _inject_local_recaptcha_asset_overrides(
        self,
        tab,
        bootstrap_source: str,
        bootstrap_candidate_urls: Iterable[str],
    ) -> bool:
        """Inject URL rewrite hooks so static reCAPTCHA assets are served from local cache."""
        try:
            bundle = await self._build_recaptcha_local_asset_bundle(
                bootstrap_source,
                bootstrap_candidate_urls,
            )
            hook_source = self._build_recaptcha_local_asset_hook_source(bundle)
            await self._tab_evaluate(
                tab,
                hook_source,
                label="inject_recaptcha_local_assets",
                timeout_seconds=12.0,
            )
            debug_logger.log_info("[BrowserCaptcha] 已注入本地 reCAPTCHA 静态资源映射")
            return True
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 注入本地 reCAPTCHA 静态资源映射失败: {e}")
            return False

    async def _download_recaptcha_bootstrap_source(self, remote_url: str) -> str:
        """Download the reCAPTCHA bootstrap source using the same proxy path as the browser."""
        proxy_url = await self._resolve_personal_proxy_download_url()
        async with AsyncSession() as session:
            response = await session.get(
                remote_url,
                timeout=RECAPTCHA_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS,
                proxy=proxy_url,
                headers={"Accept": "*/*"},
                impersonate="chrome120",
                verify=False,
            )

        if response.status_code != 200 or not response.content:
            raise RuntimeError(f"HTTP {response.status_code}")

        source = response.content.decode("utf-8", errors="ignore").strip()
        if not source or "grecaptcha" not in source or "gstatic" not in source:
            raise RuntimeError("bootstrap 内容校验失败")
        return source

    async def _load_recaptcha_bootstrap_source(
        self,
        script_path: str,
        candidate_urls: Optional[Iterable[str]] = None,
    ) -> str:
        """Load the bootstrap source from local cache first, then refresh from upstream."""
        urls = list(candidate_urls or self._get_recaptcha_bootstrap_candidate_urls(script_path))
        stale_cache_candidates: list[tuple[str, Path]] = []

        async with self._recaptcha_script_cache_lock:
            for remote_url in urls:
                cache_path = _get_recaptcha_script_cache_path(self._recaptcha_script_cache_dir, remote_url)
                if not cache_path.exists():
                    continue

                try:
                    cached_source = cache_path.read_text(encoding="utf-8").strip()
                    if not cached_source:
                        continue
                    cache_age = max(0.0, time.time() - cache_path.stat().st_mtime)
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 读取本地 reCAPTCHA 缓存失败: path={cache_path.name}, error={e}"
                    )
                    continue

                if cache_age <= RECAPTCHA_SCRIPT_CACHE_TTL_SECONDS:
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 使用本地缓存的 reCAPTCHA bootstrap: {cache_path.name}"
                    )
                    return cached_source

                stale_cache_candidates.append((remote_url, cache_path))

            last_error = None
            for remote_url in urls:
                try:
                    source = await self._download_recaptcha_bootstrap_source(remote_url)
                    cache_path = _get_recaptcha_script_cache_path(self._recaptcha_script_cache_dir, remote_url)
                    _write_text_cache(cache_path, source)
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 已刷新 reCAPTCHA bootstrap 本地缓存: {cache_path.name}"
                    )
                    return source
                except Exception as e:
                    last_error = e
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 下载 reCAPTCHA bootstrap 失败: url={remote_url}, error={e}"
                    )

            for remote_url, cache_path in stale_cache_candidates:
                try:
                    cached_source = cache_path.read_text(encoding="utf-8").strip()
                    if cached_source:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 远程刷新失败，回退使用过期缓存: url={remote_url}, path={cache_path.name}"
                        )
                        return cached_source
                except Exception:
                    continue

        raise RuntimeError(f"无法加载 reCAPTCHA bootstrap: {last_error or '未知错误'}")

    async def _inject_recaptcha_bootstrap_script(
        self,
        tab,
        script_path: str,
        website_key: str,
        label: str,
        *,
        force_remote: bool = False,
    ) -> str:
        """直接注入远程 reCAPTCHA bootstrap 脚本。"""
        candidate_urls = self._get_recaptcha_bootstrap_candidate_urls(
            script_path,
            website_key=website_key,
        )

        await self._tab_evaluate(tab, f"""
            (() => {{
                const forceRemote = {json.dumps(force_remote)};
                const stateKey = '__flow2apiRecaptchaBootstrapState';
                const scriptTimeoutMs = 8000;
                const now = () => Date.now();
                const ensureState = () => {{
                    if (!window[stateKey] || typeof window[stateKey] !== 'object') {{
                        window[stateKey] = {{
                            status: 'idle',
                            url: '',
                            error: '',
                            startedAt: 0,
                            finishedAt: 0,
                            attempts: 0,
                        }};
                    }}
                    return window[stateKey];
                }};
                const state = ensureState();
                const hasReadyApi =
                    typeof grecaptcha !== 'undefined' &&
                    (
                        typeof grecaptcha.execute === 'function' ||
                        (
                            typeof grecaptcha.enterprise !== 'undefined' &&
                            typeof grecaptcha.enterprise.execute === 'function'
                        )
                    );
                if (hasReadyApi) {{
                    state.status = 'ready';
                    state.finishedAt = now();
                    return;
                }}
                if (forceRemote) {{
                    document
                        .querySelectorAll('script[src*="recaptcha"]')
                        .forEach((node) => node.remove());
                    state.status = 'idle';
                    state.url = '';
                    state.error = '';
                    state.startedAt = 0;
                    state.finishedAt = 0;
                }} else if (
                    document.querySelector('script[src*="recaptcha"]') &&
                    state.status === 'loading'
                ) {{
                    return;
                }} else if (document.querySelector('script[src*="recaptcha"]')) {{
                    document
                        .querySelectorAll('script[src*="recaptcha"]')
                        .forEach((node) => node.remove());
                }}
                const urls = {json.dumps(candidate_urls)};
                const parent = document.head || document.documentElement || document.body;
                if (!parent) {{
                    state.status = 'error';
                    state.error = 'missing script parent';
                    state.finishedAt = now();
                    return;
                }}
                const loadScript = (index) => {{
                    if (index >= urls.length) {{
                        state.status = 'error';
                        if (!state.error) {{
                            state.error = 'all candidate urls exhausted';
                        }}
                        state.finishedAt = now();
                        return;
                    }}
                    state.status = 'loading';
                    state.url = urls[index];
                    state.error = '';
                    state.startedAt = now();
                    state.finishedAt = 0;
                    state.attempts = Number(state.attempts || 0) + 1;
                    const script = document.createElement('script');
                    script.src = urls[index];
                    script.async = true;
                    let settled = false;
                    const finish = (status, error) => {{
                        if (settled) return;
                        settled = true;
                        state.status = status;
                        state.error = error ? String(error) : '';
                        state.finishedAt = now();
                    }};
                    const timer = setTimeout(() => {{
                        script.remove();
                        finish('timeout', `script load timeout: ${{urls[index]}}`);
                        loadScript(index + 1);
                    }}, scriptTimeoutMs);
                    script.onload = () => {{
                        clearTimeout(timer);
                        finish('loaded', '');
                    }};
                    script.onerror = () => {{
                        clearTimeout(timer);
                        script.remove();
                        finish('error', `script load error: ${{urls[index]}}`);
                        loadScript(index + 1);
                    }};
                    parent.appendChild(script);
                }};
                loadScript(0);
            }})()
        """, label=label, timeout_seconds=5.0)
        debug_logger.log_info(f"[BrowserCaptcha] 已注入远程 reCAPTCHA bootstrap ({script_path})")
        return "remote"

    async def initialize(self):
        """初始化 nodriver 浏览器"""
        self._check_available()

        if (
            self._initialized
            and self.browser
            and not self.browser.stopped
            and self._is_browser_health_fresh()
        ):
            self._mark_runtime_active()
            if self._idle_reaper_task is None or self._idle_reaper_task.done():
                self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
            return

        self._raise_if_browser_launch_cooling_down()

        async with self._browser_lock:
            self._raise_if_browser_launch_cooling_down()
            browser_needs_restart = False
            browser_executable_path = None
            display_value = os.environ.get("DISPLAY", "").strip()
            browser_args = []
            sandbox_enabled = _resolve_personal_browser_sandbox_enabled()

            if self._initialized and self.browser:
                try:
                    if self.browser.stopped:
                        debug_logger.log_warning("[BrowserCaptcha] 浏览器已停止，准备重新初始化...")
                        self._mark_browser_health(False)
                        browser_needs_restart = True
                    elif getattr(self.browser, "_flow2api_runtime_disconnected", False):
                        debug_logger.log_warning("[BrowserCaptcha] 浏览器连接已标记断开，准备重新初始化...")
                        self._mark_browser_health(False)
                        browser_needs_restart = True
                    elif self._is_browser_health_fresh():
                        self._mark_runtime_active()
                        if self._idle_reaper_task is None or self._idle_reaper_task.done():
                            self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
                        return
                    elif not await self._probe_browser_runtime():
                        debug_logger.log_warning("[BrowserCaptcha] 浏览器连接已失活，准备重新初始化...")
                        browser_needs_restart = True
                    else:
                        _patch_nodriver_runtime(self.browser)
                        self._mark_runtime_active()
                        if self._idle_reaper_task is None or self._idle_reaper_task.done():
                            self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
                        return
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 浏览器状态检查异常，准备重新初始化: {e}")
                    browser_needs_restart = True
            elif self.browser is not None or self._initialized:
                browser_needs_restart = True

            if browser_needs_restart:
                await self._shutdown_browser_runtime_locked(reason="initialize_recovery")

            launch_gate = self._get_global_browser_launch_gate()
            if launch_gate.locked():
                debug_logger.log_info(
                    "[BrowserCaptcha] 浏览器启动排队中，等待全局启动配额以降低 Windows 启动尖峰内存"
                )

            async with launch_gate:
                try:
                    if self.user_data_dir:
                        debug_logger.log_info(f"[BrowserCaptcha] 正在启动 nodriver 浏览器 (用户数据目录: {self.user_data_dir})...")
                        os.makedirs(self.user_data_dir, exist_ok=True)
                    else:
                        debug_logger.log_info(
                            "[BrowserCaptcha] 正在启动 nodriver 浏览器 "
                            "(使用独立临时目录，隔离真实资料)..."
                        )

                    browser_executable_path, browser_source = _resolve_browser_executable_path()
                    if browser_executable_path and browser_source == "configured":
                        debug_logger.log_info(
                            f"[BrowserCaptcha] 使用显式配置的浏览器作为 nodriver 浏览器: {browser_executable_path}"
                        )
                    if browser_executable_path:
                        debug_logger.log_info(
                            f"[BrowserCaptcha] 使用指定浏览器可执行文件: {browser_executable_path}"
                        )

                    # 解析代理配置
                    self._cleanup_proxy_extension()
                    self._proxy_url = None
                    protocol, host, port, username, password = await self._resolve_personal_proxy()
                    self._proxy_config_signature = await self._build_proxy_config_signature()
                    proxy_server_arg = None
                    if protocol and host and port:
                        if username and password:
                            self._proxy_ext_dir = _create_proxy_auth_extension(protocol, host, port, username, password)
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Personal 代理需要认证，已创建扩展: {self._proxy_ext_dir}"
                            )
                            debug_logger.log_info(
                                "[BrowserCaptcha] Personal 认证代理改由扩展接管，跳过命令行 --proxy-server，避免浏览器原生认证弹窗"
                            )
                        else:
                            proxy_server_arg = f"--proxy-server={protocol}://{host}:{port}"
                        self._proxy_url = f"{protocol}://{host}:{port}"
                        debug_logger.log_info(f"[BrowserCaptcha] Personal 浏览器代理: {self._proxy_url}")

                    browser_args = _build_personal_browser_args(
                        headless=self.headless,
                        proxy_server_arg=proxy_server_arg,
                        proxy_extension_dir=self._proxy_ext_dir,
                    )
                    if self._requires_virtual_display():
                        browser_args = _tune_personal_browser_args_for_docker_headed(browser_args)
                        debug_logger.log_info(
                            "[BrowserCaptcha] Docker headed 指纹优化已启用，已收敛明显的容器化启动参数"
                        )
                    if self._requires_virtual_display() and '--no-startup-window' in browser_args:
                        browser_args = [
                            arg for arg in browser_args
                            if arg != '--no-startup-window'
                        ]
                        debug_logger.log_info(
                            "[BrowserCaptcha] Docker 有头虚拟显示模式已禁用 --no-startup-window，保留宿主窗口"
                        )
                    browser_args = _normalize_personal_browser_args_for_launch(
                        browser_args,
                        sandbox_enabled=sandbox_enabled,
                    )

                    effective_launch_args = list(browser_args)
                    if self._requires_virtual_display():
                        await self._wait_for_display_ready(display_value)

                    effective_uid = "n/a"
                    if hasattr(os, "geteuid"):
                        try:
                            effective_uid = str(os.geteuid())
                        except Exception:
                            effective_uid = "unknown"

                    launch_kwargs = {
                        "headless": self.headless,
                        "user_data_dir": self.user_data_dir,
                        "browser_executable_path": browser_executable_path,
                        "browser_args": browser_args,
                        "sandbox": sandbox_enabled,
                    }
                    launch_config = uc.Config(**launch_kwargs)
                    effective_launch_args = launch_config()
                    debug_logger.log_info(
                        "[BrowserCaptcha] nodriver 启动上下文: "
                        f"docker={IS_DOCKER}, display={display_value or '<empty>'}, "
                        f"uid={effective_uid}, headless={self.headless}, sandbox={sandbox_enabled}, "
                        f"executable={browser_executable_path or '<auto>'}, "
                        f"args={' '.join(effective_launch_args)}"
                    )

                    # 启动 nodriver 浏览器（后台启动，不占用前台）
                    launch_plan: list[tuple[str, Dict[str, Any], Optional[str]]] = [
                        ("nodriver.start", dict(launch_kwargs), None),
                    ]
                    tried_no_sandbox_retry = False
                    tried_fresh_profile_retry = False
                    last_start_error: Optional[Exception] = None
                    self.browser = None

                    while launch_plan:
                        launch_label, current_launch_kwargs, retry_reason = launch_plan.pop(0)
                        current_config = uc.Config(**current_launch_kwargs)
                        effective_launch_args = current_config()
                        if retry_reason:
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 浏览器启动重试 ({retry_reason}): "
                                f"label={launch_label}, profile={current_launch_kwargs.get('user_data_dir') or '<isolated-temp>'}"
                            )
                        try:
                            self.browser = await self._run_with_timeout(
                                uc.start(**current_launch_kwargs),
                                timeout_seconds=30.0,
                                label=launch_label,
                            )
                            self._browser_process_pid = self._get_browser_process_pid(self.browser)
                            break
                        except Exception as start_error:
                            last_start_error = start_error
                            failed_profile_dir = str(current_launch_kwargs.get("user_data_dir") or "").strip()
                            if failed_profile_dir and self._is_runtime_managed_profile_dir(failed_profile_dir):
                                self._managed_runtime_profile_dirs.add(os.path.normpath(failed_profile_dir))
                                self._terminate_browser_processes_for_profile_dirs(
                                    [failed_profile_dir],
                                    reason=f"{launch_label}:failed_start",
                                )

                            if (
                                not tried_no_sandbox_retry
                                and self._should_use_explicit_no_sandbox_retry(start_error)
                            ):
                                tried_no_sandbox_retry = True
                                fallback_browser_args = list(current_launch_kwargs.get("browser_args") or [])
                                if '--no-sandbox' not in fallback_browser_args:
                                    fallback_browser_args.append('--no-sandbox')
                                fallback_kwargs = dict(current_launch_kwargs)
                                fallback_kwargs["browser_args"] = fallback_browser_args
                                fallback_kwargs["sandbox"] = True
                                launch_plan.insert(
                                    0,
                                    (
                                        "nodriver.start.retry_no_sandbox",
                                        fallback_kwargs,
                                        f"explicit_no_sandbox after {type(start_error).__name__}: {start_error}",
                                    ),
                                )

                            if (
                                not tried_fresh_profile_retry
                                and self._is_retryable_browser_launch_error(start_error)
                            ):
                                tried_fresh_profile_retry = True
                                previous_profile_dir = str(current_launch_kwargs.get("user_data_dir") or self.user_data_dir or "").strip()
                                fresh_profile_dir = self._create_fresh_runtime_profile_dir(prefix="launch_retry_profile_")
                                fresh_profile_kwargs = dict(current_launch_kwargs)
                                fresh_profile_kwargs["user_data_dir"] = fresh_profile_dir
                                launch_plan.insert(
                                    0,
                                    (
                                        "nodriver.start.retry_fresh_profile",
                                        fresh_profile_kwargs,
                                        f"fresh_profile after {type(start_error).__name__}: {start_error} "
                                        f"(previous_profile={previous_profile_dir or '<empty>'})",
                                    ),
                                )

                            if launch_plan:
                                await asyncio.sleep(0.8)
                                continue
                            raise

                    if self.browser is None and last_start_error is not None:
                        raise last_start_error

                    _patch_nodriver_runtime(self.browser)
                    live_user_agent, live_product = await self._get_live_browser_runtime_identity()
                    self._refresh_runtime_fingerprint_spoof_seed(
                        user_agent=live_user_agent,
                        product=live_product,
                    )
                    await self._apply_configured_browser_startup_cookie(
                        label="initialize",
                    )
                    if self.headless:
                        try:
                            startup_warmup_tab = await self._ensure_browser_host_page(
                                label="initialize_headless_startup_warmup",
                                timeout_seconds=self._navigation_timeout_seconds,
                            )
                        except Exception as e:
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 创建无头启动预热页失败，跳过启动人类化预热: {e}"
                            )
                        else:
                            await self._simulate_startup_human_warmup(
                                startup_warmup_tab,
                                label="initialize_headless_startup_warmup",
                                duration_seconds=1.0,
                            )
                    if self._proxy_ext_dir:
                        debug_logger.log_info("[BrowserCaptcha] 等待代理认证扩展完成初始化...")
                        await asyncio.sleep(1.5)
                    if not self.headless:
                        if self._requires_virtual_display():
                            await self._ensure_browser_host_page(
                                label="initialize_host_page",
                                timeout_seconds=self._navigation_timeout_seconds,
                            )
                        else:
                            await self._capture_visible_startup_page()
                    self._initialized = True
                    self._mark_browser_health(True)
                    self._mark_runtime_active()
                    self._reset_browser_launch_failure_state()
                    if self._idle_reaper_task is None or self._idle_reaper_task.done():
                        self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
                    profile_label = self.user_data_dir or "<isolated-temp>"
                    debug_logger.log_info(
                        f"[BrowserCaptcha] ✅ nodriver 浏览器已启动 (Profile: {profile_label})"
                    )

                except Exception as e:
                    self.browser = None
                    self._initialized = False
                    self._mark_browser_health(False)
                    if self._is_memory_pressure_browser_launch_error(e):
                        await self.reclaim_runtime_memory(
                            reason="initialize_memory_pressure",
                            aggressive=True,
                        )
                    self._mark_browser_launch_failure(e)
                    debug_logger.log_error(
                        "[BrowserCaptcha] ❌ 浏览器启动失败: "
                        f"{type(e).__name__}: {str(e)} | "
                        f"display={display_value or '<empty>'} | "
                        f"executable={browser_executable_path or '<auto>'} | "
                        f"args={' '.join(effective_launch_args) if effective_launch_args else '<none>'} | "
                        f"cooldown={self._get_browser_launch_cooldown_remaining_seconds():.1f}s"
                    )
                    raise

    # ========== 常驻模式 API ==========

    async def start_resident_mode(self, project_id: str):
        """启动常驻模式（初始化浏览器，get_token 会自动创建标签页）"""
        if not str(project_id or "").strip():
            debug_logger.log_warning("[BrowserCaptcha] 启动常驻模式失败：project_id 为空")
            return
        self._mark_runtime_active()
        await self.initialize()
        debug_logger.log_info(f"[BrowserCaptcha] 浏览器已就绪 (project: {project_id})")

    async def stop_resident_mode(self, project_id: Optional[str] = None):
        """停止常驻模式
        
        Args:
            project_id: 指定 project_id 或 slot_id；如果为 None 则关闭所有常驻标签页
        """
        target_slot_id = None
        if project_id:
            async with self._resident_lock:
                target_slot_id = project_id if project_id in self._resident_tabs else self._resolve_affinity_slot_locked(project_id)

        if target_slot_id:
            await self._close_resident_tab(target_slot_id)
            self._resident_error_streaks.pop(target_slot_id, None)
            debug_logger.log_info(f"[BrowserCaptcha] 已关闭共享标签页 slot={target_slot_id} (request={project_id})")
            return

        async with self._resident_lock:
            slot_ids = list(self._resident_tabs.keys())
            resident_items = list(self._resident_tabs.values())
            self._resident_tabs.clear()
            self._project_resident_affinity.clear()
            self._token_resident_affinity.clear()
            self._resident_error_streaks.clear()
            self._resident_unavailable_slots.clear()
            self._resident_rebuild_tasks.clear()
            self._resident_recovery_tasks.clear()
            self._sync_compat_resident_state()

        for resident_info in resident_items:
            if resident_info and resident_info.tab:
                await self._dispose_browser_context_quietly(resident_info.browser_context_id)
                await self._close_tab_quietly(resident_info.tab)
        debug_logger.log_info(f"[BrowserCaptcha] 已关闭所有共享常驻标签页 (共 {len(slot_ids)} 个)")

    async def _wait_for_document_ready(self, tab, retries: int = 30, interval_seconds: float = 1.0) -> bool:
        """等待页面文档加载完成。"""
        for _ in range(retries):
            try:
                ready_state = await self._tab_evaluate(
                    tab,
                    "document.readyState",
                    label="document.readyState",
                    timeout_seconds=2.0,
                )
                if ready_state == "complete":
                    return True
            except Exception as e:
                if self._is_browser_runtime_error(e):
                    self._mark_browser_health(False)
                    raise
            await asyncio.sleep(interval_seconds)
        return False

    def _is_server_side_flow_error(self, error_text: str) -> bool:
        error_lower = (error_text or "").lower()
        if self._is_generation_policy_error(error_text):
            return False
        return any(keyword in error_lower for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
            "\"reason\":\"internal\"",
            "server error",
            "upstream error",
        ])

    def _is_external_flow_error(self, error_text: str) -> bool:
        error_lower = (error_text or "").lower()
        return any(keyword in error_lower for keyword in [
            "429",
            "too many requests",
            "tls",
            "ssl",
            "econnreset",
            "connection reset",
            "connection aborted",
            "network is unreachable",
            "name or service not known",
            "temporary failure in name resolution",
            "timed out",
            "timeout",
            "proxyerror",
            "proxy error",
            "credentials_missing",
            "missing required authentication credential",
            "login cookie",
            "access token",
            "authorization",
        ])

    def _is_generation_policy_error(self, error_text: str) -> bool:
        error_lower = (error_text or "").lower()
        return any(keyword in error_lower for keyword in [
            "public_error_unsafe_generation",
            "unsafe_generation",
            "request contains an invalid ar",
        ])

    def _is_recaptcha_cache_reset_error(self, error_text: str) -> bool:
        """Whether the upstream error should trigger browser cache/storage reset."""
        error_lower = (error_text or "").lower()
        return any(keyword in error_lower for keyword in [
            "403",
            "forbidden",
            "recaptcha evaluation failed",
            "public_error_unusual_activity",
            "unusual_activity",
            "unusual activity",
            "recaptcha",
        ])

    def _is_force_fresh_browser_restart_error(self, error_text: str) -> bool:
        """命中特定 Flow 风控错误时，直接重启为全新无状态浏览器。"""
        error_lower = (error_text or "").lower()
        if "recaptcha evaluation failed" not in error_lower:
            return False

        return any(keyword in error_lower for keyword in [
            "public_error_unusual_activity_too_much_traffic",
            "public_error_unusual_activity",
            "public_error_something_went_wrong",
        ])

    async def _clear_tab_site_storage(self, tab) -> Dict[str, Any]:
        """清理当前站点的本地存储状态，但保留 cookies 登录态。"""
        result = await self._tab_evaluate(tab, """
            (async () => {
                const summary = {
                    local_storage_cleared: false,
                    session_storage_cleared: false,
                    cache_storage_deleted: [],
                    indexed_db_deleted: [],
                    indexed_db_errors: [],
                    service_worker_unregistered: 0,
                };

                try {
                    window.localStorage.clear();
                    summary.local_storage_cleared = true;
                } catch (e) {
                    summary.local_storage_error = String(e);
                }

                try {
                    window.sessionStorage.clear();
                    summary.session_storage_cleared = true;
                } catch (e) {
                    summary.session_storage_error = String(e);
                }

                try {
                    if (typeof caches !== 'undefined') {
                        const cacheKeys = await caches.keys();
                        for (const key of cacheKeys) {
                            const deleted = await caches.delete(key);
                            if (deleted) {
                                summary.cache_storage_deleted.push(key);
                            }
                        }
                    }
                } catch (e) {
                    summary.cache_storage_error = String(e);
                }

                try {
                    if (navigator.serviceWorker) {
                        const registrations = await navigator.serviceWorker.getRegistrations();
                        for (const registration of registrations) {
                            const ok = await registration.unregister();
                            if (ok) {
                                summary.service_worker_unregistered += 1;
                            }
                        }
                    }
                } catch (e) {
                    summary.service_worker_error = String(e);
                }

                try {
                    if (typeof indexedDB !== 'undefined' && typeof indexedDB.databases === 'function') {
                        const dbs = await indexedDB.databases();
                        const names = Array.from(new Set(
                            dbs
                                .map((item) => item && item.name)
                                .filter((name) => typeof name === 'string' && name)
                        ));
                        for (const name of names) {
                            try {
                                await new Promise((resolve) => {
                                    const request = indexedDB.deleteDatabase(name);
                                    request.onsuccess = () => resolve(true);
                                    request.onerror = () => resolve(false);
                                    request.onblocked = () => resolve(false);
                                });
                                summary.indexed_db_deleted.push(name);
                            } catch (e) {
                                summary.indexed_db_errors.push(`${name}: ${String(e)}`);
                            }
                        }
                    } else {
                        summary.indexed_db_unsupported = true;
                    }
                } catch (e) {
                    summary.indexed_db_errors.push(String(e));
                }

                return summary;
            })()
        """, label="clear_tab_site_storage", timeout_seconds=15.0)
        return result if isinstance(result, dict) else {}

    async def _clear_resident_storage_and_reload(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
        *,
        clear_browser_cache: bool = False,
        refresh_local_assets: bool = False,
    ) -> bool:
        """清理常驻标签页的站点数据并刷新，尝试原地自愈。"""
        async with self._resident_lock:
            resolved_slot_id = str(slot_id or "").strip()
            if resolved_slot_id:
                resident_info = self._resident_tabs.get(resolved_slot_id)
            else:
                resolved_slot_id, resident_info = self._resolve_resident_slot_for_project_locked(project_id, token_id=token_id)

        if not resident_info or not resident_info.tab:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id or 'unknown'} 没有可清理的共享标签页"
            )
            return False

        try:
            async with resident_info.solve_lock:
                if clear_browser_cache:
                    await self._clear_browser_cache()
                if refresh_local_assets:
                    self._reset_local_recaptcha_asset_caches(purge_disk=True)
                cleanup_summary = await self._clear_tab_site_storage(resident_info.tab)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 已清理站点存储，准备刷新恢复: {cleanup_summary}"
                )

                resident_info.recaptcha_ready = False
                await self._tab_reload(
                    resident_info.tab,
                    label=f"clear_resident_reload:{resolved_slot_id or project_id}",
                )

                if not await self._wait_for_document_ready(resident_info.tab, retries=30, interval_seconds=1.0):
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 清理后页面加载超时"
                    )
                    return False

                resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
                if resident_info.recaptcha_ready:
                    resident_info.last_used_at = time.time()
                    async with self._resident_lock:
                        self._clear_resident_slot_unavailable_locked(resolved_slot_id)
                    self._remember_project_affinity(project_id, resolved_slot_id, resident_info)
                    self._remember_token_affinity(token_id, resolved_slot_id, resident_info)
                    self._resident_error_streaks.pop(resolved_slot_id, None)
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 清理后已恢复 reCAPTCHA"
                    )
                    return True

                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 清理后仍无法恢复 reCAPTCHA"
                )
                return False
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 清理或刷新失败: {e}"
            )
            return False

    async def _recreate_resident_tab(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
    ) -> bool:
        """关闭并重建常驻标签页。"""
        resolved_slot_id, resident_info = await self._rebuild_resident_tab(
            project_id,
            token_id=token_id,
            slot_id=slot_id,
            return_slot_key=True,
        )
        if resident_info is None:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id}, slot={slot_id or 'unknown'} 重建共享标签页失败"
            )
            return False
        debug_logger.log_warning(
            f"[BrowserCaptcha] project_id={project_id} 已重建共享标签页 slot={resolved_slot_id}"
        )
        return True

    async def _restart_browser_for_project(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        *,
        fresh_profile: bool = False,
    ) -> bool:
        async with self._runtime_recover_lock:
            if fresh_profile and await self._has_active_browser_work():
                self._mark_fresh_profile_restart_pending(
                    reason=f"fresh_restart_deferred:{project_id}",
                    force=True,
                )
                await self._maybe_execute_pending_fresh_profile_restart(
                    project_id,
                    token_id=token_id,
                    source="fresh_restart_deferred_active_work",
                )
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id} fresh profile 重启已延后到当前并发 drain 后立即执行"
                )
                return True
            if not fresh_profile and self._was_runtime_restarted_recently():
                try:
                    if await self._probe_browser_runtime():
                        slot_id, resident_info = await self._ensure_resident_tab(
                            project_id,
                            token_id=token_id,
                            return_slot_key=True,
                        )
                        if resident_info is not None and slot_id:
                            self._remember_project_affinity(project_id, slot_id, resident_info)
                            self._remember_token_affinity(token_id, slot_id, resident_info)
                            self._resident_error_streaks.pop(slot_id, None)
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] project_id={project_id} 检测到最近已完成浏览器恢复，复用当前运行态 (slot={slot_id})"
                            )
                            return True
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id} 复用最近恢复运行态失败，继续执行整浏览器重启: {e}"
                    )

            restarted = await self._restart_browser_for_project_unlocked(
                project_id,
                token_id=token_id,
                fresh_profile=fresh_profile,
            )
            if restarted:
                self._mark_runtime_restart()
            return restarted

    async def _restart_browser_for_project_unlocked(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        *,
        fresh_profile: bool = False,
    ) -> bool:
        """重启整个 nodriver 浏览器，仅恢复当前请求所需标签页。"""
        restart_reason = f"restart_project:{project_id}"
        if fresh_profile:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 准备执行 fresh profile 浏览器冷启动"
            )
            restart_reason = f"fresh_restart_project:{project_id}"
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 准备重启 nodriver 浏览器以恢复"
            )

        await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=restart_reason)
        if fresh_profile:
            self._reset_local_recaptcha_asset_caches(purge_disk=True)
        await self.initialize()

        slot_id, resident_info = await self._ensure_resident_tab(
            project_id,
            token_id=token_id,
            force_create=True,
            return_slot_key=True,
        )
        if resident_info is None or not slot_id:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 浏览器重启后无法定位可用共享标签页")
            return False

        self._remember_project_affinity(project_id, slot_id, resident_info)
        self._remember_token_affinity(token_id, slot_id, resident_info)
        self._resident_error_streaks.pop(slot_id, None)
        if fresh_profile:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 已使用全新无状态浏览器恢复当前共享标签页 "
                f"(active_slot={slot_id}, warmup_disabled=true)"
            )
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 浏览器重启后已恢复当前共享标签页 "
                f"(active_slot={slot_id}, warmup_disabled=true)"
            )
        return True

    async def report_flow_error(
        self,
        project_id: str,
        error_reason: str,
        error_message: str = "",
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
    ):
        """上游生成接口异常时，对常驻标签页执行自愈恢复。"""
        if not project_id:
            return

        async with self._resident_lock:
            resolved_slot_id = str(slot_id or "").strip()
            if resolved_slot_id:
                resident_info = self._resident_tabs.get(resolved_slot_id)
                if resident_info is None or not resident_info.tab:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 上游异常回调命中已失效 slot，跳过本次恢复"
                    )
                    return
            else:
                resolved_slot_id, resident_info = self._resolve_resident_slot_for_project_locked(project_id, token_id=token_id)

        if not resolved_slot_id:
            return

        error_text = f"{error_reason or ''} {error_message or ''}".strip()
        error_lower = error_text.lower()
        if self._is_generation_policy_error(error_text):
            debug_logger.log_info(
                f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 收到内容安全拒绝，跳过浏览器自愈: {error_reason}"
            )
            return
        if self._is_external_flow_error(error_text):
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 收到外部链路/鉴权错误，跳过 resident 自愈: {error_reason}"
            )
            return

        streak = self._resident_error_streaks.get(resolved_slot_id, 0) + 1
        self._resident_error_streaks[resolved_slot_id] = streak
        debug_logger.log_warning(
            f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 收到上游异常，streak={streak}, reason={error_reason}, detail={error_message[:200]}"
        )

        if not self._initialized or not self.browser:
            return

        async def _recover_current_slot():
            if self._is_force_fresh_browser_restart_error(error_text):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} "
                    "命中特定 Flow 风控错误，已标记当前 slot 不再复用；浏览器 fresh profile 轮换会等待当前并发 drain 后立即执行"
                )
                if resident_info is not None:
                    await self._mark_resident_slot_unavailable(
                        resolved_slot_id,
                        resident_info,
                        reason=f"flow_force_fresh:{project_id}:{streak}",
                    )
                self._mark_fresh_profile_restart_pending(
                    reason=f"flow_force_fresh:{project_id}:{resolved_slot_id}:streak={streak}",
                    force=True,
                )
                await self._maybe_execute_pending_fresh_profile_restart(
                    project_id,
                    token_id=token_id,
                    source="flow_force_fresh_error",
                )
                return

            # 403 / reCAPTCHA / unusual activity：浏览器级缓存清理 + 本地静态缓存刷新 + resident 恢复
            if self._is_recaptcha_cache_reset_error(error_text):
                restart_threshold = max(
                    2,
                    int(getattr(config, "browser_personal_recaptcha_restart_threshold", 2) or 2),
                )
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id} 检测到 403/reCAPTCHA/unusual_activity 错误，清理缓存并重建"
                )
                healed = await self._clear_resident_storage_and_reload(
                    project_id,
                    token_id=token_id,
                    slot_id=resolved_slot_id,
                    clear_browser_cache=True,
                    refresh_local_assets=True,
                )
                if healed and streak < restart_threshold:
                    return

                recreated = False
                if not healed:
                    recreated = await self._recreate_resident_tab(
                        project_id,
                        token_id=token_id,
                        slot_id=resolved_slot_id,
                    )
                    if recreated and streak < restart_threshold:
                        return

                if streak >= restart_threshold or (not healed and not recreated):
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} reCAPTCHA 风控连续失败，升级为整浏览器重启恢复"
                    )
                    await self._restart_browser_for_project(project_id, token_id=token_id)
                return

            # 服务端错误：根据连续失败次数决定恢复策略
            if self._is_server_side_flow_error(error_text):
                recreate_threshold = max(2, int(getattr(config, "browser_personal_recreate_threshold", 2) or 2))
                restart_threshold = max(3, int(getattr(config, "browser_personal_restart_threshold", 3) or 3))

                if streak >= restart_threshold:
                    await self._restart_browser_for_project(project_id, token_id=token_id)
                    return
                if streak >= recreate_threshold:
                    await self._recreate_resident_tab(
                        project_id,
                        token_id=token_id,
                        slot_id=resolved_slot_id,
                    )
                    return

                healed = await self._clear_resident_storage_and_reload(
                    project_id,
                    token_id=token_id,
                    slot_id=resolved_slot_id,
                )
                if not healed:
                    await self._recreate_resident_tab(
                        project_id,
                        token_id=token_id,
                        slot_id=resolved_slot_id,
                    )
                return

            # 其他错误：直接重建标签页
            await self._recreate_resident_tab(
                project_id,
                token_id=token_id,
                slot_id=resolved_slot_id,
            )

        await self._run_resident_recovery_task(
            resolved_slot_id,
            _recover_current_slot,
            project_id=project_id,
            error_reason=error_reason or error_message or "upstream_error",
        )

    async def _wait_for_recaptcha(self, tab) -> bool:
        """等待 reCAPTCHA 加载

        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[BrowserCaptcha] 注入 reCAPTCHA 脚本...")

        await self._inject_recaptcha_bootstrap_script(
            tab,
            script_path="recaptcha/enterprise.js",
            website_key=self.website_key,
            label="inject_recaptcha_script",
        )

        initial_settle_seconds = 1.0
        if IS_DOCKER:
            initial_settle_seconds = 2.0
        elif self._proxy_url:
            initial_settle_seconds = 1.5
        if initial_settle_seconds > 0:
            await tab.sleep(initial_settle_seconds)

        max_wait_seconds = 12.0 if self.headless else 15.0
        if IS_DOCKER:
            max_wait_seconds = max(max_wait_seconds, 20.0)
        if self._proxy_url:
            max_wait_seconds += 5.0

        poll_interval_seconds = 0.5
        max_attempts = max(1, int(max_wait_seconds / poll_interval_seconds))
        last_bootstrap_state = None

        for i in range(max_attempts):
            try:
                is_ready = await self._tab_evaluate(
                    tab,
                    "typeof grecaptcha !== 'undefined' && "
                    "typeof grecaptcha.enterprise !== 'undefined' && "
                    "typeof grecaptcha.enterprise.execute === 'function'",
                    label="check_recaptcha_ready",
                    timeout_seconds=4.0,
                )

                if is_ready:
                    debug_logger.log_info(
                        f"[BrowserCaptcha] reCAPTCHA 已就绪 "
                        f"(等待了 {initial_settle_seconds + i * poll_interval_seconds:.1f}s)"
                    )
                    return True

                if i in {4, 10, 18, 28}:
                    try:
                        last_bootstrap_state = await self._tab_evaluate(
                            tab,
                            """
                                (() => {
                                    const state = window.__flow2apiRecaptchaBootstrapState;
                                    if (!state || typeof state !== 'object') return null;
                                    return {
                                        status: String(state.status || ''),
                                        url: String(state.url || ''),
                                        error: String(state.error || ''),
                                        attempts: Number(state.attempts || 0),
                                    };
                                })()
                            """,
                            label="read_recaptcha_bootstrap_state",
                            timeout_seconds=3.0,
                            return_by_value=True,
                        )
                    except Exception as state_error:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 读取 reCAPTCHA bootstrap 状态失败: {state_error}"
                        )
                        last_bootstrap_state = None

                    if isinstance(last_bootstrap_state, dict):
                        status = str(last_bootstrap_state.get("status") or "").strip().lower()
                        debug_logger.log_info(
                            "[BrowserCaptcha] reCAPTCHA bootstrap 状态: "
                            f"status={status or '<empty>'}, "
                            f"attempts={last_bootstrap_state.get('attempts')}, "
                            f"url={last_bootstrap_state.get('url') or '<empty>'}, "
                            f"error={last_bootstrap_state.get('error') or '<empty>'}"
                        )
                        if status in {"error", "timeout"}:
                            await self._inject_recaptcha_bootstrap_script(
                                tab,
                                script_path="recaptcha/enterprise.js",
                                website_key=self.website_key,
                                label="inject_recaptcha_script_force_retry",
                                force_remote=True,
                            )

                await tab.sleep(poll_interval_seconds)
            except Exception as e:
                if self._is_browser_runtime_error(e):
                    self._mark_browser_health(False)
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 检查 reCAPTCHA 时浏览器运行态断开，停止等待并触发恢复: {e}"
                    )
                    raise
                debug_logger.log_warning(f"[BrowserCaptcha] 检查 reCAPTCHA 时异常: {e}")
                await tab.sleep(0.5)

        if isinstance(last_bootstrap_state, dict):
            debug_logger.log_warning(
                "[BrowserCaptcha] reCAPTCHA 加载超时 "
                f"(bootstrap_status={last_bootstrap_state.get('status') or '<empty>'}, "
                f"attempts={last_bootstrap_state.get('attempts')}, "
                f"url={last_bootstrap_state.get('url') or '<empty>'}, "
                f"error={last_bootstrap_state.get('error') or '<empty>'})"
            )
        else:
            debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 加载超时")
        return False

    async def _wait_for_custom_recaptcha(
        self,
        tab,
        website_key: str,
        enterprise: bool = False,
    ) -> bool:
        """等待任意站点的 reCAPTCHA 加载，用于分数测试。"""
        debug_logger.log_info("[BrowserCaptcha] 检测自定义 reCAPTCHA...")

        ready_check = (
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
            "typeof grecaptcha.enterprise.execute === 'function'"
        ) if enterprise else (
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
        )
        script_path = "recaptcha/enterprise.js" if enterprise else "recaptcha/api.js"
        label = "Enterprise" if enterprise else "V3"

        is_ready = await self._tab_evaluate(
            tab,
            ready_check,
            label="check_custom_recaptcha_preloaded",
            timeout_seconds=2.5,
        )
        if is_ready:
            debug_logger.log_info(f"[BrowserCaptcha] 自定义 reCAPTCHA {label} 已加载")
            return True

        debug_logger.log_info("[BrowserCaptcha] 未检测到自定义 reCAPTCHA，注入脚本...")
        await self._inject_recaptcha_bootstrap_script(
            tab,
            script_path=script_path,
            website_key=website_key,
            label="inject_custom_recaptcha_script",
        )

        await tab.sleep(3)
        for i in range(20):
            is_ready = await self._tab_evaluate(
                tab,
                ready_check,
                label="check_custom_recaptcha_ready",
                timeout_seconds=2.5,
            )
            if is_ready:
                debug_logger.log_info(f"[BrowserCaptcha] 自定义 reCAPTCHA {label} 已加载（等待了 {i * 0.5} 秒）")
                return True
            await tab.sleep(0.5)

        debug_logger.log_warning("[BrowserCaptcha] 自定义 reCAPTCHA 加载超时")
        return False

    async def _execute_recaptcha_on_tab(self, tab, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """在指定标签页执行 reCAPTCHA 获取 token

        Args:
            tab: nodriver 标签页对象
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            reCAPTCHA token 或 None
        """
        execute_timeout_ms = int(max(1000, self._solve_timeout_seconds * 1000))
        execute_result = await self._tab_evaluate(
            tab,
            f"""
                (async () => {{
                    const finishError = (error) => {{
                        const message = error && error.message ? error.message : String(error || 'execute failed');
                        return {{ ok: false, error: message }};
                    }};

                    try {{
                        const token = await new Promise((resolve, reject) => {{
                            let settled = false;
                            const done = (handler, value) => {{
                                if (settled) return;
                                settled = true;
                                handler(value);
                            }};
                            const timer = setTimeout(() => {{
                                done(reject, new Error('execute timeout'));
                            }}, {execute_timeout_ms});

                            try {{
                                grecaptcha.enterprise.ready(() => {{
                                    grecaptcha.enterprise.execute({json.dumps(self.website_key)}, {{action: {json.dumps(action)}}})
                                        .then((token) => {{
                                            clearTimeout(timer);
                                            done(resolve, token);
                                        }})
                                        .catch((error) => {{
                                            clearTimeout(timer);
                                            done(reject, error);
                                        }});
                                }});
                            }} catch (error) {{
                                clearTimeout(timer);
                                done(reject, error);
                            }}
                        }});

                        return {{ ok: true, token }};
                    }} catch (error) {{
                        return finishError(error);
                    }}
                }})()
            """,
            label=f"execute_recaptcha:{action}",
            timeout_seconds=self._solve_timeout_seconds + 2.0,
            await_promise=True,
            return_by_value=True,
        )

        token = execute_result.get("token") if isinstance(execute_result, dict) else None
        if not token:
            error = execute_result.get("error") if isinstance(execute_result, dict) else execute_result
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 错误: {error}")

        if token:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ Token 获取成功 (长度: {len(token)})")
        else:
            debug_logger.log_warning("[BrowserCaptcha] Token 获取失败，交由上层执行标签页恢复")

        return token

    async def _execute_custom_recaptcha_on_tab(
        self,
        tab,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """在指定标签页执行任意站点的 reCAPTCHA。"""
        ts = int(time.time() * 1000)
        token_var = f"_custom_recaptcha_token_{ts}"
        error_var = f"_custom_recaptcha_error_{ts}"
        execute_target = "grecaptcha.enterprise.execute" if enterprise else "grecaptcha.execute"

        execute_script = f"""
            (() => {{
                window.{token_var} = null;
                window.{error_var} = null;

                try {{
                    grecaptcha.ready(function() {{
                        {execute_target}('{website_key}', {{action: '{action}'}})
                            .then(function(token) {{
                                window.{token_var} = token;
                            }})
                            .catch(function(err) {{
                                window.{error_var} = err.message || 'execute failed';
                            }});
                    }});
                }} catch (e) {{
                    window.{error_var} = e.message || 'exception';
                }}
            }})()
        """

        await self._tab_evaluate(
            tab,
            execute_script,
            label=f"execute_custom_recaptcha:{action}",
            timeout_seconds=5.0,
        )

        token = None
        for _ in range(30):
            await tab.sleep(0.5)
            token = await self._tab_evaluate(
                tab,
                f"window.{token_var}",
                label=f"poll_custom_recaptcha_token:{action}",
                timeout_seconds=2.0,
            )
            if token:
                break
            error = await self._tab_evaluate(
                tab,
                f"window.{error_var}",
                label=f"poll_custom_recaptcha_error:{action}",
                timeout_seconds=2.0,
            )
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] 自定义 reCAPTCHA 错误: {error}")
                break

        try:
            await self._tab_evaluate(
                tab,
                f"delete window.{token_var}; delete window.{error_var};",
                label="cleanup_custom_recaptcha_temp_vars",
                timeout_seconds=5.0,
            )
        except:
            pass

        if token:
            post_wait_seconds = 3
            try:
                post_wait_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
            except Exception:
                pass
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 自定义 reCAPTCHA 已完成，额外等待 {post_wait_seconds:.1f}s 后返回 token"
                )
                await tab.sleep(post_wait_seconds)

        return token

    async def _verify_score_on_tab(self, tab, token: str, verify_url: str) -> Dict[str, Any]:
        """直接读取测试页面展示的分数，避免 verify.php 与页面显示口径不一致。"""
        _ = token
        _ = verify_url
        started_at = time.time()
        timeout_seconds = 25.0
        refresh_clicked = False
        last_snapshot: Dict[str, Any] = {}

        try:
            timeout_seconds = float(getattr(config, "browser_score_dom_wait_seconds", 25) or 25)
        except Exception:
            pass

        while (time.time() - started_at) < timeout_seconds:
            try:
                result = await self._tab_evaluate(tab, """
                    (() => {
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
                    })()
                """, label="verify_score_dom", timeout_seconds=10.0)
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
                            "current_user_agent": result.get("current_user_agent") or "",
                            "current_ip_address": result.get("current_ip_address") or "",
                            "page_title": result.get("title") or "",
                            "page_url": result.get("url") or "",
                        },
                    }

            if not refresh_clicked and (time.time() - started_at) >= 2:
                refresh_clicked = True
                try:
                    await self._tab_evaluate(tab, """
                        (() => {
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
                        })()
                    """, label="verify_score_click_refresh", timeout_seconds=5.0)
                except Exception:
                    pass

            await tab.sleep(0.5)

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

    async def _extract_tab_fingerprint(self, tab) -> Optional[Dict[str, Any]]:
        """从 nodriver 标签页提取浏览器指纹信息。"""
        try:
            fingerprint = await self._tab_evaluate(tab, """
                () => {
                    const ua = navigator.userAgent || "";
                    const lang = navigator.language || "";
                    const languages = Array.isArray(navigator.languages) ? navigator.languages.slice() : [];
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
                        language: lang,
                        languages,
                        timezone: (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || "",
                        platform: navigator.platform || "",
                        vendor: navigator.vendor || "",
                        hardware_concurrency: Number(navigator.hardwareConcurrency || 0),
                        device_memory: Number(navigator.deviceMemory || 0),
                        device_pixel_ratio: Number(window.devicePixelRatio || 0),
                        screen_width: Number(screen.width || 0),
                        screen_height: Number(screen.height || 0),
                        screen_avail_width: Number(screen.availWidth || 0),
                        screen_avail_height: Number(screen.availHeight || 0),
                    };
                }
            """, label="extract_tab_fingerprint", timeout_seconds=8.0)
            if not isinstance(fingerprint, dict):
                return None

            result: Dict[str, Any] = {"proxy_url": self._proxy_url}
            for key in (
                "user_agent",
                "accept_language",
                "sec_ch_ua",
                "sec_ch_ua_mobile",
                "sec_ch_ua_platform",
                "language",
                "timezone",
                "platform",
                "vendor",
            ):
                value = fingerprint.get(key)
                if isinstance(value, str) and value:
                    result[key] = value
            languages = fingerprint.get("languages")
            if isinstance(languages, list):
                normalized_languages = [str(item).strip() for item in languages if str(item).strip()]
                if normalized_languages:
                    result["languages"] = normalized_languages
            for key in (
                "hardware_concurrency",
                "device_memory",
                "device_pixel_ratio",
                "screen_width",
                "screen_height",
                "screen_avail_width",
                "screen_avail_height",
            ):
                value = fingerprint.get(key)
                if isinstance(value, (int, float)) and float(value) > 0:
                    result[key] = int(value) if float(value).is_integer() else float(value)
            return result
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 提取 nodriver 指纹失败: {e}")
            return None

    async def _refresh_last_fingerprint(self, tab) -> Optional[Dict[str, Any]]:
        """缓存最近一次浏览器指纹，避免每次打码成功后都追加一轮 JS 执行。"""
        if self._is_fingerprint_cache_fresh():
            return self._last_fingerprint

        fingerprint = await self._extract_tab_fingerprint(tab)
        self._last_fingerprint = fingerprint
        self._last_fingerprint_at = time.monotonic() if fingerprint else 0.0
        return fingerprint

    def _remember_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        if isinstance(fingerprint, dict) and fingerprint:
            self._last_fingerprint = dict(fingerprint)
            self._last_fingerprint_at = time.monotonic()
        else:
            self._last_fingerprint = None
            self._last_fingerprint_at = 0.0

    async def _solve_with_resident_tab(
        self,
        slot_id: str,
        project_id: str,
        resident_info: Optional[ResidentTabInfo],
        action: str,
        *,
        consume_reservation: bool = False,
        success_label: str,
    ) -> Optional[str]:
        """在共享常驻标签页上执行一次打码，并统一更新成功态。"""
        if not resident_info or not resident_info.tab or not resident_info.recaptcha_ready:
            if consume_reservation:
                await self._release_resident_slot_reservation(slot_id, resident_info=resident_info)
            return None

        start_time = time.time()
        async with resident_info.solve_lock:
            if consume_reservation:
                await self._consume_resident_slot_reservation(slot_id, resident_info=resident_info)
            token = await self._run_with_timeout(
                self._execute_recaptcha_on_tab(resident_info.tab, action),
                timeout_seconds=self._solve_timeout_seconds,
                label=f"{success_label}:{slot_id}:{project_id}:{action}",
            )

        if not token:
            return None

        duration_ms = (time.time() - start_time) * 1000
        resident_info.last_used_at = time.time()
        resident_info.use_count += 1
        browser_solve_count = self._record_browser_solve_success(
            source="resident",
            project_id=project_id,
        )
        self._remember_project_affinity(project_id, slot_id, resident_info)
        self._resident_error_streaks.pop(slot_id, None)
        self._mark_browser_health(True)
        if resident_info.fingerprint:
            self._remember_fingerprint(resident_info.fingerprint)
        else:
            resident_info.fingerprint = await self._refresh_last_fingerprint(resident_info.tab)
        debug_logger.log_info(
            "[BrowserCaptcha] ✅ Token生成成功"
            f"（slot={slot_id}, 耗时 {duration_ms:.0f}ms, "
            f"slot_use_count={resident_info.use_count}, "
            f"browser_solve_count={browser_solve_count}"
            "）"
        )
        await self._maybe_execute_pending_fresh_profile_restart(
            project_id,
            source="resident_solve_success",
        )
        return token

    # ========== 主要 API ==========

    async def _get_token_direct(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        return_slot_id: bool = False,
        allow_affinity: bool = True,
        remember_affinity: bool = True,
    ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
        """获取 reCAPTCHA token

        使用全局共享打码标签页池。标签页不再按 project_id 一对一绑定，
        谁拿到空闲 tab 就用谁的；只有 Session Token 刷新/故障恢复会优先参考最近一次映射。

        Args:
            project_id: Flow项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        def finish_result(
            token: Optional[str],
            resolved_slot_id: Optional[str] = None,
        ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
            if return_slot_id:
                return token, (str(resolved_slot_id or "").strip() or None if token else None)
            return token

        debug_logger.log_info(
            f"[BrowserCaptcha] get_token 开始: project_id={project_id}, token_id={token_id}, action={action}, 当前标签页数={len(self._resident_tabs)}/{self._max_resident_tabs}"
        )
        self._mark_runtime_active()

        await self._wait_for_pending_fresh_profile_restart_before_solve(
            project_id,
            token_id=token_id,
            source="get_token_pre_initialize",
        )

        # 确保浏览器已初始化
        await self.initialize()

        await self._wait_for_pending_fresh_profile_restart_before_solve(
            project_id,
            token_id=token_id,
            source="get_token_pre_resident_pick",
        )

        reserved_slot_id: Optional[str] = None

        async def release_reserved_slot():
            nonlocal reserved_slot_id
            if reserved_slot_id:
                await self._release_resident_slot_reservation(reserved_slot_id)
                reserved_slot_id = None

        try:
            debug_logger.log_info(
                f"[BrowserCaptcha] 开始从共享打码池获取标签页 (project: {project_id}, token_id={token_id}, 当前: {len(self._resident_tabs)}/{self._max_resident_tabs})"
            )
            resident_pick_started_at = time.monotonic()
            try:
                slot_id, resident_info = await self._ensure_resident_tab(
                    project_id,
                    token_id=token_id,
                    reserve_for_solve=True,
                    return_slot_key=True,
                )
            except Exception as e:
                if not self._is_browser_runtime_error(e):
                    raise
                self._mark_browser_health(False)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 共享标签页分配时浏览器运行态断开，立即重启恢复 (project: {project_id}, token_id={token_id}): {e}"
                )
                slot_id, resident_info = None, None
                if await self._recover_browser_runtime(project_id, reason="ensure_resident_tab_runtime_error"):
                    try:
                        slot_id, resident_info = await self._ensure_resident_tab(
                            project_id,
                            token_id=token_id,
                            reserve_for_solve=True,
                            return_slot_key=True,
                        )
                    except Exception as retry_error:
                        if not self._is_browser_runtime_error(retry_error):
                            raise
                        self._mark_browser_health(False)
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 浏览器恢复后分配共享标签页仍断开 (project: {project_id}, token_id={token_id}): {retry_error}"
                        )
                        slot_id, resident_info = None, None
            reserved_slot_id = slot_id or None
            if resident_info is None or not slot_id:
                if await self._wait_for_active_resident_rebuild(timeout_seconds=min(20.0, self._solve_timeout_seconds)):
                    slot_id, resident_info = await self._ensure_resident_tab(
                        project_id,
                        token_id=token_id,
                        reserve_for_solve=True,
                        return_slot_key=True,
                    )
                    reserved_slot_id = slot_id or None
            if resident_info is None or not slot_id:
                if not await self._probe_browser_runtime():
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 共享标签页池为空且浏览器疑似失活，尝试重启恢复 (project: {project_id}, token_id={token_id})"
                    )
                    if await self._recover_browser_runtime(project_id, reason="ensure_resident_tab"):
                        slot_id, resident_info = await self._ensure_resident_tab(
                            project_id,
                            token_id=token_id,
                            reserve_for_solve=True,
                            return_slot_key=True,
                        )
                        reserved_slot_id = slot_id or None

            if resident_info is None or not slot_id:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 共享标签页池不可用，fallback 到传统模式 (project: {project_id}, token_id={token_id})"
                )
                legacy_token = await self._get_token_legacy(project_id, action, token_id=token_id)
                return finish_result(legacy_token, None)

            debug_logger.log_info(
                "[BrowserCaptcha] 共享标签页已分配 "
                f"(project_id={project_id}, token_id={token_id}, slot={slot_id}, "
                f"pick_elapsed={time.monotonic() - resident_pick_started_at:.3f}s, "
                f"slot_use_count={int(getattr(resident_info, 'use_count', 0) or 0)}, "
                f"pending={int(getattr(resident_info, 'pending_assignment_count', 0) or 0)}, "
                f"ready={bool(getattr(resident_info, 'recaptcha_ready', False))})"
            )
            debug_logger.log_info(
                f"[BrowserCaptcha] ✅ 共享标签页可用 (slot={slot_id}, project={project_id}, token_id={token_id}, use_count={resident_info.use_count})"
            )

            if resident_info and resident_info.tab:
                cookie_bound = await self._ensure_resident_token_binding(
                    resident_info,
                    token_id,
                    label=f"get_token:{slot_id}",
                )
                if not cookie_bound:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 共享标签页 cookie 绑定校验失败，准备重建 (slot={slot_id}, project={project_id}, token_id={token_id})"
                    )

            if resident_info and resident_info.tab and not resident_info.recaptcha_ready:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 共享标签页未就绪，准备重建 cold slot={slot_id}, project={project_id}, token_id={token_id}"
                )
                await self._mark_resident_slot_unavailable(
                    slot_id,
                    resident_info,
                    reason=f"cold_slot:{project_id}",
                )
                await release_reserved_slot()
                slot_id, resident_info = await self._rebuild_resident_tab(
                    project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    reserve_for_solve=True,
                    return_slot_key=True,
                )
                reserved_slot_id = slot_id or None
                if resident_info is None:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] cold slot 重建失败，升级为浏览器级恢复 (slot={slot_id}, project={project_id}, token_id={token_id})"
                    )
                    if await self._recover_browser_runtime(project_id, reason=f"cold_resident_tab:{slot_id or 'unknown'}"):
                        slot_id, resident_info = await self._ensure_resident_tab(
                            project_id,
                            token_id=token_id,
                            reserve_for_solve=True,
                            return_slot_key=True,
                        )
                        reserved_slot_id = slot_id or None

            if resident_info and resident_info.recaptcha_ready and resident_info.tab:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 从共享常驻标签页即时生成 token (slot={slot_id}, project={project_id}, action={action})..."
                )
                runtime_recovered = False
                try:
                    token = await self._solve_with_resident_tab(
                        slot_id,
                        project_id,
                        resident_info,
                        action,
                        consume_reservation=True,
                        success_label="resident_solve",
                    )
                    reserved_slot_id = None
                    if token:
                        return finish_result(token, slot_id)
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 共享标签页生成失败 (slot={slot_id}, project={project_id}, token_id={token_id})，尝试重建..."
                    )
                    await self._mark_resident_slot_unavailable(
                        slot_id,
                        resident_info,
                        reason=f"resident_solve_empty:{project_id}",
                    )
                except Exception as e:
                    reserved_slot_id = None
                    debug_logger.log_warning(f"[BrowserCaptcha] 共享标签页异常 (slot={slot_id}): {e}，尝试重建...")
                    await self._mark_resident_slot_unavailable(
                        slot_id,
                        resident_info,
                        reason=f"resident_solve_error:{project_id}",
                    )
                    if self._is_browser_runtime_error(e):
                        runtime_recovered = await self._recover_browser_runtime(
                            project_id,
                            reason=f"resident_solve:{slot_id}",
                        )
                        if runtime_recovered:
                            slot_id, resident_info = await self._ensure_resident_tab(
                                project_id,
                                token_id=token_id,
                                reserve_for_solve=True,
                                return_slot_key=True,
                            )
                            reserved_slot_id = slot_id or None
                            if resident_info and slot_id:
                                try:
                                    token = await self._solve_with_resident_tab(
                                        slot_id,
                                        project_id,
                                        resident_info,
                                        action,
                                        consume_reservation=True,
                                        success_label="resident_solve_after_runtime_recover",
                                    )
                                    reserved_slot_id = None
                                    if token:
                                        return finish_result(token, slot_id)
                                except Exception as retry_error:
                                    reserved_slot_id = None
                                    debug_logger.log_warning(
                                        f"[BrowserCaptcha] 浏览器重启恢复后共享标签页仍失败 (slot={slot_id}): {retry_error}"
                                    )

                if not runtime_recovered:
                    await release_reserved_slot()
                    slot_id, resident_info = await self._rebuild_resident_tab(
                        project_id,
                        token_id=token_id,
                        slot_id=slot_id,
                        reserve_for_solve=True,
                        return_slot_key=True,
                    )
                    reserved_slot_id = slot_id or None
                    if resident_info is None:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 共享标签页重建返回空，升级为浏览器级恢复 (slot={slot_id}, project={project_id}, token_id={token_id})"
                        )
                        if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild_empty:{slot_id or 'unknown'}"):
                            slot_id, resident_info = await self._ensure_resident_tab(
                                project_id,
                                token_id=token_id,
                                reserve_for_solve=True,
                                return_slot_key=True,
                            )
                            reserved_slot_id = slot_id or None

                    if resident_info:
                        needs_secondary_rebuild = False
                        try:
                            token = await self._solve_with_resident_tab(
                                slot_id,
                                project_id,
                                resident_info,
                                action,
                                consume_reservation=True,
                                success_label="resident_resolve_after_rebuild",
                            )
                            reserved_slot_id = None
                            if token:
                                debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Token生成成功 (slot={slot_id})")
                                return finish_result(token, slot_id)
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 重建标签页后未拿到 token (slot={slot_id})，准备执行二次恢复"
                            )
                            needs_secondary_rebuild = True
                        except Exception as rebuild_error:
                            reserved_slot_id = None
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 重建标签页后仍无法打码 (slot={slot_id}): {rebuild_error}"
                            )
                            needs_secondary_rebuild = True
                            if self._is_browser_runtime_error(rebuild_error):
                                if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild:{slot_id}"):
                                    slot_id, resident_info = await self._ensure_resident_tab(
                                        project_id,
                                        token_id=token_id,
                                        reserve_for_solve=True,
                                        return_slot_key=True,
                                    )
                                    reserved_slot_id = slot_id or None
                                    if resident_info and slot_id:
                                        try:
                                            token = await self._solve_with_resident_tab(
                                                slot_id,
                                                project_id,
                                                resident_info,
                                                action,
                                                consume_reservation=True,
                                                success_label="resident_resolve_after_browser_restart",
                                            )
                                            reserved_slot_id = None
                                            if token:
                                                return finish_result(token, slot_id)
                                        except Exception as restart_error:
                                            reserved_slot_id = None
                                            debug_logger.log_warning(
                                                f"[BrowserCaptcha] 浏览器重启后 resident 仍失败 (slot={slot_id}): {restart_error}"
                                            )
                        if needs_secondary_rebuild and slot_id and resident_info:
                            await self._mark_resident_slot_unavailable(
                                slot_id,
                                resident_info,
                                reason=f"resident_rebuild_retry:{project_id}",
                            )
                            debug_logger.log_info(
                                f"[BrowserCaptcha] 重建标签页仍未恢复，开始二次重建 (slot={slot_id}, project={project_id}, token_id={token_id})"
                            )
                            await release_reserved_slot()
                            slot_id, resident_info = await self._rebuild_resident_tab(
                                project_id,
                                token_id=token_id,
                                slot_id=slot_id,
                                reserve_for_solve=True,
                                return_slot_key=True,
                            )
                            reserved_slot_id = slot_id or None
                            if resident_info and slot_id:
                                try:
                                    token = await self._solve_with_resident_tab(
                                        slot_id,
                                        project_id,
                                        resident_info,
                                        action,
                                        consume_reservation=True,
                                        success_label="resident_resolve_after_second_rebuild",
                                    )
                                    reserved_slot_id = None
                                    if token:
                                        debug_logger.log_info(
                                            f"[BrowserCaptcha] ✅ 二次重建后 Token生成成功 (slot={slot_id})"
                                        )
                                        return finish_result(token, slot_id)
                                except Exception as second_rebuild_error:
                                    reserved_slot_id = None
                                    debug_logger.log_warning(
                                        f"[BrowserCaptcha] 二次重建后 resident 仍失败 (slot={slot_id}): {second_rebuild_error}"
                                    )
                    elif not await self._probe_browser_runtime():
                        if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild_empty:{slot_id}"):
                            slot_id, resident_info = await self._ensure_resident_tab(
                                project_id,
                                token_id=token_id,
                                reserve_for_solve=True,
                                return_slot_key=True,
                            )
                            reserved_slot_id = slot_id or None
                            if resident_info and slot_id:
                                try:
                                    token = await self._solve_with_resident_tab(
                                        slot_id,
                                        project_id,
                                        resident_info,
                                        action,
                                        consume_reservation=True,
                                        success_label="resident_resolve_after_empty_recover",
                                    )
                                    reserved_slot_id = None
                                    if token:
                                        return finish_result(token, slot_id)
                                except Exception as empty_recover_error:
                                    reserved_slot_id = None
                                    debug_logger.log_warning(
                                        f"[BrowserCaptcha] 浏览器空恢复后 resident 仍失败 (slot={slot_id}): {empty_recover_error}"
                                    )

            debug_logger.log_warning(
                f"[BrowserCaptcha] 所有常驻方式失败，fallback 到传统模式 (project: {project_id}, token_id={token_id})"
            )
            legacy_token = await self._get_token_legacy(project_id, action, token_id=token_id)
            if legacy_token and slot_id:
                self._resident_error_streaks.pop(slot_id, None)
            return finish_result(legacy_token, None)
        finally:
            await release_reserved_slot()

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        return_slot_id: bool = False,
    ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
        """对外暴露统一取 token 接口，保持单实例与池化 worker 行为一致。"""
        return await self._get_token_direct(
            project_id,
            action=action,
            token_id=token_id,
            return_slot_id=return_slot_id,
        )

    async def get_token_with_metadata(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[int]]:
        token, slot_id = await self._get_token_direct(
            project_id,
            action=action,
            token_id=token_id,
            return_slot_id=True,
        )
        if not token:
            return None, None, None
        return token, slot_id, token_id

    async def _create_resident_tab(
        self,
        slot_id: str,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Optional[ResidentTabInfo]:
        """创建一个共享常驻打码标签页

        Args:
            slot_id: 共享标签页槽位 ID
            project_id: 触发创建的项目 ID，仅用于日志和最近映射

        Returns:
            ResidentTabInfo 对象，或 None（创建失败）
        """
        tab = None
        browser_context_id = None
        try:
            debug_logger.log_info(
                f"[BrowserCaptcha] 创建共享常驻标签页 slot={slot_id}, seed_project={project_id}, token_id={token_id}"
            )

            # 获取或创建标签页
            browser = self.browser
            if browser is None or getattr(browser, "stopped", False):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建共享常驻标签页前浏览器不可用 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                return None

            debug_logger.log_info(f"[BrowserCaptcha] 创建独立 browser context")
            tab, browser_context_id = await self._create_isolated_context_tab(
                PERSONAL_COOKIE_PREBIND_URL,
                label=f"resident_browser_create_context:{slot_id}",
                create_timeout_seconds=self._navigation_timeout_seconds,
            )
            browser_context_id = browser_context_id or self._extract_tab_browser_context_id(tab)

            # 等待页面加载完成（减少等待时间）
            page_loaded = False
            for retry in range(10):  # 减少到10次，最多5秒
                try:
                    await asyncio.sleep(0.5)
                    ready_state = await self._tab_evaluate(
                        tab,
                        "document.readyState",
                        label=f"resident_document_ready:{slot_id}",
                        timeout_seconds=2.0,
                    )
                    if ready_state == "complete":
                        page_loaded = True
                        debug_logger.log_info(f"[BrowserCaptcha] 页面已加载")
                        break
                except Exception as e:
                    if self._is_browser_runtime_error(e):
                        self._mark_browser_health(False)
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 等待页面时浏览器运行态断开 (slot={slot_id}, project={project_id}, token_id={token_id}): {e}"
                        )
                        raise
                    debug_logger.log_warning(f"[BrowserCaptcha] 等待页面异常: {e}，重试 {retry + 1}/10...")
                    await asyncio.sleep(0.3)  # 减少重试间隔

            if not page_loaded:
                debug_logger.log_error(
                    f"[BrowserCaptcha] 页面加载超时 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
                return None

            resident_info = ResidentTabInfo(
                tab,
                slot_id,
                project_id=project_id,
                token_id=token_id,
                browser_context_id=browser_context_id,
            )

            await self._apply_token_cookie_binding(
                resident_info,
                token_id,
                label=f"resident_init:{slot_id}",
                force=True,
            )

            if not await self._open_labs_bootstrap_page(tab, label=f"resident_init:{slot_id}"):
                debug_logger.log_error(
                    f"[BrowserCaptcha] 打开 labs 引导页失败 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
                return None

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error(
                    f"[BrowserCaptcha] reCAPTCHA 加载失败 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
                return None

            resident_info.recaptcha_ready = True
            resident_info.fingerprint = await self._refresh_last_fingerprint(tab)
            self._mark_browser_health(True)

            debug_logger.log_info(
                f"[BrowserCaptcha] ✅ 共享常驻标签页创建成功 (slot={slot_id}, project={project_id}, token_id={token_id})"
            )
            return resident_info

        except asyncio.CancelledError:
            if tab is not None:
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
            raise
        except Exception as e:
            if tab is not None:
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
            if self._is_browser_runtime_error(e):
                self._mark_browser_health(False)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建共享常驻标签页时浏览器运行态断开 (slot={slot_id}, project={project_id}, token_id={token_id}): {e}"
                )
                raise
            debug_logger.log_error(
                f"[BrowserCaptcha] 创建共享常驻标签页异常 (slot={slot_id}, project={project_id}, token_id={token_id}): {e}"
            )
            return None

    async def _close_resident_tab(self, slot_id: str):
        """关闭指定 slot 的共享常驻标签页

        Args:
            slot_id: 共享标签页槽位 ID
        """
        async with self._resident_lock:
            resident_info = self._resident_tabs.pop(slot_id, None)
            self._forget_token_affinity_for_slot_locked(slot_id)
            self._forget_project_affinity_for_slot_locked(slot_id)
            self._resident_error_streaks.pop(slot_id, None)
            self._clear_resident_slot_unavailable_locked(slot_id)
            self._resident_rebuild_tasks.pop(slot_id, None)
            self._resident_recovery_tasks.pop(slot_id, None)
            self._sync_compat_resident_state()

        if resident_info and resident_info.tab:
            try:
                await self._dispose_browser_context_quietly(resident_info.browser_context_id)
                await self._close_tab_quietly(resident_info.tab)
                debug_logger.log_info(f"[BrowserCaptcha] 已关闭共享常驻标签页 slot={slot_id}")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 关闭标签页时异常: {e}")

    async def invalidate_token(self, project_id: str):
        """当检测到 token 无效时调用，重建当前项目最近映射的共享标签页。

        Args:
            project_id: 项目 ID
        """
        debug_logger.log_warning(
            f"[BrowserCaptcha] Token 被标记为无效 (project: {project_id})，仅重建共享池中的对应标签页，避免清空全局浏览器状态"
        )

        # 重建标签页
        slot_id, resident_info = await self._rebuild_resident_tab(project_id, return_slot_key=True)
        if resident_info and slot_id:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 标签页已重建 (project: {project_id}, slot={slot_id})")
        else:
            debug_logger.log_error(f"[BrowserCaptcha] 标签页重建失败 (project: {project_id})")

    async def _get_token_legacy(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        *,
        token_id: Optional[int] = None,
    ) -> Optional[str]:
        """传统模式获取 reCAPTCHA token（每次创建新标签页）

        Args:
            project_id: Flow项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        max_attempts = 2
        async with self._legacy_lock:
            for attempt in range(max_attempts):
                if not self._initialized or not self.browser:
                    await self.initialize()

                start_time = time.time()
                tab = None
                browser_context_id = None

                try:
                    debug_logger.log_info(
                        "[BrowserCaptcha] [Legacy] 创建独立临时 context 执行验证，"
                        "先绑 cookie 再首跳 labs.google，避免首轮请求丢登录态"
                    )
                    tab, browser_context_id = await self._create_isolated_context_tab(
                        PERSONAL_COOKIE_PREBIND_URL,
                        label=f"legacy_browser_create_context:{project_id}",
                        create_timeout_seconds=self._navigation_timeout_seconds,
                    )
                    browser_context_id = browser_context_id or self._extract_tab_browser_context_id(tab)
                    legacy_info = ResidentTabInfo(
                        tab,
                        slot_id=f"legacy-{project_id}",
                        project_id=project_id,
                        token_id=token_id,
                        browser_context_id=browser_context_id,
                    )
                    await self._apply_token_cookie_binding(
                        legacy_info,
                        token_id,
                        label=f"legacy:{project_id}",
                        force=True,
                    )

                    if not await self._open_labs_bootstrap_page(tab, label=f"legacy:{project_id}"):
                        debug_logger.log_error("[BrowserCaptcha] [Legacy] 打开 labs 引导页失败")
                        return None

                    # 等待 reCAPTCHA 加载
                    recaptcha_ready = await self._wait_for_recaptcha(tab)

                    if not recaptcha_ready:
                        debug_logger.log_error("[BrowserCaptcha] [Legacy] reCAPTCHA 无法加载")
                        return None

                    # 执行 reCAPTCHA
                    debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 执行 reCAPTCHA 验证 (action: {action})...")
                    token = await self._run_with_timeout(
                        self._execute_recaptcha_on_tab(tab, action),
                        timeout_seconds=self._solve_timeout_seconds,
                        label=f"legacy_solve:{project_id}:{action}",
                    )

                    duration_ms = (time.time() - start_time) * 1000

                    if token:
                        browser_solve_count = self._record_browser_solve_success(
                            source="legacy",
                            project_id=project_id,
                        )
                        self._mark_browser_health(True)
                        await self._refresh_last_fingerprint(tab)
                        debug_logger.log_info(
                            "[BrowserCaptcha] [Legacy] ✅ Token获取成功"
                            f"（耗时 {duration_ms:.0f}ms, browser_solve_count={browser_solve_count}）"
                        )
                        await self._maybe_execute_pending_fresh_profile_restart(
                            project_id,
                            token_id=token_id,
                            source="legacy_solve_success",
                        )
                        return token

                    debug_logger.log_error("[BrowserCaptcha] [Legacy] Token获取失败（返回null）")
                    return None

                except Exception as e:
                    if attempt < (max_attempts - 1) and self._is_browser_runtime_error(e):
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] [Legacy] 浏览器运行态异常，尝试重启恢复后重试: {e}"
                        )
                        await self._recover_browser_runtime(project_id, reason=f"legacy_attempt_{attempt + 1}")
                        continue

                    debug_logger.log_error(f"[BrowserCaptcha] [Legacy] 获取token异常: {str(e)}")
                    return None
                finally:
                    # 关闭 legacy 临时标签页（但保留浏览器）
                    if tab:
                        await self._dispose_browser_context_quietly(browser_context_id)
                        await self._close_tab_quietly(tab)

        return None

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        """返回最近一次打码时的浏览器指纹快照。"""
        if not self._last_fingerprint:
            return None
        return dict(self._last_fingerprint)

    async def _clear_browser_cache(self):
        """清理浏览器全部缓存"""
        if not self.browser:
            return

        try:
            from nodriver import cdp

            debug_logger.log_info("[BrowserCaptcha] 开始清理浏览器缓存...")

            # 使用 Chrome DevTools Protocol 清理缓存
            # 清理所有类型的缓存数据
            await self._browser_send_command(
                cdp.network.clear_browser_cache(),
                label="clear_browser_cache",
            )

            # 清理 Cookies
            await self._browser_send_command(
                cdp.network.clear_browser_cookies(),
                label="clear_browser_cookies",
            )

            # 清理关键站点存储数据（localStorage, sessionStorage, IndexedDB, SW 等）
            origins = (
                "https://www.google.com",
                "https://www.recaptcha.net",
                "https://labs.google",
            )
            for origin in origins:
                try:
                    await self._browser_send_command(
                        cdp.storage.clear_data_for_origin(
                            origin=origin,
                            storage_types="all",
                        ),
                        label=f"clear_browser_origin_storage:{origin}",
                    )
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 清理 origin 存储失败: origin={origin}, error={e}"
                    )

            debug_logger.log_info("[BrowserCaptcha] ✅ 浏览器缓存已清理")

        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 清理缓存时异常: {e}")

    async def _shutdown_browser_runtime(self, cancel_idle_reaper: bool = False, reason: str = "shutdown"):
        if cancel_idle_reaper and self._idle_reaper_task and not self._idle_reaper_task.done():
            self._idle_reaper_task.cancel()
            try:
                await self._idle_reaper_task
            except asyncio.CancelledError:
                pass
            finally:
                self._idle_reaper_task = None

        async with self._browser_lock:
            try:
                await self._shutdown_browser_runtime_locked(reason=reason)
                debug_logger.log_info(f"[BrowserCaptcha] 浏览器运行态已清理 ({reason})")
            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] 清理浏览器运行态异常 ({reason}): {str(e)}")

    async def close(self):
        """关闭浏览器"""
        await self._shutdown_browser_runtime(cancel_idle_reaper=True, reason="service_close")

    async def open_login_window(self):
        """打开登录窗口供用户手动登录 Google"""
        await self.initialize()
        self._mark_runtime_active()
        tab = await self._open_visible_browser_tab(
            "https://accounts.google.com/",
            label="open_login_window",
        )
        debug_logger.log_info("[BrowserCaptcha] 请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")
        print("请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")

    # ========== Session Token 刷新 ==========

    async def refresh_session_token(self, project_id: str, token_id: Optional[int] = None) -> Optional[str]:
        """从常驻标签页获取最新的 Session Token
        
        复用共享打码标签页，通过刷新页面并从 cookies 中提取
        __Secure-next-auth.session-token
        
        Args:
            project_id: 项目ID，用于定位常驻标签页
            
        Returns:
            新的 Session Token，如果获取失败返回 None
        """
        for attempt in range(2):
            self._mark_runtime_active()
            # 确保浏览器已初始化
            await self.initialize()

            start_time = time.time()
            debug_logger.log_info(
                f"[BrowserCaptcha] 开始刷新 Session Token (project: {project_id}, token_id={token_id}, attempt={attempt + 1})..."
            )

            async with self._resident_lock:
                slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                    project_id,
                    token_id=token_id,
                )

            if resident_info is None or not slot_id:
                slot_id, resident_info = await self._ensure_resident_tab(
                    project_id,
                    token_id=token_id,
                    return_slot_key=True,
                )

            if resident_info is None or not slot_id:
                if attempt == 0 and not await self._probe_browser_runtime():
                    await self._recover_browser_runtime(project_id, reason="refresh_session_prepare")
                    continue
                debug_logger.log_warning(f"[BrowserCaptcha] 无法为 project_id={project_id} 获取共享常驻标签页")
                return None

            if not resident_info or not resident_info.tab:
                debug_logger.log_error(f"[BrowserCaptcha] 无法获取常驻标签页")
                return None

            if not await self._ensure_resident_token_binding(
                resident_info,
                token_id,
                label=f"refresh_session:{slot_id}",
            ):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 刷新 Session Token 前 cookie 绑定未就绪，尝试重建 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                slot_id, resident_info = await self._rebuild_resident_tab(
                    project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    return_slot_key=True,
                )
                if not resident_info or not slot_id or not resident_info.tab:
                    if attempt == 0 and not await self._probe_browser_runtime():
                        await self._recover_browser_runtime(project_id, reason="refresh_session_rebuild_cookie_binding")
                        continue
                    return None

            tab = resident_info.tab

            try:
                async with resident_info.solve_lock:
                    # 刷新页面以获取最新的 cookies
                    debug_logger.log_info(f"[BrowserCaptcha] 刷新常驻标签页以获取最新 cookies...")
                    resident_info.recaptcha_ready = False
                    await self._run_with_timeout(
                        self._tab_reload(
                            tab,
                            label=f"refresh_session_reload:{slot_id}",
                        ),
                        timeout_seconds=self._session_refresh_timeout_seconds,
                        label=f"refresh_session_reload_total:{slot_id}",
                    )

                    # 等待页面加载完成
                    for _ in range(30):
                        await asyncio.sleep(1)
                        try:
                            ready_state = await self._tab_evaluate(
                                tab,
                                "document.readyState",
                                label=f"refresh_session_ready_state:{slot_id}",
                                timeout_seconds=2.0,
                            )
                            if ready_state == "complete":
                                break
                        except Exception:
                            pass

                    resident_info.recaptcha_ready = await self._wait_for_recaptcha(tab)
                    if not resident_info.recaptcha_ready:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 刷新 Session Token 后 reCAPTCHA 未恢复就绪 (slot={slot_id})"
                        )

                    # 额外等待确保 cookies 已设置
                    await asyncio.sleep(2)

                    # 从 cookies 中提取 __Secure-next-auth.session-token
                    session_token = None

                    try:
                        cookies = await self._get_browser_cookies(
                            label=f"refresh_session_get_cookies:{slot_id}",
                            browser_context_id=resident_info.browser_context_id,
                        )

                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                session_token = cookie.value
                                break

                    except Exception as e:
                        debug_logger.log_warning(f"[BrowserCaptcha] 通过 cookies API 获取失败: {e}，尝试从 document.cookie 获取...")

                        try:
                            all_cookies = await self._tab_evaluate(
                                tab,
                                "document.cookie",
                                label=f"refresh_session_document_cookie:{slot_id}",
                            )
                            if all_cookies:
                                for part in all_cookies.split(";"):
                                    part = part.strip()
                                    if part.startswith("__Secure-next-auth.session-token="):
                                        session_token = part.split("=", 1)[1]
                                        break
                        except Exception as e2:
                            debug_logger.log_error(f"[BrowserCaptcha] document.cookie 获取失败: {e2}")

                duration_ms = (time.time() - start_time) * 1000

                if session_token:
                    resident_info.last_used_at = time.time()
                    self._remember_project_affinity(project_id, slot_id, resident_info)
                    self._remember_token_affinity(token_id, slot_id, resident_info)
                    self._resident_error_streaks.pop(slot_id, None)
                    self._mark_browser_health(True)
                    debug_logger.log_info(f"[BrowserCaptcha] ✅ Session Token 获取成功（耗时 {duration_ms:.0f}ms）")
                    return session_token

                debug_logger.log_error(f"[BrowserCaptcha] ❌ 未找到 __Secure-next-auth.session-token cookie")
                return None

            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] 刷新 Session Token 异常: {str(e)}")

                if attempt == 0 and self._is_browser_runtime_error(e):
                    if await self._recover_browser_runtime(project_id, reason=f"refresh_session:{slot_id}"):
                        continue

                slot_id, resident_info = await self._rebuild_resident_tab(
                    project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    return_slot_key=True,
                )
                if resident_info and slot_id:
                    try:
                        async with resident_info.solve_lock:
                            cookies = await self._get_browser_cookies(
                                label=f"refresh_session_get_cookies_after_rebuild:{slot_id}",
                                browser_context_id=resident_info.browser_context_id,
                            )
                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                resident_info.last_used_at = time.time()
                                self._remember_project_affinity(project_id, slot_id, resident_info)
                                self._remember_token_affinity(token_id, slot_id, resident_info)
                                self._resident_error_streaks.pop(slot_id, None)
                                self._mark_browser_health(True)
                                debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Session Token 获取成功")
                                return cookie.value
                    except Exception as rebuild_error:
                        if attempt == 0 and self._is_browser_runtime_error(rebuild_error):
                            if await self._recover_browser_runtime(project_id, reason=f"refresh_session_rebuild:{slot_id}"):
                                continue

                return None

        return None

    async def warmup_resident_tabs(
        self,
        project_ids: Optional[list[str]] = None,
        limit: int = 1,
    ) -> list[Optional[str]]:
        """启动时预热共享常驻标签页。

        对每个 project_id 调用 _ensure_resident_tab 创建标签页，
        达到 limit 数量后停止。返回已预热的 slot_id 列表。
        """
        if not project_ids:
            return []
        warmed: list[Optional[str]] = []
        for pid in project_ids:
            if len(warmed) >= limit:
                break
            try:
                _slot_id, _info = await self._ensure_resident_tab(
                    project_id=pid,
                    force_create=False,
                    return_slot_key=True,
                )
                if _slot_id:
                    warmed.append(_slot_id)
            except Exception:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] warmup_resident_tabs 预热 project={pid} 失败",
                )
        return warmed

    # ========== 状态查询 ==========

    def is_resident_mode_active(self) -> bool:
        """检查是否有任何常驻标签页激活"""
        return len(self._resident_tabs) > 0 or self._running

    def get_resident_count(self) -> int:
        """获取当前常驻标签页数量"""
        return len(self._resident_tabs)

    def get_resident_project_ids(self) -> list[str]:
        """获取所有当前共享常驻标签页的 slot_id 列表。"""
        return list(self._resident_tabs.keys())

    def get_resident_project_id(self) -> Optional[str]:
        """获取当前共享池中的第一个 slot_id（向后兼容）。"""
        if self._resident_tabs:
            return next(iter(self._resident_tabs.keys()))
        return self.resident_project_id

    def get_token_pool_status(self) -> Dict[str, Any]:
        return {
            "token_pool_enabled": bool(getattr(config, "token_pool_enabled", False)),
            "token_pool_status": "未启用" if not getattr(config, "token_pool_enabled", False) else "空闲",
            "token_pool_total_ready": 0,
            "token_pool_bucket_count": 0,
            "token_pool_waiting_requests": 0,
            "token_pool_refill_inflight": 0,
            "token_pool_last_refill_at": None,
            "token_pool_last_token_at": None,
            "token_pool_oldest_token_age_seconds": None,
            "token_pool_next_expire_in_seconds": None,
            "token_pool_hit_count": 0,
            "token_pool_miss_count": 0,
            "token_pool_wait_count": 0,
            "token_pool_expired_count": 0,
        }

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """为任意站点执行 reCAPTCHA，用于分数测试等场景。

        与普通 legacy 模式不同，这里会复用同一个常驻标签页，避免每次冷启动新 tab。
        """
        await self.initialize()
        self._mark_runtime_active()
        self._last_fingerprint = None

        cache_key = f"{website_url}|{website_key}|{1 if enterprise else 0}"
        warmup_seconds = float(getattr(config, "browser_score_test_warmup_seconds", 12) or 12)
        per_request_settle_seconds = float(
            getattr(config, "browser_score_test_settle_seconds", 2.5) or 2.5
        )
        max_retries = 2

        async with self._custom_lock:
            for attempt in range(max_retries):
                start_time = time.time()
                custom_info = self._custom_tabs.get(cache_key)
                tab = custom_info.get("tab") if isinstance(custom_info, dict) else None

                try:
                    if tab is None:
                        debug_logger.log_info(f"[BrowserCaptcha] [Custom] 创建常驻测试标签页: {website_url}")
                        tab = await self._browser_get(
                            website_url,
                            label="custom_browser_get",
                            new_tab=True,
                        )
                        custom_info = {
                            "tab": tab,
                            "recaptcha_ready": False,
                            "warmed_up": False,
                            "created_at": time.time(),
                        }
                        self._custom_tabs[cache_key] = custom_info

                    page_loaded = False
                    for _ in range(20):
                        ready_state = await self._tab_evaluate(
                            tab,
                            "document.readyState",
                            label="custom_document_ready",
                            timeout_seconds=2.0,
                        )
                        if ready_state == "complete":
                            page_loaded = True
                            break
                        await tab.sleep(0.5)

                    if not page_loaded:
                        raise RuntimeError("自定义页面加载超时")

                    if not custom_info.get("recaptcha_ready"):
                        recaptcha_ready = await self._wait_for_custom_recaptcha(
                            tab=tab,
                            website_key=website_key,
                            enterprise=enterprise,
                        )
                        if not recaptcha_ready:
                            raise RuntimeError("自定义 reCAPTCHA 无法加载")
                        custom_info["recaptcha_ready"] = True

                    try:
                        await self._tab_evaluate(tab, """
                            (() => {
                                try {
                                    const body = document.body || document.documentElement;
                                    const width = window.innerWidth || 1280;
                                    const height = window.innerHeight || 720;
                                    const x = Math.max(24, Math.floor(width * 0.38));
                                    const y = Math.max(24, Math.floor(height * 0.32));
                                    const moveEvent = new MouseEvent('mousemove', {
                                        bubbles: true,
                                        clientX: x,
                                        clientY: y
                                    });
                                    const overEvent = new MouseEvent('mouseover', {
                                        bubbles: true,
                                        clientX: x,
                                        clientY: y
                                    });
                                    window.focus();
                                    window.dispatchEvent(new Event('focus'));
                                    document.dispatchEvent(moveEvent);
                                    document.dispatchEvent(overEvent);
                                    if (body) {
                                        body.dispatchEvent(moveEvent);
                                        body.dispatchEvent(overEvent);
                                    }
                                    window.scrollTo(0, Math.min(320, document.body?.scrollHeight || 320));
                                } catch (e) {}
                            })()
                        """, label="custom_pre_warm_interaction", timeout_seconds=6.0)
                    except Exception:
                        pass

                    if not custom_info.get("warmed_up"):
                        if warmup_seconds > 0:
                            debug_logger.log_info(
                                f"[BrowserCaptcha] [Custom] 首次预热测试页面 {warmup_seconds:.1f}s 后再执行 token"
                            )
                            try:
                                await self._tab_evaluate(tab, """
                                    (() => {
                                        try {
                                            window.scrollTo(0, Math.min(240, document.body.scrollHeight || 240));
                                            window.dispatchEvent(new Event('mousemove'));
                                            window.dispatchEvent(new Event('focus'));
                                        } catch (e) {}
                                    })()
                                """, label="custom_warmup_interaction", timeout_seconds=6.0)
                            except Exception:
                                pass
                            await tab.sleep(warmup_seconds)
                        custom_info["warmed_up"] = True
                    elif per_request_settle_seconds > 0:
                        debug_logger.log_info(
                            f"[BrowserCaptcha] [Custom] 复用测试标签页，执行前额外等待 {per_request_settle_seconds:.1f}s"
                        )
                        await tab.sleep(per_request_settle_seconds)

                    debug_logger.log_info(f"[BrowserCaptcha] [Custom] 使用常驻测试标签页执行验证 (action: {action})...")
                    token = await self._execute_custom_recaptcha_on_tab(
                        tab=tab,
                        website_key=website_key,
                        action=action,
                        enterprise=enterprise,
                    )

                    duration_ms = (time.time() - start_time) * 1000
                    if token:
                        extracted_fingerprint = await self._extract_tab_fingerprint(tab)
                        if not extracted_fingerprint:
                            try:
                                fallback_ua = await self._tab_evaluate(
                                    tab,
                                    "navigator.userAgent || ''",
                                    label="custom_fallback_ua",
                                )
                                fallback_lang = await self._tab_evaluate(
                                    tab,
                                    "navigator.language || ''",
                                    label="custom_fallback_lang",
                                )
                                extracted_fingerprint = {
                                    "user_agent": fallback_ua or "",
                                    "accept_language": fallback_lang or "",
                                    "proxy_url": self._proxy_url,
                                }
                            except Exception:
                                extracted_fingerprint = None
                        self._last_fingerprint = extracted_fingerprint
                        debug_logger.log_info(
                            f"[BrowserCaptcha] [Custom] ✅ 常驻测试标签页 Token获取成功（耗时 {duration_ms:.0f}ms）"
                        )
                        return token

                    raise RuntimeError("自定义 token 获取失败（返回 null）")
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] [Custom] 尝试 {attempt + 1}/{max_retries} 失败: {str(e)}"
                    )
                    stale_info = self._custom_tabs.pop(cache_key, None)
                    stale_tab = stale_info.get("tab") if isinstance(stale_info, dict) else None
                    if stale_tab:
                        await self._close_tab_quietly(stale_tab)
                    if attempt >= max_retries - 1:
                        debug_logger.log_error(f"[BrowserCaptcha] [Custom] 获取token异常: {str(e)}")
                        return None

            return None

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        """在同一个常驻标签页里获取 token 并直接校验页面分数。"""
        self._mark_runtime_active()
        token_started_at = time.time()
        token = await self.get_custom_token(
            website_url=website_url,
            website_key=website_key,
            action=action,
            enterprise=enterprise,
        )
        token_elapsed_ms = int((time.time() - token_started_at) * 1000)

        if not token:
            return {
                "token": None,
                "token_elapsed_ms": token_elapsed_ms,
                "verify_mode": "browser_page",
                "verify_elapsed_ms": 0,
                "verify_http_status": None,
                "verify_result": {},
            }

        cache_key = f"{website_url}|{website_key}|{1 if enterprise else 0}"
        async with self._custom_lock:
            custom_info = self._custom_tabs.get(cache_key)
            tab = custom_info.get("tab") if isinstance(custom_info, dict) else None
            if tab is None:
                raise RuntimeError("页面分数测试标签页不存在")
            verify_payload = await self._verify_score_on_tab(tab, token, verify_url)

        return {
            "token": token,
            "token_elapsed_ms": token_elapsed_ms,
            **verify_payload,
        }


class _PersonalBrowserPoolService:
    """多浏览器实例调度层。保留现有单浏览器 worker 逻辑，只负责分发与扩缩容。"""

    def __init__(self, db=None):
        self.db = db
        self.headless = bool(getattr(config, "personal_headless", False))
        self._closing = False
        self._workers: list[BrowserCaptchaService] = []
        self._worker_tab_limits: list[int] = []
        self._reload_lock = asyncio.Lock()
        self._worker_dispatch_lock = asyncio.Lock()
        self._round_robin_index = 0
        self._worker_dispatch_reservations: dict[int, int] = {}
        self._project_worker_affinity: dict[str, int] = {}
        self._token_worker_affinity: dict[str, int] = {}
        self._affinity_cache_limit = 256
        self._last_successful_worker_index: Optional[int] = None
        self._idle_worker_reaper_task: Optional[asyncio.Task] = None
        self._token_pool_lock = asyncio.Lock()
        self._token_pool_queues: dict[str, deque[TokenPoolLease]] = {}
        self._token_pool_conditions: dict[str, asyncio.Condition] = {}
        self._token_pool_waiters: dict[str, int] = {}
        self._token_pool_bucket_meta: dict[str, Dict[str, Any]] = {}
        self._token_pool_refill_inflight: dict[str, int] = {}
        self._token_pool_fill_tasks: set[asyncio.Task] = set()
        self._token_pool_maintainer_task: Optional[asyncio.Task] = None
        self._token_pool_last_refill_at = 0.0
        self._token_pool_last_token_at = 0.0
        self._token_pool_stats: dict[str, int] = {
            "hit_count": 0,
            "miss_count": 0,
            "wait_count": 0,
            "expired_count": 0,
            "produced_count": 0,
            "served_count": 0,
            "dropped_count": 0,
        }

    @staticmethod
    def _format_status_timestamp(timestamp_value: float) -> Optional[str]:
        if timestamp_value <= 0:
            return None
        try:
            return datetime.fromtimestamp(timestamp_value).isoformat(timespec="seconds")
        except Exception:
            return None

    def _is_token_pool_enabled(self) -> bool:
        return bool(getattr(config, "token_pool_enabled", False))

    def _get_token_pool_target_size(self) -> int:
        try:
            return max(1, min(TOKEN_POOL_SIZE_MAX, int(getattr(config, "token_pool_size", 2) or 2)))
        except Exception:
            return 2

    def _get_token_pool_seed_project_id(self) -> str:
        return self._normalize_project_key(getattr(config, "token_pool_seed_project_id", "") or "")

    def _get_token_pool_image_target_size(self) -> int:
        try:
            return max(0, min(TOKEN_POOL_SIZE_MAX, int(getattr(config, "token_pool_image_size", 0) or 0)))
        except Exception:
            return 0

    def _get_token_pool_video_target_size(self) -> int:
        try:
            return max(0, min(TOKEN_POOL_SIZE_MAX, int(getattr(config, "token_pool_video_size", 0) or 0)))
        except Exception:
            return 0

    def _get_token_pool_bucket_target_size(
        self,
        *,
        project_id: Optional[str],
        action: Optional[str],
    ) -> int:
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        image_target_size = self._get_token_pool_image_target_size()
        video_target_size = self._get_token_pool_video_target_size()
        default_target_size = self._get_token_pool_target_size()

        if normalized_action == "IMAGE_GENERATION":
            if image_target_size > 0:
                return image_target_size
            if video_target_size <= 0:
                return default_target_size
            return 0
        if normalized_action == "VIDEO_GENERATION":
            if video_target_size > 0:
                return video_target_size
            if image_target_size <= 0:
                return default_target_size
            return 0

        return default_target_size

    def _get_token_pool_wait_timeout_seconds(self) -> float:
        try:
            return float(max(1, min(300, int(getattr(config, "token_pool_wait_timeout_seconds", 30) or 30))))
        except Exception:
            return 30.0

    def _get_token_pool_refill_parallelism(self, target_size: Optional[int] = None) -> int:
        try:
            configured_browser_count = BrowserCaptchaService._resolve_configured_browser_count()
        except Exception:
            configured_browser_count = 1

        per_worker_tabs = self._resolve_worker_resident_tabs()
        worker_limits = self._worker_tab_limits or self._build_worker_tab_limits(
            per_worker_tabs,
            configured_browser_count,
        )
        refill_capacity = max(
            1,
            sum(max(0, int(limit or 0)) for limit in worker_limits),
        )
        if target_size is None:
            return refill_capacity
        return max(1, min(max(1, int(target_size)), refill_capacity))

    def _get_token_pool_bucket_keepalive_seconds(self) -> float:
        return max(float(getattr(config, "token_pool_ttl_seconds", 120) or 120) * 2.0, 300.0)

    def _register_configured_token_pool_buckets_locked(self, *, now_value: float) -> None:
        seed_project_id = self._get_token_pool_seed_project_id()
        if not seed_project_id:
            return

        for action in ("IMAGE_GENERATION", "VIDEO_GENERATION"):
            if self._get_token_pool_bucket_target_size(project_id=seed_project_id, action=action) <= 0:
                continue
            self._register_token_pool_bucket_locked(
                bucket_key=self._build_token_pool_bucket_key(
                    project_id=seed_project_id,
                    action=action,
                    token_id=None,
                ),
                project_id=seed_project_id,
                action=action,
                token_id=None,
                now_value=now_value,
            )

    def _build_token_pool_bucket_key(
        self,
        *,
        project_id: str,
        action: str,
        token_id: Optional[int],
    ) -> str:
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        return normalized_action

    def _get_token_pool_condition_locked(self, bucket_key: str) -> asyncio.Condition:
        condition = self._token_pool_conditions.get(bucket_key)
        if condition is None:
            condition = asyncio.Condition(self._token_pool_lock)
            self._token_pool_conditions[bucket_key] = condition
        return condition

    def _register_token_pool_bucket_locked(
        self,
        *,
        bucket_key: str,
        project_id: str,
        action: str,
        token_id: Optional[int],
        now_value: float,
    ) -> None:
        self._token_pool_queues.setdefault(bucket_key, deque())
        self._get_token_pool_condition_locked(bucket_key)
        self._token_pool_bucket_meta[bucket_key] = {
            "bucket_key": bucket_key,
            "project_id": self._normalize_project_key(project_id),
            "action": str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION",
            "token_id": token_id,
            "last_requested_at": now_value,
        }

    def _prune_token_pool_bucket_locked(self, bucket_key: str, now_value: float) -> int:
        queue = self._token_pool_queues.get(bucket_key)
        if not queue:
            return 0

        expired_count = 0
        while queue and float(queue[0].expires_at or 0.0) <= now_value:
            queue.popleft()
            expired_count += 1

        if expired_count > 0:
            self._token_pool_stats["expired_count"] += expired_count

        return expired_count

    def _pop_ready_token_pool_lease_locked(
        self,
        bucket_key: str,
        *,
        now_value: float,
    ) -> Optional[TokenPoolLease]:
        self._prune_token_pool_bucket_locked(bucket_key, now_value)
        queue = self._token_pool_queues.get(bucket_key)
        if not queue:
            return None
        try:
            return queue.popleft()
        except IndexError:
            return None

    def _cleanup_token_pool_bucket_locked(self, bucket_key: str) -> None:
        if self._token_pool_queues.get(bucket_key):
            return
        if int(self._token_pool_refill_inflight.get(bucket_key, 0) or 0) > 0:
            return
        if int(self._token_pool_waiters.get(bucket_key, 0) or 0) > 0:
            return
        self._token_pool_queues.pop(bucket_key, None)
        self._token_pool_conditions.pop(bucket_key, None)
        self._token_pool_bucket_meta.pop(bucket_key, None)
        self._token_pool_refill_inflight.pop(bucket_key, None)
        self._token_pool_waiters.pop(bucket_key, None)

    def _summarize_token_pool_bucket(
        self,
        bucket_key: str,
        *,
        now_value: Optional[float] = None,
    ) -> Dict[str, Any]:
        current_time = time.time() if now_value is None else now_value
        meta = self._token_pool_bucket_meta.get(bucket_key) or {}
        queue = self._token_pool_queues.get(bucket_key)
        waiting_requests = int(self._token_pool_waiters.get(bucket_key, 0) or 0)
        refill_inflight = int(self._token_pool_refill_inflight.get(bucket_key, 0) or 0)
        action_name = str(meta.get("action") or bucket_key or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"

        ready_count = 0
        oldest_token_age_seconds: Optional[int] = None
        next_expire_in_seconds: Optional[int] = None
        if queue:
            for lease in list(queue):
                if float(lease.expires_at or 0.0) <= current_time:
                    continue
                ready_count += 1
                age_seconds = max(0, int(current_time - float(lease.created_at or current_time)))
                expire_in_seconds = max(0, int(float(lease.expires_at or current_time) - current_time))
                if oldest_token_age_seconds is None or age_seconds > oldest_token_age_seconds:
                    oldest_token_age_seconds = age_seconds
                if next_expire_in_seconds is None or expire_in_seconds < next_expire_in_seconds:
                    next_expire_in_seconds = expire_in_seconds

        return {
            "bucket_key": bucket_key,
            "action": action_name,
            "ready_count": ready_count,
            "waiting_requests": waiting_requests,
            "refill_inflight": refill_inflight,
            "oldest_token_age_seconds": oldest_token_age_seconds,
            "next_expire_in_seconds": next_expire_in_seconds,
        }

    def _discard_finished_token_pool_task(self, task: asyncio.Task) -> None:
        self._token_pool_fill_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            debug_logger.log_warning(f"[BrowserCaptchaPool] token 池后台补货任务异常: {exc}")

    @property
    def _resident_tabs(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for worker in self._workers:
            merged.update(getattr(worker, "_resident_tabs", {}) or {})
        return merged

    @staticmethod
    def _normalize_project_key(project_id: Optional[str]) -> str:
        return str(project_id or "").strip()

    @staticmethod
    def _normalize_token_key(token_id: Optional[int]) -> str:
        return BrowserCaptchaService._normalize_token_key(token_id)

    @staticmethod
    def _resolve_worker_resident_tabs(limit: Optional[int] = None) -> int:
        raw_value = config.personal_max_resident_tabs if limit is None else limit
        return resolve_effective_personal_max_resident_tabs(raw_value)

    @staticmethod
    def _build_worker_tab_limits(
        per_worker_tabs: int,
        worker_count: int,
        *,
        total_limit: Optional[int] = None,
        allow_zero: bool = False,
    ) -> list[int]:
        normalized_worker_count = resolve_effective_browser_count(worker_count)
        normalized_per_worker_tabs = resolve_effective_personal_max_resident_tabs(per_worker_tabs)
        desired_total = normalized_worker_count * normalized_per_worker_tabs
        effective_total = min(PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS, desired_total)
        if total_limit is not None:
            try:
                effective_total = min(effective_total, max(1, int(total_limit or 1)))
            except Exception:
                pass
        effective_total = max(1, effective_total)

        active_worker_count = min(normalized_worker_count, effective_total)
        base, remainder = divmod(effective_total, active_worker_count)
        limits = [
            base + (1 if index < remainder else 0)
            for index in range(active_worker_count)
        ]
        if allow_zero and len(limits) < normalized_worker_count:
            limits.extend([0] * (normalized_worker_count - len(limits)))
        return limits

    @staticmethod
    def _resolve_effective_pool_tab_capacity(
        *,
        browser_count: Optional[int] = None,
        per_worker_tabs: Optional[int] = None,
    ) -> int:
        resolved_browser_count = resolve_effective_browser_count(
            BrowserCaptchaService._resolve_configured_browser_count()
            if browser_count is None
            else browser_count
        )
        resolved_per_worker_tabs = resolve_effective_personal_max_resident_tabs(
            config.personal_max_resident_tabs if per_worker_tabs is None else per_worker_tabs
        )
        return min(
            PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS,
            resolved_browser_count * resolved_per_worker_tabs,
        )

    @staticmethod
    def _parse_worker_index_from_slot_id(slot_id: Optional[str]) -> Optional[int]:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id.startswith("b"):
            return None
        match = re.match(r"^b(\d+)-", normalized_slot_id)
        if not match:
            return None
        try:
            resolved_index = int(match.group(1)) - 1
        except Exception:
            return None
        return resolved_index if resolved_index >= 0 else None

    def _remember_affinity(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
        worker_index: Optional[int] = None,
    ) -> None:
        resolved_worker_index = worker_index
        if resolved_worker_index is None:
            resolved_worker_index = self._parse_worker_index_from_slot_id(slot_id)
        if resolved_worker_index is None or resolved_worker_index < 0:
            return
        if resolved_worker_index >= len(self._workers):
            return

        normalized_project_key = self._normalize_project_key(project_id)
        if normalized_project_key:
            self._project_worker_affinity[normalized_project_key] = resolved_worker_index
            self._trim_affinity_cache(self._project_worker_affinity)

        normalized_token_key = self._normalize_token_key(token_id)
        if normalized_token_key:
            self._token_worker_affinity[normalized_token_key] = resolved_worker_index
            self._trim_affinity_cache(self._token_worker_affinity)

    def _trim_affinity_cache(self, cache: dict[str, int]) -> None:
        while len(cache) > self._affinity_cache_limit:
            try:
                oldest_key = next(iter(cache))
            except StopIteration:
                return
            cache.pop(oldest_key, None)

    def _cleanup_affinity_maps(self) -> None:
        valid_indexes = set(range(len(self._workers)))
        self._project_worker_affinity = {
            key: value
            for key, value in self._project_worker_affinity.items()
            if value in valid_indexes
        }
        self._token_worker_affinity = {
            key: value
            for key, value in self._token_worker_affinity.items()
            if value in valid_indexes
        }

    def _worker_has_project_mapping(
        self,
        worker: BrowserCaptchaService,
        project_id: Optional[str],
    ) -> bool:
        normalized_project_key = self._normalize_project_key(project_id)
        if not normalized_project_key:
            return False
        if normalized_project_key in (getattr(worker, "_project_resident_affinity", {}) or {}):
            return True
        for resident_info in (getattr(worker, "_resident_tabs", {}) or {}).values():
            if str(getattr(resident_info, "project_id", "") or "").strip() == normalized_project_key:
                return True
        return False

    def _worker_has_token_mapping(
        self,
        worker: BrowserCaptchaService,
        token_id: Optional[int],
    ) -> bool:
        normalized_token_key = self._normalize_token_key(token_id)
        if not normalized_token_key:
            return False
        if normalized_token_key in (getattr(worker, "_token_resident_affinity", {}) or {}):
            return True
        for resident_info in (getattr(worker, "_resident_tabs", {}) or {}).values():
            try:
                if int(getattr(resident_info, "token_id", 0) or 0) == int(normalized_token_key):
                    return True
            except Exception:
                continue
        return False

    def _worker_busy_score(self, worker: BrowserCaptchaService) -> int:
        busy_score = 0
        if getattr(worker, "_browser_lock", None) and worker._browser_lock.locked():
            busy_score += 1
        if getattr(worker, "_legacy_lock", None) and worker._legacy_lock.locked():
            busy_score += 1
        if getattr(worker, "_tab_build_lock", None) and worker._tab_build_lock.locked():
            busy_score += 1
        for resident_info in (getattr(worker, "_resident_tabs", {}) or {}).values():
            try:
                if resident_info.solve_lock.locked():
                    busy_score += 1
                if int(getattr(resident_info, "pending_assignment_count", 0) or 0) > 0:
                    busy_score += 1
            except Exception:
                continue
        return busy_score

    @staticmethod
    def _worker_has_live_runtime(worker: BrowserCaptchaService) -> bool:
        browser_instance = getattr(worker, "browser", None)
        return bool(
            getattr(worker, "_initialized", False)
            and browser_instance
            and not getattr(browser_instance, "stopped", False)
            and not getattr(browser_instance, "_flow2api_runtime_disconnected", False)
        )

    @staticmethod
    def _worker_has_pending_fresh_restart(worker: BrowserCaptchaService) -> bool:
        restart_task = getattr(worker, "_fresh_profile_restart_task", None)
        return bool(
            getattr(worker, "_fresh_profile_restart_pending", False)
            or (restart_task is not None and not restart_task.done())
        )

    def _worker_runtime_unavailable_score(self, worker: BrowserCaptchaService) -> int:
        if self._worker_has_live_runtime(worker):
            return 0
        if self._worker_launch_cooldown_remaining_seconds(worker) > 0.0:
            return 3
        if getattr(worker, "_initialized", False):
            return 2
        return 1

    @staticmethod
    def _worker_launch_cooldown_remaining_seconds(worker: BrowserCaptchaService) -> float:
        try:
            return max(0.0, float(worker._get_browser_launch_cooldown_remaining_seconds() or 0.0))
        except Exception:
            return 0.0

    def _worker_dispatch_score(
        self,
        worker_index: int,
        worker: BrowserCaptchaService,
        *,
        affinity_preferred: bool = False,
    ) -> tuple[int, int, int, int, int, int, int]:
        reservations = int(self._worker_dispatch_reservations.get(worker_index, 0) or 0)
        fresh_restart_penalty = 1 if self._worker_has_pending_fresh_restart(worker) else 0
        runtime_unavailable = self._worker_runtime_unavailable_score(worker)
        launch_cooldown_penalty = 1 if self._worker_launch_cooldown_remaining_seconds(worker) > 0.0 else 0
        busy_score = reservations + self._worker_busy_score(worker)
        resident_cold = 0 if worker.get_resident_count() > 0 else 1
        round_robin_offset = (worker_index - self._round_robin_index) % max(len(self._workers), 1)
        affinity_penalty = 0 if affinity_preferred else 1
        return (
            fresh_restart_penalty,
            busy_score,
            runtime_unavailable,
            launch_cooldown_penalty,
            resident_cold,
            affinity_penalty,
            round_robin_offset,
        )

    def _find_worker_index_for_project(self, project_id: Optional[str]) -> Optional[int]:
        normalized_project_key = self._normalize_project_key(project_id)
        if not normalized_project_key:
            return None

        mapped_index = self._project_worker_affinity.get(normalized_project_key)
        if mapped_index is not None and 0 <= mapped_index < len(self._workers):
            return mapped_index

        for index, worker in enumerate(self._workers):
            if self._worker_has_project_mapping(worker, normalized_project_key):
                self._project_worker_affinity[normalized_project_key] = index
                self._trim_affinity_cache(self._project_worker_affinity)
                return index
        return None

    def _find_worker_index_for_token(self, token_id: Optional[int]) -> Optional[int]:
        normalized_token_key = self._normalize_token_key(token_id)
        if not normalized_token_key:
            return None

        mapped_index = self._token_worker_affinity.get(normalized_token_key)
        if mapped_index is not None and 0 <= mapped_index < len(self._workers):
            if self._worker_has_token_mapping(self._workers[mapped_index], normalized_token_key):
                return mapped_index
            self._token_worker_affinity.pop(normalized_token_key, None)

        for index, worker in enumerate(self._workers):
            if self._worker_has_token_mapping(worker, normalized_token_key):
                self._token_worker_affinity[normalized_token_key] = index
                self._trim_affinity_cache(self._token_worker_affinity)
                return index
        return None

    def _resolve_worker_candidate_indexes(
        self,
        *,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
        allow_affinity: bool = True,
    ) -> list[int]:
        worker_count = len(self._workers)
        if worker_count <= 0:
            return []

        preferred_indexes: list[int] = []
        exact_slot_index = self._parse_worker_index_from_slot_id(slot_id)
        if exact_slot_index is not None and 0 <= exact_slot_index < worker_count:
            preferred_indexes.append(exact_slot_index)

        soft_affinity_indexes = []
        if allow_affinity:
            for candidate in (
                self._find_worker_index_for_token(token_id),
                self._find_worker_index_for_project(project_id),
            ):
                if candidate is None or not (0 <= candidate < worker_count):
                    continue
                if candidate not in preferred_indexes and candidate not in soft_affinity_indexes:
                    soft_affinity_indexes.append(candidate)

        preferred_indexes.extend(soft_affinity_indexes)

        remaining_indexes = [index for index in range(worker_count) if index not in preferred_indexes]
        if remaining_indexes:
            rotation_offset = self._round_robin_index % len(remaining_indexes)
            rotated_indexes = remaining_indexes[rotation_offset:] + remaining_indexes[:rotation_offset]
            scored_indexes = sorted(
                enumerate(rotated_indexes),
                key=lambda item: (
                    self._worker_dispatch_score(
                        item[1],
                        self._workers[item[1]],
                        affinity_preferred=item[1] in soft_affinity_indexes,
                    ),
                    item[0],
                ),
            )
            preferred_indexes.extend(index for _, index in scored_indexes)

        return preferred_indexes

    async def _ensure_idle_worker_reaper(self) -> None:
        if self._closing:
            return
        if self._idle_worker_reaper_task is None or self._idle_worker_reaper_task.done():
            self._idle_worker_reaper_task = asyncio.create_task(self._idle_worker_reaper_loop())

    async def _ensure_token_pool_maintainer(self) -> None:
        if self._closing or not self._is_token_pool_enabled():
            return
        if self._token_pool_maintainer_task is None or self._token_pool_maintainer_task.done():
            self._token_pool_maintainer_task = asyncio.create_task(self._token_pool_maintainer_loop())

    async def _reclaim_pool_memory_pressure(
        self,
        *,
        reason: str,
        exclude_indexes: Optional[set[int]] = None,
    ) -> dict[str, int]:
        excluded = set(exclude_indexes or set())
        reclaimed = {
            "workers_touched": 0,
            "resident_tabs_closed": 0,
            "runtime_shutdown": 0,
            "profiles_deleted": 0,
            "recaptcha_cache_deleted": 0,
            "proxy_extensions_deleted": 0,
            "python_gc_collected": 0,
        }

        async with self._worker_dispatch_lock:
            workers = [
                (worker_index, worker)
                for worker_index, worker in enumerate(self._workers)
                if worker_index not in excluded
            ]

        for worker_index, worker in workers:
            try:
                worker_stats = await worker.reclaim_runtime_memory(
                    reason=f"{reason}:worker-{worker_index + 1}",
                    aggressive=True,
                )
            except Exception as exc:
                debug_logger.log_warning(
                    f"[BrowserCaptchaPool] worker 内存回收失败 (worker={worker_index + 1}, reason={reason}): {exc}"
                )
                continue

            reclaimed["workers_touched"] += 1
            for key in (
                "resident_tabs_closed",
                "runtime_shutdown",
                "profiles_deleted",
                "recaptcha_cache_deleted",
                "proxy_extensions_deleted",
                "python_gc_collected",
            ):
                reclaimed[key] += int(worker_stats.get(key, 0) or 0)

        if any(int(value or 0) > 0 for value in reclaimed.values()):
            debug_logger.log_warning(
                f"[BrowserCaptchaPool] 内存压力回收完成 ({reason}): {reclaimed}"
            )
        return reclaimed

    async def _token_pool_maintainer_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(1.0)
                await self._maintain_token_pool_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                debug_logger.log_warning(f"[BrowserCaptchaPool] token 池维护循环异常: {exc}")

    async def _maintain_token_pool_once(self) -> None:
        spawn_jobs: list[Dict[str, Any]] = []
        if self._closing or not self._is_token_pool_enabled():
            async with self._token_pool_lock:
                self._token_pool_queues.clear()
                self._token_pool_bucket_meta.clear()
                self._token_pool_waiters.clear()
                self._token_pool_refill_inflight.clear()
                self._token_pool_conditions.clear()
            return

        now_value = time.time()
        keepalive_seconds = self._get_token_pool_bucket_keepalive_seconds()
        refill_parallelism = self._get_token_pool_refill_parallelism()

        async with self._token_pool_lock:
            self._register_configured_token_pool_buckets_locked(now_value=now_value)
            bucket_keys = list(
                {
                    *self._token_pool_bucket_meta.keys(),
                    *self._token_pool_queues.keys(),
                    *self._token_pool_refill_inflight.keys(),
                    *self._token_pool_waiters.keys(),
                }
            )
            total_inflight = sum(max(0, int(value or 0)) for value in self._token_pool_refill_inflight.values())

            for bucket_key in bucket_keys:
                self._prune_token_pool_bucket_locked(bucket_key, now_value)
                waiting_count = int(self._token_pool_waiters.get(bucket_key, 0) or 0)
                inflight_count = int(self._token_pool_refill_inflight.get(bucket_key, 0) or 0)
                queue = self._token_pool_queues.get(bucket_key)
                ready_count = len(queue) if queue else 0
                meta = self._token_pool_bucket_meta.get(bucket_key)
                last_requested_at = float((meta or {}).get("last_requested_at") or 0.0)

                if (
                    ready_count <= 0
                    and inflight_count <= 0
                    and waiting_count <= 0
                    and (not last_requested_at or (now_value - last_requested_at) >= keepalive_seconds)
                ):
                    self._cleanup_token_pool_bucket_locked(bucket_key)
                    continue

                if meta is None:
                    continue
                target_size = self._get_token_pool_bucket_target_size(
                    project_id=meta.get("project_id"),
                    action=meta.get("action"),
                )
                if target_size <= 0:
                    if queue:
                        self._token_pool_stats["dropped_count"] += len(queue)
                        queue.clear()
                    if inflight_count <= 0 and waiting_count <= 0:
                        self._cleanup_token_pool_bucket_locked(bucket_key)
                    continue
                if ready_count + inflight_count >= target_size:
                    continue

                while total_inflight < refill_parallelism and (ready_count + inflight_count) < target_size:
                    self._token_pool_refill_inflight[bucket_key] = inflight_count + 1
                    inflight_count += 1
                    total_inflight += 1
                    spawn_jobs.append(dict(meta))

        for job in spawn_jobs:
            if self._closing:
                break
            task = asyncio.create_task(self._token_pool_refill_once(job))
            self._token_pool_fill_tasks.add(task)
            task.add_done_callback(self._discard_finished_token_pool_task)

    async def _token_pool_refill_once(self, bucket_meta: Dict[str, Any]) -> None:
        bucket_key = str(bucket_meta.get("bucket_key") or "").strip()
        project_id = str(bucket_meta.get("project_id") or "").strip()
        action = str(bucket_meta.get("action") or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        token_id = bucket_meta.get("token_id")

        try:
            token, slot_id = await self._get_token_direct(
                project_id,
                action=action,
                token_id=token_id,
                return_slot_id=True,
                allow_affinity=False,
                remember_affinity=False,
            )
            if not token:
                return

            created_at = time.time()
            lease = TokenPoolLease(
                bucket_key=bucket_key,
                token=token,
                project_id=project_id,
                action=action,
                token_id=token_id,
                slot_id=slot_id,
                worker_index=self._parse_worker_index_from_slot_id(slot_id),
                created_at=created_at,
                expires_at=created_at + float(getattr(config, "token_pool_ttl_seconds", 120) or 120),
            )

            async with self._token_pool_lock:
                self._token_pool_last_refill_at = created_at
                self._token_pool_last_token_at = created_at
                self._token_pool_stats["produced_count"] += 1

                current_meta = self._token_pool_bucket_meta.get(bucket_key)
                if current_meta is None or not self._is_token_pool_enabled():
                    self._token_pool_stats["dropped_count"] += 1
                    return

                target_size = self._get_token_pool_bucket_target_size(
                    project_id=current_meta.get("project_id"),
                    action=current_meta.get("action"),
                )
                self._prune_token_pool_bucket_locked(bucket_key, created_at)
                queue = self._token_pool_queues.setdefault(bucket_key, deque())
                if len(queue) < target_size:
                    queue.append(lease)
                    self._get_token_pool_condition_locked(bucket_key).notify_all()
                else:
                    self._token_pool_stats["dropped_count"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_logger.log_warning(
                f"[BrowserCaptchaPool] token 池补货失败 (bucket={bucket_key or '<empty>'}): {exc}"
            )
        finally:
            async with self._token_pool_lock:
                current_value = int(self._token_pool_refill_inflight.get(bucket_key, 0) or 0)
                if current_value <= 1:
                    self._token_pool_refill_inflight.pop(bucket_key, None)
                else:
                    self._token_pool_refill_inflight[bucket_key] = current_value - 1
                condition = self._token_pool_conditions.get(bucket_key)
                if condition is not None:
                    condition.notify_all()
                self._cleanup_token_pool_bucket_locked(bucket_key)

            if not self._closing:
                await self._maintain_token_pool_once()

    async def _wait_for_token_pool_token(
        self,
        *,
        bucket_key: str,
        project_id: str,
        action: str,
        token_id: Optional[int],
    ) -> Optional[TokenPoolLease]:
        if self._closing:
            return None

        now_value = time.time()
        async with self._token_pool_lock:
            self._register_token_pool_bucket_locked(
                bucket_key=bucket_key,
                project_id=project_id,
                action=action,
                token_id=token_id,
                now_value=now_value,
            )
            lease = self._pop_ready_token_pool_lease_locked(bucket_key, now_value=now_value)
            if lease is not None:
                self._token_pool_stats["hit_count"] += 1
                self._token_pool_stats["served_count"] += 1
                return lease
            self._token_pool_stats["miss_count"] += 1
            self._token_pool_stats["wait_count"] += 1
            self._token_pool_waiters[bucket_key] = int(self._token_pool_waiters.get(bucket_key, 0) or 0) + 1

        try:
            await self._ensure_token_pool_maintainer()
            await self._maintain_token_pool_once()

            deadline = time.monotonic() + self._get_token_pool_wait_timeout_seconds()
            while True:
                async with self._token_pool_lock:
                    self._register_token_pool_bucket_locked(
                        bucket_key=bucket_key,
                        project_id=project_id,
                        action=action,
                        token_id=token_id,
                        now_value=time.time(),
                    )
                    lease = self._pop_ready_token_pool_lease_locked(
                        bucket_key,
                        now_value=time.time(),
                    )
                    if lease is not None:
                        self._token_pool_stats["served_count"] += 1
                        return lease

                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        break
                    condition = self._get_token_pool_condition_locked(bucket_key)
                    try:
                        await asyncio.wait_for(condition.wait(), timeout=remaining_seconds)
                    except asyncio.TimeoutError:
                        break

                await self._maintain_token_pool_once()

            bucket_snapshot = self._summarize_token_pool_bucket(bucket_key, now_value=time.time())
            debug_logger.log_warning(
                f"[BrowserCaptchaPool] token 池等待超时，严格池模式下不回退同步获取 "
                f"(action_bucket={bucket_snapshot['action']}, project_id={project_id or '<empty>'}, "
                f"token_id={token_id}, action={action}, ready={bucket_snapshot['ready_count']}, "
                f"waiting={bucket_snapshot['waiting_requests']}, inflight={bucket_snapshot['refill_inflight']})"
            )
            return None
        finally:
            async with self._token_pool_lock:
                current_value = int(self._token_pool_waiters.get(bucket_key, 0) or 0)
                if current_value <= 1:
                    self._token_pool_waiters.pop(bucket_key, None)
                else:
                    self._token_pool_waiters[bucket_key] = current_value - 1
                self._cleanup_token_pool_bucket_locked(bucket_key)

    async def _idle_worker_reaper_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(30)
                try:
                    idle_ttl_seconds = max(
                        60,
                        int(getattr(config, "personal_idle_tab_ttl_seconds", 600) or 600),
                    )
                except Exception:
                    idle_ttl_seconds = 600

                async with self._worker_dispatch_lock:
                    candidates = [
                        (worker_index, worker)
                        for worker_index, worker in enumerate(self._workers)
                        if int(self._worker_dispatch_reservations.get(worker_index, 0) or 0) <= 0
                    ]

                for worker_index, worker in candidates:
                    if not self._worker_has_live_runtime(worker):
                        continue
                    try:
                        did_shutdown = await worker.shutdown_idle_runtime_if_needed(
                            idle_ttl_seconds=idle_ttl_seconds,
                            reason=f"pool_idle_runtime_ttl_{idle_ttl_seconds}s",
                        )
                    except Exception as e:
                        debug_logger.log_warning(
                            f"[BrowserCaptchaPool] 空闲浏览器实例回收失败 (worker={worker_index + 1}): {e}"
                        )
                        continue
                    if did_shutdown:
                        debug_logger.log_info(
                            f"[BrowserCaptchaPool] 已回收空闲浏览器实例运行态 (worker={worker_index + 1}, idle_ttl={idle_ttl_seconds}s)"
                        )
            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptchaPool] 空闲浏览器实例回收循环异常: {e}")

    async def _acquire_worker(
        self,
        *,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
        excluded_indexes: Optional[set[int]] = None,
        ensure_workers: bool = True,
        allow_affinity: bool = True,
    ) -> tuple[int, BrowserCaptchaService]:
        if ensure_workers:
            await self._ensure_workers()
        excluded = set(excluded_indexes or set())
        acquire_started_at = time.monotonic()
        async with self._worker_dispatch_lock:
            if not self._workers:
                raise RuntimeError("没有可用的浏览器实例")
            if len(self._workers) <= 1:
                return 0, self._workers[0]

            preferred_affinity_worker_index = None
            if allow_affinity:
                for candidate in (
                    self._find_worker_index_for_token(token_id),
                    self._find_worker_index_for_project(project_id),
                ):
                    if candidate is None or candidate in excluded:
                        continue
                    if not (0 <= candidate < len(self._workers)):
                        continue
                    preferred_affinity_worker_index = candidate
                    break

            candidate_indexes = [
                worker_index
                for worker_index in self._resolve_worker_candidate_indexes(
                    project_id=project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    allow_affinity=allow_affinity,
                )
                if worker_index not in excluded
            ]
            if not candidate_indexes:
                candidate_indexes = [
                    worker_index
                    for worker_index in range(len(self._workers))
                    if worker_index not in excluded
                ] or [0]

            selectable_indexes = [
                worker_index
                for worker_index in candidate_indexes
                if int(getattr(self._workers[worker_index], "_max_resident_tabs", 0) or 0) > 0
            ] or candidate_indexes
            non_restarting_indexes = [
                worker_index
                for worker_index in selectable_indexes
                if not self._worker_has_pending_fresh_restart(self._workers[worker_index])
            ]
            if non_restarting_indexes:
                selectable_indexes = non_restarting_indexes
            live_or_unstarted_indexes = [
                worker_index
                for worker_index in selectable_indexes
                if (
                    self._worker_has_live_runtime(self._workers[worker_index])
                    or not getattr(self._workers[worker_index], "_initialized", False)
                )
            ]
            if live_or_unstarted_indexes:
                selectable_indexes = live_or_unstarted_indexes
            selected_worker_index = min(
                selectable_indexes,
                key=lambda worker_index: self._worker_dispatch_score(
                    worker_index,
                    self._workers[worker_index],
                    affinity_preferred=worker_index == preferred_affinity_worker_index,
                ),
            )
            self._worker_dispatch_reservations[selected_worker_index] = (
                int(self._worker_dispatch_reservations.get(selected_worker_index, 0) or 0) + 1
            )
            normalized_project_key = self._normalize_project_key(project_id)
            if normalized_project_key:
                self._project_worker_affinity[normalized_project_key] = selected_worker_index
                self._trim_affinity_cache(self._project_worker_affinity)
            self._round_robin_index = (selected_worker_index + 1) % max(len(self._workers), 1)
            debug_logger.log_info(
                "[BrowserCaptchaPool] worker 已选中 "
                f"(project_id={project_id or '<empty>'}, token_id={token_id}, "
                f"selected={selected_worker_index + 1}, candidates={[index + 1 for index in candidate_indexes]}, "
                f"selectable={[index + 1 for index in selectable_indexes]}, "
                f"affinity={(preferred_affinity_worker_index + 1) if preferred_affinity_worker_index is not None else None}, "
                f"reservations={self._worker_dispatch_reservations.get(selected_worker_index, 0)}, "
                f"live={self._worker_has_live_runtime(self._workers[selected_worker_index])}, "
                f"fresh_restart={self._worker_has_pending_fresh_restart(self._workers[selected_worker_index])}, "
                f"elapsed={time.monotonic() - acquire_started_at:.3f}s)"
            )
            return selected_worker_index, self._workers[selected_worker_index]

    async def _release_worker_reservation(self, worker_index: Optional[int]) -> None:
        if worker_index is None:
            return
        async with self._worker_dispatch_lock:
            current = int(self._worker_dispatch_reservations.get(worker_index, 0) or 0)
            if current <= 1:
                self._worker_dispatch_reservations.pop(worker_index, None)
            else:
                self._worker_dispatch_reservations[worker_index] = current - 1

    def _build_project_buckets_for_workers(
        self,
        project_ids: list[str],
        *,
        worker_limits: list[int],
    ) -> list[list[str]]:
        project_buckets: list[list[str]] = [[] for _ in self._workers]
        if not project_ids or not project_buckets:
            return project_buckets

        for project_id in project_ids:
            preferred_worker_index = self._find_worker_index_for_project(project_id)
            if (
                preferred_worker_index is not None
                and 0 <= preferred_worker_index < len(project_buckets)
                and len(project_buckets[preferred_worker_index]) < worker_limits[preferred_worker_index]
            ):
                project_buckets[preferred_worker_index].append(project_id)
                continue

            candidate_indexes = [
                index
                for index, _ in enumerate(self._workers)
                if len(project_buckets[index]) < worker_limits[index]
            ]
            if not candidate_indexes:
                break

            selected_worker_index = min(
                candidate_indexes,
                key=lambda index: (
                    self._workers[index].get_resident_count() + len(project_buckets[index]),
                    self._worker_busy_score(self._workers[index]),
                    index,
                ),
            )
            project_buckets[selected_worker_index].append(project_id)

        return project_buckets

    async def _ensure_workers(self, *, reload_existing: bool = False) -> None:
        if self._closing:
            return

        extra_workers: list[BrowserCaptchaService] = []
        workers_to_reload: list[BrowserCaptchaService] = []

        async with self._reload_lock:
            async with self._worker_dispatch_lock:
                self.headless = bool(getattr(config, "personal_headless", False))
                configured_browser_count = BrowserCaptchaService._resolve_configured_browser_count()
                per_worker_tabs = self._resolve_worker_resident_tabs()
                worker_limits = self._build_worker_tab_limits(
                    per_worker_tabs,
                    configured_browser_count,
                )
                effective_total_tabs = sum(max(0, int(limit or 0)) for limit in worker_limits)

                current_worker_count = len(self._workers)
                if current_worker_count > len(worker_limits):
                    extra_workers = self._workers[len(worker_limits):]
                    self._workers = self._workers[:len(worker_limits)]

                for index, tab_limit in enumerate(worker_limits):
                    if index >= len(self._workers):
                        worker = BrowserCaptchaService(
                            self.db,
                            browser_instance_id=index + 1,
                            max_resident_tabs_override=tab_limit,
                        )
                        worker._idle_reaper_task = asyncio.create_task(worker._idle_tab_reaper_loop())
                        self._workers.append(worker)
                        continue

                    worker = self._workers[index]
                    worker.db = self.db
                    worker.apply_pool_worker_settings(
                        browser_instance_id=index + 1,
                        max_resident_tabs_override=tab_limit,
                    )
                    if reload_existing:
                        workers_to_reload.append(worker)

                self._worker_tab_limits = list(worker_limits)
                self._cleanup_affinity_maps()
                self._worker_dispatch_reservations = {
                    worker_index: count
                    for worker_index, count in self._worker_dispatch_reservations.items()
                    if 0 <= worker_index < len(self._workers) and count > 0
                }
                self._round_robin_index %= max(len(self._workers), 1)
                debug_logger.log_info(
                    "[BrowserCaptchaPool] Personal 池配置已生效 "
                    f"(browser_count={configured_browser_count}, "
                    f"per_worker_tabs={per_worker_tabs}, "
                    f"effective_workers={len(worker_limits)}, "
                    f"effective_total_tabs={effective_total_tabs}, "
                    f"worker_limits={worker_limits}, "
                    f"fresh_restart_every={getattr(config, 'browser_personal_fresh_restart_every_n_solves', 10)})"
                )

        for worker in extra_workers:
            try:
                await worker.close()
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptchaPool] 关闭多余浏览器实例失败: {e}")

        if workers_to_reload:
            await asyncio.gather(
                *(worker.reload_config() for worker in workers_to_reload),
                return_exceptions=True,
            )
        if self._closing:
            return

        if self._workers:
            await self._ensure_idle_worker_reaper()
        if self._is_token_pool_enabled():
            await self._ensure_token_pool_maintainer()
            await self._maintain_token_pool_once()
        elif self._token_pool_maintainer_task is not None and not self._token_pool_maintainer_task.done():
            self._token_pool_maintainer_task.cancel()
            try:
                await self._token_pool_maintainer_task
            except asyncio.CancelledError:
                pass
            finally:
                self._token_pool_maintainer_task = None

    async def reload_config(self):
        if self._closing:
            return
        await self._ensure_workers(reload_existing=True)

    async def close(self):
        self._closing = True
        async with self._reload_lock:
            async with self._worker_dispatch_lock:
                idle_worker_reaper_task = self._idle_worker_reaper_task
                self._idle_worker_reaper_task = None
                token_pool_maintainer_task = self._token_pool_maintainer_task
                self._token_pool_maintainer_task = None
                token_pool_fill_tasks = list(self._token_pool_fill_tasks)
                self._token_pool_fill_tasks.clear()
                workers = list(self._workers)
                self._workers = []
                self._worker_tab_limits = []
                self._worker_dispatch_reservations.clear()
                self._project_worker_affinity.clear()
                self._token_worker_affinity.clear()
                self._last_successful_worker_index = None
                self._round_robin_index = 0
        async with self._token_pool_lock:
            self._token_pool_queues.clear()
            self._token_pool_conditions.clear()
            self._token_pool_waiters.clear()
            self._token_pool_bucket_meta.clear()
            self._token_pool_refill_inflight.clear()
        for fill_task in token_pool_fill_tasks:
            if fill_task.done():
                continue
            fill_task.cancel()
        if token_pool_fill_tasks:
            await asyncio.gather(*token_pool_fill_tasks, return_exceptions=True)
        if token_pool_maintainer_task and not token_pool_maintainer_task.done():
            token_pool_maintainer_task.cancel()
            try:
                await token_pool_maintainer_task
            except asyncio.CancelledError:
                pass
                pass
        if idle_worker_reaper_task and not idle_worker_reaper_task.done():
            idle_worker_reaper_task.cancel()
            try:
                await idle_worker_reaper_task
            except asyncio.CancelledError:
                pass
        for worker in workers:
            try:
                await worker.close()
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptchaPool] 关闭浏览器实例失败: {e}")

    async def _get_token_direct(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        return_slot_id: bool = False,
        allow_affinity: bool = True,
        remember_affinity: bool = True,
    ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
        await self._ensure_workers()
        if not self._workers:
            return (None, None) if return_slot_id else None

        excluded_indexes: set[int] = set()
        max_attempts = min(len(self._workers), 3)

        for _ in range(max_attempts):
            worker_index = None
            worker = None
            try:
                worker_index, worker = await self._acquire_worker(
                    project_id=project_id,
                    token_id=token_id,
                    excluded_indexes=excluded_indexes,
                    ensure_workers=False,
                    allow_affinity=allow_affinity,
                )
                excluded_indexes.add(worker_index)
                token, slot_id = await worker.get_token(
                    project_id,
                    action=action,
                    token_id=token_id,
                    return_slot_id=True,
                )
            except Exception as e:
                worker_label = worker_index + 1 if worker_index is not None else "unknown"
                debug_logger.log_warning(
                    f"[BrowserCaptchaPool] 浏览器实例打码失败，尝试切换其他实例 (worker={worker_label}): {e}"
                )
                if BrowserCaptchaService._is_memory_pressure_browser_launch_error(e):
                    await self._reclaim_pool_memory_pressure(
                        reason=f"direct_token:{project_id or '<empty>'}",
                        exclude_indexes=excluded_indexes,
                    )
                continue
            finally:
                await self._release_worker_reservation(worker_index)

            if not token:
                continue

            self._last_successful_worker_index = worker_index
            if remember_affinity:
                self._remember_affinity(
                    project_id=project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    worker_index=worker_index,
                )
            if return_slot_id:
                return token, slot_id
            return token

        return (None, None) if return_slot_id else None

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        return_slot_id: bool = False,
    ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
        token, slot_id, _ = await self.get_token_with_metadata(
            project_id,
            action=action,
            token_id=token_id,
        )
        if return_slot_id:
            return token, slot_id
        return token

    async def get_token_with_metadata(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[int]]:
        if not self._is_token_pool_enabled():
            token, slot_id = await self._get_token_direct(
                project_id,
                action=action,
                token_id=token_id,
                return_slot_id=True,
            )
            return token, slot_id, (token_id if token else None)

        target_size = self._get_token_pool_bucket_target_size(
            project_id=project_id,
            action=action,
        )
        if target_size <= 0:
            token, slot_id = await self._get_token_direct(
                project_id,
                action=action,
                token_id=token_id,
                return_slot_id=True,
            )
            return token, slot_id, (token_id if token else None)

        bucket_key = self._build_token_pool_bucket_key(
            project_id=project_id,
            action=action,
            token_id=token_id,
        )
        lease = await self._wait_for_token_pool_token(
            bucket_key=bucket_key,
            project_id=project_id,
            action=action,
            token_id=token_id,
        )
        if lease is None:
            bucket_snapshot = self._summarize_token_pool_bucket(bucket_key, now_value=time.time())
            raise TokenPoolTimeoutError(
                "token 池等待超时且未命中可用 token "
                f"(action_bucket={bucket_snapshot['action']}, project_id={project_id or '<empty>'}, "
                f"token_id={token_id}, action={action}, ready={bucket_snapshot['ready_count']}, "
                f"waiting={bucket_snapshot['waiting_requests']}, inflight={bucket_snapshot['refill_inflight']})"
            )

        if lease.worker_index is not None:
            self._last_successful_worker_index = lease.worker_index
        self._remember_affinity(
            project_id=project_id,
            token_id=token_id if lease.token_id == token_id else None,
            slot_id=lease.slot_id,
            worker_index=lease.worker_index,
        )
        return lease.token, lease.slot_id, lease.token_id

    async def report_flow_error(
        self,
        project_id: str,
        error_reason: str,
        error_message: str = "",
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
    ):
        await self._ensure_workers()
        exact_worker_index = self._parse_worker_index_from_slot_id(slot_id)
        if exact_worker_index is not None and 0 <= exact_worker_index < len(self._workers):
            await self._workers[exact_worker_index].report_flow_error(
                project_id,
                error_reason,
                error_message=error_message,
                token_id=token_id,
                slot_id=slot_id,
            )
            return

        candidate_indexes: list[int] = []
        for worker_index in (
            self._find_worker_index_for_token(token_id),
            self._find_worker_index_for_project(project_id),
        ):
            if worker_index is None or not (0 <= worker_index < len(self._workers)):
                continue
            if worker_index not in candidate_indexes:
                candidate_indexes.append(worker_index)

        if not candidate_indexes:
            if (
                self._last_successful_worker_index is not None
                and 0 <= self._last_successful_worker_index < len(self._workers)
            ):
                candidate_indexes.append(self._last_successful_worker_index)

        if not candidate_indexes:
            resolved_candidates = self._resolve_worker_candidate_indexes(project_id=project_id, token_id=token_id)
            if resolved_candidates:
                candidate_indexes.append(resolved_candidates[0])

        for worker_index in candidate_indexes:
            await self._workers[worker_index].report_flow_error(
                project_id,
                error_reason,
                error_message=error_message,
                token_id=token_id,
                slot_id=slot_id,
            )

    async def invalidate_token(self, project_id: str):
        await self._ensure_workers()
        candidate_indexes = self._resolve_worker_candidate_indexes(project_id=project_id)
        for worker_index in candidate_indexes:
            await self._workers[worker_index].invalidate_token(project_id)

    async def open_login_window(self):
        await self._ensure_workers()
        if not self._workers:
            raise RuntimeError("没有可用的浏览器实例")
        await self._workers[0].open_login_window()

    async def warmup_resident_tabs(
        self,
        project_ids: Optional[list[str]] = None,
        limit: int = 1,
    ) -> list[Optional[str]]:
        await self._ensure_workers()
        if not project_ids or not self._workers:
            return []

        total_limit = max(1, min(
            int(limit or 1),
            self._resolve_effective_pool_tab_capacity(browser_count=len(self._workers)),
        ))
        worker_limits = self._build_worker_tab_limits(
            self._resolve_worker_resident_tabs(),
            len(self._workers),
            total_limit=total_limit,
            allow_zero=True,
        )
        project_buckets = self._build_project_buckets_for_workers(
            [str(project_id).strip() for project_id in project_ids if str(project_id or "").strip()],
            worker_limits=worker_limits,
        )

        warmup_tasks = [
            worker.warmup_resident_tabs(project_buckets[index], limit=worker_limits[index])
            for index, worker in enumerate(self._workers)
            if index < len(project_buckets) and project_buckets[index] and worker_limits[index] > 0
        ]
        if not warmup_tasks:
            return []

        warmed_slots: list[Optional[str]] = []
        results = await asyncio.gather(*warmup_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                debug_logger.log_warning(
                    f"[BrowserCaptchaPool] resident tabs 预热 worker 失败: {result}"
                )
                continue
            for slot_id in result or []:
                if slot_id:
                    warmed_slots.append(slot_id)
                    if len(warmed_slots) >= total_limit:
                        return warmed_slots
        return warmed_slots

    async def refresh_session_token(self, project_id: str, token_id: Optional[int] = None) -> Optional[str]:
        await self._ensure_workers()
        excluded_indexes: set[int] = set()
        max_attempts = min(len(self._workers), 3)

        for _ in range(max_attempts):
            worker_index = None
            worker = None
            try:
                worker_index, worker = await self._acquire_worker(
                    project_id=project_id,
                    token_id=token_id,
                    excluded_indexes=excluded_indexes,
                )
                excluded_indexes.add(worker_index)
                session_token = await worker.refresh_session_token(project_id, token_id=token_id)
            except Exception as e:
                worker_label = worker_index + 1 if worker_index is not None else "unknown"
                debug_logger.log_warning(
                    f"[BrowserCaptchaPool] Session Token 刷新失败，尝试切换其他实例 (worker={worker_label}): {e}"
                )
                continue
            finally:
                await self._release_worker_reservation(worker_index)
            if session_token:
                self._last_successful_worker_index = worker_index
                self._remember_affinity(project_id=project_id, token_id=token_id, worker_index=worker_index)
                return session_token
        return None

    async def start_resident_mode(self, project_id: str):
        await self._ensure_workers()
        worker_index = None
        worker = None
        try:
            worker_index, worker = await self._acquire_worker(project_id=project_id)
            await worker.start_resident_mode(project_id)
            self._remember_affinity(project_id=project_id, worker_index=worker_index)
        finally:
            await self._release_worker_reservation(worker_index)

    async def stop_resident_mode(self, project_id: Optional[str] = None):
        await self._ensure_workers()
        if project_id:
            exact_worker_index = self._parse_worker_index_from_slot_id(project_id)
            if exact_worker_index is not None and 0 <= exact_worker_index < len(self._workers):
                await self._workers[exact_worker_index].stop_resident_mode(project_id)
                return
            worker_index = self._find_worker_index_for_project(project_id)
            if worker_index is not None:
                await self._workers[worker_index].stop_resident_mode(project_id)
                return
            return

        await asyncio.gather(
            *(worker.stop_resident_mode(project_id=None) for worker in self._workers),
            return_exceptions=True,
        )

    def is_resident_mode_active(self) -> bool:
        return any(worker.is_resident_mode_active() for worker in self._workers)

    def get_resident_count(self) -> int:
        return sum(worker.get_resident_count() for worker in self._workers)

    def get_resident_project_ids(self) -> list[str]:
        project_ids: list[str] = []
        for worker in self._workers:
            project_ids.extend(worker.get_resident_project_ids())
        return project_ids

    def get_resident_project_id(self) -> Optional[str]:
        for worker in self._workers:
            project_id = worker.get_resident_project_id()
            if project_id:
                return project_id
        return None

    def get_token_pool_status(self) -> Dict[str, Any]:
        if not self._is_token_pool_enabled():
            return {
                "token_pool_enabled": False,
                "token_pool_status": "未启用",
                "token_pool_total_ready": 0,
                "token_pool_bucket_count": 0,
                "token_pool_waiting_requests": 0,
                "token_pool_refill_inflight": 0,
                "token_pool_last_refill_at": None,
                "token_pool_last_token_at": None,
                "token_pool_oldest_token_age_seconds": None,
                "token_pool_next_expire_in_seconds": None,
                "token_pool_bucket_details": [],
                "token_pool_hit_count": int(self._token_pool_stats.get("hit_count", 0) or 0),
                "token_pool_miss_count": int(self._token_pool_stats.get("miss_count", 0) or 0),
                "token_pool_wait_count": int(self._token_pool_stats.get("wait_count", 0) or 0),
                "token_pool_expired_count": int(self._token_pool_stats.get("expired_count", 0) or 0),
            }

        now_value = time.time()
        bucket_keys = sorted(
            {
                *self._token_pool_bucket_meta.keys(),
                *[key for key, queue in self._token_pool_queues.items() if queue],
                *[key for key, value in self._token_pool_waiters.items() if int(value or 0) > 0],
                *[key for key, value in self._token_pool_refill_inflight.items() if int(value or 0) > 0],
            }
        )
        bucket_details = [self._summarize_token_pool_bucket(bucket_key, now_value=now_value) for bucket_key in bucket_keys]
        total_ready = sum(int(detail["ready_count"] or 0) for detail in bucket_details)
        oldest_token_age_seconds: Optional[int] = None
        next_expire_in_seconds: Optional[int] = None
        for detail in bucket_details:
            age_seconds = detail.get("oldest_token_age_seconds")
            expire_in_seconds = detail.get("next_expire_in_seconds")
            if age_seconds is not None and (
                oldest_token_age_seconds is None or int(age_seconds) > oldest_token_age_seconds
            ):
                oldest_token_age_seconds = int(age_seconds)
            if expire_in_seconds is not None and (
                next_expire_in_seconds is None or int(expire_in_seconds) < next_expire_in_seconds
            ):
                next_expire_in_seconds = int(expire_in_seconds)

        waiting_requests = sum(int(detail["waiting_requests"] or 0) for detail in bucket_details)
        refill_inflight = sum(int(detail["refill_inflight"] or 0) for detail in bucket_details)
        bucket_count = len(bucket_details)

        if total_ready > 0:
            status_text = "运行中"
        elif refill_inflight > 0 and waiting_requests > 0:
            status_text = "补货中"
        elif refill_inflight > 0:
            status_text = "预热中"
        elif waiting_requests > 0:
            status_text = "等待中"
        else:
            status_text = "空闲"

        return {
            "token_pool_enabled": True,
            "token_pool_status": status_text,
            "token_pool_total_ready": total_ready,
            "token_pool_bucket_count": bucket_count,
            "token_pool_waiting_requests": waiting_requests,
            "token_pool_refill_inflight": refill_inflight,
            "token_pool_last_refill_at": self._format_status_timestamp(self._token_pool_last_refill_at),
            "token_pool_last_token_at": self._format_status_timestamp(self._token_pool_last_token_at),
            "token_pool_oldest_token_age_seconds": oldest_token_age_seconds,
            "token_pool_next_expire_in_seconds": next_expire_in_seconds,
            "token_pool_bucket_details": bucket_details,
            "token_pool_hit_count": int(self._token_pool_stats.get("hit_count", 0) or 0),
            "token_pool_miss_count": int(self._token_pool_stats.get("miss_count", 0) or 0),
            "token_pool_wait_count": int(self._token_pool_stats.get("wait_count", 0) or 0),
            "token_pool_expired_count": int(self._token_pool_stats.get("expired_count", 0) or 0),
        }

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        if self._last_successful_worker_index is not None and 0 <= self._last_successful_worker_index < len(self._workers):
            fingerprint = self._workers[self._last_successful_worker_index].get_last_fingerprint()
            if fingerprint:
                return fingerprint
        for worker in self._workers:
            fingerprint = worker.get_last_fingerprint()
            if fingerprint:
                return fingerprint
        return None

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        await self._ensure_workers()
        if not self._workers:
            return None
        worker_index = None
        worker = None
        try:
            worker_index, worker = await self._acquire_worker()
            token = await worker.get_custom_token(
                website_url=website_url,
                website_key=website_key,
                action=action,
                enterprise=enterprise,
            )
        finally:
            await self._release_worker_reservation(worker_index)
        if token:
            self._last_successful_worker_index = worker_index
        return token

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        await self._ensure_workers()
        if not self._workers:
            return {
                "token": None,
                "token_elapsed_ms": 0,
                "verify_mode": "browser_page",
                "verify_elapsed_ms": 0,
                "verify_http_status": None,
                "verify_result": {},
            }
        worker_index = None
        worker = None
        try:
            worker_index, worker = await self._acquire_worker()
            result = await worker.get_custom_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise,
            )
        finally:
            await self._release_worker_reservation(worker_index)
        if result.get("token"):
            self._last_successful_worker_index = worker_index
        return result
