"""
Browser automation for reCAPTCHA token acquisition
Using nodriver (undetected-chromedriver successor) for anti-detection browser
Supports resident mode: auto-creates persistent tabs per project_id for instant token generation
"""
import asyncio
import time
import os
import sys
import subprocess
from typing import Optional, Dict, Any

from ..core.logger import debug_logger
from ..core.config import config


# ==================== Docker environment detection ====================
def _is_running_in_docker() -> bool:
    """Detect if running in Docker container"""
    # Method 1: check /.dockerenv file
    if os.path.exists('/.dockerenv'):
        return True
    # Method 2: check cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except:
        pass
    # Method 3: check environment variables
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


def _is_truthy_env(name: str) -> bool:
    """Check if environment variable is truthy."""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DOCKER_HEADED = (
    _is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA")
    or _is_truthy_env("ALLOW_DOCKER_BROWSER_CAPTCHA")
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED


# ==================== nodriver auto-installation ====================
def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """Run pip install command
    
    Args:
        package: Package name
        use_mirror: Whether to use Chinese mirror
    
    Returns:
        Whether installation succeeded
    """
    cmd = [sys.executable, '-m', 'pip', 'install', package]
    if use_mirror:
        cmd.extend(['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])
    
    try:
        debug_logger.log_info(f"[BrowserCaptcha] Installing {package}...")
        print(f"[BrowserCaptcha] Installing {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ {package} installed successfully")
            print(f"[BrowserCaptcha] ✅ {package} installed successfully")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] {package} installation failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] {package} installation error: {e}")
        return False


def _ensure_nodriver_installed() -> bool:
    """Ensure nodriver is installed
    
    Returns:
        Whether installation succeeded/is installed
    """
    try:
        import nodriver
        debug_logger.log_info("[BrowserCaptcha] nodriver is installed")
        return True
    except ImportError:
        pass
    
    debug_logger.log_info("[BrowserCaptcha] nodriver not installed, starting auto-installation...")
    print("[BrowserCaptcha] nodriver not installed, starting auto-installation...")
    
    # First try official source
    if _run_pip_install('nodriver', use_mirror=False):
        return True
    
    # Official source failed, attempting Chinese mirror
    debug_logger.log_info("[BrowserCaptcha] official source installation failed, attempting Chinese mirror...")
    print("[BrowserCaptcha] official source installation failed, attempting Chinese mirror...")
    if _run_pip_install('nodriver', use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ nodriver auto-installation failed, please install manually: pip install nodriver")
    print("[BrowserCaptcha] ❌ nodriver auto-installation failed, please install manually: pip install nodriver")
    return False


# Try importing nodriver
uc = None
NODRIVER_AVAILABLE = False

if DOCKER_HEADED_BLOCKED:
    debug_logger.log_warning(
        "[BrowserCaptcha] Docker environment detected, built-in browser captcha disabled by default."
        "To enable, set ALLOW_DOCKER_HEADED_CAPTCHA=true and provide DISPLAY/Xvfb."
    )
    print("[BrowserCaptcha] ⚠️ Docker environment detected, built-in browser captcha disabled by default")
    print("[BrowserCaptcha] To enable, set ALLOW_DOCKER_HEADED_CAPTCHA=true and provide DISPLAY/Xvfb")
else:
    if IS_DOCKER and ALLOW_DOCKER_HEADED:
        debug_logger.log_warning(
            "[BrowserCaptcha] Docker built-in browser captcha whitelist enabled, ensure DISPLAY/Xvfb is available"
        )
        print("[BrowserCaptcha] ✅ Docker built-in browser captcha whitelist enabled")
    if _ensure_nodriver_installed():
        try:
            import nodriver as uc
            NODRIVER_AVAILABLE = True
        except ImportError as e:
            debug_logger.log_error(f"[BrowserCaptcha] nodriver import failed: {e}")
            print(f"[BrowserCaptcha] ❌ nodriver import failed: {e}")


class ResidentTabInfo:
    """Resident tab info structure"""
    def __init__(self, tab, project_id: str):
        self.tab = tab
        self.project_id = project_id
        self.recaptcha_ready = False
        self.created_at = time.time()


class BrowserCaptchaService:
    """Browser automation for reCAPTCHA token acquisition (nodriver headed mode)
    
    Supports two modes:
    1. Resident Mode: maintains persistent tabs per project_id for instant token generation
    2. Legacy Mode: creates new tab per request (fallback)
    """

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """Initialize service"""
        self.headless = False  # nodriver headed mode
        self.browser = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        # Persistent profile directory
        self.user_data_dir = os.path.join(os.getcwd(), "browser_data")
        
        # Resident mode properties (multi project_id support)
        self._resident_tabs: dict[str, 'ResidentTabInfo'] = {}  # project_id -> resident tab info
        self._resident_lock = asyncio.Lock()  # Protect resident tab operations
        
        # Backward compatible API (retain single resident properties as aliases)
        self.resident_project_id: Optional[str] = None  # Backward compatible
        self.resident_tab = None                         # Backward compatible
        self._running = False                            # Backward compatible
        self._recaptcha_ready = False                    # Backward compatible
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self._resident_error_streaks: dict[str, int] = {}
        # Custom site captcha resident page (for score-test)
        self._custom_tabs: dict[str, Dict[str, Any]] = {}
        self._custom_lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """Get singleton instance"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
        return cls._instance
    
    def _check_available(self):
        """Check if service is available"""
        if DOCKER_HEADED_BLOCKED:
            raise RuntimeError(
                "Docker environment detected, built-in browser captcha disabled by default."
                "To enable, set ALLOW_DOCKER_HEADED_CAPTCHA=true and provide DISPLAY/Xvfb."
            )
        if IS_DOCKER and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Docker built-in browser captcha enabled, but DISPLAY not set."
                "Please set DISPLAY (e.g. :99) and start Xvfb."
            )
        if not NODRIVER_AVAILABLE or uc is None:
            raise RuntimeError(
                "nodriver is not installed or unavailable."
                "Please install manually: pip install nodriver"
            )

    async def initialize(self):
        """Initialize nodriver browser"""
        # Check if service is available
        self._check_available()
        
        if self._initialized and self.browser:
            # Check if browser is still alive
            try:
                # Try to get browser info to verify it's alive
                if self.browser.stopped:
                    debug_logger.log_warning("[BrowserCaptcha] Browser stopped, reinitializing...")
                    self._initialized = False
                else:
                    return
            except Exception:
                debug_logger.log_warning("[BrowserCaptcha] Browser unresponsive, reinitializing...")
                self._initialized = False

        try:
            debug_logger.log_info(f"[BrowserCaptcha] Starting nodriver browser (user data directory: {self.user_data_dir})...")

            # Ensure user_data_dir exists
            os.makedirs(self.user_data_dir, exist_ok=True)

            browser_executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
            if browser_executable_path:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Using specified browser executable: {browser_executable_path}"
                )

            # Launch nodriver browser
            self.browser = await uc.start(
                headless=self.headless,
                user_data_dir=self.user_data_dir,
                browser_executable_path=browser_executable_path,
                sandbox=False,  # nodriver needs this to disable sandbox
                browser_args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--window-size=1280,720',
                    '--profile-directory=Default',  # Skip Profile selector page
                ]
            )

            self._initialized = True
            debug_logger.log_info(f"[BrowserCaptcha] ✅ nodriver browser started (Profile: {self.user_data_dir})")

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ Browser launch failed: {str(e)}")
            raise

    # ========== Resident mode API ==========

    async def start_resident_mode(self, project_id: str):
        """Start resident mode
        
        Args:
            project_id: Project ID for resident mode
        """
        if self._running:
            debug_logger.log_warning("[BrowserCaptcha] Resident mode already running")
            return
        
        await self.initialize()
        
        self.resident_project_id = project_id
        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        
        debug_logger.log_info(f"[BrowserCaptcha] Start resident mode, navigating to page: {website_url}")
        
        # Create an independent new tab (don't use main_tab to avoid reclamation)
        self.resident_tab = await self.browser.get(website_url, new_tab=True)
        
        debug_logger.log_info("[BrowserCaptcha] Tab created, waiting for page load...")
        
        # Wait for page load with retry mechanism
        page_loaded = False
        for retry in range(60):
            try:
                await asyncio.sleep(1)
                ready_state = await self.resident_tab.evaluate("document.readyState")
                debug_logger.log_info(f"[BrowserCaptcha] Page state: {ready_state} (retry {retry + 1}/60)")
                if ready_state == "complete":
                    page_loaded = True
                    break
            except ConnectionRefusedError as e:
                debug_logger.log_warning(f"[BrowserCaptcha] Tab connection lost: {e}, attempting to reconnect...")
                # Tab may be closed, try recreating
                try:
                    self.resident_tab = await self.browser.get(website_url, new_tab=True)
                    debug_logger.log_info("[BrowserCaptcha] Tab recreated")
                except Exception as e2:
                    debug_logger.log_error(f"[BrowserCaptcha] Tab recreation failed: {e2}")
                await asyncio.sleep(2)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] Page wait exception: {e}, retry {retry + 1}/15...")
                await asyncio.sleep(2)
        
        if not page_loaded:
            debug_logger.log_error("[BrowserCaptcha] Page load timeout, resident mode startup failed")
            return
        
        # Wait for reCAPTCHA to load
        self._recaptcha_ready = await self._wait_for_recaptcha(self.resident_tab)
        
        if not self._recaptcha_ready:
            debug_logger.log_error("[BrowserCaptcha] reCAPTCHA load failed, resident mode startup failed")
            return
        
        self._running = True
        debug_logger.log_info(f"[BrowserCaptcha] ✅ Resident mode started (project: {project_id})")

    async def stop_resident_mode(self, project_id: Optional[str] = None):
        """Stop resident mode
        
        Args:
            project_id: Specify project_id to close, or None to close all resident tabs
        """
        async with self._resident_lock:
            if project_id:
                # Close specified resident tab
                await self._close_resident_tab(project_id)
                self._resident_error_streaks.pop(project_id, None)
                debug_logger.log_info(f"[BrowserCaptcha] Closed resident mode for project_id={project_id}")
            else:
                # Close all resident tabs
                project_ids = list(self._resident_tabs.keys())
                for pid in project_ids:
                    resident_info = self._resident_tabs.pop(pid, None)
                    if resident_info and resident_info.tab:
                        try:
                            await resident_info.tab.close()
                        except Exception:
                            pass
                self._resident_error_streaks.clear()
                debug_logger.log_info(f"[BrowserCaptcha] Closed all resident tabs ({len(project_ids)} total)")
        
        # Backward compatible: clear old properties
        if not self._running:
            return
        
        self._running = False
        if self.resident_tab:
            try:
                await self.resident_tab.close()
            except Exception:
                pass
            self.resident_tab = None
        
        self.resident_project_id = None
        self._recaptcha_ready = False

    async def _wait_for_document_ready(self, tab, retries: int = 30, interval_seconds: float = 1.0) -> bool:
        """Wait for document to be ready."""
        for _ in range(retries):
            try:
                ready_state = await tab.evaluate("document.readyState")
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
        """Clear local storage state for the current site while preserving cookie login state."""
        result = await tab.evaluate("""
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
        """)
        return result if isinstance(result, dict) else {}

    async def _clear_resident_storage_and_reload(self, project_id: str) -> bool:
        """Clear resident tab site data and refresh, attempting self-healing."""
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)

        if not resident_info or not resident_info.tab:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} no cleanable resident tabs")
            return False

        try:
            cleanup_summary = await self._clear_tab_site_storage(resident_info.tab)
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} Cleaned site storage, preparing refresh recovery: {cleanup_summary}"
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Site storage cleanup failed: {e}")
            return False

        try:
            resident_info.recaptcha_ready = False
            await resident_info.tab.reload()
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Tab refresh after cleanup failed: {e}")
            return False

        if not await self._wait_for_document_ready(resident_info.tab, retries=30, interval_seconds=1.0):
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Page load timeout after cleanup")
            return False

        resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
        if resident_info.recaptcha_ready:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Recovery after cleanup reCAPTCHA")
            return True

        debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Still unable to recover after cleanup reCAPTCHA")
        return False

    async def _recreate_resident_tab(self, project_id: str) -> bool:
        """Close and rebuild resident tabs."""
        async with self._resident_lock:
            await self._close_resident_tab(project_id)
            resident_info = await self._create_resident_tab(project_id)
            if resident_info is None:
                debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Resident tab rebuild failed")
                return False
            self._resident_tabs[project_id] = resident_info
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Resident tabs rebuilt")
            return True

    async def _restart_browser_for_project(self, project_id: str) -> bool:
        """Restart nodriver browser and restore specified project resident tabs."""
        debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Preparing to restart nodriver browser to recover")
        await self.close()
        await self.initialize()

        async with self._resident_lock:
            resident_info = await self._create_resident_tab(project_id)
            if resident_info is None:
                debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Resident tab restoration after browser restart failed")
                return False
            self._resident_tabs[project_id] = resident_info
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} Resident tabs restored after browser restart")
            return True

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        """When upstream generation API errors, perform self-healing on resident tabs."""
        if not project_id:
            return

        streak = self._resident_error_streaks.get(project_id, 0) + 1
        self._resident_error_streaks[project_id] = streak
        error_text = f"{error_reason or ''} {error_message or ''}".strip()
        debug_logger.log_warning(
            f"[BrowserCaptcha] project_id={project_id} received upstream error, streak={streak}, reason={error_reason}, detail={error_message[:200]}"
        )

        if not self._initialized or not self.browser:
            return

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

        await self._recreate_resident_tab(project_id)

    async def _wait_for_recaptcha(self, tab) -> bool:
        """Wait for reCAPTCHA to load
        
        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[BrowserCaptcha] detect reCAPTCHA...")
        
        # Check grecaptcha.enterprise.execute
        is_enterprise = await tab.evaluate(
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
        )
        
        if is_enterprise:
            debug_logger.log_info("[BrowserCaptcha] reCAPTCHA Enterprise loaded")
            return True
        
        # Attempting script injection
        debug_logger.log_info("[BrowserCaptcha] reCAPTCHA not detected, injecting script...")
        
        await tab.evaluate(f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/recaptcha/api.js?render={self.website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """)
        
        # Waiting for script to load
        await tab.sleep(3)
        
        # Poll and wait for reCAPTCHA to load
        for i in range(20):
            is_enterprise = await tab.evaluate(
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
            )
            
            if is_enterprise:
                debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA Enterprise loaded (waited {i * 0.5} seconds)")
                return True
            await tab.sleep(0.5)
        
        debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA load timeout")
        return False

    async def _wait_for_custom_recaptcha(
        self,
        tab,
        website_key: str,
        enterprise: bool = False,
    ) -> bool:
        """Wait for any site reCAPTCHA to load, for score test."""
        debug_logger.log_info("[BrowserCaptcha] Detecting custom reCAPTCHA...")

        ready_check = (
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
            "typeof grecaptcha.enterprise.execute === 'function'"
        ) if enterprise else (
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
        )
        script_path = "recaptcha/enterprise.js" if enterprise else "recaptcha/api.js"
        label = "Enterprise" if enterprise else "V3"

        is_ready = await tab.evaluate(ready_check)
        if is_ready:
            debug_logger.log_info(f"[BrowserCaptcha] Custom reCAPTCHA {label} loaded")
            return True

        debug_logger.log_info("[BrowserCaptcha] Custom reCAPTCHA not detected, injecting script...")
        await tab.evaluate(f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/{script_path}?render={website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """)

        await tab.sleep(3)
        for i in range(20):
            is_ready = await tab.evaluate(ready_check)
            if is_ready:
                debug_logger.log_info(f"[BrowserCaptcha] Custom reCAPTCHA {label} loaded (waited {i * 0.5} seconds)")
                return True
            await tab.sleep(0.5)

        debug_logger.log_warning("[BrowserCaptcha] Custom reCAPTCHA load timeout")
        return False

    async def _execute_recaptcha_on_tab(self, tab, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """ Execute reCAPTCHA to get token on specified tab
        
        Args:
            tab: nodriver tab object
            action: reCAPTCHA actiontype (IMAGE_GENERATION or VIDEO_GENERATION)
            
        Returns:
            reCAPTCHA token or None
        """
        # Generate unique variable name to avoid conflicts
        ts = int(time.time() * 1000)
        token_var = f"_recaptcha_token_{ts}"
        error_var = f"_recaptcha_error_{ts}"
        
        execute_script = f"""
            (() => {{
                window.{token_var} = null;
                window.{error_var} = null;
                
                try {{
                    grecaptcha.enterprise.ready(function() {{
                        grecaptcha.enterprise.execute('{self.website_key}', {{action: '{action}'}})
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
        
        # Inject execution script
        await tab.evaluate(execute_script)
        
        # Polling for result (up to 15 seconds)
        token = None
        for i in range(30):
            await tab.sleep(0.5)
            token = await tab.evaluate(f"window.{token_var}")
            if token:
                break
            error = await tab.evaluate(f"window.{error_var}")
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA error: {error}")
                break
        
        # Clean up temporary variables
        try:
            await tab.evaluate(f"delete window.{token_var}; delete window.{error_var};")
        except:
            pass
        
        return token

    async def _execute_custom_recaptcha_on_tab(
        self,
        tab,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """ Execute arbitrary site reCAPTCHA on specified tab."""
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

        await tab.evaluate(execute_script)

        token = None
        for _ in range(30):
            await tab.sleep(0.5)
            token = await tab.evaluate(f"window.{token_var}")
            if token:
                break
            error = await tab.evaluate(f"window.{error_var}")
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] Custom reCAPTCHA error: {error}")
                break

        try:
            await tab.evaluate(f"delete window.{token_var}; delete window.{error_var};")
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
                    f"[BrowserCaptcha] Custom reCAPTCHA complete, extra wait {post_wait_seconds:.1f}s before returning token"
                )
                await tab.sleep(post_wait_seconds)

        return token

    async def _verify_score_on_tab(self, tab, token: str, verify_url: str) -> Dict[str, Any]:
        """Directly read score from test page display, avoiding discrepancy with verify.php."""
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
                result = await tab.evaluate("""
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
                """)
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
                    await tab.evaluate("""
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
                    """)
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
                "error": last_snapshot.get("error") or "Could not read score from page",
            },
        }

    async def _extract_tab_fingerprint(self, tab) -> Optional[Dict[str, Any]]:
        """Extract browser fingerprint info from nodriver tab."""
        try:
            fingerprint = await tab.evaluate("""
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
                return None

            # Personal mode currently has no separate browser proxy config, explicitly using direct connection to avoid confusion with global proxy.
            result: Dict[str, Any] = {"proxy_url": None}
            for key in ("user_agent", "accept_language", "sec_ch_ua", "sec_ch_ua_mobile", "sec_ch_ua_platform"):
                value = fingerprint.get(key)
                if isinstance(value, str) and value:
                    result[key] = value
            return result
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Failed to extract nodriver fingerprint: {e}")
            return None

    # ========== Main API ==========

    async def get_token(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """get reCAPTCHA token
        
        Auto resident mode: if project_id has no resident tabs, auto-create and start resident mode
        
        Args:
            project_id: FlowProject ID
            action: reCAPTCHA actiontype
                - IMAGE_GENERATION: image generation and 2K/4K image upscale (default)
                - VIDEO_GENERATION: video generation and videoupscale

        Returns:
            reCAPTCHA token string, returns None if acquisition failed
        """
        # Ensure browser is initialized
        await self.initialize()
        self._last_fingerprint = None
        
        # Attempt to get token from resident tabs
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)
            
            # If this project_id has no resident tabs, auto-create one
            if resident_info is None:
                debug_logger.log_info(f"[BrowserCaptcha] project_id={project_id} has no resident tabs, creating...")
                resident_info = await self._create_resident_tab(project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[BrowserCaptcha] unable to create resident tabs for project_id={project_id}, falling back to Legacy Mode")
                    return await self._get_token_legacy(project_id, action)
                self._resident_tabs[project_id] = resident_info
                debug_logger.log_info(f"[BrowserCaptcha] ✅ created resident tabs for project_id={project_id} (current total: {len(self._resident_tabs)})")
        
        # use resident tabsgeneration token
        if resident_info and resident_info.recaptcha_ready and resident_info.tab:
            start_time = time.time()
            debug_logger.log_info(f"[BrowserCaptcha] generating token from resident tabs (project: {project_id}, action: {action})...")
            try:
                token = await self._execute_recaptcha_on_tab(resident_info.tab, action)
                duration_ms = (time.time() - start_time) * 1000
                if token:
                    self._resident_error_streaks.pop(project_id, None)
                    self._last_fingerprint = await self._extract_tab_fingerprint(resident_info.tab)
                    debug_logger.log_info(f"[BrowserCaptcha] ✅ Token generation succeeded ({duration_ms:.0f}ms)")
                    return token
                else:
                    debug_logger.log_warning(f"[BrowserCaptcha] resident tabs generation failed (project: {project_id}), attempting rebuild...")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] resident tabs error: {e}, attempting rebuild...")
            
            # Resident tabs invalid, attempting rebuild
            async with self._resident_lock:
                await self._close_resident_tab(project_id)
                resident_info = await self._create_resident_tab(project_id)
                if resident_info:
                    self._resident_tabs[project_id] = resident_info
                    # Immediately attempt generation after rebuild
                    try:
                        token = await self._execute_recaptcha_on_tab(resident_info.tab, action)
                        if token:
                            self._resident_error_streaks.pop(project_id, None)
                            self._last_fingerprint = await self._extract_tab_fingerprint(resident_info.tab)
                            debug_logger.log_info(f"[BrowserCaptcha] ✅ Token generation succeeded after rebuild")
                            return token
                    except Exception:
                        pass
        
        # Final Fallback: use Legacy Mode
        debug_logger.log_warning(f"[BrowserCaptcha] all resident methods failed, falling back to Legacy Mode (project: {project_id})")
        legacy_token = await self._get_token_legacy(project_id, action)
        if legacy_token:
            self._resident_error_streaks.pop(project_id, None)
        return legacy_token

    async def _create_resident_tab(self, project_id: str) -> Optional[ResidentTabInfo]:
        """Create resident tabs for the specified project_id

        Args:
            project_id: Project ID

        Returns:
            ResidentTabInfo object, or None (if creation failed)
        """
        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] creating resident tabs for project_id={project_id}, navigating to: {website_url}")
            
            # Create new tab
            tab = await self.browser.get(website_url, new_tab=True)
            
            # Wait for page load to complete
            page_loaded = False
            for retry in range(60):
                try:
                    await asyncio.sleep(1)
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        page_loaded = True
                        break
                except ConnectionRefusedError as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] Tab connection lost: {e}")
                    return None
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] Page wait exception: {e}, retry {retry + 1}/60...")
                    await asyncio.sleep(1)
            
            if not page_loaded:
                debug_logger.log_error(f"[BrowserCaptcha] Page load timeout (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return None
            
            # Wait for reCAPTCHA to load
            recaptcha_ready = await self._wait_for_recaptcha(tab)
            
            if not recaptcha_ready:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA load failed (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return None
            
            # Create resident info object
            resident_info = ResidentTabInfo(tab, project_id)
            resident_info.recaptcha_ready = True
            
            debug_logger.log_info(f"[BrowserCaptcha] ✅ resident tabscreatesuccess (project: {project_id})")
            return resident_info
            
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] create resident tabs error: {e}")
            return None

    async def _close_resident_tab(self, project_id: str):
        """Close resident tabs for the specified project_id

        Args:
            project_id: Project ID
        """
        resident_info = self._resident_tabs.pop(project_id, None)
        if resident_info and resident_info.tab:
            try:
                await resident_info.tab.close()
                debug_logger.log_info(f"[BrowserCaptcha] Closed project_id={project_id}  resident tabs")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] error closing tab: {e}")

    async def _get_token_legacy(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """Legacy Mode: get reCAPTCHA token (creates a new tab each time)

        Args:
            project_id: Flow Project ID
            action: reCAPTCHA action type (IMAGE_GENERATION or VIDEO_GENERATION)

        Returns:
            reCAPTCHA token string, returns None if acquisition failed
        """
        # Ensure browser is started
        if not self._initialized or not self.browser:
            await self.initialize()

        start_time = time.time()
        tab = None

        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] [Legacy] navigating to page: {website_url}")

            # Create new tab and navigate to page
            tab = await self.browser.get(website_url)

            # Wait for page to fully load (increased wait time)
            debug_logger.log_info("[BrowserCaptcha] [Legacy] waiting for page to load...")
            await tab.sleep(3)
            
            # wait for page DOM complete
            for _ in range(10):
                ready_state = await tab.evaluate("document.readyState")
                if ready_state == "complete":
                    break
                await tab.sleep(0.5)

            # Wait for reCAPTCHA to load
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] reCAPTCHA could not be loaded")
                return None

            # Execute reCAPTCHA
            debug_logger.log_info(f"[BrowserCaptcha] [Legacy] executing reCAPTCHA validation (action: {action})...")
            token = await self._execute_recaptcha_on_tab(tab, action)

            duration_ms = (time.time() - start_time) * 1000

            if token:
                self._last_fingerprint = await self._extract_tab_fingerprint(tab)
                debug_logger.log_info(f"[BrowserCaptcha] [Legacy] ✅ Token acquired successfully ({duration_ms:.0f}ms)")
                return token
            else:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] Token acquisition failed (returned null)")
                return None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] [Legacy] token acquisition error: {str(e)}")
            return None
        finally:
            # Close tab (but keep browser open)
            if tab:
                try:
                    await tab.close()
                except Exception:
                    pass

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        """Return the fingerprint snapshot from the most recent captcha browser session."""
        if not self._last_fingerprint:
            return None
        return dict(self._last_fingerprint)

    async def close(self):
        """Close browser"""
        # First stop all resident modes (close all resident tabs)
        await self.stop_resident_mode()
        
        try:
            custom_items = list(self._custom_tabs.values())
            self._custom_tabs.clear()
            for item in custom_items:
                tab = item.get("tab") if isinstance(item, dict) else None
                if tab:
                    try:
                        await tab.close()
                    except Exception:
                        pass

            if self.browser:
                try:
                    self.browser.stop()
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] error while closing browser: {str(e)}")
                finally:
                    self.browser = None

            self._initialized = False
            self._resident_tabs.clear()  # Ensure resident dictionary is cleared
            self._resident_error_streaks.clear()
            debug_logger.log_info("[BrowserCaptcha] Browser closed")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] error closing browser: {str(e)}")

    async def open_login_window(self):
        """Open login window for user to manually log in to Google"""
        await self.initialize()
        tab = await self.browser.get("https://accounts.google.com/")
        debug_logger.log_info("[BrowserCaptcha] Please log in to your account in the opened browser. After login is complete, you do not need to close the browser; the script will automatically use it on the next run.")
        print("Please log in to your account in the opened browser. After login is complete, you do not need to close the browser; the script will automatically use it on the next run.")

    # ========== Session Token refresh ==========

    async def refresh_session_token(self, project_id: str) -> Optional[str]:
        """Get the latest Session Token from resident tabs

        Reuses the reCAPTCHA resident tabs by refreshing the page and extracting
        __Secure-next-auth.session-token from cookies

        Args:
            project_id: Project ID, used to locate resident tabs

        Returns:
            New Session Token, or None if acquisition failed
        """
        # Ensure browser is initialized
        await self.initialize()
        
        start_time = time.time()
        debug_logger.log_info(f"[BrowserCaptcha] Starting refresh Session Token (project: {project_id})...")
        
        # attemptgetorcreateresident tabs
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)
            
            # If this project_id has no resident tabs, create one
            if resident_info is None:
                debug_logger.log_info(f"[BrowserCaptcha] project_id={project_id} has no resident tabs, creating...")
                resident_info = await self._create_resident_tab(project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[BrowserCaptcha] unable to create resident tabs for project_id={project_id}")
                    return None
                self._resident_tabs[project_id] = resident_info
        
        if not resident_info or not resident_info.tab:
            debug_logger.log_error(f"[BrowserCaptcha] unable to get resident tabs")
            return None
        
        tab = resident_info.tab
        
        try:
            # Refresh page to get the latest cookies
            debug_logger.log_info(f"[BrowserCaptcha] refreshing resident tabs to get latest cookies...")
            await tab.reload()
            
            # Wait for page load to complete
            for i in range(30):
                await asyncio.sleep(1)
                try:
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        break
                except Exception:
                    pass
            
            # Extra wait to ensure cookies are set
            await asyncio.sleep(2)
            
            # Extract __Secure-next-auth.session-token from cookies
            # nodriver can get cookies through the browser
            session_token = None
            
            try:
                # Using nodriver cookies API to get all cookies
                cookies = await self.browser.cookies.get_all()
                
                for cookie in cookies:
                    if cookie.name == "__Secure-next-auth.session-token":
                        session_token = cookie.value
                        break
                        
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] cookies API retrieval failed: {e}, attempting to get from document.cookie...")
                
                # Fallback: get via JavaScript (note: HttpOnly cookies may not be accessible this way)
                try:
                    all_cookies = await tab.evaluate("document.cookie")
                    if all_cookies:
                        for part in all_cookies.split(";"):
                            part = part.strip()
                            if part.startswith("__Secure-next-auth.session-token="):
                                session_token = part.split("=", 1)[1]
                                break
                except Exception as e2:
                    debug_logger.log_error(f"[BrowserCaptcha] document.cookie getfailed: {e2}")
            
            duration_ms = (time.time() - start_time) * 1000
            
            if session_token:
                debug_logger.log_info(f"[BrowserCaptcha] ✅ Session Token acquired successfully ({duration_ms:.0f}ms)")
                return session_token
            else:
                debug_logger.log_error(f"[BrowserCaptcha] ❌ __Secure-next-auth.session-token cookie not found")
                return None
                
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] refresh Session Token error: {str(e)}")
            
            # Resident tabs may be invalid, attempting rebuild
            async with self._resident_lock:
                await self._close_resident_tab(project_id)
                resident_info = await self._create_resident_tab(project_id)
                if resident_info:
                    self._resident_tabs[project_id] = resident_info
                    # Attempt to get again after rebuild
                    try:
                        cookies = await self.browser.cookies.get_all()
                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                debug_logger.log_info(f"[BrowserCaptcha] ✅ Session Token acquired successfully after rebuild")
                                return cookie.value
                    except Exception:
                        pass
            
            return None

    # ========== Status Query ==========

    def is_resident_mode_active(self) -> bool:
        """Check whether any resident tabs are active"""
        return len(self._resident_tabs) > 0 or self._running

    def get_resident_count(self) -> int:
        """Get current resident tabs count"""
        return len(self._resident_tabs)

    def get_resident_project_ids(self) -> list[str]:
        """Get list of all current resident project_ids"""
        return list(self._resident_tabs.keys())

    def get_resident_project_id(self) -> Optional[str]:
        """Get current resident project_id (backward compatible, returns first one)"""
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
        """Execute reCAPTCHA on any site, for score testing and other scenarios.

        Unlike regular legacy mode, this reuses the same resident tabs to avoid cold-starting a new tab each time.
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
                        debug_logger.log_info(f"[BrowserCaptcha] [Custom] createresidenttesttab: {website_url}")
                        tab = await self.browser.get(website_url, new_tab=True)
                        custom_info = {
                            "tab": tab,
                            "recaptcha_ready": False,
                            "warmed_up": False,
                            "created_at": time.time(),
                        }
                        self._custom_tabs[cache_key] = custom_info

                    page_loaded = False
                    for _ in range(20):
                        ready_state = await tab.evaluate("document.readyState")
                        if ready_state == "complete":
                            page_loaded = True
                            break
                        await tab.sleep(0.5)

                    if not page_loaded:
                        raise RuntimeError("Custom page load timeout")

                    if not custom_info.get("recaptcha_ready"):
                        recaptcha_ready = await self._wait_for_custom_recaptcha(
                            tab=tab,
                            website_key=website_key,
                            enterprise=enterprise,
                        )
                        if not recaptcha_ready:
                            raise RuntimeError("Custom reCAPTCHA could not be loaded")
                        custom_info["recaptcha_ready"] = True

                    try:
                        await tab.evaluate("""
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
                        """)
                    except Exception:
                        pass

                    if not custom_info.get("warmed_up"):
                        if warmup_seconds > 0:
                            debug_logger.log_info(
                                f"[BrowserCaptcha] [Custom] initial warmup of test page {warmup_seconds:.1f}s before executing token"
                            )
                            try:
                                await tab.evaluate("""
                                    (() => {
                                        try {
                                            window.scrollTo(0, Math.min(240, document.body.scrollHeight || 240));
                                            window.dispatchEvent(new Event('mousemove'));
                                            window.dispatchEvent(new Event('focus'));
                                        } catch (e) {}
                                    })()
                                """)
                            except Exception:
                                pass
                            await tab.sleep(warmup_seconds)
                        custom_info["warmed_up"] = True
                    elif per_request_settle_seconds > 0:
                        debug_logger.log_info(
                            f"[BrowserCaptcha] [Custom] reusing test tab, extra wait {per_request_settle_seconds:.1f}s before execution"
                        )
                        await tab.sleep(per_request_settle_seconds)

                    debug_logger.log_info(f"[BrowserCaptcha] [Custom] using resident test tab for validation (action: {action})...")
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
                                fallback_ua = await tab.evaluate("navigator.userAgent || ''")
                                fallback_lang = await tab.evaluate("navigator.language || ''")
                                extracted_fingerprint = {
                                    "user_agent": fallback_ua or "",
                                    "accept_language": fallback_lang or "",
                                    "proxy_url": None,
                                }
                            except Exception:
                                extracted_fingerprint = None
                        self._last_fingerprint = extracted_fingerprint
                        debug_logger.log_info(
                            f"[BrowserCaptcha] [Custom] ✅ resident test tab Token acquired successfully ({duration_ms:.0f}ms)"
                        )
                        return token

                    raise RuntimeError("Custom token acquisition failed (returned null)")
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] [Custom] attempt {attempt + 1}/{max_retries} failed: {str(e)}"
                    )
                    stale_info = self._custom_tabs.pop(cache_key, None)
                    stale_tab = stale_info.get("tab") if isinstance(stale_info, dict) else None
                    if stale_tab:
                        try:
                            await stale_tab.close()
                        except Exception:
                            pass
                    if attempt >= max_retries - 1:
                        debug_logger.log_error(f"[BrowserCaptcha] [Custom] token acquisition error: {str(e)}")
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
        """Get token and directly verify page score within the same resident tabs."""
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
                raise RuntimeError("Page score test tab does not exist")
            verify_payload = await self._verify_score_on_tab(tab, token, verify_url)

        return {
            "token": token,
            "token_elapsed_ms": token_elapsed_ms,
            **verify_payload,
        }
