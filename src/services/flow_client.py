"""Flow API Client for VideoFX (Veo)"""
import asyncio
import time
import uuid
import random
import base64
from typing import Dict, Any, Optional, List
from curl_cffi.requests import AsyncSession
from ..core.logger import debug_logger
from ..core.config import config


class FlowClient:
    """VideoFX API客户端"""

    def __init__(self, proxy_manager, db=None):
        self.proxy_manager = proxy_manager
        self.db = db  # Database instance for captcha config
        self.labs_base_url = config.flow_labs_base_url  # https://labs.google/fx/api
        self.api_base_url = config.flow_api_base_url    # https://aisandbox-pa.googleapis.com/v1
        self.timeout = config.flow_timeout
        # 缓存每个账号的 User-Agent
        self._user_agent_cache = {}

        # Default "real browser" headers (Android Chrome style) to reduce upstream 4xx/5xx instability.
        # These will be applied as defaults (won't override caller-provided headers).
        self._default_client_headers = {
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": "\"Android\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "x-browser-channel": "stable",
            "x-browser-copyright": "Copyright 2026 Google LLC. All Rights reserved.",
            "x-browser-validation": "UujAs0GAwdnCJ9nvrswZ+O+oco0=",
            "x-browser-year": "2026",
            "x-client-data": "CJS2yQEIpLbJAQipncoBCNj9ygEIlKHLAQiFoM0BGP6lzwE="
        }

    def _generate_user_agent(self, account_id: str = None) -> str:
        """基于账号ID生成固定的 User-Agent
        
        Args:
            account_id: 账号标识（如 email 或 token_id），相同账号返回相同 UA
            
        Returns:
            User-Agent 字符串
        """
        # 如果没有提供账号ID，生成随机UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"
        
        # 如果已缓存，直接返回
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]
        
        # 使用账号ID作为随机种子，确保同一账号生成相同的UA
        import hashlib
        seed = int(hashlib.md5(account_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        
        # Chrome 版本池
        chrome_versions = ["130.0.0.0", "131.0.0.0", "132.0.0.0", "129.0.0.0"]
        # Firefox 版本池
        firefox_versions = ["133.0", "132.0", "131.0", "134.0"]
        # Safari 版本池
        safari_versions = ["18.2", "18.1", "18.0", "17.6"]
        # Edge 版本池
        edge_versions = ["130.0.0.0", "131.0.0.0", "132.0.0.0"]

        # 操作系统配置
        os_configs = [
            # Windows
            {
                "platform": "Windows NT 10.0; Win64; x64",
                "browsers": [
                    lambda r: f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{r.choice(chrome_versions)} Safari/537.36",
                    lambda r: f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{r.choice(firefox_versions).split('.')[0]}.0) Gecko/20100101 Firefox/{r.choice(firefox_versions)}",
                    lambda r: f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{r.choice(chrome_versions)} Safari/537.36 Edg/{r.choice(edge_versions)}",
                ]
            },
            # macOS
            {
                "platform": "Macintosh; Intel Mac OS X 10_15_7",
                "browsers": [
                    lambda r: f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{r.choice(chrome_versions)} Safari/537.36",
                    lambda r: f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{r.choice(safari_versions)} Safari/605.1.15",
                    lambda r: f"Mozilla/5.0 (Macintosh; Intel Mac OS X 14.{r.randint(0, 7)}; rv:{r.choice(firefox_versions).split('.')[0]}.0) Gecko/20100101 Firefox/{r.choice(firefox_versions)}",
                ]
            },
            # Linux
            {
                "platform": "X11; Linux x86_64",
                "browsers": [
                    lambda r: f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{r.choice(chrome_versions)} Safari/537.36",
                    lambda r: f"Mozilla/5.0 (X11; Linux x86_64; rv:{r.choice(firefox_versions).split('.')[0]}.0) Gecko/20100101 Firefox/{r.choice(firefox_versions)}",
                    lambda r: f"Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:{r.choice(firefox_versions).split('.')[0]}.0) Gecko/20100101 Firefox/{r.choice(firefox_versions)}",
                ]
            }
        ]

        # 使用固定种子随机选择操作系统和浏览器
        os_config = rng.choice(os_configs)
        browser_generator = rng.choice(os_config["browsers"])
        user_agent = browser_generator(rng)
        
        # 缓存结果
        self._user_agent_cache[account_id] = user_agent
        
        return user_agent

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """统一HTTP请求处理

        Args:
            method: HTTP方法 (GET/POST)
            url: 完整URL
            headers: 请求头
            json_data: JSON请求体
            use_st: 是否使用ST认证 (Cookie方式)
            st_token: Session Token
            use_at: 是否使用AT认证 (Bearer方式)
            at_token: Access Token
            timeout: 自定义超时时间(秒)，不传则使用默认值
        """
        proxy_url = await self.proxy_manager.get_proxy_url()
        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}

        # ST认证 - 使用Cookie
        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        # AT认证 - 使用Bearer
        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        # 确定账号标识（优先使用 token 的前16个字符作为标识）
        account_id = None
        if st_token:
            account_id = st_token[:16]  # 使用 ST 的前16个字符
        elif at_token:
            account_id = at_token[:16]  # 使用 AT 的前16个字符

        # 通用请求头 - 基于账号生成固定的 User-Agent
        headers.update({
            "Content-Type": "application/json",
            "User-Agent": self._generate_user_agent(account_id)
        })

        # Add default Chromium/Android client headers (do not override explicitly provided values).
        for key, value in self._default_client_headers.items():
            headers.setdefault(key, value)

        # Log request
        if config.debug_enabled:
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=json_data,
                proxy=proxy_url
            )

        start_time = time.time()

        try:
            async with AsyncSession() as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate="chrome110"
                    )
                else:  # POST
                    response = await session.post(
                        url,
                        headers=headers,
                        json=json_data,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate="chrome110"
                    )

                duration_ms = (time.time() - start_time) * 1000

                # Log response
                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms
                    )

                # 检查HTTP错误
                if response.status_code >= 400:
                    # 解析错误响应
                    error_reason = f"HTTP Error {response.status_code}"
                    try:
                        error_body = response.json()
                        # 提取 Google API 错误格式中的 reason
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            # 从 details 中提取 reason
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except:
                        error_reason = f"HTTP Error {response.status_code}: {response.text[:200]}"
                    
                    # 失败时输出请求体和错误内容到控制台
                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(f"[API FAILED] Request Body: {json_data}")
                    debug_logger.log_error(f"[API FAILED] Response: {response.text}")
                    
                    raise Exception(error_reason)

                return response.json()

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = str(e)

            # 如果不是我们自己抛出的异常，记录日志
            if "HTTP Error" not in error_msg and not any(x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(f"[API FAILED] Request Body: {json_data}")
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")

            raise Exception(f"Flow API request failed: {error_msg}")

    # ========== 认证相关 (使用ST) ==========

    async def st_to_at(self, st: str) -> dict:
        """ST转AT

        Args:
            st: Session Token

        Returns:
            {
                "access_token": "AT",
                "expires": "2025-11-15T04:46:04.000Z",
                "user": {...}
            }
        """
        url = f"{self.labs_base_url}/auth/session"
        result = await self._make_request(
            method="GET",
            url=url,
            use_st=True,
            st_token=st
        )
        return result

    # ========== 项目管理 (使用ST) ==========

    async def create_project(self, st: str, title: str) -> str:
        """创建项目,返回project_id

        Args:
            st: Session Token
            title: 项目标题

        Returns:
            project_id (UUID)
        """
        url = f"{self.labs_base_url}/trpc/project.createProject"
        json_data = {
            "json": {
                "projectTitle": title,
                "toolName": "PINHOLE"
            }
        }

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st
        )

        # 解析返回的project_id
        project_id = result["result"]["data"]["json"]["result"]["projectId"]
        return project_id

    async def delete_project(self, st: str, project_id: str):
        """删除项目

        Args:
            st: Session Token
            project_id: 项目ID
        """
        url = f"{self.labs_base_url}/trpc/project.deleteProject"
        json_data = {
            "json": {
                "projectToDeleteId": project_id
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st
        )

    # ========== 余额查询 (使用AT) ==========

    async def get_credits(self, at: str) -> dict:
        """查询余额

        Args:
            at: Access Token

        Returns:
            {
                "credits": 920,
                "userPaygateTier": "PAYGATE_TIER_ONE"
            }
        """
        url = f"{self.api_base_url}/credits"
        result = await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at
        )
        return result

    # ========== 图片上传 (使用AT) ==========

    def _detect_image_mime_type(self, image_bytes: bytes) -> str:
        """通过文件头 magic bytes 检测图片 MIME 类型

        Args:
            image_bytes: 图片字节数据

        Returns:
            MIME 类型字符串，默认 image/jpeg
        """
        if len(image_bytes) < 12:
            return "image/jpeg"

        # WebP: RIFF....WEBP
        if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
            return "image/webp"
        # PNG: 89 50 4E 47
        if image_bytes[:4] == b'\x89PNG':
            return "image/png"
        # JPEG: FF D8 FF
        if image_bytes[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        # GIF: GIF87a 或 GIF89a
        if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        # BMP: BM
        if image_bytes[:2] == b'BM':
            return "image/bmp"
        # JPEG 2000: 00 00 00 0C 6A 50
        if image_bytes[:6] == b'\x00\x00\x00\x0cjP':
            return "image/jp2"

        return "image/jpeg"

    def _convert_to_jpeg(self, image_bytes: bytes) -> bytes:
        """将图片转换为 JPEG 格式

        Args:
            image_bytes: 原始图片字节数据

        Returns:
            JPEG 格式的图片字节数据
        """
        from io import BytesIO
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        # 如果有透明通道，转换为 RGB
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        
        output = BytesIO()
        img.save(output, format='JPEG', quality=95)
        return output.getvalue()

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE"
    ) -> str:
        """上传图片,返回mediaGenerationId

        Args:
            at: Access Token
            image_bytes: 图片字节数据
            aspect_ratio: 图片或视频宽高比（会自动转换为图片格式）

        Returns:
            mediaGenerationId (CAM...)
        """
        # 转换视频aspect_ratio为图片aspect_ratio
        # VIDEO_ASPECT_RATIO_LANDSCAPE -> IMAGE_ASPECT_RATIO_LANDSCAPE
        # VIDEO_ASPECT_RATIO_PORTRAIT -> IMAGE_ASPECT_RATIO_PORTRAIT
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        # 自动检测图片 MIME 类型
        mime_type = self._detect_image_mime_type(image_bytes)

        # 编码为base64 (去掉前缀)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        url = f"{self.api_base_url}:uploadUserImage"
        json_data = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": mime_type,
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio
            },
            "clientContext": {
                "sessionId": self._generate_session_id(),
                "tool": "ASSET_MANAGER"
            }
        }

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )

        # 返回mediaGenerationId
        media_id = result["mediaGenerationId"]["mediaGenerationId"]
        return media_id

    # ========== 图片生成 (使用AT) - 同步返回 ==========

    async def generate_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None
    ) -> dict:
        """生成图片(同步返回)

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_name: GEM_PIX, GEM_PIX_2 或 IMAGEN_3_5
            aspect_ratio: 图片宽高比
            image_inputs: 参考图片列表(图生图时使用)

        Returns:
            {
                "media": [{
                    "image": {
                        "generatedImage": {
                            "fifeUrl": "图片URL",
                            ...
                        }
                    }
                }]
            }
        """
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token
            recaptcha_token, browser_id = await self._get_recaptcha_token(project_id, action="IMAGE_GENERATION")
            if not recaptcha_token:
                raise Exception("Failed to obtain reCAPTCHA token")
            session_id = self._generate_session_id()

            # 构建请求 - clientContext 只在外层，requests 内不重复
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE"
            }

            request_data = {
                "seed": random.randint(1, 99999),
                "imageModelName": model_name,
                "imageAspectRatio": aspect_ratio,
                "prompt": prompt,
                "imageInputs": image_inputs or []
            }

            json_data = {
                "clientContext": client_context,
                "requests": [request_data]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                return result
            except Exception as e:
                error_str = str(e)
                last_error = e
                retry_reason = self._get_retry_reason(error_str)
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(f"[IMAGE] 生成遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})...")
                    await self._notify_browser_captcha_error(browser_id)
                    await asyncio.sleep(1)
                    continue
                else:
                    raise e
        
        # 所有重试都失败
        raise last_error

    async def upsample_image(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K"
    ) -> str:
        """放大图片到 2K/4K

        Args:
            at: Access Token
            project_id: 项目ID
            media_id: 图片的 mediaId (从 batchGenerateImages 返回的 media[0]["name"])
            target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K 或 UPSAMPLE_IMAGE_RESOLUTION_4K

        Returns:
            base64 编码的图片数据
        """
        url = f"{self.api_base_url}/flow/upsampleImage"

        # 获取 reCAPTCHA token - 使用 IMAGE_GENERATION action
        recaptcha_token, _ = await self._get_recaptcha_token(project_id, action="IMAGE_GENERATION")
        if not recaptcha_token:
            raise Exception("Failed to obtain reCAPTCHA token")
        session_id = self._generate_session_id()

        json_data = {
            "mediaId": media_id,
            "targetResolution": target_resolution,
            "clientContext": {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE"
            }
        }

        # 4K/2K 放大使用专用超时，因为返回的 base64 数据量很大
        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at,
            timeout=config.upsample_timeout
        )

        # 返回 base64 编码的图片
        return result.get("encodedImage", "")

    # ========== 视频生成 (使用AT) - 异步返回 ==========

    async def generate_video_text(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE"
    ) -> dict:
        """文生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_t2v_fast 等
            aspect_ratio: 视频宽高比
            user_paygate_tier: 用户等级

        Returns:
            {
                "operations": [{
                    "operation": {"name": "task_id"},
                    "sceneId": "uuid",
                    "status": "MEDIA_GENERATION_STATUS_PENDING"
                }],
                "remainingCredits": 900
            }
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(project_id, action="VIDEO_GENERATION")
            if not recaptcha_token:
                raise Exception("Failed to obtain reCAPTCHA token")
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "prompt": prompt
                    },
                    "videoModelKey": model_key,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                return result
            except Exception as e:
                error_str = str(e)
                last_error = e
                retry_reason = self._get_retry_reason(error_str)
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(f"[VIDEO T2V] 生成遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})...")
                    await self._notify_browser_captcha_error(browser_id)
                    await asyncio.sleep(1)
                    continue
                else:
                    raise e
        
        # 所有重试都失败
        raise last_error

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE"
    ) -> dict:
        """图生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_0_r2v_fast
            aspect_ratio: 视频宽高比
            reference_images: 参考图片列表 [{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "..."}]
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoReferenceImages"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(project_id, action="VIDEO_GENERATION")
            if not recaptcha_token:
                raise Exception("Failed to obtain reCAPTCHA token")
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "prompt": prompt
                    },
                    "videoModelKey": model_key,
                    "referenceImages": reference_images,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                return result
            except Exception as e:
                error_str = str(e)
                last_error = e
                retry_reason = self._get_retry_reason(error_str)
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(f"[VIDEO R2V] 生成遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})...")
                    await self._notify_browser_captcha_error(browser_id)
                    await asyncio.sleep(1)
                    continue
                else:
                    raise e
        
        # 所有重试都失败
        raise last_error

    async def generate_video_start_end(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE"
    ) -> dict:
        """收尾帧生成视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_i2v_s_fast_fl
            aspect_ratio: 视频宽高比
            start_media_id: 起始帧mediaId
            end_media_id: 结束帧mediaId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartAndEndImage"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(project_id, action="VIDEO_GENERATION")
            if not recaptcha_token:
                raise Exception("Failed to obtain reCAPTCHA token")
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "prompt": prompt
                    },
                    "videoModelKey": model_key,
                    "startImage": {
                        "mediaId": start_media_id
                    },
                    "endImage": {
                        "mediaId": end_media_id
                    },
                    "metadata": {
                        "sceneId": scene_id
                    }
                }]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                return result
            except Exception as e:
                error_str = str(e)
                last_error = e
                retry_reason = self._get_retry_reason(error_str)
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(f"[VIDEO I2V] 首尾帧生成遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})...")
                    await self._notify_browser_captcha_error(browser_id)
                    await asyncio.sleep(1)
                    continue
                else:
                    raise e
        
        # 所有重试都失败
        raise last_error

    async def generate_video_start_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE"
    ) -> dict:
        """仅首帧生成视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_i2v_s_fast_fl等
            aspect_ratio: 视频宽高比
            start_media_id: 起始帧mediaId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartImage"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(project_id, action="VIDEO_GENERATION")
            if not recaptcha_token:
                raise Exception("Failed to obtain reCAPTCHA token")
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "prompt": prompt
                    },
                    "videoModelKey": model_key,
                    "startImage": {
                        "mediaId": start_media_id
                    },
                    # 注意: 没有endImage字段,只用首帧
                    "metadata": {
                        "sceneId": scene_id
                    }
                }]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                return result
            except Exception as e:
                error_str = str(e)
                last_error = e
                retry_reason = self._get_retry_reason(error_str)
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(f"[VIDEO I2V] 首帧生成遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})...")
                    await self._notify_browser_captcha_error(browser_id)
                    await asyncio.sleep(1)
                    continue
                else:
                    raise e
        
        # 所有重试都失败
        raise last_error

    # ========== 视频放大 (Video Upsampler) ==========

    async def upsample_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str,
        model_key: str
    ) -> dict:
        """视频放大到 4K/1080P，返回 task_id

        Args:
            at: Access Token
            project_id: 项目ID
            video_media_id: 视频的 mediaId
            aspect_ratio: 视频宽高比 VIDEO_ASPECT_RATIO_PORTRAIT/LANDSCAPE
            resolution: VIDEO_RESOLUTION_4K 或 VIDEO_RESOLUTION_1080P
            model_key: veo_3_1_upsampler_4k 或 veo_3_1_upsampler_1080p

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoUpsampleVideo"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            recaptcha_token, browser_id = await self._get_recaptcha_token(project_id, action="VIDEO_GENERATION")
            if not recaptcha_token:
                raise Exception("Failed to obtain reCAPTCHA token")
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "resolution": resolution,
                    "seed": random.randint(1, 99999),
                    "videoInput": {
                        "mediaId": video_media_id
                    },
                    "videoModelKey": model_key,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }],
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id
                }
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                return result
            except Exception as e:
                error_str = str(e)
                last_error = e
                retry_reason = self._get_retry_reason(error_str)
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(f"[VIDEO UPSAMPLE] 放大遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})...")
                    await self._notify_browser_captcha_error(browser_id)
                    await asyncio.sleep(1)
                    continue
                else:
                    raise e
        
        raise last_error

    # ========== 任务轮询 (使用AT) ==========

    async def check_video_status(self, at: str, operations: List[Dict]) -> dict:
        """查询视频生成状态

        Args:
            at: Access Token
            operations: 操作列表 [{"operation": {"name": "task_id"}, "sceneId": "...", "status": "..."}]

        Returns:
            {
                "operations": [{
                    "operation": {
                        "name": "task_id",
                        "metadata": {...}  # 完成时包含视频信息
                    },
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                }]
            }
        """
        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"

        json_data = {
            "operations": operations
        }

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )

        return result

    # ========== 媒体删除 (使用ST) ==========

    async def delete_media(self, st: str, media_names: List[str]):
        """删除媒体

        Args:
            st: Session Token
            media_names: 媒体ID列表
        """
        url = f"{self.labs_base_url}/trpc/media.deleteMedia"
        json_data = {
            "json": {
                "names": media_names
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st
        )

    # ========== 辅助方法 ==========

    def _get_retry_reason(self, error_str: str) -> Optional[str]:
        """判断是否需要重试，返回日志提示内容"""
        error_lower = error_str.lower()
        if "403" in error_lower:
            return "403错误"
        if "recaptcha evaluation failed" in error_lower:
            return "reCAPTCHA 验证失败"
        if "recaptcha" in error_lower:
            return "reCAPTCHA 错误"
        return None

    async def _notify_browser_captcha_error(self, browser_id: int = None):
        """通知有头浏览器打码切换指纹（仅当使用 browser 打码方式时）
        
        Args:
            browser_id: 要标记为 bad 的浏览器 ID
        """
        if config.captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_error(browser_id)
            except Exception:
                pass

    def _generate_session_id(self) -> str:
        """生成sessionId: ;timestamp"""
        return f";{int(time.time() * 1000)}"

    def _generate_scene_id(self) -> str:
        """生成sceneId: UUID"""
        return str(uuid.uuid4())

    async def _get_recaptcha_token(self, project_id: str, action: str = "IMAGE_GENERATION") -> tuple[Optional[str], Optional[int]]:
        """获取reCAPTCHA token - 支持多种打码方式
        
        Args:
            project_id: 项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大
        
        Returns:
            (token, browser_id) 元组，browser_id 用于失败时调用 report_error
            对于非 browser 打码方式，browser_id 为 None
        """
        captcha_method = config.captcha_method

        # 恒定浏览器打码
        if captcha_method == "personal":
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                return await service.get_token(project_id, action), None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] error: {str(e)}")
                return None, None
        # 有头浏览器打码
        elif captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                return await service.get_token(project_id, action)
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] error: {str(e)}")
                return None, None
        # API打码服务
        elif captcha_method in ["yescaptcha", "capmonster", "ezcaptcha", "capsolver"]:
            token = await self._get_api_captcha_token(captcha_method, project_id, action)
            return token, None
        else:
            debug_logger.log_info(f"[reCAPTCHA] 未知的打码方式: {captcha_method}")
            return None, None

    async def _get_api_captcha_token(self, method: str, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """通用API打码服务
        
        Args:
            method: 打码服务类型
            project_id: 项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)
        """
        # 获取配置
        if method == "yescaptcha":
            client_key = config.yescaptcha_api_key
            base_url = config.yescaptcha_base_url
            task_type = "RecaptchaV3TaskProxylessM1"
        elif method == "capmonster":
            client_key = config.capmonster_api_key
            base_url = config.capmonster_base_url
            task_type = "RecaptchaV3TaskProxyless"
        elif method == "ezcaptcha":
            client_key = config.ezcaptcha_api_key
            base_url = config.ezcaptcha_base_url
            task_type = "ReCaptchaV3TaskProxylessS9"
        elif method == "capsolver":
            client_key = config.capsolver_api_key
            base_url = config.capsolver_base_url
            task_type = "ReCaptchaV3EnterpriseTaskProxyLess"
        else:
            debug_logger.log_error(f"[reCAPTCHA] Unknown API method: {method}")
            return None

        if not client_key:
            debug_logger.log_info(f"[reCAPTCHA] {method} API key not configured, skipping")
            return None

        website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        page_action = action

        try:
            async with AsyncSession() as session:
                create_url = f"{base_url}/createTask"
                create_data = {
                    "clientKey": client_key,
                    "task": {
                        "websiteURL": website_url,
                        "websiteKey": website_key,
                        "type": task_type,
                        "pageAction": page_action
                    }
                }

                result = await session.post(create_url, json=create_data, impersonate="chrome110")
                result_json = result.json()
                task_id = result_json.get('taskId')

                debug_logger.log_info(f"[reCAPTCHA {method}] created task_id: {task_id}")

                if not task_id:
                    error_desc = result_json.get('errorDescription', 'Unknown error')
                    debug_logger.log_error(f"[reCAPTCHA {method}] Failed to create task: {error_desc}")
                    return None

                get_url = f"{base_url}/getTaskResult"
                for i in range(40):
                    get_data = {
                        "clientKey": client_key,
                        "taskId": task_id
                    }
                    result = await session.post(get_url, json=get_data, impersonate="chrome110")
                    result_json = result.json()

                    debug_logger.log_info(f"[reCAPTCHA {method}] polling #{i+1}: {result_json}")

                    status = result_json.get('status')
                    if status == 'ready':
                        solution = result_json.get('solution', {})
                        response = solution.get('gRecaptchaResponse')
                        if response:
                            debug_logger.log_info(f"[reCAPTCHA {method}] Token获取成功")
                            return response

                    time.sleep(3)

                debug_logger.log_error(f"[reCAPTCHA {method}] Timeout waiting for token")
                return None

        except Exception as e:
            debug_logger.log_error(f"[reCAPTCHA {method}] error: {str(e)}")
            return None
