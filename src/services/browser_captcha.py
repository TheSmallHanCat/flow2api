"""
浏览器自动化获取 reCAPTCHA token
使用 Playwright 访问页面并执行 reCAPTCHA 验证
支持多标签页并发
"""
import asyncio
import time
import re
from typing import Optional, Dict
from playwright.async_api import async_playwright, Browser, BrowserContext, Route

from ..core.logger import debug_logger


# 配置
DEFAULT_MAX_PAGES = 5  # 默认最大并发标签页数
DEFAULT_TAB_INTERVAL = 1.0  # 默认标签页创建间隔（秒）


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


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（单例模式，支持多标签并发）
    
    特点：
    - 单个浏览器实例，多标签页并发
    - 使用信号量控制最大并发数
    - 支持标签页创建间隔控制
    - 使用路由拦截优化加载速度
    """

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务（默认有头模式）"""
        self.headless = False  # 默认有头
        self.playwright = None
        self.browser: Optional[Browser] = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        
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

    async def initialize(self):
        """初始化浏览器（启动一次）"""
        if self._initialized:
            return

        try:
            # 获取浏览器专用代理配置
            proxy_url = None
            if self.db:
                captcha_config = await self.db.get_captcha_config()
                if captcha_config.browser_proxy_enabled and captcha_config.browser_proxy_url:
                    proxy_url = captcha_config.browser_proxy_url

            debug_logger.log_info(f"[BrowserCaptcha] 正在启动浏览器... (proxy={proxy_url or 'None'}, max_pages={self._max_pages})")
            self.playwright = await async_playwright().start()

            # 配置浏览器启动参数
            launch_options = {
                'headless': self.headless,
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--no-first-run',
                    '--no-zygote',
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
                ]
            }

            # 如果有代理，解析并添加代理配置
            if proxy_url:
                proxy_config = parse_proxy_url(proxy_url)
                if proxy_config:
                    launch_options['proxy'] = proxy_config
                    auth_info = "auth=yes" if 'username' in proxy_config else "auth=no"
                    debug_logger.log_info(f"[BrowserCaptcha] 代理配置: {proxy_config['server']} ({auth_info})")
                else:
                    debug_logger.log_warning(f"[BrowserCaptcha] 代理URL格式错误: {proxy_url}")

            self.browser = await self.playwright.chromium.launch(**launch_options)
            self._initialized = True
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 浏览器已启动 (headless={self.headless}, proxy={proxy_url or 'None'}, max_pages={self._max_pages})")
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
                
                # 创建新标签页
                context = await self.browser.new_context(
                    viewport={'width': 1280, 'height': 720},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    locale='en-US',
                    timezone_id='America/New_York'
                )
                page = await context.new_page()
                
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
                
                # 访问页面
                await page.goto(page_url, wait_until="load", timeout=30000)
                
                # 等待 reCAPTCHA 脚本加载
                try:
                    await page.wait_for_function(
                        "typeof grecaptcha !== 'undefined' && grecaptcha.enterprise && typeof grecaptcha.enterprise.execute === 'function'",
                        timeout=15000
                    )
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] reCAPTCHA 脚本加载超时: {e}")
                    self._error_count += 1
                    return None
                
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
                # 关闭页面和上下文
                if page:
                    try:
                        context = page.context
                        await page.close()
                        await context.close()
                    except:
                        pass

    async def close(self):
        """关闭浏览器"""
        try:
            if self.browser:
                try:
                    await self.browser.close()
                except Exception as e:
                    # 忽略连接关闭错误（正常关闭场景）
                    if "Connection closed" not in str(e):
                        debug_logger.log_warning(f"[BrowserCaptcha] 关闭浏览器时出现异常: {str(e)}")
                finally:
                    self.browser = None

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
