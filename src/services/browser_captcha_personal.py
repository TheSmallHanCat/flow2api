"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
支持常驻模式：维护全局共享的常驻标签页池，即时生成 token
"""
import asyncio
import inspect
import time
import os
import sys
import re
import json
import shutil
import tempfile
import subprocess
import types
from typing import Optional, Dict, Any, Iterable

from ..core.logger import debug_logger
from ..core.config import config

# 复用 browser 模式的浏览器缓存目录约定，避免容器内每次换位置。
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")


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


def _get_optional_bool_env(name: str) -> Optional[bool]:
    """读取可选布尔环境变量，未设置或无法识别时返回 None。"""
    value = os.environ.get(name)
    if value is None:
        return None

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


ALLOW_DOCKER_HEADED = (
    _is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA")
    or _is_truthy_env("ALLOW_DOCKER_BROWSER_CAPTCHA")
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED


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


def _run_playwright_install(use_mirror: bool = False) -> bool:
    """安装 playwright chromium 浏览器，复用 browser 模式的安装方式。"""
    cmd = [sys.executable, '-m', 'playwright', 'install', 'chromium']
    env = os.environ.copy()

    if use_mirror:
        env['PLAYWRIGHT_DOWNLOAD_HOST'] = 'https://npmmirror.com/mirrors/playwright'

    try:
        debug_logger.log_info("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        print("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if result.returncode == 0:
            debug_logger.log_info("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            print("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            return True

        debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装失败: {result.stderr[:200]}")
        return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装异常: {e}")
        return False


def _ensure_playwright_installed() -> bool:
    """确保 playwright 可用，便于复用其 chromium 二进制。"""
    try:
        import playwright  # noqa: F401
        debug_logger.log_info("[BrowserCaptcha] playwright 已安装")
        return True
    except ImportError:
        pass

    debug_logger.log_info("[BrowserCaptcha] playwright 未安装，开始自动安装...")
    print("[BrowserCaptcha] playwright 未安装，开始自动安装...")

    if _run_pip_install('playwright', use_mirror=False):
        return True

    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install('playwright', use_mirror=True):
        return True

    debug_logger.log_error("[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright")
    print("[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright")
    return False


def _detect_playwright_browser_path() -> Optional[str]:
    """读取 playwright 管理的 chromium 可执行文件路径。"""
    detect_script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    print(p.chromium.executable_path or '')\n"
    )
    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0") or "0")

    try:
        result = subprocess.run(
            [sys.executable, "-c", detect_script],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        browser_path_lines = (result.stdout or "").strip().splitlines()
        browser_path = browser_path_lines[-1].strip() if browser_path_lines else ""
        if result.returncode == 0 and browser_path and os.path.exists(browser_path):
            debug_logger.log_info(f"[BrowserCaptcha] 检测到 playwright chromium: {browser_path}")
            return browser_path

        stderr_text = (result.stderr or "").strip()
        if stderr_text:
            debug_logger.log_warning(f"[BrowserCaptcha] 检测 playwright chromium 失败: {stderr_text[:200]}")
    except Exception as e:
        debug_logger.log_info(f"[BrowserCaptcha] 检测 playwright chromium 时出错: {e}")

    return None


def _ensure_playwright_browser_path() -> Optional[str]:
    """确保存在可复用的 chromium 二进制，并返回路径。"""
    browser_path = _detect_playwright_browser_path()
    if browser_path:
        return browser_path

    if not _ensure_playwright_installed():
        return None

    debug_logger.log_info("[BrowserCaptcha] playwright chromium 未安装，开始自动安装...")
    print("[BrowserCaptcha] playwright chromium 未安装，开始自动安装...")

    if not _run_playwright_install(use_mirror=False):
        debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
        print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
        if not _run_playwright_install(use_mirror=True):
            debug_logger.log_error("[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium")
            print("[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium")
            return None

    return _detect_playwright_browser_path()


# 尝试导入 nodriver
uc = None
NODRIVER_AVAILABLE = False
_NODRIVER_RUNTIME_PATCHED = False

if DOCKER_HEADED_BLOCKED:
    debug_logger.log_warning(
        "[BrowserCaptcha] 检测到 Docker 环境，默认禁用内置浏览器打码。"
        "如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true。"
        "personal 模式默认支持无头，不强制依赖 DISPLAY/Xvfb。"
    )
    print("[BrowserCaptcha] ⚠️ 检测到 Docker 环境，默认禁用内置浏览器打码")
    print("[BrowserCaptcha] 如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true")
else:
    if IS_DOCKER and ALLOW_DOCKER_HEADED:
        debug_logger.log_warning(
            "[BrowserCaptcha] Docker 内置浏览器打码白名单已启用，personal 模式将按 headless 配置决定是否需要 DISPLAY/Xvfb"
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


def _patch_nodriver_connection_instance(connection_instance):
    """在连接实例级别收口 websocket.send 的后台异常。"""
    if not connection_instance or getattr(connection_instance, "_flow2api_send_patched", False):
        return

    try:
        from nodriver.core import connection as nodriver_connection_module
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] 加载 nodriver.connection 失败，跳过连接补丁: {e}")
        return

    async def patched_send(self, cdp_obj, _is_update=False):
        if self.closed:
            await self.connect()
        if not _is_update:
            await self._register_handlers()

        transaction = nodriver_connection_module.Transaction(cdp_obj)
        tx_id = next(self.__count__)
        transaction.id = tx_id
        self.mapper[tx_id] = transaction

        send_task = asyncio.create_task(self.websocket.send(transaction.message))
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


def _create_proxy_auth_extension(protocol: str, host: str, port: str, username: str, password: str) -> str:
    """Create a temporary Chrome extension directory for proxy authentication.
    Returns the path to the extension directory."""
    ext_dir = tempfile.mkdtemp(prefix="nodriver_proxy_auth_")

    scheme_map = {"http": "http", "https": "https", "socks5": "socks5"}
    scheme = scheme_map.get(protocol, "http")

    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth Helper",
        "permissions": [
            "proxy", "tabs", "unlimitedStorage", "storage",
            "<all_urls>", "webRequest", "webRequestBlocking"
        ],
        "background": {"scripts": ["background.js"]},
        "minimum_chrome_version": "76.0.0"
    }
    background_js = (
        "var config = {\n"
        '    mode: "fixed_servers",\n'
        "    rules: {\n"
        "        singleProxy: {\n"
        f'            scheme: "{scheme}",\n'
        f'            host: "{host}",\n'
        f"            port: parseInt({port})\n"
        "        },\n"
        '        bypassList: ["localhost"]\n'
        "    }\n"
        "};\n"
        'chrome.proxy.settings.set({value: config, scope: "regular"}, function(){});\n'
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "    function(details) {\n"
        "        return {\n"
        "            authCredentials: {\n"
        f'                username: "{username}",\n'
        f'                password: "{password}"\n'
        "            }\n"
        "        };\n"
        "    },\n"
        '    {urls: ["<all_urls>"]},\n'
        "    ['blocking']\n"
        ");\n"
    )
    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)
    return ext_dir


class ResidentTabInfo:
    """常驻标签页信息结构"""
    def __init__(self, tab, slot_id: str, project_id: Optional[str] = None):
        self.tab = tab
        self.slot_id = slot_id
        self.project_id = project_id or slot_id
        self.recaptcha_ready = False
        self.created_at = time.time()
        self.last_used_at = time.time()  # 最后使用时间
        self.use_count = 0  # 使用次数
        self.fingerprint: Optional[Dict[str, Any]] = None
        self.solve_lock = asyncio.Lock()  # 串行化同一标签页上的执行，降低并发冲突


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（nodriver 有头模式）
    
    支持两种模式：
    1. 常驻模式 (Resident Mode): 维护全局共享常驻标签页池，谁抢到空闲页谁执行
    2. 传统模式 (Legacy Mode): 每次请求创建新标签页 (fallback)
    """

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        self.headless = self._resolve_headless_mode()  # 默认改为有头，可用环境变量回退到无头
        self.browser = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        # 使用 None 让 nodriver 自动创建临时目录，避免目录锁定问题
        self.user_data_dir = None

        # 常驻模式相关属性：打码标签页是全局共享池，不再按 project_id 一对一绑定
        self._resident_tabs: dict[str, 'ResidentTabInfo'] = {}  # slot_id -> 常驻标签页信息
        self._project_resident_affinity: dict[str, str] = {}  # project_id -> slot_id（最近一次使用）
        self._resident_slot_seq = 0
        self._resident_pick_index = 0
        self._resident_lock = asyncio.Lock()  # 保护常驻标签页操作
        self._browser_lock = asyncio.Lock()  # 保护浏览器初始化/关闭/重启，避免重复拉起实例
        self._runtime_recover_lock = asyncio.Lock()  # 串行化浏览器级恢复，避免并发重启风暴
        self._tab_build_lock = asyncio.Lock()  # 串行化冷启动/重建，降低 nodriver 抖动
        self._legacy_lock = asyncio.Lock()  # 避免 legacy fallback 并发失控创建临时标签页
        self._max_resident_tabs = 5  # 最大常驻标签页数量（支持并发）
        self._idle_tab_ttl_seconds = 600  # 标签页空闲超时(秒)
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
        self._last_runtime_restart_at = 0.0
        self._proxy_url: Optional[str] = None
        self._proxy_ext_dir: Optional[str] = None
        # 自定义站点打码常驻页（用于 score-test）
        self._custom_tabs: dict[str, Dict[str, Any]] = {}
        self._custom_lock = asyncio.Lock()
        self._refresh_runtime_tunables()

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    # 启动空闲标签页回收任务
                    cls._instance._idle_reaper_task = asyncio.create_task(
                        cls._instance._idle_tab_reaper_loop()
                    )
        return cls._instance

    async def reload_config(self):
        """热更新配置（从数据库重新加载）"""
        from ..core.config import config
        old_max_tabs = self._max_resident_tabs
        old_idle_ttl = self._idle_tab_ttl_seconds
        old_probe_ttl = self._health_probe_ttl_seconds
        old_fingerprint_ttl = self._fingerprint_cache_ttl_seconds

        self._max_resident_tabs = config.personal_max_resident_tabs
        self._idle_tab_ttl_seconds = config.personal_idle_tab_ttl_seconds
        self._refresh_runtime_tunables()

        debug_logger.log_info(
            f"[BrowserCaptcha] Personal 配置已热更新: "
            f"max_tabs {old_max_tabs}->{self._max_resident_tabs}, "
            f"idle_ttl {old_idle_ttl}s->{self._idle_tab_ttl_seconds}s, "
            f"probe_ttl {old_probe_ttl}s->{self._health_probe_ttl_seconds}s, "
            f"fingerprint_ttl {old_fingerprint_ttl}s->{self._fingerprint_cache_ttl_seconds}s"
        )

    def _resolve_headless_mode(self) -> bool:
        """personal 模式默认改为有头，仅在显式环境变量要求时回退到无头。"""
        for env_name in ("PERSONAL_BROWSER_HEADLESS", "FLOW2API_PERSONAL_HEADLESS"):
            override = _get_optional_bool_env(env_name)
            if override is not None:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Personal headless 模式由环境变量 {env_name} 控制: {override}"
                )
                return override

        return False

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

    def _requires_virtual_display(self) -> bool:
        """仅在显式有头模式下要求 Docker/Linux 提供 DISPLAY/Xvfb。"""
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
                "请设置 DISPLAY（例如 :99）并启动 Xvfb。"
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
        """Docker 有头模式下等待 Xvfb socket 就绪，避免容器重启后立刻拉起浏览器失败。"""
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
            f"DISPLAY={display_value} 对应的 Xvfb socket 未就绪: {socket_path}"
        )

    def _mark_browser_health(self, healthy: bool):
        self._last_health_probe_at = time.monotonic()
        self._last_health_probe_ok = bool(healthy)

    def _is_browser_health_fresh(self) -> bool:
        if not (self._initialized and self.browser and self._last_health_probe_ok):
            return False
        try:
            if self.browser.stopped:
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

    def _was_runtime_restarted_recently(self, window_seconds: float = 5.0) -> bool:
        if self._last_runtime_restart_at <= 0.0:
            return False
        return (time.time() - self._last_runtime_restart_at) <= max(0.0, window_seconds)

    def _is_browser_runtime_error(self, error: Any) -> bool:
        """识别浏览器运行态已损坏/已关闭的典型异常。"""
        return _is_runtime_disconnect_error(error)

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
        if self._is_browser_health_fresh():
            return True

        try:
            _ = self.browser.tabs
            await self._run_with_timeout(
                self.browser.connection.send("Browser.getVersion"),
                timeout_seconds=3.0,
                label="browser.health_probe",
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
        return_by_value: bool = True,
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

    async def _browser_get(self, url: str, label: str, new_tab: bool = False, timeout_seconds: Optional[float] = None):
        return await self._run_with_timeout(
            self.browser.get(url, new_tab=new_tab),
            timeout_seconds or self._navigation_timeout_seconds,
            label,
        )

    async def _tab_reload(self, tab, label: str, timeout_seconds: Optional[float] = None):
        return await self._run_with_timeout(
            tab.reload(),
            timeout_seconds or self._navigation_timeout_seconds,
            label,
        )

    async def _get_browser_cookies(self, label: str, timeout_seconds: Optional[float] = None):
        return await self._run_with_timeout(
            self.browser.cookies.get_all(),
            timeout_seconds or self._command_timeout_seconds,
            label,
        )

    async def _browser_send_command(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        label: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ):
        return await self._run_with_timeout(
            self.browser.connection.send(method, params) if params else self.browser.connection.send(method),
            timeout_seconds or self._command_timeout_seconds,
            label or method,
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
        return f"slot-{self._resident_slot_seq}"

    def _forget_project_affinity_for_slot_locked(self, slot_id: Optional[str]):
        if not slot_id:
            return
        stale_projects = [
            project_id
            for project_id, mapped_slot_id in self._project_resident_affinity.items()
            if mapped_slot_id == slot_id
        ]
        for project_id in stale_projects:
            self._project_resident_affinity.pop(project_id, None)

    def _resolve_affinity_slot_locked(self, project_id: Optional[str]) -> Optional[str]:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return None
        slot_id = self._project_resident_affinity.get(normalized_project_id)
        if slot_id and slot_id in self._resident_tabs:
            return slot_id
        if slot_id:
            self._project_resident_affinity.pop(normalized_project_id, None)
        return None

    def _remember_project_affinity(self, project_id: Optional[str], slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id or not slot_id or resident_info is None:
            return
        self._project_resident_affinity[normalized_project_id] = slot_id
        resident_info.project_id = normalized_project_id

    def _resolve_resident_slot_for_project_locked(
        self,
        project_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[ResidentTabInfo]]:
        """优先走最近映射；没有映射时退化到共享池全局挑选。"""
        slot_id = self._resolve_affinity_slot_locked(project_id)
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if resident_info and resident_info.tab:
                return slot_id, resident_info
        return self._select_resident_slot_locked(project_id)

    def _select_resident_slot_locked(
        self,
        project_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[ResidentTabInfo]]:
        candidates = [
            (slot_id, resident_info)
            for slot_id, resident_info in self._resident_tabs.items()
            if resident_info and resident_info.tab
        ]
        if not candidates:
            return None, None

        # 共享打码池不再按 project_id 绑定；这里只根据“是否就绪 / 是否空闲 / 使用历史”
        # 做全局选择，避免 4 token/4 project 时把请求硬绑定到固定 tab。
        ready_idle = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if resident_info.recaptcha_ready and not resident_info.solve_lock.locked()
        ]
        ready_busy = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if resident_info.recaptcha_ready and resident_info.solve_lock.locked()
        ]
        cold_idle = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if not resident_info.recaptcha_ready and not resident_info.solve_lock.locked()
        ]

        pool = ready_idle or ready_busy or cold_idle or candidates
        pool.sort(key=lambda item: (item[1].last_used_at, item[1].use_count, item[1].created_at, item[0]))

        pick_index = self._resident_pick_index % len(pool)
        self._resident_pick_index = (self._resident_pick_index + 1) % max(len(candidates), 1)
        return pool[pick_index]

    async def _ensure_resident_tab(
        self,
        project_id: Optional[str] = None,
        *,
        force_create: bool = False,
        return_slot_key: bool = False,
    ):
        """确保共享打码标签页池中有可用 tab。

        逻辑：
        - 优先复用空闲 tab
        - 如果所有 tab 都忙且未到上限，继续扩容
        - 到达上限后允许请求排队等待已有 tab
        """
        def wrap(slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
            if return_slot_key:
                return slot_id, resident_info
            return resident_info

        async with self._resident_lock:
            slot_id, resident_info = self._select_resident_slot_locked(project_id)
            if self._resident_tabs:
                all_busy = all(info.solve_lock.locked() for info in self._resident_tabs.values())
            else:
                all_busy = True

            should_create = force_create or not resident_info or (all_busy and len(self._resident_tabs) < self._max_resident_tabs)
            if not should_create:
                return wrap(slot_id, resident_info)

            if len(self._resident_tabs) >= self._max_resident_tabs:
                return wrap(slot_id, resident_info)

        async with self._tab_build_lock:
            async with self._resident_lock:
                slot_id, resident_info = self._select_resident_slot_locked(project_id)
                if self._resident_tabs:
                    all_busy = all(info.solve_lock.locked() for info in self._resident_tabs.values())
                else:
                    all_busy = True

                should_create = force_create or not resident_info or (all_busy and len(self._resident_tabs) < self._max_resident_tabs)
                if not should_create:
                    return wrap(slot_id, resident_info)

                if len(self._resident_tabs) >= self._max_resident_tabs:
                    return wrap(slot_id, resident_info)

                new_slot_id = self._next_resident_slot_id()

            resident_info = await self._create_resident_tab(new_slot_id, project_id=project_id)
            if resident_info is None:
                async with self._resident_lock:
                    slot_id, fallback_info = self._select_resident_slot_locked(project_id)
                return wrap(slot_id, fallback_info)

            async with self._resident_lock:
                self._resident_tabs[new_slot_id] = resident_info
                self._sync_compat_resident_state()
                return wrap(new_slot_id, resident_info)

    async def _rebuild_resident_tab(
        self,
        project_id: Optional[str] = None,
        *,
        slot_id: Optional[str] = None,
        return_slot_key: bool = False,
    ):
        """重建共享池中的一个标签页。优先重建当前项目最近使用的 slot。"""
        def wrap(actual_slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
            if return_slot_key:
                return actual_slot_id, resident_info
            return resident_info

        async with self._tab_build_lock:
            async with self._resident_lock:
                actual_slot_id = slot_id
                if actual_slot_id is None:
                    actual_slot_id, _ = self._resolve_resident_slot_for_project_locked(project_id)

                old_resident = self._resident_tabs.pop(actual_slot_id, None) if actual_slot_id else None
                self._forget_project_affinity_for_slot_locked(actual_slot_id)
                if actual_slot_id:
                    self._resident_error_streaks.pop(actual_slot_id, None)
                self._sync_compat_resident_state()

            if old_resident:
                try:
                    async with old_resident.solve_lock:
                        await self._close_tab_quietly(old_resident.tab)
                except Exception:
                    await self._close_tab_quietly(old_resident.tab)

            actual_slot_id = actual_slot_id or self._next_resident_slot_id()
            resident_info = await self._create_resident_tab(actual_slot_id, project_id=project_id)
            if resident_info is None:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] slot={actual_slot_id}, project_id={project_id} 重建共享标签页失败"
                )
                return wrap(actual_slot_id, None)

            async with self._resident_lock:
                self._resident_tabs[actual_slot_id] = resident_info
                self._remember_project_affinity(project_id, actual_slot_id, resident_info)
                self._sync_compat_resident_state()
                return wrap(actual_slot_id, resident_info)

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

    async def _disconnect_browser_connection_quietly(self, browser_instance, reason: str):
        """尽量先关闭 DevTools websocket，减少 nodriver 后台任务在浏览器退场时炸栈。"""
        if not browser_instance:
            return

        connection = getattr(browser_instance, "connection", None)
        disconnect_method = getattr(connection, "disconnect", None) if connection else None
        if disconnect_method is None:
            return

        try:
            result = disconnect_method()
            if inspect.isawaitable(result):
                await self._run_with_timeout(
                    result,
                    timeout_seconds=5.0,
                    label=f"browser.disconnect:{reason}",
                )
            await asyncio.sleep(0)
        except Exception as e:
            if self._is_browser_runtime_error(e):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 浏览器连接关闭时检测到已断连状态 ({reason}): {e}"
                )
                return
            debug_logger.log_warning(
                f"[BrowserCaptcha] 浏览器连接关闭异常 ({reason}): {type(e).__name__}: {e}"
            )

    async def _stop_browser_process(self, browser_instance, reason: str = "browser_stop"):
        """兼容 nodriver 同步 stop API，安全停止浏览器进程。"""
        if not browser_instance:
            return

        await self._disconnect_browser_connection_quietly(browser_instance, reason=reason)

        stop_method = getattr(browser_instance, "stop", None)
        if stop_method is None:
            return
        result = stop_method()
        if inspect.isawaitable(result):
            await self._run_with_timeout(
                result,
                timeout_seconds=10.0,
                label="browser.stop",
            )

    async def _shutdown_browser_runtime_locked(self, reason: str):
        """在持有 _browser_lock 的前提下，彻底清理当前浏览器运行态。"""
        browser_instance = self.browser
        self.browser = None
        self._initialized = False
        self._last_fingerprint = None
        self._last_fingerprint_at = 0.0
        self._mark_browser_health(False)
        self._cleanup_proxy_extension()
        self._proxy_url = None

        async with self._resident_lock:
            resident_items = list(self._resident_tabs.values())
            self._resident_tabs.clear()
            self._project_resident_affinity.clear()
            self._resident_error_streaks.clear()
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
            await close_once(resident_info.tab)

        for item in custom_items:
            tab = item.get("tab") if isinstance(item, dict) else None
            await close_once(tab)

        if browser_instance:
            try:
                await self._stop_browser_process(browser_instance, reason=reason)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 停止浏览器实例失败 ({reason}): {e}"
                )

    async def _resolve_personal_proxy(self):
        """Read proxy config for personal captcha browser.
        Priority: captcha browser_proxy > request proxy."""
        if not self.db:
            return None, None, None, None, None
        try:
            captcha_cfg = await self.db.get_captcha_config()
            if captcha_cfg.browser_proxy_enabled and captcha_cfg.browser_proxy_url:
                url = captcha_cfg.browser_proxy_url.strip()
                if url:
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 使用验证码代理: {url}")
                    return _parse_proxy_url(url)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取验证码代理配置失败: {e}")
        try:
            proxy_cfg = await self.db.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                url = proxy_cfg.proxy_url.strip()
                if url:
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 回退使用请求代理: {url}")
                    return _parse_proxy_url(url)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取请求代理配置失败: {e}")
        return None, None, None, None, None

    def _cleanup_proxy_extension(self):
        """Remove temporary proxy auth extension directory."""
        if self._proxy_ext_dir and os.path.isdir(self._proxy_ext_dir):
            try:
                shutil.rmtree(self._proxy_ext_dir, ignore_errors=True)
            except Exception:
                pass
            self._proxy_ext_dir = None

    async def initialize(self):
        """初始化 nodriver 浏览器"""
        self._check_available()

        if (
            self._initialized
            and self.browser
            and not self.browser.stopped
            and self._is_browser_health_fresh()
        ):
            if self._idle_reaper_task is None or self._idle_reaper_task.done():
                self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
            return

        async with self._browser_lock:
            browser_needs_restart = False
            browser_executable_path = None
            display_value = os.environ.get("DISPLAY", "").strip()
            browser_args = []

            if self._initialized and self.browser:
                try:
                    if self.browser.stopped:
                        debug_logger.log_warning("[BrowserCaptcha] 浏览器已停止，准备重新初始化...")
                        self._mark_browser_health(False)
                        browser_needs_restart = True
                    elif self._is_browser_health_fresh():
                        if self._idle_reaper_task is None or self._idle_reaper_task.done():
                            self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
                        return
                    elif not await self._probe_browser_runtime():
                        debug_logger.log_warning("[BrowserCaptcha] 浏览器连接已失活，准备重新初始化...")
                        browser_needs_restart = True
                    else:
                        _patch_nodriver_runtime(self.browser)
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

            try:
                if self.user_data_dir:
                    debug_logger.log_info(f"[BrowserCaptcha] 正在启动 nodriver 浏览器 (用户数据目录: {self.user_data_dir})...")
                    os.makedirs(self.user_data_dir, exist_ok=True)
                else:
                    debug_logger.log_info(f"[BrowserCaptcha] 正在启动 nodriver 浏览器 (使用临时目录)...")

                browser_executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
                if browser_executable_path and not os.path.exists(browser_executable_path):
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 指定浏览器不存在，改为自动发现: {browser_executable_path}"
                    )
                    browser_executable_path = None
                if not browser_executable_path:
                    playwright_browser_path = _ensure_playwright_browser_path()
                    if playwright_browser_path:
                        browser_executable_path = playwright_browser_path
                        debug_logger.log_info(
                            f"[BrowserCaptcha] 复用 playwright chromium 作为 nodriver 浏览器: {browser_executable_path}"
                        )
                if browser_executable_path:
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 使用指定浏览器可执行文件: {browser_executable_path}"
                    )
                    try:
                        version_result = subprocess.run(
                            [browser_executable_path, "--version"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        version_output = (
                            (version_result.stdout or "").strip()
                            or (version_result.stderr or "").strip()
                            or "<empty>"
                        )
                        debug_logger.log_info(
                            "[BrowserCaptcha] 浏览器版本探测: "
                            f"rc={version_result.returncode}, output={version_output[:200]}"
                        )
                    except Exception as version_error:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 浏览器版本探测失败: {version_error}"
                        )

                # 解析代理配置
                self._cleanup_proxy_extension()
                self._proxy_url = None
                protocol, host, port, username, password = await self._resolve_personal_proxy()
                proxy_server_arg = None
                if protocol and host and port:
                    if username and password:
                        self._proxy_ext_dir = _create_proxy_auth_extension(protocol, host, port, username, password)
                        debug_logger.log_info(
                            f"[BrowserCaptcha] Personal 代理需要认证，已创建扩展: {self._proxy_ext_dir}"
                        )
                    proxy_server_arg = f"--proxy-server={protocol}://{host}:{port}"
                    self._proxy_url = f"{protocol}://{host}:{port}"
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 浏览器代理: {self._proxy_url}")

                launch_in_background = bool(getattr(config, "browser_launch_background", True))
                browser_args = [
                    '--disable-quic',
                    '--disable-features=UseDnsHttpsSvcb',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--disable-infobars',
                    '--hide-scrollbars',
                    '--window-size=1280,720',
                    '--profile-directory=Default',
                    '--disable-background-networking',
                    '--disable-sync',
                    '--disable-translate',
                    '--disable-default-apps',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--no-zygote',
                ]
                if launch_in_background and not self.headless:
                    browser_args.extend([
                        '--start-minimized',
                        '--disable-background-timer-throttling',
                        '--disable-renderer-backgrounding',
                        '--disable-backgrounding-occluded-windows',
                    ])
                    if sys.platform.startswith("win"):
                        browser_args.append('--window-position=-32000,-32000')
                    else:
                        browser_args.append('--window-position=3000,3000')
                    debug_logger.log_info("[BrowserCaptcha] Personal 有头浏览器将以后台模式启动")
                elif not self.headless:
                    debug_logger.log_info("[BrowserCaptcha] Personal 有头浏览器将以可见窗口模式启动")
                if proxy_server_arg:
                    browser_args.append(proxy_server_arg)
                if self._proxy_ext_dir:
                    browser_args.append(f'--load-extension={self._proxy_ext_dir}')
                else:
                    browser_args.append('--disable-extensions')

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
                    "sandbox": False,
                }
                launch_config = uc.Config(**launch_kwargs)
                effective_launch_args = launch_config()
                debug_logger.log_info(
                    "[BrowserCaptcha] nodriver 启动上下文: "
                    f"docker={IS_DOCKER}, display={display_value or '<empty>'}, "
                    f"uid={effective_uid}, headless={self.headless}, background={launch_in_background}, sandbox=False, "
                    f"executable={browser_executable_path or '<auto>'}, "
                    f"args={' '.join(effective_launch_args)}"
                )

                # 启动 nodriver 浏览器（后台启动，不占用前台）
                try:
                    self.browser = await self._run_with_timeout(
                        uc.start(**launch_kwargs),
                        timeout_seconds=30.0,
                        label="nodriver.start",
                    )
                except Exception as start_error:
                    error_text = str(start_error or "").lower()
                    needs_explicit_no_sandbox = "no_sandbox" in error_text or "root" in error_text
                    if not needs_explicit_no_sandbox:
                        raise

                    fallback_browser_args = list(browser_args)
                    if '--no-sandbox' not in fallback_browser_args:
                        fallback_browser_args.append('--no-sandbox')

                    fallback_kwargs = dict(launch_kwargs)
                    fallback_kwargs["browser_args"] = fallback_browser_args
                    fallback_kwargs["sandbox"] = True
                    fallback_config = uc.Config(**fallback_kwargs)
                    effective_launch_args = fallback_config()
                    debug_logger.log_warning(
                        "[BrowserCaptcha] nodriver 首次启动失败，使用显式 --no-sandbox 重试: "
                        f"{type(start_error).__name__}: {start_error}"
                    )
                    self.browser = await self._run_with_timeout(
                        uc.start(**fallback_kwargs),
                        timeout_seconds=30.0,
                        label="nodriver.start.retry_no_sandbox",
                    )

                _patch_nodriver_runtime(self.browser)
                self._initialized = True
                self._mark_browser_health(True)
                if self._idle_reaper_task is None or self._idle_reaper_task.done():
                    self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
                debug_logger.log_info(f"[BrowserCaptcha] ✅ nodriver 浏览器已启动 (Profile: {self.user_data_dir})")

            except Exception as e:
                self.browser = None
                self._initialized = False
                self._mark_browser_health(False)
                debug_logger.log_error(
                    "[BrowserCaptcha] ❌ 浏览器启动失败: "
                    f"{type(e).__name__}: {str(e)} | "
                    f"display={display_value or '<empty>'} | "
                    f"executable={browser_executable_path or '<auto>'} | "
                    f"args={' '.join(effective_launch_args) if effective_launch_args else '<none>'}"
                )
                raise

    async def warmup_resident_tabs(self, project_ids: Iterable[str], limit: Optional[int] = None) -> list[str]:
        """预热共享打码标签页池，减少首个请求的冷启动抖动。"""
        normalized_project_ids: list[str] = []
        seen_projects = set()
        for raw_project_id in project_ids:
            project_id = str(raw_project_id or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            normalized_project_ids.append(project_id)

        await self.initialize()

        try:
            warm_limit = self._max_resident_tabs if limit is None else max(1, min(self._max_resident_tabs, int(limit)))
        except Exception:
            warm_limit = self._max_resident_tabs

        warmed_slots: list[str] = []
        for index in range(warm_limit):
            warm_project_id = normalized_project_ids[index] if index < len(normalized_project_ids) else f"warmup-{index + 1}"
            slot_id, resident_info = await self._ensure_resident_tab(
                warm_project_id,
                force_create=True,
                return_slot_key=True,
            )
            if resident_info and resident_info.tab and slot_id:
                if slot_id not in warmed_slots:
                    warmed_slots.append(slot_id)
                continue
            debug_logger.log_warning(f"[BrowserCaptcha] 预热共享标签页失败 (seed={warm_project_id})")

        return warmed_slots

    # ========== 常驻模式 API ==========

    async def start_resident_mode(self, project_id: str):
        """启动常驻模式
        
        Args:
            project_id: 用于常驻的项目 ID
        """
        if not str(project_id or "").strip():
            debug_logger.log_warning("[BrowserCaptcha] 启动常驻模式失败：project_id 为空")
            return

        warmed_slots = await self.warmup_resident_tabs([project_id], limit=1)
        if warmed_slots:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 共享常驻打码池已启动 (seed_project: {project_id})")
            return

        debug_logger.log_error(f"[BrowserCaptcha] 常驻模式启动失败 (seed_project: {project_id})")

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
            self._resident_error_streaks.clear()
            self._sync_compat_resident_state()

        for resident_info in resident_items:
            if resident_info and resident_info.tab:
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
            except Exception:
                pass
            await asyncio.sleep(interval_seconds)
        return False

    def _is_server_side_flow_error(self, error_text: str) -> bool:
        error_lower = (error_text or "").lower()
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

    async def _clear_resident_storage_and_reload(self, project_id: str) -> bool:
        """清理常驻标签页的站点数据并刷新，尝试原地自愈。"""
        async with self._resident_lock:
            slot_id, resident_info = self._resolve_resident_slot_for_project_locked(project_id)

        if not resident_info or not resident_info.tab:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 没有可清理的共享标签页")
            return False

        try:
            async with resident_info.solve_lock:
                cleanup_summary = await self._clear_tab_site_storage(resident_info.tab)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id}, slot={slot_id} 已清理站点存储，准备刷新恢复: {cleanup_summary}"
                )

                resident_info.recaptcha_ready = False
                await self._tab_reload(
                    resident_info.tab,
                    label=f"clear_resident_reload:{slot_id or project_id}",
                )

                if not await self._wait_for_document_ready(resident_info.tab, retries=30, interval_seconds=1.0):
                    debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id}, slot={slot_id} 清理后页面加载超时")
                    return False

                resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
                if resident_info.recaptcha_ready:
                    resident_info.last_used_at = time.time()
                    self._remember_project_affinity(project_id, slot_id, resident_info)
                    self._resident_error_streaks.pop(slot_id, None)
                    debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id}, slot={slot_id} 清理后已恢复 reCAPTCHA")
                    return True

                debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id}, slot={slot_id} 清理后仍无法恢复 reCAPTCHA")
                return False
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id}, slot={slot_id} 清理或刷新失败: {e}")
            return False

    async def _recreate_resident_tab(self, project_id: str) -> bool:
        """关闭并重建常驻标签页。"""
        slot_id, resident_info = await self._rebuild_resident_tab(project_id, return_slot_key=True)
        if resident_info is None:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 重建共享标签页失败")
            return False
        debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 已重建共享标签页 slot={slot_id}")
        return True

    async def _restart_browser_for_project(self, project_id: str) -> bool:
        async with self._runtime_recover_lock:
            if self._was_runtime_restarted_recently():
                try:
                    if await self._probe_browser_runtime():
                        slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)
                        if resident_info is not None and slot_id:
                            self._remember_project_affinity(project_id, slot_id, resident_info)
                            self._resident_error_streaks.pop(slot_id, None)
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] project_id={project_id} 检测到最近已完成浏览器恢复，复用当前运行态 (slot={slot_id})"
                            )
                            return True
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id} 复用最近恢复运行态失败，继续执行整浏览器重启: {e}"
                    )

            restarted = await self._restart_browser_for_project_unlocked(project_id)
            if restarted:
                self._mark_runtime_restart()
            return restarted

    async def _restart_browser_for_project_unlocked(self, project_id: str) -> bool:
        """重启整个 nodriver 浏览器，并恢复共享打码池。"""
        async with self._resident_lock:
            restore_slots = max(1, min(self._max_resident_tabs, len(self._resident_tabs) or 1))
            restore_project_ids: list[str] = []
            seen_projects = set()
            for candidate in [project_id, *self._project_resident_affinity.keys()]:
                normalized_project_id = str(candidate or "").strip()
                if not normalized_project_id or normalized_project_id in seen_projects:
                    continue
                seen_projects.add(normalized_project_id)
                restore_project_ids.append(normalized_project_id)
                if len(restore_project_ids) >= restore_slots:
                    break

        debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 准备重启 nodriver 浏览器以恢复")
        await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=f"restart_project:{project_id}")

        warmed_slots = await self.warmup_resident_tabs(restore_project_ids, limit=restore_slots)
        if not warmed_slots:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 浏览器重启后恢复共享标签页失败")
            return False

        slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)
        if resident_info is None or not slot_id:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 浏览器重启后无法定位可用共享标签页")
            return False

        self._remember_project_affinity(project_id, slot_id, resident_info)
        self._resident_error_streaks.pop(slot_id, None)
        debug_logger.log_warning(
            f"[BrowserCaptcha] project_id={project_id} 浏览器重启后已恢复共享标签页池 "
            f"(slots={len(warmed_slots)}, active_slot={slot_id})"
        )
        return True

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        """上游生成接口异常时，对常驻标签页执行自愈恢复。"""
        if not project_id:
            return

        async with self._resident_lock:
            slot_id, _ = self._resolve_resident_slot_for_project_locked(project_id)

        if not slot_id:
            return

        streak = self._resident_error_streaks.get(slot_id, 0) + 1
        self._resident_error_streaks[slot_id] = streak
        error_text = f"{error_reason or ''} {error_message or ''}".strip()
        error_lower = error_text.lower()
        debug_logger.log_warning(
            f"[BrowserCaptcha] project_id={project_id}, slot={slot_id} 收到上游异常，streak={streak}, reason={error_reason}, detail={error_message[:200]}"
        )

        if not self._initialized or not self.browser:
            return

        # 403 错误：先清理缓存再重建
        if "403" in error_text or "forbidden" in error_lower or "recaptcha" in error_lower:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 检测到 403/reCAPTCHA 错误，清理缓存并重建"
            )
            healed = await self._clear_resident_storage_and_reload(project_id)
            if not healed:
                await self._recreate_resident_tab(project_id)
            return

        # 服务端错误：根据连续失败次数决定恢复策略
        if self._is_server_side_flow_error(error_text):
            recreate_threshold = max(2, int(getattr(config, "browser_personal_recreate_threshold", 2) or 2))
            restart_threshold = max(3, int(getattr(config, "browser_personal_restart_threshold", 3) or 3))

            if streak >= restart_threshold:
                await self._restart_browser_for_project(project_id)
                return
            if streak >= recreate_threshold:
                await self._recreate_resident_tab(project_id)
                return

            healed = await self._clear_resident_storage_and_reload(project_id)
            if not healed:
                await self._recreate_resident_tab(project_id)
            return

        # 其他错误：直接重建标签页
        await self._recreate_resident_tab(project_id)

    async def _wait_for_recaptcha(self, tab) -> bool:
        """等待 reCAPTCHA 加载

        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[BrowserCaptcha] 注入 reCAPTCHA 脚本...")

        # 注入 reCAPTCHA Enterprise 脚本
        await self._tab_evaluate(tab, f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/recaptcha/enterprise.js?render={self.website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """, label="inject_recaptcha_script", timeout_seconds=5.0)

        # 等待 reCAPTCHA 加载（减少等待时间）
        for i in range(15):  # 减少到15次，最多7.5秒
            try:
                is_ready = await self._tab_evaluate(
                    tab,
                    "typeof grecaptcha !== 'undefined' && "
                    "typeof grecaptcha.enterprise !== 'undefined' && "
                    "typeof grecaptcha.enterprise.execute === 'function'",
                    label="check_recaptcha_ready",
                    timeout_seconds=2.5,
                )

                if is_ready:
                    debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA 已就绪 (等待了 {i * 0.5}s)")
                    return True

                await tab.sleep(0.5)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 检查 reCAPTCHA 时异常: {e}")
                await tab.sleep(0.3)  # 异常时减少等待时间

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
        await self._tab_evaluate(tab, f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/{script_path}?render={website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """, label="inject_custom_recaptcha_script", timeout_seconds=5.0)

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
                (() => {
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
                })()
            """, label="extract_tab_fingerprint", timeout_seconds=8.0)
            if not isinstance(fingerprint, dict):
                return None

            result: Dict[str, Any] = {"proxy_url": self._proxy_url}
            for key in ("user_agent", "accept_language", "sec_ch_ua", "sec_ch_ua_mobile", "sec_ch_ua_platform"):
                value = fingerprint.get(key)
                if isinstance(value, str) and value:
                    result[key] = value
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
        success_label: str,
    ) -> Optional[str]:
        """在共享常驻标签页上执行一次打码，并统一更新成功态。"""
        if not resident_info or not resident_info.tab or not resident_info.recaptcha_ready:
            return None

        start_time = time.time()
        async with resident_info.solve_lock:
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
        self._remember_project_affinity(project_id, slot_id, resident_info)
        self._resident_error_streaks.pop(slot_id, None)
        self._mark_browser_health(True)
        if resident_info.fingerprint:
            self._remember_fingerprint(resident_info.fingerprint)
        else:
            resident_info.fingerprint = await self._refresh_last_fingerprint(resident_info.tab)
        debug_logger.log_info(
            f"[BrowserCaptcha] ✅ Token生成成功（slot={slot_id}, 耗时 {duration_ms:.0f}ms, 使用次数: {resident_info.use_count}）"
        )
        return token

    # ========== 主要 API ==========

    async def get_token(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
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
        debug_logger.log_info(f"[BrowserCaptcha] get_token 开始: project_id={project_id}, action={action}, 当前标签页数={len(self._resident_tabs)}/{self._max_resident_tabs}")

        # 确保浏览器已初始化
        await self.initialize()

        debug_logger.log_info(
            f"[BrowserCaptcha] 开始从共享打码池获取标签页 (project: {project_id}, 当前: {len(self._resident_tabs)}/{self._max_resident_tabs})"
        )
        slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)
        if resident_info is None or not slot_id:
            if not await self._probe_browser_runtime():
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 共享标签页池为空且浏览器疑似失活，尝试重启恢复 (project: {project_id})"
                )
                if await self._recover_browser_runtime(project_id, reason="ensure_resident_tab"):
                    slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)

        if resident_info is None or not slot_id:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 共享标签页池不可用，fallback 到传统模式 (project: {project_id})"
            )
            return await self._get_token_legacy(project_id, action)

        debug_logger.log_info(
            f"[BrowserCaptcha] ✅ 共享标签页可用 (slot={slot_id}, project={project_id}, use_count={resident_info.use_count})"
        )

        if resident_info and resident_info.tab and not resident_info.recaptcha_ready:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 共享标签页未就绪，准备重建 cold slot={slot_id}, project={project_id}"
            )
            slot_id, resident_info = await self._rebuild_resident_tab(
                project_id,
                slot_id=slot_id,
                return_slot_key=True,
            )
            if resident_info is None:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] cold slot 重建失败，升级为浏览器级恢复 (slot={slot_id}, project={project_id})"
                )
                if await self._recover_browser_runtime(project_id, reason=f"cold_resident_tab:{slot_id or 'unknown'}"):
                    slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)

        # 使用常驻标签页生成 token（在锁外执行，避免阻塞）
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
                    success_label="resident_solve",
                )
                if token:
                    return token
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 共享标签页生成失败 (slot={slot_id}, project={project_id})，尝试重建..."
                )
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 共享标签页异常 (slot={slot_id}): {e}，尝试重建...")
                if self._is_browser_runtime_error(e):
                    runtime_recovered = await self._recover_browser_runtime(
                        project_id,
                        reason=f"resident_solve:{slot_id}",
                    )
                    if runtime_recovered:
                        slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)
                        if resident_info and slot_id:
                            try:
                                token = await self._solve_with_resident_tab(
                                    slot_id,
                                    project_id,
                                    resident_info,
                                    action,
                                    success_label="resident_solve_after_runtime_recover",
                                )
                                if token:
                                    return token
                            except Exception as retry_error:
                                debug_logger.log_warning(
                                    f"[BrowserCaptcha] 浏览器重启恢复后共享标签页仍失败 (slot={slot_id}): {retry_error}"
                                )

            if not runtime_recovered:
                # 常驻标签页失效，尝试重建
                debug_logger.log_info(f"[BrowserCaptcha] 开始重建共享标签页 (slot={slot_id}, project={project_id})")
                slot_id, resident_info = await self._rebuild_resident_tab(
                    project_id,
                    slot_id=slot_id,
                    return_slot_key=True,
                )
                debug_logger.log_info(f"[BrowserCaptcha] 共享标签页重建结束 (slot={slot_id}, project={project_id})")
                if resident_info is None:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 共享标签页重建返回空，升级为浏览器级恢复 (slot={slot_id}, project={project_id})"
                    )
                    if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild_empty:{slot_id or 'unknown'}"):
                        slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)

                # 重建后立即尝试生成（在锁外执行）
                if resident_info:
                    try:
                        token = await self._solve_with_resident_tab(
                            slot_id,
                            project_id,
                            resident_info,
                            action,
                            success_label="resident_resolve_after_rebuild",
                        )
                        if token:
                            debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Token生成成功 (slot={slot_id})")
                            return token
                    except Exception as rebuild_error:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 重建标签页后仍无法打码 (slot={slot_id}): {rebuild_error}"
                        )
                        if self._is_browser_runtime_error(rebuild_error):
                            if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild:{slot_id}"):
                                slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)
                                if resident_info and slot_id:
                                    try:
                                        token = await self._solve_with_resident_tab(
                                            slot_id,
                                            project_id,
                                            resident_info,
                                            action,
                                            success_label="resident_resolve_after_browser_restart",
                                        )
                                        if token:
                                            return token
                                    except Exception as restart_error:
                                        debug_logger.log_warning(
                                            f"[BrowserCaptcha] 浏览器重启后 resident 仍失败 (slot={slot_id}): {restart_error}"
                                        )
                elif not await self._probe_browser_runtime():
                    if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild_empty:{slot_id}"):
                        slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)
                        if resident_info and slot_id:
                            try:
                                token = await self._solve_with_resident_tab(
                                    slot_id,
                                    project_id,
                                    resident_info,
                                    action,
                                    success_label="resident_resolve_after_empty_recover",
                                )
                                if token:
                                    return token
                            except Exception as empty_recover_error:
                                debug_logger.log_warning(
                                    f"[BrowserCaptcha] 浏览器空恢复后 resident 仍失败 (slot={slot_id}): {empty_recover_error}"
                                )

        # 最终 Fallback: 使用传统模式
        debug_logger.log_warning(f"[BrowserCaptcha] 所有常驻方式失败，fallback 到传统模式 (project: {project_id})")
        legacy_token = await self._get_token_legacy(project_id, action)
        if legacy_token:
            if slot_id:
                self._resident_error_streaks.pop(slot_id, None)
        return legacy_token

    async def _create_resident_tab(self, slot_id: str, project_id: Optional[str] = None) -> Optional[ResidentTabInfo]:
        """创建一个共享常驻打码标签页

        Args:
            slot_id: 共享标签页槽位 ID
            project_id: 触发创建的项目 ID，仅用于日志和最近映射

        Returns:
            ResidentTabInfo 对象，或 None（创建失败）
        """
        try:
            # 使用 Flow API 地址作为基础页面
            website_url = "https://labs.google/fx/api/auth/providers"
            debug_logger.log_info(f"[BrowserCaptcha] 创建共享常驻标签页 slot={slot_id}, seed_project={project_id}")

            async with self._resident_lock:
                existing_tabs = [info.tab for info in self._resident_tabs.values() if info.tab]

            # 获取或创建标签页
            browser = self.browser
            if browser is None or getattr(browser, "stopped", False):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建共享常驻标签页前浏览器不可用 (slot={slot_id}, project={project_id})"
                )
                return None

            tabs = list(getattr(browser, "tabs", []) or [])
            available_tab = None

            # 查找未被占用的标签页
            for tab in tabs:
                if tab not in existing_tabs:
                    available_tab = tab
                    break

            if available_tab:
                tab = available_tab
                debug_logger.log_info(f"[BrowserCaptcha] 复用未占用的标签页")
                await self._tab_get(
                    tab,
                    website_url,
                    label=f"resident_tab_get:{slot_id}",
                )
            else:
                debug_logger.log_info(f"[BrowserCaptcha] 创建新标签页")
                tab = await self._browser_get(
                    website_url,
                    label=f"resident_browser_get:{slot_id}",
                    new_tab=True,
                )

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
                    debug_logger.log_warning(f"[BrowserCaptcha] 等待页面异常: {e}，重试 {retry + 1}/10...")
                    await asyncio.sleep(0.3)  # 减少重试间隔

            if not page_loaded:
                debug_logger.log_error(f"[BrowserCaptcha] 页面加载超时 (slot={slot_id}, project={project_id})")
                await self._close_tab_quietly(tab)
                return None

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 加载失败 (slot={slot_id}, project={project_id})")
                await self._close_tab_quietly(tab)
                return None

            # 创建常驻信息对象
            resident_info = ResidentTabInfo(tab, slot_id, project_id=project_id)
            resident_info.recaptcha_ready = True
            resident_info.fingerprint = await self._refresh_last_fingerprint(tab)
            self._mark_browser_health(True)

            debug_logger.log_info(f"[BrowserCaptcha] ✅ 共享常驻标签页创建成功 (slot={slot_id}, project={project_id})")
            return resident_info

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 创建共享常驻标签页异常 (slot={slot_id}, project={project_id}): {e}")
            return None

    async def _close_resident_tab(self, slot_id: str):
        """关闭指定 slot 的共享常驻标签页

        Args:
            slot_id: 共享标签页槽位 ID
        """
        async with self._resident_lock:
            resident_info = self._resident_tabs.pop(slot_id, None)
            self._forget_project_affinity_for_slot_locked(slot_id)
            self._resident_error_streaks.pop(slot_id, None)
            self._sync_compat_resident_state()

        if resident_info and resident_info.tab:
            try:
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

    async def _get_token_legacy(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
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

                try:
                    website_url = "https://labs.google/fx/api/auth/providers"
                    debug_logger.log_info(
                        f"[BrowserCaptcha] [Legacy] 创建独立临时标签页执行验证，避免污染 resident/custom 页面: {website_url}"
                    )
                    tab = await self._browser_get(
                        website_url,
                        label=f"legacy_browser_get:{project_id}",
                        new_tab=True,
                    )

                    # 等待页面完全加载（增加等待时间）
                    debug_logger.log_info("[BrowserCaptcha] [Legacy] 等待页面加载...")
                    await tab.sleep(3)

                    # 等待页面 DOM 完成
                    for _ in range(10):
                        ready_state = await self._tab_evaluate(
                            tab,
                            "document.readyState",
                            label=f"legacy_document_ready:{project_id}",
                            timeout_seconds=2.0,
                        )
                        if ready_state == "complete":
                            break
                        await tab.sleep(0.5)

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
                        self._mark_browser_health(True)
                        await self._refresh_last_fingerprint(tab)
                        debug_logger.log_info(f"[BrowserCaptcha] [Legacy] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
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
            debug_logger.log_info("[BrowserCaptcha] 开始清理浏览器缓存...")

            # 使用 Chrome DevTools Protocol 清理缓存
            # 清理所有类型的缓存数据
            await self._browser_send_command(
                "Network.clearBrowserCache",
                label="clear_browser_cache",
            )

            # 清理 Cookies
            await self._browser_send_command(
                "Network.clearBrowserCookies",
                label="clear_browser_cookies",
            )

            # 清理存储数据（localStorage, sessionStorage, IndexedDB 等）
            await self._browser_send_command(
                "Storage.clearDataForOrigin",
                {
                    "origin": "https://www.google.com",
                    "storageTypes": "all"
                },
                label="clear_browser_origin_storage",
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
        tab = await self._browser_get(
            "https://accounts.google.com/",
            label="open_login_window",
            new_tab=True,
        )
        debug_logger.log_info("[BrowserCaptcha] 请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")
        print("请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")

    # ========== Session Token 刷新 ==========

    async def refresh_session_token(self, project_id: str) -> Optional[str]:
        """从常驻标签页获取最新的 Session Token
        
        复用共享打码标签页，通过刷新页面并从 cookies 中提取
        __Secure-next-auth.session-token
        
        Args:
            project_id: 项目ID，用于定位常驻标签页
            
        Returns:
            新的 Session Token，如果获取失败返回 None
        """
        for attempt in range(2):
            # 确保浏览器已初始化
            await self.initialize()

            start_time = time.time()
            debug_logger.log_info(f"[BrowserCaptcha] 开始刷新 Session Token (project: {project_id}, attempt={attempt + 1})...")

            async with self._resident_lock:
                slot_id = self._resolve_affinity_slot_locked(project_id)
                resident_info = self._resident_tabs.get(slot_id) if slot_id else None

            if resident_info is None or not slot_id:
                slot_id, resident_info = await self._ensure_resident_tab(project_id, return_slot_key=True)

            if resident_info is None or not slot_id:
                if attempt == 0 and not await self._probe_browser_runtime():
                    await self._recover_browser_runtime(project_id, reason="refresh_session_prepare")
                    continue
                debug_logger.log_warning(f"[BrowserCaptcha] 无法为 project_id={project_id} 获取共享常驻标签页")
                return None

            if not resident_info or not resident_info.tab:
                debug_logger.log_error(f"[BrowserCaptcha] 无法获取常驻标签页")
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

                slot_id, resident_info = await self._rebuild_resident_tab(project_id, slot_id=slot_id, return_slot_key=True)
                if resident_info and slot_id:
                    try:
                        async with resident_info.solve_lock:
                            cookies = await self._get_browser_cookies(
                                label=f"refresh_session_get_cookies_after_rebuild:{slot_id}",
                            )
                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                resident_info.last_used_at = time.time()
                                self._remember_project_affinity(project_id, slot_id, resident_info)
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
