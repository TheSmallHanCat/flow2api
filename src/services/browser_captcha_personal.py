"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
支持常驻模式：为每个 project_id 自动创建常驻标签页，即时生成 token
"""
import asyncio
import time
import os
from typing import Optional

import nodriver as uc

from ..core.logger import debug_logger


class ResidentTabInfo:
    """常驻标签页信息结构"""
    def __init__(self, tab, project_id: str):
        self.tab = tab
        self.project_id = project_id
        self.recaptcha_ready = False
        self.created_at = time.time()


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（nodriver 有头模式）
    
    支持两种模式：
    1. 常驻模式 (Resident Mode): 为每个 project_id 保持常驻标签页，即时生成 token
    2. 传统模式 (Legacy Mode): 每次请求创建新标签页 (fallback)
    """

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        self.headless = False  # nodriver 有头模式
        self.browser = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        # 持久化 profile 目录
        self.user_data_dir = os.path.join(os.getcwd(), "browser_data")
        
        # 常驻模式相关属性 (支持多 project_id)
        self._resident_tabs: dict[str, 'ResidentTabInfo'] = {}  # project_id -> 常驻标签页信息
        self._resident_lock = asyncio.Lock()  # 保护常驻标签页操作
        
        # 兼容旧 API（保留 single resident 属性作为别名）
        self.resident_project_id: Optional[str] = None  # 向后兼容
        self.resident_tab = None                         # 向后兼容
        self._running = False                            # 向后兼容
        self._recaptcha_ready = False                    # 向后兼容

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
        return cls._instance

    async def initialize(self):
        """初始化 nodriver 浏览器"""
        if self._initialized and self.browser:
            # 检查浏览器是否仍然存活
            try:
                # 尝试获取浏览器信息验证存活
                if self.browser.stopped:
                    debug_logger.log_warning("[BrowserCaptcha] 浏览器已停止，重新初始化...")
                    self._initialized = False
                else:
                    return
            except Exception:
                debug_logger.log_warning("[BrowserCaptcha] 浏览器无响应，重新初始化...")
                self._initialized = False

        try:
            debug_logger.log_info(f"[BrowserCaptcha] 正在启动 nodriver 浏览器 (用户数据目录: {self.user_data_dir})...")

            # 确保 user_data_dir 存在
            os.makedirs(self.user_data_dir, exist_ok=True)

            # 启动 nodriver 浏览器
            self.browser = await uc.start(
                headless=self.headless,
                user_data_dir=self.user_data_dir,
                sandbox=False,  # nodriver 需要此参数来禁用 sandbox
                browser_args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--window-size=1280,720',
                    '--profile-directory=Default',  # 跳过 Profile 选择器页面
                ]
            )

            self._initialized = True
            debug_logger.log_info(f"[BrowserCaptcha] ✅ nodriver 浏览器已启动 (Profile: {self.user_data_dir})")

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 浏览器启动失败: {str(e)}")
            raise

    # ========== 常驻模式 API ==========

    async def start_resident_mode(self, project_id: str):
        """启动常驻模式
        
        Args:
            project_id: 用于常驻的项目 ID
        """
        if self._running:
            debug_logger.log_warning("[BrowserCaptcha] 常驻模式已在运行")
            return
        
        await self.initialize()
        
        self.resident_project_id = project_id
        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        
        debug_logger.log_info(f"[BrowserCaptcha] 启动常驻模式，访问页面: {website_url}")
        
        # 创建一个独立的新标签页（不使用 main_tab，避免被回收）
        self.resident_tab = await self.browser.get(website_url, new_tab=True)
        
        debug_logger.log_info("[BrowserCaptcha] 标签页已创建，等待页面加载...")
        
        # 等待页面加载完成（带重试机制）
        page_loaded = False
        for retry in range(60):
            try:
                await asyncio.sleep(1)
                ready_state = await self.resident_tab.evaluate("document.readyState")
                debug_logger.log_info(f"[BrowserCaptcha] 页面状态: {ready_state} (重试 {retry + 1}/60)")
                if ready_state == "complete":
                    page_loaded = True
                    break
            except ConnectionRefusedError as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 标签页连接丢失: {e}，尝试重新获取...")
                # 标签页可能已关闭，尝试重新创建
                try:
                    self.resident_tab = await self.browser.get(website_url, new_tab=True)
                    debug_logger.log_info("[BrowserCaptcha] 已重新创建标签页")
                except Exception as e2:
                    debug_logger.log_error(f"[BrowserCaptcha] 重新创建标签页失败: {e2}")
                await asyncio.sleep(2)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 等待页面异常: {e}，重试 {retry + 1}/15...")
                await asyncio.sleep(2)
        
        if not page_loaded:
            debug_logger.log_error("[BrowserCaptcha] 页面加载超时，常驻模式启动失败")
            return
        
        # 等待 reCAPTCHA 加载
        self._recaptcha_ready = await self._wait_for_recaptcha(self.resident_tab)
        
        if not self._recaptcha_ready:
            debug_logger.log_error("[BrowserCaptcha] reCAPTCHA 加载失败，常驻模式启动失败")
            return
        
        self._running = True
        debug_logger.log_info(f"[BrowserCaptcha] ✅ 常驻模式已启动 (project: {project_id})")

    async def stop_resident_mode(self, project_id: Optional[str] = None):
        """停止常驻模式
        
        Args:
            project_id: 指定要关闭的 project_id，如果为 None 则关闭所有常驻标签页
        """
        async with self._resident_lock:
            if project_id:
                # 关闭指定的常驻标签页
                await self._close_resident_tab(project_id)
                debug_logger.log_info(f"[BrowserCaptcha] 已关闭 project_id={project_id} 的常驻模式")
            else:
                # 关闭所有常驻标签页
                project_ids = list(self._resident_tabs.keys())
                for pid in project_ids:
                    resident_info = self._resident_tabs.pop(pid, None)
                    if resident_info and resident_info.tab:
                        try:
                            await resident_info.tab.close()
                        except Exception:
                            pass
                debug_logger.log_info(f"[BrowserCaptcha] 已关闭所有常驻标签页 (共 {len(project_ids)} 个)")
        
        # 向后兼容：清理旧属性
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

    async def _wait_for_recaptcha(self, tab) -> bool:
        """等待 reCAPTCHA 加载
        
        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[BrowserCaptcha] 检测 reCAPTCHA...")
        
        # 检查 grecaptcha.enterprise.execute
        is_enterprise = await tab.evaluate(
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
        )
        
        if is_enterprise:
            debug_logger.log_info("[BrowserCaptcha] reCAPTCHA Enterprise 已加载")
            return True
        
        # 尝试注入脚本
        debug_logger.log_info("[BrowserCaptcha] 未检测到 reCAPTCHA，注入脚本...")
        
        await tab.evaluate(f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/recaptcha/api.js?render={self.website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """)
        
        # 等待脚本加载
        await tab.sleep(3)
        
        # 轮询等待 reCAPTCHA 加载
        for i in range(20):
            is_enterprise = await tab.evaluate(
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
            )
            
            if is_enterprise:
                debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA Enterprise 已加载（等待了 {i * 0.5} 秒）")
                return True
            await tab.sleep(0.5)
        
        debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 加载超时")
        return False

    async def _execute_recaptcha_on_tab(self, tab) -> Optional[str]:
        """在指定标签页执行 reCAPTCHA 获取 token
        
        Args:
            tab: nodriver 标签页对象
            
        Returns:
            reCAPTCHA token 或 None
        """
        # 生成唯一变量名避免冲突
        ts = int(time.time() * 1000)
        token_var = f"_recaptcha_token_{ts}"
        error_var = f"_recaptcha_error_{ts}"
        
        execute_script = f"""
            (() => {{
                window.{token_var} = null;
                window.{error_var} = null;
                
                try {{
                    grecaptcha.enterprise.ready(function() {{
                        grecaptcha.enterprise.execute('{self.website_key}', {{action: 'FLOW_GENERATION'}})
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
        
        # 注入执行脚本
        await tab.evaluate(execute_script)
        
        # 轮询等待结果（最多 15 秒）
        token = None
        for i in range(30):
            await tab.sleep(0.5)
            token = await tab.evaluate(f"window.{token_var}")
            if token:
                break
            error = await tab.evaluate(f"window.{error_var}")
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 错误: {error}")
                break
        
        # 清理临时变量
        try:
            await tab.evaluate(f"delete window.{token_var}; delete window.{error_var};")
        except:
            pass
        
        return token

    # ========== 主要 API ==========

    async def get_token(self, project_id: str) -> Optional[str]:
        """获取 reCAPTCHA token
        
        自动常驻模式：如果该 project_id 没有常驻标签页，则自动创建并常驻
        
        Args:
            project_id: Flow项目ID

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        # 确保浏览器已初始化
        await self.initialize()
        
        # 尝试从常驻标签页获取 token
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)
            
            # 如果该 project_id 没有常驻标签页，则自动创建
            if resident_info is None:
                debug_logger.log_info(f"[BrowserCaptcha] project_id={project_id} 没有常驻标签页，正在创建...")
                resident_info = await self._create_resident_tab(project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[BrowserCaptcha] 无法为 project_id={project_id} 创建常驻标签页，fallback 到传统模式")
                    return await self._get_token_legacy(project_id)
                self._resident_tabs[project_id] = resident_info
                debug_logger.log_info(f"[BrowserCaptcha] ✅ 已为 project_id={project_id} 创建常驻标签页 (当前共 {len(self._resident_tabs)} 个)")
        
        # 使用常驻标签页生成 token
        if resident_info and resident_info.recaptcha_ready and resident_info.tab:
            start_time = time.time()
            debug_logger.log_info(f"[BrowserCaptcha] 从常驻标签页即时生成 token (project: {project_id})...")
            try:
                token = await self._execute_recaptcha_on_tab(resident_info.tab)
                duration_ms = (time.time() - start_time) * 1000
                if token:
                    debug_logger.log_info(f"[BrowserCaptcha] ✅ Token生成成功（耗时 {duration_ms:.0f}ms）")
                    return token
                else:
                    debug_logger.log_warning(f"[BrowserCaptcha] 常驻标签页生成失败 (project: {project_id})，尝试重建...")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 常驻标签页异常: {e}，尝试重建...")
            
            # 常驻标签页失效，尝试重建
            async with self._resident_lock:
                await self._close_resident_tab(project_id)
                resident_info = await self._create_resident_tab(project_id)
                if resident_info:
                    self._resident_tabs[project_id] = resident_info
                    # 重建后立即尝试生成
                    try:
                        token = await self._execute_recaptcha_on_tab(resident_info.tab)
                        if token:
                            debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Token生成成功")
                            return token
                    except Exception:
                        pass
        
        # 最终 Fallback: 使用传统模式
        debug_logger.log_warning(f"[BrowserCaptcha] 所有常驻方式失败，fallback 到传统模式 (project: {project_id})")
        return await self._get_token_legacy(project_id)

    async def _create_resident_tab(self, project_id: str) -> Optional[ResidentTabInfo]:
        """为指定 project_id 创建常驻标签页
        
        Args:
            project_id: 项目 ID
            
        Returns:
            ResidentTabInfo 对象，或 None（创建失败）
        """
        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] 为 project_id={project_id} 创建常驻标签页，访问: {website_url}")
            
            # 创建新标签页
            tab = await self.browser.get(website_url, new_tab=True)
            
            # 等待页面加载完成
            page_loaded = False
            for retry in range(60):
                try:
                    await asyncio.sleep(1)
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        page_loaded = True
                        break
                except ConnectionRefusedError as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 标签页连接丢失: {e}")
                    return None
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 等待页面异常: {e}，重试 {retry + 1}/60...")
                    await asyncio.sleep(1)
            
            if not page_loaded:
                debug_logger.log_error(f"[BrowserCaptcha] 页面加载超时 (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return None
            
            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)
            
            if not recaptcha_ready:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 加载失败 (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return None
            
            # 创建常驻信息对象
            resident_info = ResidentTabInfo(tab, project_id)
            resident_info.recaptcha_ready = True
            
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 常驻标签页创建成功 (project: {project_id})")
            return resident_info
            
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 创建常驻标签页异常: {e}")
            return None

    async def _close_resident_tab(self, project_id: str):
        """关闭指定 project_id 的常驻标签页
        
        Args:
            project_id: 项目 ID
        """
        resident_info = self._resident_tabs.pop(project_id, None)
        if resident_info and resident_info.tab:
            try:
                await resident_info.tab.close()
                debug_logger.log_info(f"[BrowserCaptcha] 已关闭 project_id={project_id} 的常驻标签页")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 关闭标签页时异常: {e}")

    async def _get_token_legacy(self, project_id: str) -> Optional[str]:
        """传统模式获取 reCAPTCHA token（每次创建新标签页）

        Args:
            project_id: Flow项目ID

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        # 确保浏览器已启动
        if not self._initialized or not self.browser:
            await self.initialize()

        start_time = time.time()
        tab = None

        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 访问页面: {website_url}")

            # 新建标签页并访问页面
            tab = await self.browser.get(website_url)

            # 等待页面完全加载（增加等待时间）
            debug_logger.log_info("[BrowserCaptcha] [Legacy] 等待页面加载...")
            await tab.sleep(3)
            
            # 等待页面 DOM 完成
            for _ in range(10):
                ready_state = await tab.evaluate("document.readyState")
                if ready_state == "complete":
                    break
                await tab.sleep(0.5)

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] reCAPTCHA 无法加载")
                return None

            # 执行 reCAPTCHA
            debug_logger.log_info("[BrowserCaptcha] [Legacy] 执行 reCAPTCHA 验证...")
            token = await self._execute_recaptcha_on_tab(tab)

            duration_ms = (time.time() - start_time) * 1000

            if token:
                debug_logger.log_info(f"[BrowserCaptcha] [Legacy] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
                return token
            else:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] Token获取失败（返回null）")
                return None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] [Legacy] 获取token异常: {str(e)}")
            return None
        finally:
            # 关闭标签页（但保留浏览器）
            if tab:
                try:
                    await tab.close()
                except Exception:
                    pass

    async def close(self):
        """关闭浏览器"""
        # 先停止所有常驻模式（关闭所有常驻标签页）
        await self.stop_resident_mode()
        
        try:
            if self.browser:
                try:
                    self.browser.stop()
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 关闭浏览器时出现异常: {str(e)}")
                finally:
                    self.browser = None

            self._initialized = False
            self._resident_tabs.clear()  # 确保清空常驻字典
            debug_logger.log_info("[BrowserCaptcha] 浏览器已关闭")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 关闭浏览器异常: {str(e)}")

    async def open_login_window(self):
        """打开登录窗口供用户手动登录 Google"""
        await self.initialize()
        tab = await self.browser.get("https://accounts.google.com/")
        debug_logger.log_info("[BrowserCaptcha] 请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")
        print("请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")

    # ========== Session Token 刷新 ==========

    async def refresh_session_token(self, project_id: str) -> Optional[str]:
        """从常驻标签页获取最新的 Session Token
        
        复用 reCAPTCHA 常驻标签页，通过刷新页面并从 cookies 中提取
        __Secure-next-auth.session-token
        
        Args:
            project_id: 项目ID，用于定位常驻标签页
            
        Returns:
            新的 Session Token，如果获取失败返回 None
        """
        # 确保浏览器已初始化
        await self.initialize()
        
        start_time = time.time()
        debug_logger.log_info(f"[BrowserCaptcha] 开始刷新 Session Token (project: {project_id})...")
        
        # 尝试获取或创建常驻标签页
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)
            
            # 如果该 project_id 没有常驻标签页，则创建
            if resident_info is None:
                debug_logger.log_info(f"[BrowserCaptcha] project_id={project_id} 没有常驻标签页，正在创建...")
                resident_info = await self._create_resident_tab(project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[BrowserCaptcha] 无法为 project_id={project_id} 创建常驻标签页")
                    return None
                self._resident_tabs[project_id] = resident_info
        
        if not resident_info or not resident_info.tab:
            debug_logger.log_error(f"[BrowserCaptcha] 无法获取常驻标签页")
            return None
        
        tab = resident_info.tab
        
        try:
            # 刷新页面以获取最新的 cookies
            debug_logger.log_info(f"[BrowserCaptcha] 刷新常驻标签页以获取最新 cookies...")
            await tab.reload()
            
            # 等待页面加载完成
            for i in range(30):
                await asyncio.sleep(1)
                try:
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        break
                except Exception:
                    pass
            
            # 额外等待确保 cookies 已设置
            await asyncio.sleep(2)
            
            # 从 cookies 中提取 __Secure-next-auth.session-token
            # nodriver 可以通过 browser 获取 cookies
            session_token = None
            
            try:
                # 使用 nodriver 的 cookies API 获取所有 cookies
                cookies = await self.browser.cookies.get_all()
                
                for cookie in cookies:
                    if cookie.name == "__Secure-next-auth.session-token":
                        session_token = cookie.value
                        break
                        
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 通过 cookies API 获取失败: {e}，尝试从 document.cookie 获取...")
                
                # 备选方案：通过 JavaScript 获取 (注意：HttpOnly cookies 可能无法通过此方式获取)
                try:
                    all_cookies = await tab.evaluate("document.cookie")
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
                debug_logger.log_info(f"[BrowserCaptcha] ✅ Session Token 获取成功（耗时 {duration_ms:.0f}ms）")
                return session_token
            else:
                debug_logger.log_error(f"[BrowserCaptcha] ❌ 未找到 __Secure-next-auth.session-token cookie")
                return None
                
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 刷新 Session Token 异常: {str(e)}")
            
            # 常驻标签页可能已失效，尝试重建
            async with self._resident_lock:
                await self._close_resident_tab(project_id)
                resident_info = await self._create_resident_tab(project_id)
                if resident_info:
                    self._resident_tabs[project_id] = resident_info
                    # 重建后再次尝试获取
                    try:
                        cookies = await self.browser.cookies.get_all()
                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Session Token 获取成功")
                                return cookie.value
                    except Exception:
                        pass
            
            return None

    # ========== 状态查询 ==========

    def is_resident_mode_active(self) -> bool:
        """检查是否有任何常驻标签页激活"""
        return len(self._resident_tabs) > 0 or self._running

    def get_resident_count(self) -> int:
        """获取当前常驻标签页数量"""
        return len(self._resident_tabs)

    def get_resident_project_ids(self) -> list[str]:
        """获取所有当前常驻的 project_id 列表"""
        return list(self._resident_tabs.keys())

    def get_resident_project_id(self) -> Optional[str]:
        """获取当前常驻的 project_id（向后兼容，返回第一个）"""
        if self._resident_tabs:
            return next(iter(self._resident_tabs.keys()))
        return self.resident_project_id