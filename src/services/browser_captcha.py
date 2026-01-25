"""
浏览器自动化获取 reCAPTCHA token
使用本地真实浏览器 (persistent context) 访问页面并执行 reCAPTCHA 验证
支持多标签页并发
"""
import asyncio
import time
import re
import os
from pathlib import Path
from typing import Optional, Dict
from playwright.async_api import async_playwright, BrowserContext, Route

from ..core.logger import debug_logger


# 配置
DEFAULT_MAX_PAGES = 5  # 默认最大并发标签页数
DEFAULT_TAB_INTERVAL = 1.0  # 默认标签页创建间隔（秒）

# 默认浏览器路径（空表示自动检测）
DEFAULT_BROWSER_PATH = None


def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    """解析代理URL，分离协议、主机、端口、认证信息

    Args:
        proxy_url: 代理URL，格式：protocol://[username:password@]host:port

    Returns:
        代理配置字典，包含server、username、password（如果有认证）
    """
    proxy_pattern = r'^(socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$'
    match = re.match(proxy_pattern, proxy_url)

    if match:
        protocol, username, password, host, port = match.groups()
        proxy_config = {'server': f'{protocol}://{host}:{port}'}

        if username and password:
            proxy_config['username'] = username
            proxy_config['password'] = password

        return proxy_config
    return None


def validate_browser_proxy_url(proxy_url: str) -> tuple[bool, str]:
    """验证浏览器代理URL格式（仅支持HTTP和无认证SOCKS5）

    Args:
        proxy_url: 代理URL

    Returns:
        (是否有效, 错误信息)
    """
    if not proxy_url or not proxy_url.strip():
        return True, ""  # 空URL视为有效（不使用代理）

    proxy_url = proxy_url.strip()
    parsed = parse_proxy_url(proxy_url)

    if not parsed:
        return False, "代理URL格式错误，正确格式：http://host:port 或 socks5://host:port"

    # 检查是否有认证信息
    has_auth = 'username' in parsed

    # 获取协议
    protocol = parsed['server'].split('://')[0]

    # SOCKS5不支持认证
    if protocol == 'socks5' and has_auth:
        return False, "浏览器不支持带认证的SOCKS5代理，请使用HTTP代理或移除SOCKS5认证"

    # HTTP/HTTPS支持认证
    if protocol in ['http', 'https']:
        return True, ""

    # SOCKS5无认证支持
    if protocol == 'socks5' and not has_auth:
        return True, ""

    return False, f"不支持的代理协议：{protocol}"


def detect_browser_path() -> Optional[str]:
    """自动检测本机浏览器路径（优先 Edge，其次 Chrome）"""
    import platform
    system = platform.system()
    
    # 常见浏览器路径
    if system == "Windows":
        paths = [
            # Edge
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            # Chrome
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif system == "Darwin":  # macOS
        paths = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:  # Linux
        paths = [
            "/usr/bin/microsoft-edge",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
    
    for path in paths:
        if os.path.exists(path):
            return path
    
    return None


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（单例模式，使用本地真实浏览器）
    
    特点：
    - 支持调用本机 Edge/Chrome 浏览器
    - 可配置浏览器安装路径
    - 使用 persistent context 复用浏览器配置
    - 多标签页并发
    """

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        self.headless = False  # 默认有头
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        
        # 用户数据目录
        self.user_data_dir = os.path.join(os.getcwd(), "browser_data")
        
        # 浏览器路径（空表示自动检测）
        self.browser_path: Optional[str] = DEFAULT_BROWSER_PATH
        
        # 并发控制
        self._max_pages = DEFAULT_MAX_PAGES
        self._semaphore = asyncio.Semaphore(self._max_pages)
        
        # 标签页间隔控制
        self._tab_interval = DEFAULT_TAB_INTERVAL
        self._last_tab_time = 0.0
        self._tab_lock = asyncio.Lock()
        
        # 统计信息
        self._solve_count = 0
        self._error_count = 0

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    await cls._instance.initialize()
        return cls._instance

    def set_max_pages(self, max_pages: int):
        """设置最大并发标签页数"""
        self._max_pages = max(1, max_pages)
        self._semaphore = asyncio.Semaphore(self._max_pages)
        debug_logger.log_info(f"[BrowserCaptcha] 最大并发标签页设置为 {self._max_pages}")

    def set_tab_interval(self, seconds: float):
        """设置标签页创建间隔（秒）"""
        self._tab_interval = max(0.1, seconds)
        debug_logger.log_info(f"[BrowserCaptcha] 标签页间隔设置为 {self._tab_interval} 秒")

    def set_browser_path(self, path: str):
        """设置浏览器安装路径
        
        Args:
            path: 浏览器可执行文件路径，例如:
                - Windows Edge: C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe
                - Windows Chrome: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe
                - macOS Edge: /Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge
                - macOS Chrome: /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
        """
        if path and os.path.exists(path):
            self.browser_path = path
            debug_logger.log_info(f"[BrowserCaptcha] 浏览器路径设置为: {path}")
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] 浏览器路径不存在: {path}")

    async def initialize(self):
        """初始化浏览器（使用本机 Edge/Chrome）"""
        if self._initialized:
            return

        try:
            # 确保用户数据目录存在
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
            
            # 从配置读取浏览器路径
            from ..core.config import config
            config_browser_path = config.browser_path
            
            # 优先级：实例设置 > 配置文件 > 自动检测
            browser_path = self.browser_path or config_browser_path or detect_browser_path()
            if browser_path:
                debug_logger.log_info(f"[BrowserCaptcha] 使用浏览器: {browser_path}")
            else:
                debug_logger.log_info(f"[BrowserCaptcha] 未检测到本地浏览器，使用 Playwright 内置 Chromium")
            
            debug_logger.log_info(f"[BrowserCaptcha] 正在启动浏览器... (profile={self.user_data_dir}, max_pages={self._max_pages})")
            self.playwright = await async_playwright().start()

            # 构建启动参数
            launch_args = {
                'headless': self.headless,
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--no-first-run',
                    '--no-zygote',
                    '--window-size=1280,720',
                    '--disable-infobars',
                    '--disable-extensions',
                    '--disable-plugins-discovery',
                    '--disable-default-apps',
                    '--disable-component-update',
                    '--disable-background-networking',
                    '--disable-sync',
                    '--metrics-recording-only',
                    '--disable-hang-monitor',
                    '--disable-prompt-on-repost',
                    '--disable-features=TranslateUI',
                    '--disable-ipc-flooding-protection',
                ],
                'ignore_default_args': ['--enable-automation'],
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            
            # 如果有指定浏览器路径，添加到参数
            if browser_path:
                launch_args['executable_path'] = browser_path
            
            # 使用 persistent context 启动浏览器
            self.context = await self.playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                **launch_args
            )
            
            self._initialized = True
            browser_name = os.path.basename(browser_path) if browser_path else "Chromium"
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 浏览器已启动 (browser={browser_name}, headless={self.headless}, max_pages={self._max_pages})")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 浏览器启动失败: {str(e)}")
            raise

    async def get_token(self, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """获取 reCAPTCHA token（支持多标签并发）

        Args:
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成 (默认)
                - VIDEO_GENERATION: 视频生成和2K/4K图片放大

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        if not self._initialized:
            await self.initialize()

        start_time = time.time()
        page_url = "https://labs.google/"
        
        # 使用信号量限制并发标签页数
        async with self._semaphore:
            debug_logger.log_info(f"[BrowserCaptcha] 开始获取 token (action={action})")
            
            page = None
            try:
                # 控制标签页创建间隔
                async with self._tab_lock:
                    now = time.time()
                    elapsed = now - self._last_tab_time
                    if elapsed < self._tab_interval:
                        wait_time = self._tab_interval - elapsed
                        await asyncio.sleep(wait_time)
                    self._last_tab_time = time.time()
                
                # 直接在 persistent context 中创建新标签页
                page = await self.context.new_page()
                
                # 添加反检测脚本
                await page.add_init_script("""
                    // 隐藏 webdriver 标志
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined,
                        configurable: true
                    });
                    
                    // 模拟真实的 plugins
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => {
                            const plugins = [
                                {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
                                {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
                                {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''}
                            ];
                            plugins.length = 3;
                            return plugins;
                        },
                        configurable: true
                    });
                    
                    // 模拟真实的 languages
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en', 'zh-CN', 'zh'],
                        configurable: true
                    });
                    
                    // 隐藏 automation 相关属性
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
                    
                    // 模拟 chrome 对象
                    window.chrome = {
                        runtime: {
                            connect: () => {},
                            sendMessage: () => {},
                            onMessage: { addListener: () => {} }
                        },
                        loadTimes: () => ({}),
                        csi: () => ({}),
                        app: { isInstalled: false }
                    };
                    
                    // 修改 permissions query
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                    );
                """)
                
                # 设置路由拦截，优化加载速度
                async def handle_route(route: Route):
                    req_url = route.request.url
                    # 只允许 reCAPTCHA 相关请求
                    if req_url.rstrip('/') == page_url.rstrip('/') or req_url == page_url:
                        # 返回最小化 HTML，只包含 reCAPTCHA 脚本
                        html = f"""<html><head>
                            <script src="https://www.google.com/recaptcha/enterprise.js?render={self.website_key}"></script>
                        </head><body></body></html>"""
                        await route.fulfill(status=200, content_type="text/html", body=html)
                    elif any(d in req_url for d in ["google.com", "gstatic.com", "recaptcha.net"]):
                        await route.continue_()
                    else:
                        await route.abort()
                
                await page.route("**/*", handle_route)
                
                # 访问页面并等待网络空闲
                await page.goto(page_url, wait_until="networkidle", timeout=30000)
                
                # 等待 reCAPTCHA 脚本完全加载
                try:
                    await page.wait_for_function(
                        "typeof grecaptcha !== 'undefined' && grecaptcha.enterprise && typeof grecaptcha.enterprise.execute === 'function'",
                        timeout=20000
                    )
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] reCAPTCHA 脚本加载超时: {e}")
                    self._error_count += 1
                    return None
                
                # 额外等待确保完全初始化
                await asyncio.sleep(0.5)
                
                # 执行 reCAPTCHA
                try:
                    token = await asyncio.wait_for(
                        page.evaluate(f"""() => {{
                            return new Promise((resolve, reject) => {{
                                const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                                grecaptcha.enterprise.execute('{self.website_key}', {{action: '{action}'}})
                                    .then(t => {{
                                        clearTimeout(timeout);
                                        resolve(t);
                                    }})
                                    .catch(e => {{
                                        clearTimeout(timeout);
                                        reject(e);
                                    }});
                            }});
                        }}"""),
                        timeout=30
                    )
                    
                    duration_ms = (time.time() - start_time) * 1000
                    
                    if token:
                        self._solve_count += 1
                        debug_logger.log_info(f"[BrowserCaptcha] ✅ Token 获取成功（耗时 {duration_ms:.0f}ms）")
                        return token
                    else:
                        self._error_count += 1
                        debug_logger.log_warning(f"[BrowserCaptcha] Token 为空")
                        return None
                        
                except asyncio.TimeoutError:
                    self._error_count += 1
                    debug_logger.log_warning(f"[BrowserCaptcha] reCAPTCHA 执行超时")
                    return None
                except Exception as e:
                    self._error_count += 1
                    debug_logger.log_warning(f"[BrowserCaptcha] reCAPTCHA 执行异常: {e}")
                    return None
                    
            except Exception as e:
                self._error_count += 1
                debug_logger.log_error(f"[BrowserCaptcha] 获取token异常: {str(e)}")
                return None
            finally:
                # 只关闭页面，不关闭 context
                if page:
                    try:
                        await page.close()
                    except:
                        pass

    async def close(self):
        """关闭浏览器"""
        try:
            if self.context:
                try:
                    await self.context.close()
                except Exception as e:
                    # 忽略连接关闭错误（正常关闭场景）
                    if "Connection closed" not in str(e):
                        debug_logger.log_warning(f"[BrowserCaptcha] 关闭浏览器时出现异常: {str(e)}")
                finally:
                    self.context = None

            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass  # 静默处理 playwright 停止异常
                finally:
                    self.playwright = None

            self._initialized = False
            debug_logger.log_info("[BrowserCaptcha] 浏览器已关闭")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 关闭浏览器异常: {str(e)}")

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            "initialized": self._initialized,
            "max_pages": self._max_pages,
            "tab_interval": self._tab_interval,
            "solve_count": self._solve_count,
            "error_count": self._error_count,
            "success_rate": f"{(self._solve_count / (self._solve_count + self._error_count) * 100):.1f}%" if (self._solve_count + self._error_count) > 0 else "N/A"
        }
