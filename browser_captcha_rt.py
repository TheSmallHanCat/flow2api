"""
基于 RT 的本地 reCAPTCHA 打码服务
使用 Playwright 实现，每个 Token 对应一个独立的浏览器实例
支持自动刷新 Session Token
"""
import asyncio
import time
import os
from pathlib import Path
from typing import Optional, Dict, Callable, Awaitable
from datetime import datetime

from playwright.async_api import async_playwright, Route, BrowserContext

from ..core.logger import debug_logger


# 配置
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
LABS_URL = "https://labs.google/fx/tools/flow"
DEFAULT_REFRESH_INTERVAL = 3600  # 默认 1 小时刷新一次
DEFAULT_TAB_INTERVAL = 1.0  # 默认标签页间隔（秒）


class TokenBrowser:
    """单个 Token 对应的浏览器实例
    
    支持：
    - reCAPTCHA token 获取
    - 自动刷新 Session Token
    """
    
    MAX_PAGES = 5  # 每个浏览器最多并发标签页数
    
    def __init__(self, token_id: int, user_data_dir: str, db=None):
        self.token_id = token_id
        self.user_data_dir = user_data_dir
        self.db = db
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self._initialized = False
        self._semaphore = asyncio.Semaphore(self.MAX_PAGES)
        self._lock = asyncio.Lock()
        self._solve_count = 0
        self._error_count = 0
        
        # ST 自动刷新相关
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_interval = DEFAULT_REFRESH_INTERVAL  # 秒
        self._last_refresh_time: Optional[datetime] = None
        self._refresh_count = 0
        self._refresh_error_count = 0
        self._refresh_lock = asyncio.Lock()  # 防止并发刷新
        self._is_refreshing = False  # 刷新状态标志
        
        # 标签页间隔控制
        self._tab_interval = DEFAULT_TAB_INTERVAL  # 秒
        self._last_tab_time = 0.0  # 上次创建标签页的时间
        self._tab_lock = asyncio.Lock()  # 标签页创建锁
    
    async def start(self, headless: bool = False, start_refresh_task: bool = True):
        """启动浏览器实例
        
        Args:
            headless: 是否无头模式
            start_refresh_task: 是否启动 ST 自动刷新任务
        """
        if self._initialized:
            return
        
        async with self._lock:
            if self._initialized:
                return
            
            self.playwright = await async_playwright().start()
            
            # 确保用户数据目录存在
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
            
            self.context = await self.playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                headless=headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
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
                ignore_default_args=['--enable-automation'],
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            self._initialized = True
            debug_logger.log_info(f"[RT-Token-{self.token_id}] 浏览器启动成功 (profile: {self.user_data_dir})")
            
            # 启动 ST 自动刷新任务
            if start_refresh_task and self.db:
                self._start_refresh_task()
    
    async def stop(self):
        """停止浏览器实例"""
        if not self._initialized:
            return
        
        # 停止刷新任务
        self._stop_refresh_task()
        
        try:
            if self.context:
                await self.context.close()
                self.context = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            self._initialized = False
            debug_logger.log_info(f"[RT-Token-{self.token_id}] 浏览器已停止")
        except Exception as e:
            debug_logger.log_warning(f"[RT-Token-{self.token_id}] 停止时异常: {e}")
    
    async def get_token(self, project_id: str, website_key: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """获取 reCAPTCHA token
        
        Args:
            project_id: Flow 项目 ID
            website_key: reCAPTCHA site key
            action: reCAPTCHA action (IMAGE_GENERATION, VIDEO_GENERATION 等)
            
        Returns:
            reCAPTCHA token 或 None
        """
        # 确保浏览器已启动
        if not self._initialized:
            print(f"[RT-Token-{self.token_id}] 浏览器未初始化，正在启动...")
            await self.start()
        
        start_time = time.time()
        page_url = "https://labs.google/"
        
        # 使用信号量限制并发标签页数
        async with self._semaphore:
            print(f"[RT-Token-{self.token_id}] 开始获取 token (project: {project_id[:8]}...)")
            debug_logger.log_info(f"[RT-Token-{self.token_id}] 开始获取 token (project: {project_id[:8]}...)")
            
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
                    
                    // 隐藏 Playwright/Puppeteer 特征
                    Object.defineProperty(navigator, 'maxTouchPoints', {
                        get: () => 0,
                        configurable: true
                    });
                    
                    // 覆盖 toString 方法防止检测
                    const origToString = Function.prototype.toString;
                    Function.prototype.toString = function() {
                        if (this === window.navigator.permissions.query) {
                            return 'function query() { [native code] }';
                        }
                        return origToString.call(this);
                    };
                """)
                
                # 设置路由拦截
                async def handle_route(route: Route):
                    req_url = route.request.url
                    if req_url.rstrip('/') == page_url.rstrip('/') or req_url == page_url:
                        html = f"""<html><head>
                            <script src="https://www.google.com/recaptcha/enterprise.js?render={website_key}"></script>
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
                    debug_logger.log_warning(f"[RT-Token-{self.token_id}] reCAPTCHA 脚本加载超时: {e}")
                    self._error_count += 1
                    return None
                
                # 执行 reCAPTCHA
                try:
                    token = await asyncio.wait_for(
                        page.evaluate(f"""() => {{
                            return new Promise((resolve, reject) => {{
                                const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                                grecaptcha.enterprise.execute('{website_key}', {{action: '{action}'}})
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
                        print(f"[RT-Token-{self.token_id}] ✅ Token 获取成功（耗时 {duration_ms:.0f}ms）")
                        debug_logger.log_info(f"[RT-Token-{self.token_id}] ✅ Token 获取成功（耗时 {duration_ms:.0f}ms）")
                        return token
                    else:
                        self._error_count += 1
                        print(f"[RT-Token-{self.token_id}] ❌ Token 为空")
                        debug_logger.log_warning(f"[RT-Token-{self.token_id}] Token 为空")
                        return None
                        
                except asyncio.TimeoutError:
                    self._error_count += 1
                    print(f"[RT-Token-{self.token_id}] ❌ reCAPTCHA 执行超时")
                    debug_logger.log_warning(f"[RT-Token-{self.token_id}] reCAPTCHA 执行超时")
                    return None
                except Exception as e:
                    self._error_count += 1
                    print(f"[RT-Token-{self.token_id}] ❌ reCAPTCHA 执行异常: {e}")
                    debug_logger.log_warning(f"[RT-Token-{self.token_id}] reCAPTCHA 执行异常: {e}")
                    return None
                    
            except Exception as e:
                self._error_count += 1
                print(f"[RT-Token-{self.token_id}] ❌ 页面执行异常: {e}")
                debug_logger.log_error(f"[RT-Token-{self.token_id}] 页面执行异常: {e}")
                return None
            finally:
                if page:
                    try:
                        await page.close()
                    except:
                        pass
    
    # ========== ST 自动刷新相关方法 ==========
    
    def _start_refresh_task(self):
        """启动 ST 自动刷新任务"""
        if self._refresh_task is not None:
            return
        
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        debug_logger.log_info(f"[RT-Token-{self.token_id}] ST 自动刷新任务已启动 (间隔: {self._refresh_interval}秒)")
    
    def _stop_refresh_task(self):
        """停止 ST 自动刷新任务"""
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
            debug_logger.log_info(f"[RT-Token-{self.token_id}] ST 自动刷新任务已停止")
    
    def set_refresh_interval(self, seconds: int):
        """设置刷新间隔（秒）"""
        self._refresh_interval = max(60, seconds)  # 最小 60 秒
        debug_logger.log_info(f"[RT-Token-{self.token_id}] ST 刷新间隔已设置为 {self._refresh_interval} 秒")
    
    def set_tab_interval(self, seconds: float):
        """设置标签页创建间隔（秒）"""
        self._tab_interval = max(0.1, seconds)  # 最小 0.1 秒
        debug_logger.log_info(f"[RT-Token-{self.token_id}] 标签页间隔已设置为 {self._tab_interval} 秒")
    
    async def _refresh_loop(self):
        """刷新循环"""
        # 等待一段时间后开始第一次刷新
        await asyncio.sleep(60)  # 启动后 1 分钟先执行一次
        
        while True:
            try:
                await self.refresh_session_token()
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_error(f"[RT-Token-{self.token_id}] ST 刷新循环异常: {e}")
            
            # 等待下一次刷新
            try:
                await asyncio.sleep(self._refresh_interval)
            except asyncio.CancelledError:
                break
    
    async def refresh_session_token(self) -> Optional[str]:
        """刷新 Session Token
        
        通过访问 labs.google 页面刷新 session，然后从 cookies 提取新的 ST
        
        Returns:
            新的 Session Token 或 None
        """
        if not self._initialized or not self.context:
            debug_logger.log_warning(f"[RT-Token-{self.token_id}] 浏览器未初始化，跳过 ST 刷新")
            return None
        
        # 防止并发刷新：如果正在刷新，直接返回
        if self._is_refreshing:
            debug_logger.log_info(f"[RT-Token-{self.token_id}] ST 刷新正在进行中，跳过本次刷新")
            return None
        
        # 使用锁确保只有一个刷新任务执行
        async with self._refresh_lock:
            # 双重检查
            if self._is_refreshing:
                return None
            
            self._is_refreshing = True
            debug_logger.log_info(f"[RT-Token-{self.token_id}] 开始刷新 Session Token...")
            
            page = None
            try:
                # 使用信号量确保不会超过并发限制
                async with self._semaphore:
                    # 创建新标签页
                    page = await self.context.new_page()
                    
                    # 访问 labs.google 页面触发 session 刷新
                    debug_logger.log_info(f"[RT-Token-{self.token_id}] 访问 {LABS_URL} 刷新 session...")
                    await page.goto(LABS_URL, wait_until="networkidle", timeout=60000)
                    
                    # 等待页面完全加载，确保 cookie 已更新
                    await asyncio.sleep(3)
                    
                    # 再次等待网络空闲
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except:
                        pass
                    
                    # 提取 cookie
                    cookies = await self.context.cookies("https://labs.google")
                    new_st = None
                    for cookie in cookies:
                        if cookie["name"] == SESSION_COOKIE_NAME:
                            new_st = cookie["value"]
                            break
                    
                    if new_st:
                        self._last_refresh_time = datetime.now()
                        self._refresh_count += 1
                        
                        # 更新数据库中的 ST
                        if self.db:
                            try:
                                await self.db.update_token_st(self.token_id, new_st)
                                debug_logger.log_info(f"[RT-Token-{self.token_id}] ✅ ST 刷新成功并已更新到数据库")
                            except Exception as e:
                                debug_logger.log_error(f"[RT-Token-{self.token_id}] 更新数据库失败: {e}")
                        else:
                            debug_logger.log_info(f"[RT-Token-{self.token_id}] ✅ ST 刷新成功 (未配置数据库)")
                        
                        return new_st
                    else:
                        self._refresh_error_count += 1
                        debug_logger.log_warning(f"[RT-Token-{self.token_id}] 未找到 Session Token，会话可能已过期")
                        return None
                        
            except Exception as e:
                self._refresh_error_count += 1
                debug_logger.log_error(f"[RT-Token-{self.token_id}] ST 刷新异常: {e}")
                return None
            finally:
                self._is_refreshing = False  # 确保释放刷新状态
                if page:
                    try:
                        await page.close()
                    except:
                        pass
    
    def get_refresh_stats(self) -> dict:
        """获取刷新统计信息"""
        return {
            "refresh_interval": self._refresh_interval,
            "last_refresh_time": self._last_refresh_time.isoformat() if self._last_refresh_time else None,
            "refresh_count": self._refresh_count,
            "refresh_error_count": self._refresh_error_count,
            "refresh_task_running": self._refresh_task is not None and not self._refresh_task.done()
        }


class BrowserCaptchaService:
    """每个 Token 对应独立浏览器的打码服务（单例模式）
    
    特点：
    - 每个 Token 拥有独立的浏览器实例和 Profile
    - 每个浏览器最多 5 个并发标签页
    - 超过并发限制时自动排队
    - Token 删除时自动关闭对应浏览器
    - 自动刷新 ST（Session Token）
    """
    
    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()
    
    def __init__(self, db=None):
        """初始化服务"""
        self.db = db
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.base_user_data_dir = os.path.join(os.getcwd(), "browser_data_rt")
        
        # token_id -> TokenBrowser 的映射
        self._browsers: Dict[int, TokenBrowser] = {}
        self._browsers_lock = asyncio.Lock()
        
        # 默认刷新间隔
        self._default_refresh_interval = DEFAULT_REFRESH_INTERVAL
        # 默认标签页间隔
        self._default_tab_interval = DEFAULT_TAB_INTERVAL
    
    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
        return cls._instance
    
    def set_refresh_interval(self, seconds: int):
        """设置 ST 刷新间隔（秒）
        
        会应用到所有已存在的浏览器实例，并作为新浏览器的默认值
        """
        self._default_refresh_interval = max(60, seconds)  # 最小 60 秒
        debug_logger.log_info(f"[RT-Captcha] ST 刷新间隔设置为 {self._default_refresh_interval} 秒")
        
        # 应用到所有已存在的浏览器
        for browser in self._browsers.values():
            browser.set_refresh_interval(self._default_refresh_interval)
    
    def set_tab_interval(self, seconds: float):
        """设置标签页创建间隔（秒）
        
        会应用到所有已存在的浏览器实例，并作为新浏览器的默认值
        """
        self._default_tab_interval = max(0.1, seconds)  # 最小 0.1 秒
        debug_logger.log_info(f"[RT-Captcha] 标签页间隔设置为 {self._default_tab_interval} 秒")
        
        # 应用到所有已存在的浏览器
        for browser in self._browsers.values():
            browser.set_tab_interval(self._default_tab_interval)
    
    async def _get_or_create_browser(self, token_id: int) -> TokenBrowser:
        """获取或创建 Token 对应的浏览器实例"""
        async with self._browsers_lock:
            if token_id not in self._browsers:
                user_data_dir = os.path.join(self.base_user_data_dir, f"token_{token_id}")
                browser = TokenBrowser(token_id, user_data_dir, db=self.db)
                browser.set_refresh_interval(self._default_refresh_interval)  # 应用默认刷新间隔
                browser.set_tab_interval(self._default_tab_interval)  # 应用默认标签页间隔
                self._browsers[token_id] = browser
                debug_logger.log_info(f"[RT-Captcha] 为 Token-{token_id} 创建浏览器实例")
            return self._browsers[token_id]
    
    async def get_token(self, project_id: str, token_id: int = None, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """获取 reCAPTCHA token
        
        Args:
            project_id: Flow 项目 ID
            token_id: Token ID（用于确定使用哪个浏览器）
            action: reCAPTCHA action (IMAGE_GENERATION, VIDEO_GENERATION 等)
            
        Returns:
            reCAPTCHA token 或 None
        """
        if token_id is None:
            # 如果没有指定 token_id，使用默认浏览器 (ID=0)
            token_id = 0
        
        browser = await self._get_or_create_browser(token_id)
        return await browser.get_token(project_id, self.website_key, action)
    
    async def remove_browser(self, token_id: int):
        """移除并关闭 Token 对应的浏览器"""
        async with self._browsers_lock:
            if token_id in self._browsers:
                browser = self._browsers.pop(token_id)
                await browser.stop()
                debug_logger.log_info(f"[RT-Captcha] 已移除 Token-{token_id} 的浏览器")
    
    async def close(self):
        """关闭所有浏览器实例"""
        debug_logger.log_info("[RT-Captcha] 正在关闭所有浏览器实例...")
        
        async with self._browsers_lock:
            for token_id, browser in list(self._browsers.items()):
                await browser.stop()
            self._browsers.clear()
        
        debug_logger.log_info("[RT-Captcha] 所有浏览器实例已关闭")
    
    async def open_login_browser(self) -> dict:
        """打开一个新的浏览器窗口让用户登录 Google
        
        用户登录后，自动提取 ST 并返回
        
        Returns:
            {
                "success": True/False,
                "st": "session_token_value",
                "email": "user@gmail.com"
            }
        """
        import uuid
        
        # 创建一个临时的浏览器实例用于登录
        temp_id = f"login_{uuid.uuid4().hex[:8]}"
        user_data_dir = os.path.join(self.base_user_data_dir, temp_id)
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        
        playwright = None
        context = None
        
        try:
            debug_logger.log_info(f"[RT-Login] 打开登录浏览器窗口...")
            
            playwright = await async_playwright().start()
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,  # 必须有头模式
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--window-size=1280,800',
                ]
            )
            
            # 打开登录页面
            page = await context.new_page()
            await page.goto(LABS_URL, wait_until="networkidle", timeout=60000)
            
            debug_logger.log_info(f"[RT-Login] 等待用户登录...")
            
            # 等待用户登录（最多等待 5 分钟）
            # 通过检测 cookie 来判断是否已登录
            max_wait = 300  # 5 分钟
            check_interval = 2  # 每 2 秒检查一次
            waited = 0
            st = None
            
            while waited < max_wait:
                await asyncio.sleep(check_interval)
                waited += check_interval
                
                # 检查是否有 session cookie
                cookies = await context.cookies("https://labs.google")
                for cookie in cookies:
                    if cookie["name"] == SESSION_COOKIE_NAME:
                        st = cookie["value"]
                        break
                
                if st:
                    debug_logger.log_info(f"[RT-Login] 检测到登录成功！")
                    break
                
                # 每 30 秒输出一次日志
                if waited % 30 == 0:
                    debug_logger.log_info(f"[RT-Login] 等待用户登录中... ({waited}/{max_wait}秒)")
            
            if not st:
                debug_logger.log_warning(f"[RT-Login] 登录超时，未获取到 Session Token")
                return {"success": False, "error": "登录超时"}
            
            # 尝试获取用户邮箱
            email = None
            try:
                # 通过 ST 转 AT 获取用户信息
                from .flow_client import FlowClient
                from .proxy_manager import ProxyManager
                temp_client = FlowClient(ProxyManager(self.db), self.db)
                result = await temp_client.st_to_at(st)
                email = result.get("user", {}).get("email")
            except Exception as e:
                debug_logger.log_warning(f"[RT-Login] 获取用户信息失败: {e}")
            
            debug_logger.log_info(f"[RT-Login] 登录成功! 邮箱: {email or '未知'}")
            
            return {
                "success": True,
                "st": st,
                "email": email,
                "user_data_dir": user_data_dir  # 返回 profile 目录，以便后续复用
            }
            
        except Exception as e:
            debug_logger.log_error(f"[RT-Login] 登录失败: {e}")
            return {"success": False, "error": str(e)}
        finally:
            # 关闭浏览器
            if context:
                try:
                    await context.close()
                except:
                    pass
            if playwright:
                try:
                    await playwright.stop()
                except:
                    pass
    
    async def create_browser_for_token(self, token_id: int, source_user_data_dir: str = None):
        """为新 Token 创建浏览器实例
        
        如果提供了 source_user_data_dir，会复制登录状态到新的 profile
        
        Args:
            token_id: 新 Token 的 ID
            source_user_data_dir: 源 profile 目录（从登录浏览器获取）
        """
        import shutil
        
        target_dir = os.path.join(self.base_user_data_dir, f"token_{token_id}")
        
        # 如果有源目录，复制 profile
        if source_user_data_dir and os.path.exists(source_user_data_dir):
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            shutil.copytree(source_user_data_dir, target_dir)
            debug_logger.log_info(f"[RT-Captcha] 已复制登录 profile 到 Token-{token_id}")
            
            # 删除临时登录目录
            try:
                shutil.rmtree(source_user_data_dir)
            except:
                pass
        
        # 创建浏览器实例
        browser = await self._get_or_create_browser(token_id)
        await browser.start(headless=False)  # RT 模式使用有头浏览器
        
        return browser
    
    def get_stats(self) -> dict:
        """获取服务统计信息"""
        total_solve = 0
        total_error = 0
        browser_info = []
        
        for token_id, browser in self._browsers.items():
            total_solve += browser._solve_count
            total_error += browser._error_count
            browser_info.append({
                "token_id": token_id,
                "solve_count": browser._solve_count,
                "error_count": browser._error_count,
                "initialized": browser._initialized,
                "refresh_stats": browser.get_refresh_stats()
            })
        
        return {
            "total_solve_count": total_solve,
            "total_error_count": total_error,
            "browser_count": len(self._browsers),
            "max_pages_per_browser": TokenBrowser.MAX_PAGES,
            "browsers": browser_info
        }
