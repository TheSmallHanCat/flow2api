"""
浏览器自动化获取 reCAPTCHA token
使用 Playwright 访问页面并执行 reCAPTCHA 验证
"""
import asyncio
import time
import re
from typing import Optional, Dict
from playwright.async_api import async_playwright, Browser, BrowserContext

from ..core.logger import debug_logger


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


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（单例模式）"""

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, proxy_manager=None):
        """初始化服务（始终使用无头模式）"""
        self.headless = True  # 始终无头
        self.playwright = None
        self.browser: Optional[Browser] = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.proxy_manager = proxy_manager

    @classmethod
    async def get_instance(cls, proxy_manager=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(proxy_manager)
                    await cls._instance.initialize()
        return cls._instance

    async def initialize(self):
        """初始化浏览器（启动一次）"""
        if self._initialized:
            return

        try:
            # 获取代理配置
            proxy_url = None
            if self.proxy_manager:
                proxy_url = await self.proxy_manager.get_proxy_url()

            debug_logger.log_info(f"[BrowserCaptcha] 正在启动浏览器... (proxy={proxy_url or 'None'})")
            self.playwright = await async_playwright().start()

            # 配置浏览器启动参数
            launch_options = {
                'headless': self.headless,
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox'
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
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 浏览器已启动 (headless={self.headless}, proxy={proxy_url or 'None'})")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 浏览器启动失败: {str(e)}")
            raise

    async def get_token(self, project_id: str) -> Optional[str]:
        """获取 reCAPTCHA token

        Args:
            project_id: Flow项目ID

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        if not self._initialized:
            await self.initialize()

        start_time = time.time()
        context = None

        try:
            # 创建新的上下文
            context = await self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York'
            )
            page = await context.new_page()

            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"

            debug_logger.log_info(f"[BrowserCaptcha] 访问页面: {website_url}")

            # 访问页面
            try:
                await page.goto(website_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 页面加载超时或失败: {str(e)}")

            # 检查并注入 reCAPTCHA v3 脚本
            debug_logger.log_info("[BrowserCaptcha] 检查并加载 reCAPTCHA v3 脚本...")
            script_loaded = await page.evaluate("""
                () => {
                    if (window.grecaptcha && typeof window.grecaptcha.execute === 'function') {
                        return true;
                    }
                    return false;
                }
            """)

            if not script_loaded:
                # 注入脚本
                debug_logger.log_info("[BrowserCaptcha] 注入 reCAPTCHA v3 脚本...")
                await page.evaluate(f"""
                    () => {{
                        return new Promise((resolve) => {{
                            const script = document.createElement('script');
                            script.src = 'https://www.google.com/recaptcha/api.js?render={self.website_key}';
                            script.async = true;
                            script.defer = true;
                            script.onload = () => resolve(true);
                            script.onerror = () => resolve(false);
                            document.head.appendChild(script);
                        }});
                    }}
                """)

            # 等待reCAPTCHA加载和初始化
            debug_logger.log_info("[BrowserCaptcha] 等待reCAPTCHA初始化...")
            for i in range(20):
                grecaptcha_ready = await page.evaluate("""
                    () => {
                        return window.grecaptcha &&
                               typeof window.grecaptcha.execute === 'function';
                    }
                """)
                if grecaptcha_ready:
                    debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA 已准备好（等待了 {i*0.5} 秒）")
                    break
                await asyncio.sleep(0.5)
            else:
                debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 初始化超时，继续尝试执行...")

            # 额外等待确保完全初始化
            await page.wait_for_timeout(1000)

            # 执行reCAPTCHA并获取token
            debug_logger.log_info("[BrowserCaptcha] 执行reCAPTCHA验证...")
            token = await page.evaluate("""
                async (websiteKey) => {
                    try {
                        if (!window.grecaptcha) {
                            console.error('[BrowserCaptcha] window.grecaptcha 不存在');
                            return null;
                        }

                        if (typeof window.grecaptcha.execute !== 'function') {
                            console.error('[BrowserCaptcha] window.grecaptcha.execute 不是函数');
                            return null;
                        }

                        // 确保grecaptcha已准备好
                        await new Promise((resolve, reject) => {
                            const timeout = setTimeout(() => {
                                reject(new Error('reCAPTCHA加载超时'));
                            }, 15000);

                            if (window.grecaptcha && window.grecaptcha.ready) {
                                window.grecaptcha.ready(() => {
                                    clearTimeout(timeout);
                                    resolve();
                                });
                            } else {
                                clearTimeout(timeout);
                                resolve();
                            }
                        });

                        // 执行reCAPTCHA v3
                        const token = await window.grecaptcha.execute(websiteKey, {
                            action: 'FLOW_GENERATION'
                        });

                        return token;
                    } catch (error) {
                        console.error('[BrowserCaptcha] reCAPTCHA执行错误:', error);
                        return null;
                    }
                }
            """, self.website_key)

            duration_ms = (time.time() - start_time) * 1000

            if token:
                debug_logger.log_info(f"[BrowserCaptcha] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
                return token
            else:
                debug_logger.log_error("[BrowserCaptcha] Token获取失败（返回null）")
                return None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 获取token异常: {str(e)}")
            return None
        finally:
            # 关闭上下文
            if context:
                try:
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
