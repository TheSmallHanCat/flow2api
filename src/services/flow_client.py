"""Flow API Client for VideoFX (Veo)"""

import asyncio
import json
import contextvars
import time
import uuid
import random
import base64
import gzip
import ssl
import re
from typing import Dict, Any, Optional, List, Union, Callable, Awaitable
from urllib.parse import quote, urljoin, urlparse
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from curl_cffi import CurlInfo, CurlOpt
from curl_cffi.requests import AsyncSession
from ..core.logger import debug_logger
from ..core.config import (
    config,
    get_yescaptcha_min_score,
    FLOW2API_TEAM_CAPTCHA_BASE_URL,
    FLOW2API_TEAM_CAPTCHA_CLIENT_KEY,
    FLOW2API_TEAM_CAPTCHA_METHOD,
    FLOW2API_TEAM_CAPTCHA_TASK_TYPE,
    is_yes_style_captcha_method,
)
from .flow_current import CurrentFlowClientMixin
from .flow_frontend import FlowFrontendMixin
from .flow_cookies import FlowCookieJar

try:
    import httpx
except ImportError:
    httpx = None


def _safe_updated_flow_cookies(owner, original) -> str:
    """读取 Cookie 最新快照，缺失实现时安全降级。

    历史报错：'FlowClient' object has no attribute 'get_updated_flow_cookies'
    成因是自定义 flow_frontend 覆盖了上游新增的 Cookie 作用域方法。
    """
    getter = getattr(owner, "get_updated_flow_cookies", None)
    if getter is None:
        return str(original or "").strip()
    try:
        return getter(original)
    except Exception:
        return str(original or "").strip()


class _FlowClientBase(FlowFrontendMixin):
    """VideoFX API客户端"""

    FLOW_PUBLIC_API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"
    FLOW_BROWSER_CHANNEL_HEADER = "stable"
    FLOW_BROWSER_COPYRIGHT_HEADER = "Copyright 2026 Google LLC. All Rights Reserved."
    FLOW_BROWSER_VALIDATION_HEADER = "MRCPrt/rS3JY47x2Yiz9h3ag4U8="
    FLOW_BROWSER_YEAR_HEADER = "2026"
    FLOW_FRONTEND_EXPERIMENT_IDS = (
        "106184493,106256669,105798603,106281924,106259075,106262194,"
        "105993823,106104244,105484652,1714252,105928947,106238955,"
        "106225453,106131447,1706538,119157484,106243706,105746691,"
        "106151974,106125249,106001691,106077941"
    )
    YESCAPTCHA_SLOT_BACKOFF_SECONDS = (3, 5, 8, 13, 20)

    def __init__(self, proxy_manager, db=None):
        self.proxy_manager = proxy_manager
        self.db = db  # Database instance for captcha config
        self.labs_base_url = config.flow_labs_base_url
        self.api_base_url = config.flow_api_base_url
        self.timeout = config.flow_timeout
        # 缓存每个账号的 User-Agent
        self._user_agent_cache = {}
        # 当前请求链路绑定的浏览器指纹（基于 contextvar，避免并发串扰）
        self._request_fingerprint_ctx: contextvars.ContextVar[
            Optional[Dict[str, Any]]
        ] = contextvars.ContextVar("flow_request_fingerprint", default=None)
        self._remote_browser_prefill_last_sent: Dict[str, float] = {}

        # 仅保留当前上游仍稳定出现的最小浏览器风格头；具体 UA / Accept-Language / UA-CH
        # 统一以当前请求链路绑定的 runtime fingerprint 为准，不再兼容旧版随机平台策略。
        self._default_client_headers = {
            "sec-ch-ua-mobile": "?0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        # 发车策略改为“请求到就发”：
        # 不在 flow2api 本地对提交做批次整形或排队，避免把同批请求打成阶梯。

    def _generate_user_agent(self, account_id: str = None) -> str:
        """基于账号ID生成固定的 User-Agent
        
        Args:
            account_id: 账号标识（如 email 或 token_id），相同账号返回相同 UA
            
        Returns:
            User-Agent 字符串
        """
        if not hasattr(self, "_user_agent_cache"):
            self._user_agent_cache = {}

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
        
        # fallback 仅在没有 runtime fingerprint 时兜底，直接与当前上游 Windows Chrome 风格对齐
        chrome_versions = ["136.0.0.0"]
        ch_version = rng.choice(chrome_versions)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ch_version} Safari/537.36"
        )
        
        # 缓存结果
        self._user_agent_cache[account_id] = user_agent
        
        return user_agent

    def _set_request_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        """设置当前请求链路的浏览器指纹上下文。"""
        if not hasattr(self, "_request_fingerprint_ctx"):
            self._request_fingerprint_ctx = contextvars.ContextVar(
                "flow_request_fingerprint",
                default=None,
            )
        self._request_fingerprint_ctx.set(dict(fingerprint) if fingerprint else None)

    def get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """获取当前请求链路绑定的浏览器指纹快照。"""
        if not hasattr(self, "_request_fingerprint_ctx"):
            return None
        fingerprint = self._request_fingerprint_ctx.get()
        if not isinstance(fingerprint, dict) or not fingerprint:
            return None
        return dict(fingerprint)

    def clear_request_fingerprint(self):
        """清理请求链路绑定的浏览器指纹。"""
        self._set_request_fingerprint(None)

    def _set_captcha_request_context(self, context: Optional[Dict[str, Any]]):
        if not hasattr(self, "_captcha_request_ctx"):
            self._captcha_request_ctx = contextvars.ContextVar("flow_captcha_request", default=None)
        self._captcha_request_ctx.set(dict(context) if context else None)

    def _get_captcha_request_context(self) -> Dict[str, Any]:
        context = self._captcha_request_ctx.get() if hasattr(self, "_captcha_request_ctx") else None
        return dict(context) if context else {}

    def _current_browser_ref(self, token_id: Optional[int]):
        context = self._get_captcha_request_context()
        if context.get("method") == "browser" and context.get("token_id") == token_id:
            return context.get("browser_ref")
        return None

    def _get_primary_accept_language(self, fallback: str = "zh-CN,zh;q=0.9") -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            accept_language = str(fingerprint.get("accept_language") or "").strip()
            if accept_language:
                return self._normalize_accept_language_header(
                    accept_language, fallback=fallback
                )
        return self._normalize_accept_language_header(fallback, fallback=fallback)

    def _get_primary_locale_code(self, fallback: str = "en-US") -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            language = str(fingerprint.get("language") or "").strip()
            if language:
                return language
            accept_language = str(fingerprint.get("accept_language") or "").strip()
            if accept_language:
                primary = accept_language.split(",", 1)[0].strip()
                if primary:
                    return primary
        return fallback

    def _infer_sec_ch_ua_from_user_agent(self, user_agent: Optional[str]) -> str:
        ua = str(user_agent or "").strip()
        if not ua:
            return ""
        import re

        match = re.search(r"(?:Chrome|Chromium)/(\d+)", ua, re.IGNORECASE)
        major = match.group(1) if match else "124"
        return (
            f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not)A;Brand";v="24"'
        )

    def _normalize_sec_ch_ua_header(
        self,
        sec_ch_ua: Optional[str],
        *,
        user_agent: Optional[str] = None,
    ) -> str:
        raw = str(sec_ch_ua or "").strip()
        inferred = self._infer_sec_ch_ua_from_user_agent(user_agent)
        if not raw:
            return inferred
        ua_text = str(user_agent or "").lower()
        if "chrome/" in ua_text and "google chrome" not in raw.lower():
            return inferred
        return raw or inferred

    def _build_fingerprint_from_user_agent(
        self,
        user_agent: Optional[str],
        *,
        accept_language: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        ua = str(user_agent or "").strip()
        if not ua:
            return {}
        ua_lower = ua.lower()
        platform = '"Windows"'
        mobile = "?0"
        if "android" in ua_lower:
            platform = '"Android"'
            mobile = "?1"
        elif "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
            platform = '"iOS"'
            mobile = "?1"
        elif "mac" in ua_lower:
            platform = '"macOS"'
        elif "linux" in ua_lower or "x11" in ua_lower:
            platform = '"Linux"'
        fingerprint: Dict[str, Any] = {
            "user_agent": ua,
            "sec_ch_ua": self._infer_sec_ch_ua_from_user_agent(ua),
            "sec_ch_ua_mobile": mobile,
            "sec_ch_ua_platform": platform,
        }
        normalized_accept_language = self._normalize_accept_language_header(
            accept_language
        )
        if normalized_accept_language:
            fingerprint["accept_language"] = normalized_accept_language
        if proxy_url:
            fingerprint["proxy_url"] = proxy_url
        return fingerprint

    def _normalize_accept_language_header(
        self,
        accept_language: Optional[str],
        fallback: str = "zh-CN,zh;q=0.9",
    ) -> str:
        raw = str(accept_language or "").strip()
        if not raw:
            return fallback
        if "," in raw:
            normalized_parts: list[str] = []
            for index, item in enumerate(raw.split(",")):
                candidate = str(item or "").strip()
                if not candidate:
                    continue
                language = candidate.split(";", 1)[0].strip()
                if not language:
                    continue
                if index == 0:
                    normalized_parts.append(language)
                    continue
                q_match = re.search(r";\s*q=([0-9.]+)", candidate, re.IGNORECASE)
                q_value = (
                    q_match.group(1)
                    if q_match
                    else f"{max(0.1, 1 - (index * 0.1)):.1f}"
                )
                normalized_parts.append(f"{language};q={q_value}")
            if normalized_parts:
                return ",".join(normalized_parts)
            return fallback
        if "-" in raw:
            primary = raw.split("-", 1)[0].strip()
            if len(primary) == 2 and primary.isalpha():
                return f"{raw},{primary};q=0.9"
        return raw

    def _get_effective_request_user_agent(
        self, account_id: Optional[str] = None
    ) -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            user_agent = str(fingerprint.get("user_agent") or "").strip()
            if user_agent:
                return user_agent
        return self._generate_user_agent(account_id)

    @staticmethod
    def _should_attach_runtime_session_cookies(url: str) -> bool:
        host = str(urlparse(str(url or "")).hostname or "").lower()
        if not host:
            return False
        return any(
            host == candidate or host.endswith(f".{candidate}")
            for candidate in (
                "google.com",
                "googleapis.com",
                "recaptcha.net",
            )
        )

    @staticmethod
    def _merge_cookie_header(
        existing_cookie_header: Optional[str],
        extra_cookies: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        cookie_items: Dict[str, str] = {}
        raw_existing = str(existing_cookie_header or "").strip()
        if raw_existing:
            for part in raw_existing.split(";"):
                item = str(part or "").strip()
                if not item or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                key = str(key or "").strip()
                value = str(value or "").strip()
                if key:
                    cookie_items[key] = value

        if isinstance(extra_cookies, dict):
            for key, value in extra_cookies.items():
                normalized_key = str(key or "").strip()
                normalized_value = str(value or "").strip()
                if (
                    normalized_key
                    and normalized_value
                    and normalized_key not in cookie_items
                ):
                    cookie_items[normalized_key] = normalized_value

        if not cookie_items:
            return raw_existing or None
        return "; ".join(f"{key}={value}" for key, value in cookie_items.items())

    def _build_flow_project_page_url(self, project_id: str) -> str:
        return f"https://flow.google.com/project/{project_id}"

    def _build_current_flow_media_headers(
        self,
        *,
        content_type: str = "application/json",
    ) -> Dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": self._get_primary_accept_language(
                fallback="zh-CN,zh;q=0.9"
            ),
            "Content-Type": content_type,
            "Origin": "https://flow.google.com",
            "Priority": "u=1, i",
            "Referer": "https://flow.google.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "sec-fetch-storage-access": "active",
        }
        headers.setdefault("x-browser-channel", self.FLOW_BROWSER_CHANNEL_HEADER)
        headers.setdefault("x-browser-copyright", self.FLOW_BROWSER_COPYRIGHT_HEADER)
        headers.setdefault("x-browser-validation", self.FLOW_BROWSER_VALIDATION_HEADER)
        headers.setdefault("x-browser-year", self.FLOW_BROWSER_YEAR_HEADER)
        return headers

    def _build_labs_request_context_headers(
        self, project_id: Optional[str]
    ) -> Dict[str, str]:
        return self._build_current_flow_media_headers()

    def _compact_json_dumps(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _encode_trpc_input(self, payload: Dict[str, Any]) -> str:
        return quote(self._compact_json_dumps(payload), safe="")

    @staticmethod
    def _extract_project_id_from_request_payload(
        payload: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        client_context = payload.get("clientContext")
        if isinstance(client_context, dict):
            project_id = str(client_context.get("projectId") or "").strip()
            if project_id:
                return project_id
        requests = payload.get("requests")
        if isinstance(requests, list):
            for item in requests:
                if not isinstance(item, dict):
                    continue
                item_client_context = item.get("clientContext")
                if not isinstance(item_client_context, dict):
                    continue
                project_id = str(item_client_context.get("projectId") or "").strip()
                if project_id:
                    return project_id
        return None

    async def _get_token_st_by_id(self, token_id: Optional[int]) -> Optional[str]:
        if not token_id or self.db is None or not hasattr(self.db, "get_token"):
            return None
        try:
            token = await self.db.get_token(int(token_id))
            st_value = str(getattr(token, "st", "") or "").strip() if token else ""
            return st_value or None
        except Exception as e:
            debug_logger.log_warning(
                f"[VIDEO WARMUP] 读取 Token-{token_id} 的 ST 失败: {e}"
            )
            return None

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        use_media_proxy: bool = False,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        allow_urllib_fallback: bool = True,
        apply_default_client_headers: bool = True,
        impersonate: str = "chrome",
    ) -> Dict[str, Any]:
        """统一HTTP请求处理"""
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if use_media_proxy and hasattr(
                    self.proxy_manager, "get_media_proxy_url"
                ):
                    proxy_url = await self.proxy_manager.get_media_proxy_url()
                elif hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if (
                respect_fingerprint_proxy
                and isinstance(fingerprint, dict)
                and "proxy_url" in fingerprint
            ):
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None
        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")

        effective_user_agent = str(
            fingerprint_user_agent or self._generate_user_agent(account_id)
        ).strip()
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", effective_user_agent)
        headers.setdefault(
            "Accept-Language",
            self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
        )

        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]

        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        inferred_fingerprint = self._build_fingerprint_from_user_agent(
            headers.get("User-Agent"),
            accept_language=headers.get("Accept-Language"),
            proxy_url=proxy_url,
        )
        if not headers.get("sec-ch-ua") and inferred_fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = inferred_fingerprint["sec_ch_ua"]
        if not headers.get("sec-ch-ua-platform") and inferred_fingerprint.get(
            "sec_ch_ua_platform"
        ):
            headers["sec-ch-ua-platform"] = inferred_fingerprint["sec_ch_ua_platform"]
        if not headers.get("sec-ch-ua-mobile") and inferred_fingerprint.get(
            "sec_ch_ua_mobile"
        ):
            headers["sec-ch-ua-mobile"] = inferred_fingerprint["sec_ch_ua_mobile"]

        if isinstance(
            fingerprint, dict
        ) and self._should_attach_runtime_session_cookies(url):
            origin = (
                str(fingerprint.get("origin") or "").strip()
                or "https://flow.google.com"
            )
            referer = str(fingerprint.get("referer") or "").strip()
            if not referer:
                fingerprint_project_id = str(
                    fingerprint.get("project_id") or ""
                ).strip()
                if fingerprint_project_id:
                    referer = self._build_flow_project_page_url(fingerprint_project_id)
            if origin:
                headers.setdefault("Origin", origin)
            if referer:
                headers.setdefault("Referer", referer)
            merged_cookie_header = self._merge_cookie_header(
                headers.get("Cookie"),
                fingerprint.get("session_cookies"),
            )
            if merged_cookie_header:
                headers["Cookie"] = merged_cookie_header

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(
                json_data
            )
            headers.setdefault("Origin", "https://flow.google.com")
            if derived_project_id:
                headers.setdefault(
                    "Referer", self._build_flow_project_page_url(derived_project_id)
                )

        request_body_for_log = raw_body if raw_body is not None else json_data
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] 使用打码浏览器指纹提交请求: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=request_body_for_log,
                proxy=proxy_url,
            )

        start_time = time.time()

        try:
            async with AsyncSession(trust_env=False) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate,
                    )
                else:
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                duration_ms = (time.time() - start_time) * 1000

                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms,
                    )

                if response.status_code >= 400:
                    error_reason = f"HTTP Error {response.status_code}"
                    try:
                        error_body = response.json()
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except Exception:
                        error_reason = (
                            f"HTTP Error {response.status_code}: {response.text[:200]}"
                        )

                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(
                        f"[API FAILED] Request Body: {request_body_for_log}"
                    )
                    debug_logger.log_error(f"[API FAILED] Response: {response.text}")
                    raise Exception(error_reason)

                return response.json()

        except Exception as e:
            error_msg = str(e)
            if "HTTP Error" not in error_msg and not any(
                x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]
            ):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(
                    f"[API FAILED] Request Body: {request_body_for_log}"
                )
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")

            if allow_urllib_fallback and self._should_fallback_to_urllib(error_msg):
                debug_logger.log_warning(
                    f"[HTTP FALLBACK] curl_cffi 请求失败，回退 urllib: {method.upper()} {url}"
                )
                try:
                    return await asyncio.to_thread(
                        self._sync_json_request_via_urllib,
                        method.upper(),
                        url,
                        headers,
                        json_data,
                        proxy_url,
                        request_timeout,
                    )
                except Exception as fallback_error:
                    debug_logger.log_error(
                        f"[HTTP FALLBACK] urllib 回退也失败: {fallback_error}"
                    )
                    raise Exception(
                        f"Flow API request failed: curl={error_msg}; urllib={fallback_error}"
                    )

            raise Exception(f"Flow API request failed: {error_msg}")

    async def _make_text_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        apply_default_client_headers: bool = True,
        return_error_response: bool = False,
        redact_sensitive_logs: bool = False,
        impersonate: str = "chrome",
        cookie_jar: Optional[CookieJar] = None,
    ) -> str:
        """执行原始文本请求（如 SSE），返回响应文本。"""
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if (
                respect_fingerprint_proxy
                and isinstance(fingerprint, dict)
                and "proxy_url" in fingerprint
            ):
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None

        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")

        effective_user_agent = str(
            fingerprint_user_agent or self._generate_user_agent(account_id)
        ).strip()
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", effective_user_agent)
        headers.setdefault(
            "Accept-Language",
            self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
        )

        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]
            if self._should_attach_runtime_session_cookies(url):
                origin = (
                    str(fingerprint.get("origin") or "").strip()
                    or "https://flow.google.com"
                )
                referer = str(fingerprint.get("referer") or "").strip()
                if not referer:
                    fingerprint_project_id = str(
                        fingerprint.get("project_id") or ""
                    ).strip()
                    if fingerprint_project_id:
                        referer = self._build_flow_project_page_url(
                            fingerprint_project_id
                        )
                if origin:
                    headers.setdefault("Origin", origin)
                if referer:
                    headers.setdefault("Referer", referer)
            if self._should_attach_runtime_session_cookies(url):
                merged_cookie_header = self._merge_cookie_header(
                    headers.get("Cookie"),
                    fingerprint.get("session_cookies"),
                )
                if merged_cookie_header:
                    headers["Cookie"] = merged_cookie_header

        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(
                json_data
            )
            headers.setdefault("Origin", "https://flow.google.com")
            if derived_project_id:
                headers.setdefault(
                    "Referer", self._build_flow_project_page_url(derived_project_id)
                )

        inferred_fingerprint = self._build_fingerprint_from_user_agent(
            headers.get("User-Agent"),
            accept_language=headers.get("Accept-Language"),
            proxy_url=proxy_url,
        )
        if not headers.get("sec-ch-ua") and inferred_fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = inferred_fingerprint["sec_ch_ua"]
        if not headers.get("sec-ch-ua-platform") and inferred_fingerprint.get(
            "sec_ch_ua_platform"
        ):
            headers["sec-ch-ua-platform"] = inferred_fingerprint["sec_ch_ua_platform"]
        if not headers.get("sec-ch-ua-mobile") and inferred_fingerprint.get(
            "sec_ch_ua_mobile"
        ):
            headers["sec-ch-ua-mobile"] = inferred_fingerprint["sec_ch_ua_mobile"]

        if cookie_jar is not None:
            # The scoped jar is authoritative, including cookies deleted on retry.
            headers.pop("Cookie", None)
        if isinstance(cookie_jar, FlowCookieJar):
            cookie_jar.add_runtime_cookies(
                (fingerprint or {}).get("session_cookies"), url
            )

        request_body_for_log = (
            "<redacted>"
            if redact_sensitive_logs
            else (raw_body if raw_body is not None else json_data)
        )
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] 使用打码浏览器指纹提交文本请求: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers={} if redact_sensitive_logs else headers,
                body=request_body_for_log,
                proxy=proxy_url,
            )

        start_time = time.time()

        try:
            session_kwargs: Dict[str, Any] = {"trust_env": False}
            if cookie_jar is not None:
                session_kwargs.update(
                    cookies=cookie_jar, curl_infos=[CurlInfo.COOKIELIST]
                )
            if isinstance(cookie_jar, FlowCookieJar):
                extra = cookie_jar.extra_cookie_header(url)
                session_kwargs.update(
                    curl_options={CurlOpt.COOKIE: extra}, allow_redirects=not bool(extra)
                )
            async with AsyncSession(**session_kwargs) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate,
                    )
                else:
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                if cookie_jar is not None:
                    # 兼容自定义/旧版 flow_frontend：缺少该方法时跳过快照回写
                    updater = getattr(self, "_update_flow_cookie_jar", None)
                    if updater is not None:
                        updater(cookie_jar, response)

                duration_ms = (time.time() - start_time) * 1000
                response_text = response.text

                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers={} if redact_sensitive_logs else dict(response.headers),
                        body="<redacted>" if redact_sensitive_logs else response_text,
                        duration_ms=duration_ms,
                    )

                if response.status_code >= 400 and return_error_response:
                    return response_text

                if response.status_code >= 400:
                    error_reason = (
                        f"HTTP Error {response.status_code}: {response_text[:500]}"
                    )
                    try:
                        error_body = response.json()
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except Exception:
                        pass

                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(
                        f"[API FAILED] Request Body: {request_body_for_log}"
                    )
                    debug_logger.log_error(
                        "[API FAILED] Response: <redacted>"
                        if redact_sensitive_logs
                        else f"[API FAILED] Response: {response_text}"
                    )
                    raise Exception(error_reason)

                return response_text

        except Exception as e:
            error_msg = str(e)
            if "HTTP Error" not in error_msg and not any(
                x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]
            ):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(
                    f"[API FAILED] Request Body: {request_body_for_log}"
                )
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")
            raise Exception(f"Flow API text request failed: {error_msg}")

    def _should_fallback_to_urllib(self, error_message: str) -> bool:
        """判断是否应从 curl_cffi 回退到 urllib。"""
        error_lower = (error_message or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
                "curl: (6)",
                "curl: (7)",
                "curl: (28)",
                "curl: (35)",
                "curl: (52)",
                "curl: (56)",
                "connection timed out",
                "could not connect",
                "failed to connect",
                "ssl connect error",
                "tls connect error",
                "network is unreachable",
            ]
        )

    def _sync_json_request_via_urllib(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, Any]],
        json_data: Optional[Dict[str, Any]],
        proxy_url: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """使用 urllib 执行 JSON 请求，作为 curl_cffi 的网络回退。"""
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")
        request_headers["Accept-Encoding"] = "identity"

        data = None
        if method.upper() != "GET" and json_data is not None:
            data = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )

        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            url=url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )

        try:
            with opener.open(
                request,
                timeout=timeout,
            ) as response:
                payload = response.read()
                status_code = int(response.getcode() or 0)
                content_encoding = str(
                    response.headers.get("Content-Encoding") or ""
                ).lower()
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            status_code = int(getattr(exc, "code", 500) or 500)
            content_encoding = str(
                getattr(exc, "headers", {}).get("Content-Encoding") or ""
            ).lower()
            if content_encoding == "gzip" and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass
            body_text = payload.decode("utf-8", errors="replace")
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}") from exc
        except Exception as exc:
            raise Exception(str(exc)) from exc

        if content_encoding == "gzip" and payload:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        body_text = payload.decode("utf-8", errors="replace")
        if status_code >= 400:
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}")

        try:
            return json.loads(body_text) if body_text else {}
        except Exception as exc:
            raise Exception(f"Invalid JSON response: {body_text[:200]}") from exc

    def _is_timeout_error(self, error: Exception) -> bool:
        """判断是否为网络超时，便于快速失败重试。"""
        error_lower = str(error).lower()
        return any(
            keyword in error_lower
            for keyword in [
            "timed out",
            "timeout",
            "curl: (28)",
            "connection timed out",
            "operation timed out",
            ]
        )

    def _is_proxy_connection_error(self, error: Exception) -> bool:
        """识别本地/上游代理不可用导致的连接失败。"""
        error_lower = str(error).lower()
        return any(
            keyword in error_lower
            for keyword in [
            "failed to connect to 127.0.0.1 port",
            "failed to connect to localhost port",
            "proxyerror",
            "proxy error",
            "failed to connect to proxy",
            "couldn't connect to server",
            "curl: (7)",
            ]
        )

    def _is_retryable_network_error(self, error_str: str) -> bool:
        """识别可重试的 TLS/连接类网络错误。"""
        error_lower = (error_str or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
            "curl: (35)",
            "curl: (52)",
            "curl: (56)",
            "ssl_error_syscall",
            "tls connect error",
            "ssl connect error",
            "connection reset",
            "connection aborted",
            "connection was reset",
            "connection timed out",
            "curl: (28)",
            "timed out",
            "timeout",
            "unexpected eof",
            "empty reply from server",
            "recv failure",
            "send failure",
            "connection refused",
            "network is unreachable",
            "remote host closed connection",
            ]
        )

    def _get_control_plane_timeout(self) -> int:
        """控制轻量控制面请求的超时，避免认证/项目接口长时间挂起。"""
        return max(5, min(int(self.timeout or 0) or 120, 10))

    def _get_video_submit_timeout(self) -> int:
        """视频提交接口应快速返回 operation，避免单次网络挂死拖满整条链路。"""
        return max(30, min(int(self.timeout or 0) or 120, 75))

    def _get_video_poll_timeout(self) -> int:
        """视频状态查询是轻量轮询，请求超时不应超过下一轮轮询太久。"""
        return max(10, min(int(self.timeout or 0) or 120, 45))

    def _resolve_generation_retry_budget(
        self, base_max_retries: int, error: Optional[Union[Exception, str]] = None
    ) -> int:
        """计算当前生成链路允许的总重试次数。"""
        effective_max_retries = max(1, int(base_max_retries or 1))
        if error is None:
            return effective_max_retries

        error_str = str(error)
        error_lower = error_str.lower()
        if (
            "recaptcha evaluation failed" in error_lower
            or "recaptcha 验证失败" in error_str
        ):
            return max(
                effective_max_retries,
                int(config.browser_captcha_generation_retries or 6),
            )
        return effective_max_retries

    def _build_realistic_video_submit_headers(self) -> Dict[str, str]:
        """构造当前上游真实抓包风格的视频提交头。"""
        return self._build_current_flow_media_headers(
            content_type="text/plain;charset=UTF-8"
        )

    def _resolve_runtime_impersonate(self, fallback: str = "chrome") -> str:
        resolved = self._resolve_impersonate_from_fingerprint(fallback=fallback)
        return resolved or fallback

    def _resolve_impersonate_from_fingerprint(self, fallback: str = "chrome") -> str:
        """根据当前请求链路绑定的浏览器指纹，选择最接近的 curl_cffi impersonate。"""
        fingerprint = self.get_request_fingerprint()
        if not isinstance(fingerprint, dict):
            return fallback

        ua = str(fingerprint.get("user_agent") or "").strip()
        ua_lower = ua.lower()
        if not ua_lower:
            return fallback

        if "android" in ua_lower:
            return "chrome_android"
        if "edg/" in ua_lower or " edge/" in ua_lower:
            return "edge"
        if (
            "safari/" in ua_lower
            and "chrome/" not in ua_lower
            and "chromium/" not in ua_lower
        ):
            if "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
                return "safari_ios"
            return "safari"

        if "chrome/" not in ua_lower and "chromium/" not in ua_lower:
            return fallback

        return "chrome"

    async def _make_video_api_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        timeout: int,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        action: str = "VIDEO_GENERATION",
    ) -> Dict[str, Any]:
        """视频 API 加硬截止，避免 curl_cffi 底层偶发卡住导致整条请求悬挂。"""
        raw_body = json.dumps(json_data, ensure_ascii=False, separators=(",", ":"))
        headers = self._build_realistic_video_submit_headers()
        headers.update(self._build_labs_request_context_headers(project_id))
        try:
            return await asyncio.wait_for(
                self._make_request(
                    method="POST",
                    url=url,
                    headers=headers,
                    json_data=json_data,
                    raw_body=raw_body,
                    use_at=True,
                    at_token=at,
                    timeout=timeout,
                    allow_urllib_fallback=False,
                    apply_default_client_headers=False,
                    impersonate=self._resolve_runtime_impersonate(),
                ),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError as exc:
            raise Exception(
                f"Flow video API request timed out after {timeout}s"
            ) from exc

    async def _acquire_image_launch_gate(
        self,
        token_id: Optional[int],
        token_image_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """图片请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_image_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    async def _acquire_video_launch_gate(
        self,
        token_id: Optional[int],
        token_video_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """视频请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_video_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    async def _make_image_generation_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        attempt_trace: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """图片生成请求使用更短超时，并在网络超时时快速重试。"""
        request_timeout = config.flow_image_request_timeout
        total_attempts = max(1, config.flow_image_timeout_retry_count + 1)
        retry_delay = config.flow_image_timeout_retry_delay

        # 对于浏览器/远程浏览器打码链路，优先保持与打码时一致的出口。
        # 否则在首跳改走媒体代理时，容易触发 reCAPTCHA 校验失败并放大长尾。
        fingerprint = self._request_fingerprint_ctx.get()
        has_fingerprint_context = bool(isinstance(fingerprint, dict) and fingerprint)

        has_media_proxy = False
        if self.proxy_manager and config.flow_image_timeout_use_media_proxy_fallback:
            try:
                has_media_proxy = bool(await self.proxy_manager.get_media_proxy_url())
            except Exception:
                has_media_proxy = False
        prefer_media_first = bool(
            has_media_proxy and config.flow_image_prefer_media_proxy
        )

        if has_fingerprint_context and prefer_media_first:
            prefer_media_first = False
            debug_logger.log_info(
                "[IMAGE] 检测到打码浏览器指纹上下文，首跳固定走打码链路；"
                "媒体代理仅在网络超时时作为兜底回退。"
            )

        last_error: Optional[Exception] = None

        for attempt_index in range(total_attempts):
            if has_media_proxy:
                # 两次重试时采用“主链路 + 备链路”策略，避免每次都先卡在错误链路上。
                if attempt_index == 0:
                    prefer_media_proxy = prefer_media_first
                elif attempt_index == 1:
                    prefer_media_proxy = not prefer_media_first
                else:
                    prefer_media_proxy = prefer_media_first
            else:
                prefer_media_proxy = False
            route_label = "媒体代理链路" if prefer_media_proxy else "打码链路"
            http_attempt_started_at = time.time()
            http_attempt_info: Optional[Dict[str, Any]] = None
            if isinstance(attempt_trace, dict):
                http_attempt_info = {
                    "attempt": attempt_index + 1,
                    "route": route_label,
                    "timeout_seconds": request_timeout,
                    "used_media_proxy": bool(prefer_media_proxy),
                }
            try:
                if config.captcha_method == "browser" and project_id:
                    from .browser_captcha import BrowserCaptchaService

                    service = await BrowserCaptchaService.get_instance(self.db)
                    (
                        response_payload,
                        _browser_ref,
                        fingerprint,
                    ) = await service.submit_flow_request(
                        project_id=project_id,
                        action="IMAGE_GENERATION",
                        token_id=token_id,
                        url=url,
                        at_token=at,
                        json_data=json_data,
                        timeout=request_timeout,
                        browser_ref=self._current_browser_ref(token_id),
                    )
                    self._set_request_fingerprint(fingerprint if fingerprint else None)

                    status_code = int(response_payload.get("status") or 0)
                    response_text = response_payload.get("text") or ""
                    if status_code >= 400:
                        error_reason = f"HTTP Error {status_code}"
                        parsed_body = None
                        try:
                            parsed_body = (
                                json.loads(response_text) if response_text else None
                            )
                        except Exception:
                            parsed_body = None
                        if isinstance(parsed_body, dict) and "error" in parsed_body:
                            error_info = parsed_body["error"] or {}
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details or []:
                                if isinstance(detail, dict) and detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                        elif response_text:
                            error_reason = (
                                f"HTTP Error {status_code}: {response_text[:200]}"
                            )
                        error = RuntimeError(error_reason)
                        error.browser_ref = _browser_ref
                        raise error

                    result = json.loads(response_text) if response_text else {}
                else:
                    result = await self._make_request(
                        method="POST",
                        url=url,
                        headers=self._build_labs_request_context_headers(project_id),
                        json_data=json_data,
                        use_at=True,
                        at_token=at,
                        timeout=request_timeout,
                        use_media_proxy=prefer_media_proxy,
                        respect_fingerprint_proxy=not prefer_media_proxy,
                    )
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int(
                        (time.time() - http_attempt_started_at) * 1000
                    )
                    http_attempt_info["success"] = True
                    attempt_trace.setdefault("http_attempts", []).append(
                        http_attempt_info
                    )
                return result
            except Exception as e:
                last_error = e
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int(
                        (time.time() - http_attempt_started_at) * 1000
                    )
                    http_attempt_info["success"] = False
                    http_attempt_info["timeout_error"] = bool(self._is_timeout_error(e))
                    http_attempt_info["error"] = str(e)[:240]
                    attempt_trace.setdefault("http_attempts", []).append(
                        http_attempt_info
                    )
                if getattr(e, "submission_uncertain", False) or not self._is_timeout_error(e) or attempt_index >= total_attempts - 1:
                    raise

                if has_media_proxy and total_attempts > 1:
                    next_prefer_media_proxy = (
                        not prefer_media_proxy
                        if attempt_index == 0
                        else prefer_media_proxy
                    )
                else:
                    next_prefer_media_proxy = prefer_media_proxy
                next_route_label = (
                    "媒体代理链路" if next_prefer_media_proxy else "打码链路"
                )
                debug_logger.log_warning(
                    f"[IMAGE] 图片生成请求网络超时，准备快速重试 "
                    f"({attempt_index + 2}/{total_attempts})，当前链路={route_label}，"
                    f"下一链路={next_route_label}，timeout={request_timeout}s"
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("图片生成请求失败")

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
        try:
            return await self._make_request(
                method="GET",
                url=url,
                use_st=True,
                st_token=st,
                timeout=self._get_control_plane_timeout(),
            )
        except Exception as e:
            if not self._is_proxy_connection_error(e):
                raise

            debug_logger.log_warning(
                f"[AUTH] ST->AT failed via configured proxy, retrying direct connection: {e}"
            )
            return await self._make_request(
                method="GET",
                url=url,
                use_st=True,
                st_token=st,
                timeout=self._get_control_plane_timeout(),
                force_no_proxy=True,
            )

    # ========== 项目管理 (使用ST) ==========

    async def create_project(
        self,
        st: str,
        title: str,
        google_cookies: Optional[str] = None,
    ) -> str:
        """创建项目,返回project_id

        Args:
            st: Session Token
            title: 项目标题

        Returns:
            project_id (UUID)
        """
        cookie_storage = str(google_cookies or "").strip()
        if cookie_storage:
            try:
                payload = await self._call_flow_frontend_rpc(
                    rpc_id="jHPbke",
                    argument=["projects/*", [None, [title]], [None, 22]],
                    cookie_header="",
                    timeout=max(
                        self._get_control_plane_timeout(), min(self.timeout, 20)
                    ),
                    cookie_storage=cookie_storage,
                )
                project_id = self._extract_flow_project_id(payload)
                if not project_id:
                    raise RuntimeError(
                        "Flow frontend createProject response missing projectId"
                    )
                return project_id
            except Exception as frontend_error:
                debug_logger.log_warning(
                    "[PROJECT] Flow frontend createProject failed; "
                    f"falling back to the legacy endpoint: {frontend_error}"
                )

        # Legacy Session Token accounts remain usable until their Google cookies are synced.
        url = f"{self.labs_base_url}/trpc/project.createProject"
        json_data = {"json": {"projectTitle": title, "toolName": "PINHOLE"}}
        max_retries = config.flow_max_retries
        request_timeout = max(self._get_control_plane_timeout(), min(self.timeout, 15))
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_st=True,
                    st_token=st,
                    timeout=request_timeout,
                )
                project_result = (
                    result.get("result", {})
                    .get("data", {})
                    .get("json", {})
                    .get("result", {})
                )
                project_id = project_result.get("projectId")
                if not project_id:
                    raise Exception(
                        "Invalid project.createProject response: missing projectId"
                    )
                return project_id
            except Exception as e:
                last_error = e
                retry_reason = (
                    "网络超时"
                    if self._is_timeout_error(e)
                    else self._get_retry_reason(str(e))
                )
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[PROJECT] 创建项目失败，准备重试 ({retry_attempt + 2}/{max_retries}) "
                        f"title={title!r}, reason={retry_reason}: {e}"
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("创建项目失败")

    async def delete_project(self, st: str, project_id: str):
        """删除项目

        Args:
            st: Session Token
            project_id: 项目ID
        """
        url = f"{self.labs_base_url}/trpc/project.deleteProject"
        json_data = {"json": {"projectToDeleteId": project_id}}

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
        )

    # ========== 媒体获取 (使用AT) ==========

    async def get_media(self, at: str, media_name: str) -> dict:
        """获取媒体内容 (视频返回base64编码数据)

        Google 的 batchCheckAsyncVideoGenerationStatus 接口在视频生成成功后
        不返回下载 URL。需要通过 GET /v1/media/{name} 获取视频内容。

        Args:
            at: Access Token
            media_name: 媒体名称 (UUID格式)

        Returns:
            {
                "name": "uuid",
                "video": {
                    "encodedVideo": "base64...",
                    "seed": 74602,
                    "prompt": "...",
                    "model": "veo_3_1_t2v_fast",
                    "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE"
                }
            }
        """
        url = f"{self.api_base_url}/media/{media_name}"
        return await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            timeout=max(60, int(self.timeout or 120)),
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
            at_token=at,
            timeout=self._get_control_plane_timeout(),
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
        if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            return "image/webp"
        # PNG: 89 50 4E 47
        if image_bytes[:4] == b"\x89PNG":
            return "image/png"
        # JPEG: FF D8 FF
        if image_bytes[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        # GIF: GIF87a 或 GIF89a
        if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        # BMP: BM
        if image_bytes[:2] == b"BM":
            return "image/bmp"
        # JPEG 2000: 00 00 00 0C 6A 50
        if image_bytes[:6] == b"\x00\x00\x00\x0cjP":
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
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        
        output = BytesIO()
        img.save(output, format="JPEG", quality=95)
        return output.getvalue()

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id: Optional[str] = None,
    ) -> str:
        """上传图片,返回mediaId

        Args:
            at: Access Token
            image_bytes: 图片字节数据
            aspect_ratio: 图片或视频宽高比（会自动转换为图片格式）
            project_id: 项目ID（新上传接口可使用）

        Returns:
            mediaId
        """
        # 转换视频aspect_ratio为图片aspect_ratio
        # VIDEO_ASPECT_RATIO_LANDSCAPE -> IMAGE_ASPECT_RATIO_LANDSCAPE
        # VIDEO_ASPECT_RATIO_PORTRAIT -> IMAGE_ASPECT_RATIO_PORTRAIT
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        # 自动检测图片 MIME 类型
        mime_type = self._detect_image_mime_type(image_bytes)

        # 编码为base64 (去掉前缀)
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        # 优先尝试新版上传接口: /v1/flow/uploadImage
        # 若失败则自动回退到旧接口,保证兼容
        ext = "png" if "png" in mime_type else "jpg"
        upload_file_name = f"flow2api_upload_{int(time.time() * 1000)}.{ext}"
        new_url = f"{self.api_base_url}/flow/uploadImage"
        normalized_project_id = str(project_id or "").strip()
        new_client_context = {
            "sessionId": self._generate_session_id(),
            "tool": "PINHOLE",
        }
        if normalized_project_id:
            new_client_context["projectId"] = normalized_project_id

        new_json_data = {
            "clientContext": new_client_context,
            "fileName": upload_file_name,
            "imageBytes": image_base64,
            "isHidden": False,
            "isUserUploaded": True,
            "mimeType": mime_type,
        }

        # 兼容回退：旧接口 :uploadUserImage
        legacy_url = f"{self.api_base_url}:uploadUserImage"
        legacy_json_data = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": mime_type,
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio,
            },
            "clientContext": {
                "sessionId": self._generate_session_id(),
                "tool": "ASSET_MANAGER",
            },
        }
        max_retries = config.flow_max_retries
        last_error: Optional[Exception] = None

        captcha_method = getattr(config, "captcha_method", "personal")
        if captcha_method == "personal":
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                fingerprint = service.get_last_fingerprint()
                if not fingerprint:
                    await service.get_token(project_id, "uploadUserImage")
                    fingerprint = service.get_last_fingerprint()
                self._set_request_fingerprint(fingerprint)
            except Exception as e:
                debug_logger.log_error(f"[UPLOAD] Failed to pre-fetch fingerprint: {e}")

        for retry_attempt in range(max_retries):
            try:
                new_result = await self._make_request(
                    method="POST",
                    url=new_url,
                    json_data=new_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True,
                )
                media_id = self._extract_media_name(
                    new_result.get("media")
                ) or new_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                if media_id:
                    return media_id
                raise Exception(
                    f"Invalid upload response: missing media id, keys={list(new_result.keys())}"
                )
            except Exception as new_upload_error:
                last_error = new_upload_error
                retry_reason = (
                    "网络超时"
                    if self._is_timeout_error(new_upload_error)
                    else self._get_retry_reason(str(new_upload_error))
                )

                # 旧接口不携带 projectId，带项目上下文的上传一旦回退就可能把图片挂到错误项目。
                if normalized_project_id:
                    if retry_reason and retry_attempt < max_retries - 1:
                        debug_logger.log_warning(
                            f"[UPLOAD] Project-scoped upload 遇到{retry_reason}，准备重试新版接口 "
                            f"({retry_attempt + 2}/{max_retries}, project_id={normalized_project_id})..."
                        )
                        await asyncio.sleep(1)
                        continue
                    raise RuntimeError(
                        "Project-scoped image upload failed via /flow/uploadImage; "
                        "legacy :uploadUserImage fallback is disabled because it may attach media "
                        f"to a different project (project_id={normalized_project_id})."
                    ) from new_upload_error

                debug_logger.log_warning(
                    f"[UPLOAD] New upload API failed, fallback to legacy endpoint: {new_upload_error}"
                )

            try:
                legacy_result = await self._make_request(
                    method="POST",
                    url=legacy_url,
                    json_data=legacy_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True,
                )

                media_id = legacy_result.get("mediaGenerationId", {}).get(
                    "mediaGenerationId"
                ) or legacy_result.get("media", {}).get("name")
                if media_id:
                    return media_id
                raise Exception(
                    f"Legacy upload response missing media id: keys={list(legacy_result.keys())}"
                )
            except Exception as legacy_upload_error:
                last_error = legacy_upload_error
                retry_reason = self._get_retry_reason(str(legacy_upload_error))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[UPLOAD] 上传遇到{retry_reason}，准备重试 ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("上传图片失败")

    # ========== 图片生成 (使用AT) - 同步返回 ==========

    async def generate_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None,
        token_id: Optional[int] = None,
        token_image_concurrency: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int], Awaitable[None]]] = None,
        google_cookies: Optional[str] = None,
    ) -> tuple[dict, str, Dict[str, Any]]:
        """生成图片(同步返回)

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_name: NARWHAL / GEM_PIX / GEM_PIX_2 / IMAGEN_3_5
            aspect_ratio: 图片宽高比
            image_inputs: 参考图片列表(图生图时使用)

        Returns:
            (result, session_id, perf_trace)
            result: 上游返回的生成结果
            session_id: 本次成功图片生成请求使用的 sessionId
            perf_trace: 生成重试与链路耗时轨迹
        """
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"
        frontend_cookie_storage = ""
        if not image_inputs and (google_cookies or token_id is not None):
            try:
                frontend_cookie_storage = (
                    await self._resolve_flow_frontend_cookie_storage(
                        google_cookies=google_cookies,
                        token_id=token_id,
                    )
                )
            except RuntimeError:
                frontend_cookie_storage = ""

        # The frontend attempt is extra; legacy keeps its configured retry budget.
        max_retries = config.flow_max_retries + (1 if frontend_cookie_storage else 0)
        last_error = None
        perf_trace: Dict[str, Any] = {
            "max_retries": max_retries,
            "generation_attempts": [],
        }
        
        for retry_attempt in range(max_retries):
            attempt_trace: Dict[str, Any] = {
                "attempt": retry_attempt + 1,
                "recaptcha_ok": False,
            }
            attempt_started_at = time.time()
            # 每次重试都重新获取 reCAPTCHA token
            recaptcha_started_at = time.time()
            if progress_callback is not None:
                await progress_callback("solving_image_captcha", 38)
            launch_gate_acquired = False
            (
                launch_ok,
                launch_queue_ms,
                launch_stagger_ms,
            ) = await self._acquire_image_launch_gate(
                token_id=token_id,
                token_image_concurrency=token_image_concurrency,
            )
            attempt_trace["launch_queue_ms"] = launch_queue_ms
            attempt_trace["launch_stagger_ms"] = launch_stagger_ms
            if not launch_ok:
                last_error = Exception("Image launch queue wait timeout")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int(
                    (time.time() - attempt_started_at) * 1000
                )
                perf_trace["generation_attempts"].append(attempt_trace)
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="IMAGE_GENERATION",
                    token_id=token_id,
                    website_url=(
                        self._build_flow_frontend_project_page_url(project_id)
                        if frontend_cookie_storage
                        else None
                    ),
                )
            finally:
                if launch_gate_acquired:
                    await self._release_image_launch_gate(token_id)
            attempt_trace["recaptcha_ms"] = int(
                (time.time() - recaptcha_started_at) * 1000
            )
            attempt_trace["recaptcha_ok"] = bool(recaptcha_token)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int(
                    (time.time() - attempt_started_at) * 1000
                )
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            if progress_callback is not None:
                await progress_callback("submitting_image", 48)
            session_id = self._generate_session_id()

            # 构建请求 - 新版接口在外层和 requests 内都带 clientContext
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
            }

            # 新版图片接口使用结构化提示词 + new media 开关
            request_data = {
                "clientContext": client_context,
                "seed": random.randint(1, 999999),
                "imageModelName": model_name,
                "imageAspectRatio": aspect_ratio,
                "structuredPrompt": {"parts": [{"text": prompt}]},
                "imageInputs": image_inputs or [],
            }

            json_data = {
                "clientContext": client_context,
                "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
                "useNewMedia": True,
                "requests": [request_data],
            }

            try:
                if frontend_cookie_storage:
                    frontend_payload = await self._call_flow_frontend_rpc(
                        rpc_id="ogiZ0b",
                        argument=self._build_frontend_image_generation_argument(
                            project_id=project_id,
                            prompt=prompt,
                            model_name=model_name,
                            aspect_ratio=aspect_ratio,
                            recaptcha_token=recaptcha_token,
                            session_id=str(uuid.uuid4()).upper(),
                        ),
                        cookie_header="",
                        timeout=max(config.flow_image_request_timeout, 90),
                        project_id=project_id,
                        cookie_storage=frontend_cookie_storage,
                    )
                    result = self._normalize_frontend_image_generation_response(
                        frontend_payload
                    )
                else:
                    result = await self._make_image_generation_request(
                        url=url,
                        json_data=json_data,
                        at=at,
                        attempt_trace=attempt_trace,
                        project_id=project_id,
                        token_id=token_id,
                    )
                attempt_trace["success"] = True
                attempt_trace["duration_ms"] = int(
                    (time.time() - attempt_started_at) * 1000
                )
                perf_trace["generation_attempts"].append(attempt_trace)
                perf_trace["final_success_attempt"] = retry_attempt + 1
                return result, session_id, perf_trace
            except Exception as e:
                last_error = e
                attempt_trace["success"] = False
                attempt_trace["error"] = str(e)[:240]
                attempt_trace["duration_ms"] = int(
                    (time.time() - attempt_started_at) * 1000
                )
                perf_trace["generation_attempts"].append(attempt_trace)
                if frontend_cookie_storage:
                    debug_logger.log_warning(
                        "[IMAGE] Flow frontend RPC failed; falling back to the legacy "
                        f"generation endpoint: {e}"
                    )
                    frontend_cookie_storage = ""
                    continue
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        perf_trace["final_success_attempt"] = None
        raise last_error

    async def upsample_image(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
        user_paygate_tier: str = "PAYGATE_TIER_NOT_PAID",
        session_id: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> str:
        """放大图片到 2K/4K

        Args:
            at: Access Token
            project_id: 项目ID
            media_id: 图片的 mediaId (从 batchGenerateImages 返回的 media[0]["name"])
            target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K 或 UPSAMPLE_IMAGE_RESOLUTION_4K
            user_paygate_tier: 用户等级 (如 PAYGATE_TIER_NOT_PAID / PAYGATE_TIER_ONE)
            session_id: 可选，复用图片生成请求的 sessionId

        Returns:
            base64 编码的图片数据
        """
        url = f"{self.api_base_url}/flow/upsampleImage"

        # 403/reCAPTCHA/500 重试逻辑 - 使用配置的最大重试次数
        max_retries = config.flow_max_retries
        last_error = None

        for retry_attempt in range(max_retries):
            # 获取 reCAPTCHA token - 使用 IMAGE_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(
                project_id, action="IMAGE_GENERATION", token_id=token_id
            )
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise last_error
            upsample_session_id = session_id or self._generate_session_id()

            json_data = {
                "mediaId": media_id,
                "targetResolution": target_resolution,
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": upsample_session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
            }

            # 4K/2K 放大使用专用超时，因为返回的 base64 数据量很大
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=config.upsample_timeout,
                )

                # 返回 base64 编码的图片
                return result.get("encodedImage", "")
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        raise last_error

    # ========== 视频生成 (使用AT) - 异步返回 ==========

    def _extract_media_name(self, media: Any) -> Optional[str]:
        """从新版 media 对象或数组中提取 media id。"""
        if isinstance(media, list):
            for item in media:
                media_name = self._extract_media_name(item)
                if media_name:
                    return media_name
            return None
        if isinstance(media, dict):
            name = media.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _build_video_text_input(
        self, prompt: str, use_v2_model_config: bool = False
    ) -> Dict[str, Any]:
        # 当前 Flow 上游视频链路统一使用 structuredPrompt，不再兼容旧 prompt 字段。
        return {"structuredPrompt": {"parts": [{"text": prompt}]}}

    def _build_video_media_generation_context(
        self, batch_id: Optional[str] = None
    ) -> Dict[str, Any]:
        return {
            "batchId": batch_id or str(uuid.uuid4()),
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        }

    def _find_nested_string(self, value: Any, keys: tuple[str, ...]) -> Optional[str]:
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for candidate in value.values():
                found = self._find_nested_string(candidate, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_nested_string(item, keys)
                if found:
                    return found
        return None

    def _truncate_large_debug_value(self, data: Any, max_length: int = 240) -> Any:
        """截断调试输出中的超长字段，避免污染控制台。"""
        if isinstance(data, dict):
            return {
                key: self._truncate_large_debug_value(value, max_length=max_length)
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [
                self._truncate_large_debug_value(item, max_length=max_length)
                for item in data
            ]
        if isinstance(data, str) and len(data) > max_length:
            return f"{data[:max_length]}... (truncated, total {len(data)} chars)"
        return data

    def _extract_video_status_from_media(
        self, media: Dict[str, Any]
    ) -> tuple[Optional[str], Dict[str, Any]]:
        status_block = (
            media.get("mediaMetadata", {}).get("mediaStatus", {})
            or media.get("mediaStatus", {})
            or {}
        )
        status = (
            status_block.get("mediaGenerationStatus")
            or status_block.get("status")
            or media.get("status")
        )
        return status, status_block if isinstance(status_block, dict) else {}

    def _extract_video_url_from_media(self, media: Dict[str, Any]) -> Optional[str]:
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        candidates = [
            self._find_nested_string(
                video, ("fifeUrl", "videoUrl", "outputUri", "downloadUri")
            ),
            self._find_nested_string(
                media, ("fifeUrl", "videoUrl", "outputUri", "downloadUri")
            ),
            self._find_nested_string(video, ("uri", "url")),
        ]
        for candidate in candidates:
            if candidate and (
                candidate.startswith("http://")
                or candidate.startswith("https://")
                or candidate.startswith("/")
            ):
                return candidate
        return None

    def _extract_video_metadata_from_media(
        self,
        media: Dict[str, Any],
    ) -> Dict[str, Any]:
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        generated_video = (
            video.get("generatedVideo")
            if isinstance(video.get("generatedVideo"), dict)
            else {}
        )
        request_data = (
            media.get("mediaMetadata", {})
            .get("requestData", {})
            .get("videoGenerationRequestData", {})
            if isinstance(media.get("mediaMetadata"), dict)
            else {}
        )
        control_input = (
            request_data.get("videoModelControlInput", {})
            if isinstance(request_data, dict)
            else {}
        )
        dimensions = (
            video.get("dimensions") if isinstance(video.get("dimensions"), dict) else {}
        )

        media_name = self._extract_media_name(media)
        aspect_ratio = (
            self._find_nested_string(generated_video, ("aspectRatio",))
            or self._find_nested_string(video, ("aspectRatio", "videoAspectRatio"))
            or self._find_nested_string(
                control_input, ("videoAspectRatio", "aspectRatio")
        )
            or self._find_nested_string(
                media.get("mediaMetadata", {}), ("videoAspectRatio", "aspectRatio")
        )
        )
        model_name = self._find_nested_string(
            generated_video, ("model",)
        ) or self._find_nested_string(
            control_input, ("videoModelName", "videoModelKey")
        )
        duration = self._find_nested_string(
            dimensions, ("length", "duration")
        ) or self._find_nested_string(generated_video, ("length", "duration"))

        video_metadata: Dict[str, Any] = {}
        if media_name:
            video_metadata["mediaName"] = media_name
            video_metadata["mediaGenerationId"] = media_name
        if aspect_ratio:
            video_metadata["aspectRatio"] = aspect_ratio
        if model_name:
            video_metadata["model"] = model_name
        if duration:
            video_metadata["duration"] = duration

        embedded_url = self._extract_video_url_from_media(media)
        if embedded_url:
            video_metadata["embeddedUrl"] = embedded_url
            video_metadata["fifeUrl"] = embedded_url

        return video_metadata

    def _media_to_video_operation(
        self,
        media: Dict[str, Any],
        fallback_project_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(media, dict):
            return None

        media_name = self._extract_media_name(media)
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        video_operation = (
            video.get("operation") if isinstance(video.get("operation"), dict) else {}
        )
        operation_name = (
            video_operation.get("name")
            or self._find_nested_string(video_operation, ("name",))
            or media_name
        )
        if not operation_name:
            return None

        project_id = media.get("projectId") or fallback_project_id
        status, status_block = self._extract_video_status_from_media(media)
        operation: Dict[str, Any] = {
            "operation": {
                "name": operation_name,
            },
            "status": status or "MEDIA_GENERATION_STATUS_PENDING",
        }
        if media_name:
            operation["mediaName"] = media_name
        if project_id:
            operation["projectId"] = project_id

        scene_id = (
            media.get("sceneId")
            or media.get("workflowStepId")
            or video_operation.get("sceneId")
        )
        if scene_id:
            operation["sceneId"] = scene_id

        video_metadata = self._extract_video_metadata_from_media(media)
        if video_metadata:
            operation["operation"]["metadata"] = {"video": video_metadata}

        error = status_block.get("error") if isinstance(status_block, dict) else None
        if isinstance(error, dict):
            operation["operation"]["error"] = error

        return operation

    def _merge_video_operations_with_media(
        self,
        operations: List[Dict[str, Any]],
        media_operations: List[Dict[str, Any]],
        fallback_project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        media_by_name: Dict[str, Dict[str, Any]] = {}
        for item in media_operations:
            media_name = item.get("mediaName") or (item.get("operation") or {}).get(
                "name"
            )
            if media_name:
                media_by_name[media_name] = item

        merged: List[Dict[str, Any]] = []
        for raw_operation in operations:
            operation = dict(raw_operation) if isinstance(raw_operation, dict) else {}
            operation_body = dict(operation.get("operation") or {})
            operation["operation"] = operation_body
            name = operation_body.get("name") or operation.get("mediaName")
            media_operation = media_by_name.get(name) if name else None
            if media_operation:
                operation.setdefault("mediaName", media_operation.get("mediaName"))
                operation.setdefault("projectId", media_operation.get("projectId"))
                operation.setdefault("status", media_operation.get("status"))
                operation.setdefault("sceneId", media_operation.get("sceneId"))
                if "metadata" not in operation_body and (
                    media_operation.get("operation") or {}
                ).get("metadata"):
                    operation_body["metadata"] = (
                        media_operation.get("operation") or {}
                    ).get("metadata")
                if "error" not in operation_body and (
                    media_operation.get("operation") or {}
                ).get("error"):
                    operation_body["error"] = (
                        media_operation.get("operation") or {}
                    ).get("error")
            elif fallback_project_id:
                operation.setdefault("projectId", fallback_project_id)
            merged.append(operation)

        return merged

    def _normalize_video_generation_response(
        self,
        result: Dict[str, Any],
        fallback_project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return result

        normalized = dict(result)
        media_items = normalized.get("media")
        media_operations: List[Dict[str, Any]] = []
        if isinstance(media_items, list):
            for media in media_items:
                operation = self._media_to_video_operation(
                    media, fallback_project_id=fallback_project_id
                )
                if operation:
                    media_operations.append(operation)

        operations = normalized.get("operations")
        if isinstance(operations, list) and operations:
            normalized["operations"] = self._merge_video_operations_with_media(
                operations,
                media_operations,
                fallback_project_id=fallback_project_id,
            )
        elif media_operations:
            normalized["operations"] = media_operations

        return normalized

    def _build_video_media_generation_context(self, batch_id: str) -> Dict[str, Any]:
        return {
            "batchId": batch_id,
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        }

    def _operations_to_media_refs(
        self,
        operations: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        media_refs: List[Dict[str, str]] = []
        for operation in operations or []:
            if not isinstance(operation, dict):
                continue
            operation_body = operation.get("operation") or {}
            media_name = (
                operation.get("mediaName")
                or operation.get("name")
                or operation_body.get("name")
            )
            project_id = (
                operation.get("projectId")
                or operation.get("project_id")
                or operation_body.get("projectId")
            )
            if (
                isinstance(media_name, str)
                and media_name.strip()
                and isinstance(project_id, str)
                and project_id.strip()
            ):
                media_refs.append(
                    {
                    "name": media_name.strip(),
                    "projectId": project_id.strip(),
                    }
                )
        return media_refs

    async def generate_video_text(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
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
        frontend_cookie_storage = ""
        if google_cookies or token_id is not None:
            try:
                frontend_cookie_storage = (
                    await self._resolve_flow_frontend_cookie_storage(
                        google_cookies=google_cookies,
                        token_id=token_id,
                    )
                )
            except RuntimeError:
                frontend_cookie_storage = ""

        # 403/reCAPTCHA 重试逻辑 - reCAPTCHA evaluation failed 时允许更高重试上限
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        if frontend_cookie_storage:
            max_retries += 1
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    website_url=(
                        self._build_flow_frontend_project_page_url(project_id)
                        if frontend_cookie_storage
                        else None
                    ),
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] 生成",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not frontend_cookie_storage and not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier,
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(
                    prompt, use_v2_model_config=True
                ),
                "videoModelKey": model_key,
                "metadata": {},
            }
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                if frontend_cookie_storage:
                    frontend_payload = await self._call_flow_frontend_rpc(
                        rpc_id="YhhmEf",
                        argument=self._build_frontend_video_generation_argument(
                            project_id=project_id,
                            prompt=prompt,
                            model_key=model_key,
                            recaptcha_token=recaptcha_token,
                            session_id=str(uuid.uuid4()).upper(),
                        ),
                        cookie_header="",
                        timeout=max(self._get_video_submit_timeout(), 90),
                        project_id=project_id,
                        cookie_storage=frontend_cookie_storage,
                    )
                    return self._normalize_frontend_video_submission(
                        frontend_payload,
                        project_id=project_id,
                        aspect_ratio=aspect_ratio,
                        model_key=model_key,
                    )
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result,
                    fallback_project_id=project_id,
                )
            except Exception as e:
                last_error = e
                if frontend_cookie_storage:
                    debug_logger.log_warning(
                        "[VIDEO T2V] Flow frontend RPC failed; falling back to the "
                        f"legacy generation endpoint: {e}"
                    )
                    frontend_cookie_storage = ""
                    retry_attempt += 1
                    continue
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] 生成",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # 所有重试都失败
        raise last_error

    def _build_browser_style_control_headers(
        self,
        referer: str,
        origin: Optional[str] = None,
        account_id: Optional[str] = None,
        content_type: Optional[str] = None,
        accept_language: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": referer,
            "User-Agent": self._get_effective_request_user_agent(account_id),
            "Accept-Language": accept_language
            or self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
            "Priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        if origin:
            headers["Origin"] = origin
            if origin == "https://flow.google.com":
                headers.setdefault("sec-fetch-storage-access", "active")
        if content_type:
            headers["Content-Type"] = content_type
        if api_key:
            headers["x-goog-api-key"] = api_key
        headers.setdefault("x-browser-channel", self.FLOW_BROWSER_CHANNEL_HEADER)
        headers.setdefault("x-browser-copyright", self.FLOW_BROWSER_COPYRIGHT_HEADER)
        headers.setdefault("x-browser-validation", self.FLOW_BROWSER_VALIDATION_HEADER)
        headers.setdefault("x-browser-year", self.FLOW_BROWSER_YEAR_HEADER)
        return headers

    async def _labs_trpc_get_with_st(
        self,
        path_with_query: str,
        st: str,
        project_id: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        page_url = self._build_flow_project_page_url(project_id)
        return await self._make_request(
            method="GET",
            url=f"{self.labs_base_url}/trpc/{path_with_query}",
            headers=self._build_browser_style_control_headers(
                referer=page_url,
                account_id=st[:16],
                content_type="application/json",
            ),
            use_st=True,
            st_token=st,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _labs_trpc_post_with_st(
        self,
        trpc_path: str,
        payload: Dict[str, Any],
        st: str,
        project_id: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        page_url = self._build_flow_project_page_url(project_id)
        return await self._make_request(
            method="POST",
            url=f"{self.labs_base_url}/trpc/{trpc_path}",
            headers=self._build_browser_style_control_headers(
                referer=page_url,
                origin="https://flow.google.com",
                account_id=st[:16],
                content_type="application/json",
            ),
            json_data=payload,
            use_st=True,
            st_token=st,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _aisandbox_request(
        self,
        method: str,
        path: str,
        at: Optional[str],
        *,
        json_data: Optional[Dict[str, Any]] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        content_type: Optional[str] = "text/plain;charset=UTF-8",
        accept_language: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = self._build_browser_style_control_headers(
            referer="https://flow.google.com/",
            origin="https://flow.google.com",
            account_id=account_id,
            content_type=content_type,
            accept_language=accept_language,
            api_key=api_key,
        )
        return await self._make_request(
            method=method,
            url=f"{self.api_base_url}{path}",
            headers=headers,
            json_data=json_data,
            raw_body=raw_body,
            use_at=bool(at),
            at_token=at,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _warmup_flow_video_frontend_context(
        self,
        *,
        at: str,
        project_id: str,
        token_id: Optional[int],
        session_id: str,
        user_paygate_tier: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
    ) -> None:
        """按当前上游真实页面顺序补齐视频提交前的最小首方初始化/telemetry链路。"""
        account_id = at[:16] if at else None
        page_url = self._build_flow_project_page_url(project_id)
        session_create_time = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        st = await self._get_token_st_by_id(token_id)
        if st:
            null_meta_input = self._encode_trpc_input(
                {
                "json": None,
                "meta": {"values": ["undefined"]},
                }
            )
            labs_get_paths = [
                f"general.fetchUserPreferences?input={null_meta_input}",
                f"videoFx.getFlowAppConfig?input={null_meta_input}",
                f"videoFx.getUserSettings?input={null_meta_input}",
            ]
            for path_with_query in labs_get_paths:
                try:
                    await self._labs_trpc_get_with_st(
                        path_with_query, st=st, project_id=project_id
                    )
                except Exception as e:
                    debug_logger.log_warning(
                        f"[VIDEO WARMUP] Labs GET 失败 ({path_with_query}): {e}"
                    )

            labs_batch_log_payload = {
                "json": {
                    "appEvents": [
                        {
                        "event": "PAGE_VIEW",
                        "eventProperties": [
                            {"key": "URL", "stringValue": page_url},
                                {
                                    "key": "USER_AGENT",
                                    "stringValue": self._get_effective_request_user_agent(
                                        st[:16]
                                    ),
                                },
                            {"key": "IS_DESKTOP"},
                        ],
                        "activeExperiments": [],
                        "eventMetadata": {"sessionId": session_id},
                        "eventTime": session_create_time,
                        }
                    ]
                }
            }
            try:
                await self._labs_trpc_post_with_st(
                    "general.submitBatchLog",
                    payload=labs_batch_log_payload,
                    st=st,
                    project_id=project_id,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[VIDEO WARMUP] Labs submitBatchLog 失败: {e}"
                )

        try:
            await self._aisandbox_request(
                "POST",
                ":checkAppAvailability",
                at=None,
                raw_body=self._compact_json_dumps(
                    {"clientContext": {"tool": "PINHOLE"}}
                ),
                api_key=self.FLOW_PUBLIC_API_KEY,
                account_id=account_id,
            )
        except Exception as e:
            debug_logger.log_warning(f"[VIDEO WARMUP] checkAppAvailability 失败: {e}")

        debug_logger.log_info(
            f"[VIDEO WARMUP] 当前视频最小初始化链路已补齐: project_id={project_id}, "
            f"session_id={session_id}, model_key={model_key}, aspect_ratio={aspect_ratio}, "
            f"user_paygate_tier={user_paygate_tier}, prompt_len={len(prompt or '')}"
        )

    def _video_aspect_ratio_to_agent_aspect_ratio(self, aspect_ratio: str) -> str:
        mapping = {
            "VIDEO_ASPECT_RATIO_LANDSCAPE": "16:9",
            "VIDEO_ASPECT_RATIO_PORTRAIT": "9:16",
            "VIDEO_ASPECT_RATIO_SQUARE": "1:1",
        }
        return mapping.get(str(aspect_ratio or "").strip(), "16:9")

    def _parse_sse_json_events(self, raw_text: str) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if not raw_text:
            return events

        for block in raw_text.split("\n\n"):
            block = block.strip()
            if not block:
                continue

            data_lines = [
                line[5:].strip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue

            payload_text = "\n".join(data_lines).strip()
            if not payload_text or payload_text == "[DONE]":
                continue

            try:
                payload = json.loads(payload_text)
            except Exception:
                continue

            if isinstance(payload, dict):
                events.append(payload)

        return events

    def _extract_agent_session_id(
        self, sessions_payload: Dict[str, Any]
    ) -> Optional[str]:
        if not isinstance(sessions_payload, dict):
            return None

        sessions = sessions_payload.get("sessions")
        if isinstance(sessions, list):
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                session_id = str(session.get("agentSessionId") or "").strip()
                if session_id:
                    return session_id

        session_info = sessions_payload.get("sessionInfo")
        if isinstance(session_info, dict):
            session_id = str(session_info.get("agentSessionId") or "").strip()
            if session_id:
                return session_id

        return None

    def _extract_flow_entity_id(self, entity_payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(entity_payload, dict):
            return None

        candidates = [entity_payload]
        result = entity_payload.get("result")
        if isinstance(result, dict):
            candidates.append(result)
            data = result.get("data")
            if isinstance(data, dict):
                candidates.append(data)
                json_node = data.get("json")
                if isinstance(json_node, dict):
                    candidates.append(json_node)
                    nested_result = json_node.get("result")
                    if isinstance(nested_result, dict):
                        candidates.append(nested_result)

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for key in ("entityId", "id", "parentEntityId"):
                entity_id = str(candidate.get(key) or "").strip()
                if entity_id:
                    return entity_id

        return None

    def _extract_turn_count(self, session_detail: Dict[str, Any]) -> int:
        if not isinstance(session_detail, dict):
            return 0
        turns = session_detail.get("turns")
        if isinstance(turns, list):
            return len(turns)
        return 0

    def _extract_generate_video_with_references_result(
        self,
        events: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for event in events:
            if not isinstance(event, dict):
                continue
            agent_message = event.get("agentMessage")
            if not isinstance(agent_message, dict):
                continue
            agent_events = agent_message.get("agentEvents")
            if not isinstance(agent_events, list):
                continue
            for agent_event in agent_events:
                if not isinstance(agent_event, dict):
                    continue
                tool_result_wrapper = agent_event.get("toolResult")
                if not isinstance(tool_result_wrapper, dict):
                    continue
                if (
                    tool_result_wrapper.get("toolName")
                    != "generate_video_with_references"
                ):
                    continue
                tool_result = tool_result_wrapper.get("toolResult")
                if isinstance(tool_result, dict):
                    return tool_result
        return None

    async def get_flow_creation_agent_session(
        self,
        at: str,
        project_id: str,
        *,
        account_id: Optional[str] = None,
        allow_global_fallback: bool = True,
    ) -> Optional[str]:
        sessions_result = await self._aisandbox_request(
            "GET",
            f"/flowCreationAgent/sessions?projectId={quote(project_id, safe='')}",
            at=at,
            content_type=None,
            account_id=account_id,
        )
        session_id = self._extract_agent_session_id(sessions_result)
        if session_id or not allow_global_fallback:
            return session_id

        global_sessions_result = await self._aisandbox_request(
            "GET",
            "/flowCreationAgent/sessions",
            at=at,
            content_type=None,
            account_id=account_id,
        )
        return self._extract_agent_session_id(global_sessions_result)

    async def get_flow_creation_agent_session_detail(
        self,
        at: str,
        agent_session_id: str,
        *,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._aisandbox_request(
            "GET",
            f"/flowCreationAgent/sessions/{quote(agent_session_id, safe='')}",
            at=at,
            content_type=None,
            account_id=account_id,
        )

    async def create_flow_entity(
        self,
        st: str,
        project_id: str,
    ) -> str:
        payload = {"json": {"projectId": project_id}}
        result = await self._labs_trpc_post_with_st(
            "flow.createEntity",
            payload=payload,
            st=st,
            project_id=project_id,
        )
        entity_id = self._extract_flow_entity_id(result)
        if not entity_id:
            raise RuntimeError(
                f"flow.createEntity 响应缺少 entityId: keys={list(result.keys())}"
            )
        return entity_id

    async def copy_project_media_to_character_slot(
        self,
        at: str,
        *,
        project_id: str,
        media_id: str,
        entity_id: str,
        image_reference_index: int,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "mediaId": media_id,
            "destinationProjectId": project_id,
            "destinationMediaContext": {
                "entityContext": {
                    "entityId": entity_id,
                    "characterSlot": {
                        "imageReferenceIndex": int(image_reference_index),
                    },
                }
            },
        }
        return await self._aisandbox_request(
            "POST",
            "/flow:copyProjectMedia",
            at=at,
            raw_body=self._compact_json_dumps(payload),
            account_id=account_id,
        )

    async def stream_flow_creation_agent(
        self,
        at: str,
        payload: Dict[str, Any],
        *,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        action: str = "VIDEO_GENERATION",
        account_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        url = f"{self.api_base_url}/flowCreationAgent:streamChat?alt=sse"
        headers = self._build_browser_style_control_headers(
            referer="https://flow.google.com/",
            origin="https://flow.google.com",
            account_id=account_id,
            content_type="application/json",
            accept_language=self._get_primary_accept_language(),
        )
        headers["Accept"] = "text/event-stream, text/event-stream"

        if config.captcha_method == "browser" and project_id:
            from .browser_captcha import BrowserCaptchaService

            service = await BrowserCaptchaService.get_instance(self.db)
            (
                response_payload,
                _browser_ref,
                fingerprint,
            ) = await service.submit_flow_request(
                project_id=project_id,
                action=action,
                token_id=token_id,
                url=url,
                at_token=at,
                json_data=payload,
                timeout=self._get_video_submit_timeout(),
                browser_ref=self._current_browser_ref(token_id),
            )
            self._set_request_fingerprint(fingerprint if fingerprint else None)

            status_code = int(response_payload.get("status") or 0)
            raw_text = response_payload.get("text") or ""
            if status_code >= 400:
                error_reason = f"HTTP Error {status_code}"
                parsed_body = None
                try:
                    parsed_body = json.loads(raw_text) if raw_text else None
                except Exception:
                    parsed_body = None
                if isinstance(parsed_body, dict) and "error" in parsed_body:
                    error_info = parsed_body["error"] or {}
                    error_message = error_info.get("message", "")
                    details = error_info.get("details", [])
                    for detail in details or []:
                        if isinstance(detail, dict) and detail.get("reason"):
                            error_reason = detail.get("reason")
                            break
                    if error_message:
                        error_reason = f"{error_reason}: {error_message}"
                elif raw_text:
                    error_reason = f"HTTP Error {status_code}: {raw_text[:200]}"
                error = RuntimeError(error_reason)
                error.browser_ref = _browser_ref
                raise error
        else:
            raw_text = await self._make_text_request(
                method="POST",
                url=url,
                headers=headers,
                json_data=payload,
                use_at=True,
                at_token=at,
                timeout=self._get_video_submit_timeout(),
                apply_default_client_headers=False,
                impersonate=self._resolve_runtime_impersonate(),
            )
        return self._parse_sse_json_events(raw_text)

    async def generate_omni_reference_video(
        self,
        at: str,
        st: str,
        project_id: str,
        prompt: str,
        aspect_ratio: str,
        reference_media_ids: List[str],
        model_usage_key: str = "abra_r2v_10s",
        model_display_name: str = "Omni Flash",
        duration: int = 10,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not st:
            raise RuntimeError("Omni 参考图视频需要 ST token，但当前账号未提供 ST")
        if not reference_media_ids:
            raise RuntimeError("Omni 参考图视频至少需要 1 张参考图")

        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        entity_handle = "entity-0"
        video_context_warmed = False
        account_id = at[:16] if at else None
        entity_id: Optional[str] = None
        agent_aspect_ratio = self._video_aspect_ratio_to_agent_aspect_ratio(
            aspect_ratio
        )
        agent_prompt = (
            f"{prompt}\n\nUse a {agent_aspect_ratio} aspect ratio."
            if prompt
            else f"Use a {agent_aspect_ratio} aspect ratio."
        )

        while retry_attempt < max_retries:
            launch_gate_acquired = False
            approve_browser_id = None
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            browser_id = None
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)

            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO OMNI-R2V] 生成",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error

            try:
                if not entity_id:
                    entity_id = await self.create_flow_entity(
                        st=st, project_id=project_id
                    )
                    for index, media_id in enumerate(reference_media_ids):
                        await self.copy_project_media_to_character_slot(
                            at=at,
                            project_id=project_id,
                            media_id=media_id,
                            entity_id=entity_id,
                            image_reference_index=index,
                            account_id=account_id,
                        )
                    debug_logger.log_info(
                        f"[VIDEO OMNI-R2V] 已创建角色实体并挂载参考图: project_id={project_id}, entity_id={entity_id}, refs={len(reference_media_ids)}"
                    )

                if not video_context_warmed:
                    await self._warmup_flow_video_frontend_context(
                        at=at,
                        project_id=project_id,
                        token_id=token_id,
                        session_id=session_id,
                        user_paygate_tier=user_paygate_tier,
                        prompt=agent_prompt,
                        model_key=model_usage_key,
                        aspect_ratio=aspect_ratio,
                    )
                    video_context_warmed = True

                agent_session_id = await self.get_flow_creation_agent_session(
                    at=at,
                    project_id=project_id,
                    account_id=account_id,
                    allow_global_fallback=True,
                )
                if not agent_session_id:
                    raise RuntimeError(
                        f"未找到 Flow Creation Agent Session: project_id={project_id}"
                    )

                session_detail = await self.get_flow_creation_agent_session_detail(
                    at=at,
                    agent_session_id=agent_session_id,
                    account_id=account_id,
                )
                current_turn_count = self._extract_turn_count(session_detail)
                next_turn_number = current_turn_count + 1

                prompt_payload = {
                    "agentSessionId": agent_session_id,
                    "agentClientContext": {
                        "projectId": f"projects/{project_id}",
                        "clientSessionId": session_id,
                        "recaptchaContext": {
                            "token": recaptcha_token,
                            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                        },
                        "turnNumber": next_turn_number,
                    },
                    "userMessage": {
                        "userPrompt": {
                            "parts": [{"text": agent_prompt}],
                        },
                        "entityReferences": [
                            {
                                "entityId": entity_id,
                                "handle": entity_handle,
                            }
                        ],
                    },
                }

                prompt_events = await self.stream_flow_creation_agent(
                    at=at,
                    payload=prompt_payload,
                    project_id=project_id,
                    token_id=token_id,
                    account_id=account_id,
                )
                tool_result = self._extract_generate_video_with_references_result(
                    prompt_events
                )

                if not tool_result:
                    (
                        approve_recaptcha_token,
                        approve_browser_id,
                    ) = await self._get_recaptcha_token(
                        project_id,
                        action="VIDEO_GENERATION",
                        token_id=token_id,
                    )
                    if not approve_recaptcha_token:
                        raise RuntimeError(
                            "Omni 参考图视频审批阶段获取 reCAPTCHA token 失败"
                        )
                    approve_payload = {
                        "agentSessionId": agent_session_id,
                        "agentClientContext": {
                            "projectId": f"projects/{project_id}",
                            "clientSessionId": session_id,
                            "recaptchaContext": {
                                "token": approve_recaptcha_token,
                                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                            },
                            "turnNumber": next_turn_number + 1,
                        },
                        "userMessage": {
                            "userPrompt": {
                                "parts": [{"text": "Approve"}],
                            }
                        },
                    }
                    approve_events = await self.stream_flow_creation_agent(
                        at=at,
                        payload=approve_payload,
                        project_id=project_id,
                        token_id=token_id,
                        account_id=account_id,
                    )
                    tool_result = self._extract_generate_video_with_references_result(
                        approve_events
                    )
                    await self._notify_browser_captcha_request_finished(
                        approve_browser_id
                    )
                    approve_browser_id = None

                if not tool_result:
                    raise RuntimeError(
                        "Omni 参考图视频链路未返回 generate_video_with_references toolResult"
                    )

                media_id = str(tool_result.get("media_id") or "").strip()
                resolved_project_id = (
                    str(tool_result.get("project_id") or project_id).strip()
                    or project_id
                )
                if not media_id:
                    raise RuntimeError(
                        f"Omni 参考图视频 toolResult 缺少 media_id: {tool_result}"
                    )

                operation = {
                    "operation": {"name": media_id},
                    "name": media_id,
                    "mediaName": media_id,
                    "projectId": resolved_project_id,
                    "workflowId": tool_result.get("workflow_id"),
                    "batchId": tool_result.get("batch_id"),
                    "status": "MEDIA_GENERATION_STATUS_ACTIVE",
                }
                return {
                    "operations": [operation],
                    "agentToolResult": tool_result,
                    "projectId": resolved_project_id,
                    "entityId": entity_id,
                    "entityHandle": entity_handle,
                    "modelUsageKey": model_usage_key,
                    "aspectRatio": tool_result.get("aspect_ratio") or aspect_ratio,
                    "duration": duration,
                    "modelDisplayName": model_display_name,
                }
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO OMNI-R2V] 生成",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
                if approve_browser_id:
                    await self._notify_browser_captcha_request_finished(
                        approve_browser_id
                    )

            retry_attempt += 1

        raise last_error

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """图生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_r2v_fast_landscape
            aspect_ratio: 视频宽高比
            reference_images: 参考图片列表 [{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "..."}]
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoReferenceImages"

        # 403/reCAPTCHA 重试逻辑 - reCAPTCHA evaluation failed 时允许更高重试上限
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] 生成",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            request_seed = random.randint(1, 99999)
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
                "requests": [
                    {
                    "aspectRatio": aspect_ratio,
                    "seed": request_seed,
                        "textInput": self._build_video_text_input(
                            prompt, use_v2_model_config=True
                        ),
                    "videoModelKey": model_key,
                    "referenceImages": reference_images,
                        "metadata": {},
                    }
                ],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] 生成",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
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
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
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

        # 403/reCAPTCHA 重试逻辑 - reCAPTCHA evaluation failed 时允许更高重试上限
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首尾帧生成",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier,
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(
                    prompt, use_v2_model_config=True
                ),
                "videoModelKey": model_key,
                "startImage": {"mediaId": start_media_id},
                "endImage": {"mediaId": end_media_id},
                "metadata": {},
            }
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首尾帧生成",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
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
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
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

        # 403/reCAPTCHA 重试逻辑 - reCAPTCHA evaluation failed 时允许更高重试上限
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首帧生成",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier,
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(
                    prompt, use_v2_model_config=True
                ),
                "videoModelKey": model_key,
                "startImage": {"mediaId": start_media_id},
                # 注意: 没有endImage字段,只用首帧
                "metadata": {},
            }
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首帧生成",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # 所有重试都失败
        raise last_error

    # ========== 视频续写 (Video Extend) ==========

    async def generate_video_extend(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        video_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """视频续写,基于已生成的视频延伸7秒

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 续写提示词
            model_key: veo_3_1_extend_portrait / veo_3_1_extend 等
            aspect_ratio: 视频宽高比
            video_media_id: 源视频的 mediaGenerationId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text (operations 列表)
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoExtendVideo"

        # 403/reCAPTCHA 重试逻辑 - reCAPTCHA evaluation failed 时允许更高重试上限
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        workflow_id = str(uuid.uuid4())
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] 续写",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True

            request_seed = random.randint(1, 99999)
            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "requests": [
                    {
                    "aspectRatio": aspect_ratio,
                    "seed": request_seed,
                    "textInput": {
                            "structuredPrompt": {"parts": [{"text": prompt}]}
                    },
                        "videoInput": {"mediaId": video_media_id},
                    "videoModelKey": model_key,
                        "metadata": {"workflowId": workflow_id},
                    }
                ],
                "useV2ModelConfig": True,
            }

            # Debug: 打印请求体用于调试
            import json as _json

            debug_logger.log_info(f"[VIDEO EXTEND] Request URL: {url}")
            debug_logger.log_info(
                f"[VIDEO EXTEND] Request JSON: {_json.dumps(json_data, indent=2, ensure_ascii=False)[:2000]}"
            )

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] 续写",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1

        # 所有重试都失败
        raise last_error

    # ========== 视频拼接 (Video Concatenation) ==========

    async def run_concatenation(
        self,
        at: str,
        original_media_id: str,
        extend_media_id: str,
    ) -> dict:
        """
        调用 Google runVideoFxConcatenation API 拼接视频
        
        Args:
            at: 认证 token
            original_media_id: 原始视频的 mediaGenerationId (UUID)
            extend_media_id: 续写视频的 mediaGenerationId (UUID)
        
        Returns:
            包含 operation name 的字典
        """
        url = f"{self.api_base_url}:runVideoFxConcatenation"
        
        json_data = {
            "inputVideos": [
                {
                    "mediaGenerationId": original_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "0s",
                    "endTimeOffset": "8s",
                },
                {
                    "mediaGenerationId": extend_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "1s",
                    "endTimeOffset": "8s",
                },
            ]
        }
        
        debug_logger.log_info(
            f"[CONCAT] 提交拼接任务: original={original_media_id[:12]}..., extend={extend_media_id[:12]}..."
        )
        
        result = await self._make_request(
            method="POST", url=url, json_data=json_data, use_at=True, at_token=at
        )
        debug_logger.log_info(
            f"[CONCAT] 拼接任务已提交: {json.dumps(result, ensure_ascii=False)[:300]}"
        )
        return result

    async def poll_concatenation_status(
        self,
        at: str,
        operation_name: str,
        timeout: int = 300,
        poll_interval: int = 3,
    ) -> dict:
        """
        轮询拼接任务状态，直到完成或超时
        
        Args:
            at: 认证 token
            operation_name: 拼接任务的 operation name
            timeout: 超时秒数
            poll_interval: 轮询间隔秒数
        
        Returns:
            包含 outputUri 和 mediaGenerationId 的字典
        """
        url = f"{self.api_base_url}:runVideoFxCheckConcatenationStatus"
        json_data = {"operation": {"operation": {"name": operation_name}}}
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            result = await self._make_request(
                method="POST",
                url=url,
                json_data=json_data,
                use_at=True,
                at_token=at,
                timeout=300,  # concat API returns base64 video (~14MB), needs longer timeout
            )
            
            status = result.get("status", "")
            output_uri = result.get("outputUri", "")
            encoded_video = result.get("encodedVideo", "")
            
            ev_len = len(encoded_video) if encoded_video else 0
            elapsed = int(time.time() - start_time)
            all_keys = list(result.keys())
            debug_logger.log_info(
                f"[CONCAT] 状态: {status}, outputUri={'yes' if output_uri else 'no'}, "
                f"encodedVideo={ev_len} chars, elapsed={elapsed}s, keys={all_keys}"
            )
            
            # 优先检查 outputUri
            if output_uri:
                debug_logger.log_info(
                    f"[CONCAT] 拼接完成 (outputUri): {output_uri[:120]}"
                )
                return result
            
            # Google API 返回 encodedVideo（base64 编码的 MP4）而不是 outputUri
            if encoded_video and "SUCCESSFUL" in status:
                try:
                    import os

                    video_bytes = base64.b64decode(encoded_video)
                    video_filename = f"concat_{uuid.uuid4().hex[:12]}.mp4"
                    
                    # 保存到 tmp/ 目录（FastAPI 已挂载为 /tmp 静态文件）
                    save_dir = "tmp"
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, video_filename)
                    
                    with open(save_path, "wb") as f:
                        f.write(video_bytes)
                    
                    # 构造 URL：FastAPI 挂载了 /tmp -> /app/tmp/
                    serve_url = f"/tmp/{video_filename}"
                    debug_logger.log_info(
                        f"[CONCAT] 拼接完成 (encodedVideo): 保存 {len(video_bytes)} bytes -> {serve_url}"
                    )
                    
                    result["outputUri"] = serve_url
                    result["local_file"] = save_path
                    return result
                except Exception as e:
                    debug_logger.log_error(f"[CONCAT] 解码 encodedVideo 失败: {e}")
                    raise Exception(f"解码拼接视频失败: {e}")
            
            # SUCCESSFUL but neither outputUri nor encodedVideo
            if "SUCCESSFUL" in status:
                debug_logger.log_warning(
                    f"[CONCAT] SUCCESSFUL 但无 outputUri/encodedVideo: {json.dumps(result, ensure_ascii=False)[:300]}"
                )

            if "FAILED" in status or "ERROR" in status:
                debug_logger.log_error(
                    f"[CONCAT] 失败: {status}, 响应: {json.dumps(result, ensure_ascii=False)[:300]}"
                )
                raise Exception(f"视频拼接失败: {status}")
            
            await asyncio.sleep(poll_interval)
        
        debug_logger.log_error(f"[CONCAT] 超时 ({timeout}s)，放弃拼接")
        raise Exception(f"视频拼接超时 ({timeout}s)")

    # ========== 视频放大 (Video Upsampler) ==========

    async def upsample_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str,
        model_key: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
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

        # 403/reCAPTCHA 重试逻辑 - reCAPTCHA evaluation failed 时允许更高重试上限
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        scene_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] 放大",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt="",
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True

            request_seed = random.randint(1, 99999)
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "requests": [
                    {
                    "aspectRatio": aspect_ratio,
                    "resolution": resolution,
                    "seed": request_seed,
                        "videoInput": {"mediaId": video_media_id},
                    "videoModelKey": model_key,
                        "metadata": {"sceneId": scene_id},
                    }
                ],
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] 放大",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        raise last_error

    # ========== 任务轮询 (使用AT) ==========

    async def check_video_status(
        self,
        at: str,
        operations: List[Dict],
        token_id: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        """查询视频生成状态

        Args:
            at: Access Token
            operations: 内部操作列表；当前上游状态查询仅使用其中抽取出的 media 引用

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
        use_frontend_rpc = any(
            isinstance(operation, dict)
            and operation.get("frontendRpc") in {"YhhmEf", "jwpduf"}
            for operation in operations or []
        )
        if use_frontend_rpc:
            try:
                operation_ids: List[str] = []
                for operation in operations or []:
                    operation_id = str(
                        (operation or {}).get("name")
                        or ((operation or {}).get("operation") or {}).get("name")
                        or ""
                    ).strip()
                    if operation_id and operation_id not in operation_ids:
                        operation_ids.append(operation_id)
                if not operation_ids:
                    raise ValueError("视频状态查询缺少 Flow frontend operation ID")
                cookie_storage = await self._resolve_flow_frontend_cookie_storage(
                    google_cookies=google_cookies,
                    token_id=token_id,
                )
                first_operation = operations[0] if operations else {}
                payload = await self._call_flow_frontend_rpc(
                    rpc_id="jwpduf",
                    argument=[
                        None,
                        None,
                        [[operation_id] for operation_id in operation_ids],
                    ],
                    cookie_header="",
                    timeout=max(self._get_video_poll_timeout(), 30),
                    project_id=(first_operation or {}).get("projectId"),
                    cookie_storage=cookie_storage,
                )
                return self._normalize_frontend_video_status(payload, operations)
            except Exception as frontend_error:
                debug_logger.log_warning(
                    "[VIDEO POLL] Flow frontend RPC failed; falling back to the "
                    f"legacy status endpoint: {frontend_error}"
                )

        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"

        media_refs = self._operations_to_media_refs(operations)
        if not media_refs:
            raise ValueError("视频状态查询缺少 media 引用，无法按当前上游结构发起查询")

        json_data = {"media": media_refs}
        max_retries = config.flow_max_retries
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_poll_timeout(),
                )
                try:
                    media_preview = (
                        result.get("media") if isinstance(result, dict) else None
                    )
                    operations_preview = (
                        result.get("operations") if isinstance(result, dict) else None
                    )
                    preview_payload = {
                        "top_keys": list(result.keys())
                        if isinstance(result, dict)
                        else [],
                        "media_count": len(media_preview)
                        if isinstance(media_preview, list)
                        else 0,
                        "operations_count": len(operations_preview)
                        if isinstance(operations_preview, list)
                        else 0,
                        "first_media": media_preview[0]
                        if isinstance(media_preview, list) and media_preview
                        else None,
                        "first_operation": operations_preview[0]
                        if isinstance(operations_preview, list) and operations_preview
                        else None,
                    }
                    print(
                        "[VIDEO POLL RAW] "
                        + json.dumps(
                            self._truncate_large_debug_value(preview_payload),
                            ensure_ascii=False,
                        )[:4000]
                    )
                except Exception:
                    pass
                return self._normalize_video_generation_response(result)
            except Exception as e:
                last_error = e
                retry_reason = self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[VIDEO POLL] 状态查询遇到{retry_reason}，准备重试 ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("视频状态查询失败")

    # ========== 媒体删除 (使用ST) ==========

    async def delete_media(self, st: str, media_names: List[str]):
        """删除媒体

        Args:
            st: Session Token
            media_names: 媒体ID列表
        """
        url = f"{self.labs_base_url}/trpc/media.deleteMedia"
        json_data = {"json": {"names": media_names}}

        await self._make_request(
            method="POST", url=url, json_data=json_data, use_st=True, st_token=st
        )

    async def get_media_url_redirect(
        self,
        st: str,
        media_name: str,
        media_url_type: str = "MEDIA_URL_TYPE_FULL_MEDIA",
    ) -> Optional[str]:
        """通过 trpc media.getMediaUrlRedirect 获取媒体实际访问 URL。"""
        normalized_media_name = str(media_name or "").strip()
        if not normalized_media_name:
            return None

        url = (
            f"{self.labs_base_url}/trpc/media.getMediaUrlRedirect"
            f"?name={quote(normalized_media_name, safe='')}"
            f"&mediaUrlType={quote(str(media_url_type or 'MEDIA_URL_TYPE_FULL_MEDIA'), safe='')}"
        )

        proxy_url = None
        if self.proxy_manager:
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                proxy_url = await self.proxy_manager.get_request_proxy_url()
            else:
                proxy_url = await self.proxy_manager.get_proxy_url()

        headers = {
            "Cookie": f"__Secure-next-auth.session-token={st}",
            "User-Agent": self._generate_user_agent(st[:16] if st else None),
            "Accept": "*/*",
        }
        for key, value in self._default_client_headers.items():
            headers.setdefault(key, value)

        request_timeout = self._get_control_plane_timeout()
        start_time = time.time()
        try:
            async with AsyncSession(trust_env=False) as session:
                response = await session.get(
                    url,
                    headers=headers,
                    proxy=proxy_url,
                    timeout=request_timeout,
                    impersonate="chrome",
                    allow_redirects=False,
                )

            duration_ms = (time.time() - start_time) * 1000
            if config.debug_enabled:
                debug_logger.log_response(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=response.text,
                    duration_ms=duration_ms,
                )

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location") or response.headers.get(
                    "location"
                )
                if location:
                    return urljoin(url, location)

            final_url = str(getattr(response, "url", "") or "").strip()
            if response.status_code == 200 and final_url and final_url != url:
                return final_url

            raise RuntimeError(
                f"media.getMediaUrlRedirect returned unexpected status={response.status_code} "
                f"for media={normalized_media_name}"
            )
        except Exception as e:
            raise RuntimeError(
                f"获取媒体重定向地址失败: media={normalized_media_name}, type={media_url_type}, error={e}"
            ) from e

    # ========== 辅助方法 ==========

    async def _handle_retryable_generation_error(
        self,
        error: Exception,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
        defer_browser_error_notification: bool = False,
    ) -> bool:
        """统一处理生成链路的重试判定与打码自愈通知。"""
        error_str = str(error)
        retry_reason = self._get_retry_reason(error_str)
        retry_delay = self._get_retry_delay_seconds(error_str, retry_attempt)

        effective_max_retries = self._resolve_generation_retry_budget(
            max_retries, error_str
        )
        is_terminal_attempt = retry_attempt >= effective_max_retries - 1
        should_notify_browser = (
            not defer_browser_error_notification
            or not retry_reason
            or is_terminal_attempt
            or (config.captcha_method == "browser" and "recaptcha" in error_str.lower())
        )

        if should_notify_browser:
            notify_reason = retry_reason or error_str[:120] or type(error).__name__
            await self._notify_browser_captcha_error(
                browser_id=getattr(error, "browser_ref", None) or browser_id,
                project_id=project_id,
                error_reason=notify_reason,
                error_message=error_str,
            )

        if not retry_reason or getattr(error, "submission_uncertain", False):
            return False

        if is_terminal_attempt:
            debug_logger.log_warning(
                f"{log_prefix}遇到{retry_reason}，已达到最大重试次数({effective_max_retries})，本次请求失败并执行关闭回收。"
            )
            return False

        debug_logger.log_warning(
            f"{log_prefix}遇到{retry_reason}，将在 {retry_delay} 秒后重新获取验证码重试 ({retry_attempt + 2}/{effective_max_retries})..."
        )
        await asyncio.sleep(retry_delay)
        return True

    async def _handle_missing_recaptcha_token(
        self,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        token_error = Exception("Failed to obtain reCAPTCHA token")
        return await self._handle_retryable_generation_error(
            error=token_error,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            browser_id=browser_id,
            project_id=project_id,
            log_prefix=log_prefix,
        )

    async def _build_team_captcha_cookie_map(
        self, token_id: Optional[int]
    ) -> Dict[str, str]:
        """团队打码下发的 Cookie 键值对（按 flow.google.com 请求口径选取）。"""
        try:
            cookie_storage = await self._resolve_flow_frontend_cookie_storage(
                token_id=token_id
            )
            cookie_storage = _safe_updated_flow_cookies(self, cookie_storage)
        except Exception as e:
            debug_logger.log_warning(
                f"[reCAPTCHA {FLOW2API_TEAM_CAPTCHA_METHOD}] 读取账号 Cookie 失败: {e}"
            )
            return {}
        if not str(cookie_storage or "").strip():
            return {}
        from .browser_cookie_utils import cookie_pairs_for_url

        cookie_map: Dict[str, str] = {}
        for name, value in cookie_pairs_for_url(cookie_storage):
            if name:
                cookie_map[name] = value
        return cookie_map

    def _resolve_third_party_captcha_method(self) -> Optional[str]:
        """返回第一个已配置密钥的第三方打码方式（yescaptcha 优先）。

        SPrCad 等 batchexecute RPC 只接受第三方打码 token；
        browser/personal harvest 出的 token 会被 UNUSUAL_ACTIVITY 拒绝。
        """
        if config.captcha_method == FLOW2API_TEAM_CAPTCHA_METHOD:
            # 内置团队打码始终可用（地址/密钥写死），优先作为第三方 token 来源。
            return FLOW2API_TEAM_CAPTCHA_METHOD
        for method, attr in (
            ("yescaptcha", "yescaptcha_api_key"),
            ("capmonster", "capmonster_api_key"),
            ("ezcaptcha", "ezcaptcha_api_key"),
            ("capsolver", "capsolver_api_key"),
            ("captcharun", "captcharun_api_key"),
            ("twocaptcha", "twocaptcha_api_key"),
        ):
            if str(getattr(config, attr, "") or "").strip():
                return method
        return None

    @staticmethod
    def _extract_browser_captcha_session_id(
        browser_ref: Optional[Union[int, str]],
    ) -> Optional[str]:
        """从 browser 请求句柄提取本次 harvest 对应的 Flow session id。"""
        if not isinstance(browser_ref, str):
            return None
        raw = browser_ref.strip()
        if raw.startswith("personal:"):
            return raw.partition("personal:")[2] or None
        browser_id, separator, request_ref = raw.partition(":")
        if not separator or not browser_id.isdigit() or not request_ref:
            return None
        if request_ref.startswith("lease-"):
            return request_ref.partition("/")[2] or None
        return request_ref

    def _get_retry_reason(self, error_str: str) -> Optional[str]:
        """判断是否需要重试，返回日志提示内容"""
        error_lower = error_str.lower()
        if "browser captcha queue wait timeout" in error_lower:
            return None
        if "error_no_slot_available_block" in error_lower:
            return "打码服务资源阻塞"
        if "error_no_slot_available" in error_lower:
            return "打码服务资源不足"
        if "model_access_denied" in error_lower:
            # 账号无对应模型权限，重试不会改变结果
            return None
        if "403" in error_lower:
            return "403错误"
        if "429" in error_lower or "too many requests" in error_lower:
            return "429限流"
        if self._is_retryable_network_error(error_str):
            return "网络/TLS错误"
        if "recaptcha evaluation failed" in error_lower:
            return "reCAPTCHA 验证失败"
        if "recaptcha" in error_lower:
            return "reCAPTCHA 错误"
        if any(
            keyword in error_lower
            for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
                '"reason":"internal"',
            "server error",
            "upstream error",
            ]
        ):
            return "500/内部错误"
        return None

    def _get_retry_delay_seconds(self, error_str: str, retry_attempt: int) -> int:
        error_lower = str(error_str or "").lower()
        if "error_no_slot_available_block" in error_lower:
            return 20
        if "error_no_slot_available" in error_lower:
            index = max(
                0, min(retry_attempt, len(self.YESCAPTCHA_SLOT_BACKOFF_SECONDS) - 1)
            )
            return self.YESCAPTCHA_SLOT_BACKOFF_SECONDS[index]
        if "recaptcha evaluation failed" in error_lower:
            return 2
        if "recaptcha" in error_lower:
            return 2
        return 1

    def _resolve_recaptcha_runtime_settings(
        self,
        method: str,
        action: str,
    ) -> Dict[str, Any]:
        normalized_action = (
            str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        )
        website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

        if method == "flow2api_team":
            page_action = normalized_action
            task_type = FLOW2API_TEAM_CAPTCHA_TASK_TYPE
            min_score = get_yescaptcha_min_score(task_type)
        elif method == "yescaptcha":
            page_action = normalized_action
            task_type = config.yescaptcha_task_type
            min_score = get_yescaptcha_min_score(task_type)
        elif method == "capmonster":
            page_action = normalized_action
            task_type = "RecaptchaV3TaskProxyless"
            min_score = None
        elif method == "ezcaptcha":
            page_action = normalized_action
            task_type = "ReCaptchaV3TaskProxylessS9"
            min_score = None
        elif method == "capsolver":
            page_action = normalized_action
            task_type = "ReCaptchaV3EnterpriseTaskProxyLess"
            min_score = None
        elif method == "captcharun":
            page_action = normalized_action
            task_type = "ReCaptchaV3"
            min_score = None
        elif method == "twocaptcha":
            page_action = normalized_action
            task_type = "RecaptchaV3TaskProxyless"
            min_score = 0.9
        else:
            page_action = normalized_action
            task_type = None
            min_score = None

        return {
            "website_key": website_key,
            "page_action": page_action,
            "task_type": task_type,
            "min_score": min_score,
        }

    def _merge_request_fingerprint(self, patch: Optional[Dict[str, Any]]):
        if not isinstance(patch, dict) or not patch:
            return
        current = self.get_request_fingerprint() or {}
        merged = dict(current)
        for key, value in patch.items():
            if value is None:
                continue
            if isinstance(value, str):
                normalized = value.strip()
                if not normalized:
                    continue
                merged[key] = normalized
            else:
                merged[key] = value
        self._set_request_fingerprint(merged if merged else None)

    async def _notify_browser_captcha_error(
        self,
        browser_id: Optional[Union[int, str]] = None,
        project_id: Optional[str] = None,
        error_reason: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """通知浏览器打码服务执行失败自愈。
        
        Args:
            browser_id: browser 模式使用的浏览器 ID
            project_id: personal 模式使用的 project_id
            error_reason: 已归类的错误原因
            error_message: 原始错误文本
        """
        context = self._get_captcha_request_context()
        if context.get("browser_ref") != browser_id or context.get("project_id") != project_id:
            context = {}
        method = context.get("method") or config.captcha_method
        if method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_error(
                    browser_id,
                    error_reason=error_message or error_reason or "upstream_error",
                )
            except Exception:
                pass
        elif method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService

                service = await ExtensionCaptchaService.get_instance()
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass
        elif method == "personal" and project_id:
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                    token_id=context.get("token_id"),
                    slot_id=context.get("slot_id"),
                    issued_at=context.get("issued_at"),
                )
            except Exception:
                pass
        elif method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/error",
                    json_data={
                        "error_reason": error_reason
                        or error_message
                        or "upstream_error"
                    },
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[reCAPTCHA RemoteBrowser] 上报 error 失败: {e}"
                )

    async def _notify_browser_captcha_request_finished(
        self, browser_id: Optional[Union[int, str]] = None, *, success: Optional[bool] = None,
    ):
        """通知有头浏览器：上游图片/视频请求已结束，可关闭对应打码浏览器。"""
        context = self._get_captcha_request_context()
        method = context.get("method") if context.get("browser_ref") == browser_id else None
        method = method or config.captcha_method
        if method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                if success is None:
                    await service.report_request_finished(browser_id)
                else:
                    await service.report_request_finished(browser_id, success=success)
            except Exception:
                pass
        elif method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/finish",
                    json_data={"status": "success"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[reCAPTCHA RemoteBrowser] 上报 finish 失败: {e}"
                )
        context = self._get_captcha_request_context()
        if context.get("browser_ref") == browser_id:
            self._set_captcha_request_context(None)

    def _generate_session_id(self) -> str:
        """生成sessionId: ;timestamp"""
        return f";{int(time.time() * 1000)}"

    def _generate_scene_id(self) -> str:
        """生成sceneId: UUID"""
        return str(uuid.uuid4())

    def _get_remote_browser_service_config(self) -> tuple[str, str, int]:
        base_url = (config.remote_browser_base_url or "").strip().rstrip("/")
        api_key = (config.remote_browser_api_key or "").strip()
        timeout = max(5, int(config.remote_browser_timeout or 60))

        if not base_url:
            raise RuntimeError("remote_browser 服务地址未配置")
        if not api_key:
            raise RuntimeError("remote_browser API Key 未配置")

        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise RuntimeError("remote_browser 服务地址格式错误")

        return base_url, api_key, timeout

    @staticmethod
    def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
        read_value = max(3.0, float(read_timeout))
        write_value = min(10.0, max(3.0, read_value))
        if httpx is None:
            return read_value
        return httpx.Timeout(
            connect=2.5,
            read=read_value,
            write=write_value,
            pool=2.5,
        )

    @staticmethod
    def _parse_json_response_text(text: str) -> Optional[Any]:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    async def _stdlib_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_data: Optional[bytes] = None

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_data = json.dumps(payload).encode("utf-8")

        def do_request() -> tuple[int, str]:
            request = urllib.request.Request(
                url=url,
                data=request_data,
                headers=req_headers,
                method=request_method,
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                    status_code = int(
                        getattr(response, "status", 0) or response.getcode() or 0
                    )
                    body = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    return status_code, body.decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                body = exc.read()
                charset = exc.headers.get_content_charset() if exc.headers else None
                return int(getattr(exc, "code", 0) or 0), body.decode(
                    charset or "utf-8", errors="replace"
                )

        try:
            status_code, text = await asyncio.to_thread(do_request)
        except Exception as e:
            raise RuntimeError(f"remote_browser 请求失败: {e}") from e

        return status_code, FlowClient._parse_json_response_text(text), text

    @staticmethod
    async def _sync_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_kwargs: Dict[str, Any] = {
            "headers": req_headers,
            "timeout": FlowClient._build_remote_browser_http_timeout(timeout),
        }

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_kwargs["json"] = payload

        if httpx is None:
            return await FlowClient._stdlib_json_http_request(
                method=method,
                url=url,
                headers=req_headers,
                payload=payload,
                timeout=timeout,
            )

        try:
            # remote_browser 控制面只需要稳定传输 JSON，不需要浏览器指纹伪装。
            # 使用 httpx 可以避免 curl_cffi 在当前环境下 POST body 被吞掉。
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False
            ) as session:
                response = await session.request(
                    method=request_method,
                    url=url,
                    **request_kwargs,
                )
        except Exception as e:
            raise RuntimeError(f"remote_browser 请求失败: {e}") from e

        status_code = int(getattr(response, "status_code", 0) or 0)
        text = response.text or ""
        parsed = FlowClient._parse_json_response_text(text)

        return status_code, parsed, text

    async def _call_remote_browser_service(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        timeout_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        base_url, api_key, timeout = self._get_remote_browser_service_config()
        url = f"{base_url}{path}"
        effective_timeout = max(5, int(timeout_override or timeout))

        status_code, payload, response_text = await self._sync_json_http_request(
            method=method,
            url=url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=json_data,
            timeout=effective_timeout,
        )

        if status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or str(payload)
            if not detail:
                detail = (response_text or "").strip() or f"HTTP {status_code}"
            raise RuntimeError(f"remote_browser 请求失败: {detail}")

        if not isinstance(payload, dict):
            raise RuntimeError("remote_browser 返回格式错误")

        return payload

    async def prefill_remote_browser_pool(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        cooldown_seconds: float = 8.0,
    ) -> bool:
        """让本地 remote_browser 服务提前开始补池，尽量把取 token 等待搬到前面。"""
        if config.captcha_method != "remote_browser":
            return False

        normalized_project = str(project_id or "").strip()
        normalized_action = (
            str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        )
        if not normalized_project:
            return False

        cache_key = f"{normalized_project}|{normalized_action}|{int(token_id or 0)}"
        now_value = time.monotonic()
        last_sent = float(
            self._remote_browser_prefill_last_sent.get(cache_key, 0.0) or 0.0
        )
        if (now_value - last_sent) < max(0.5, float(cooldown_seconds)):
            return False

        try:
            await self._call_remote_browser_service(
                method="POST",
                path="/api/v1/prefill",
                json_data={
                    "project_id": normalized_project,
                    "action": normalized_action,
                    "token_id": token_id,
                },
                timeout_override=3,
            )
            self._remote_browser_prefill_last_sent[cache_key] = now_value
            return True
        except Exception as e:
            debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] prefill 失败: {e}")
            return False

    async def prefill_remote_browser_for_tokens(
        self, tokens: List[Any], action: str = "IMAGE_GENERATION"
    ) -> int:
        if config.captcha_method != "remote_browser":
            return 0

        unique_projects: List[str] = []
        seen_projects = set()
        for token in tokens or []:
            project_id = str(getattr(token, "current_project_id", "") or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            unique_projects.append(project_id)

        warmed = 0
        for project_id in unique_projects:
            if await self.prefill_remote_browser_pool(project_id, action=action):
                warmed += 1
        return warmed

    def _resolve_remote_browser_solve_timeout(self, action: str) -> int:
        base_timeout = max(5, int(config.remote_browser_timeout or 60))
        action_name = str(action or "").strip().upper()

        # 这里只是拿 reCAPTCHA token，不应该跟整条生成链路共用数百秒级超时。
        target_timeout = 45 if action_name == "VIDEO_GENERATION" else 35
        return max(12, min(base_timeout, target_timeout))

    async def _get_recaptcha_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        website_url: Optional[str] = None,
        method_override: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[Union[int, str]]]:
        """获取reCAPTCHA token - 支持多种打码方式
        
        Args:
            project_id: 项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大
            token_id: 当前业务 token id（browser 模式下用于读取 token 级打码代理）
            method_override: 强制指定打码方式（如 "yescaptcha"）。
                用于 harvest token 不被目标 RPC 接受的场景（如 SPrCad 放大）。
        
        Returns:
            (token, browser_id) 元组。
            - browser 模式: browser_id 为本地浏览器 ID
            - remote_browser 模式: browser_id 为远程 session_id
            - 其他模式: browser_id 为 None
        """
        captcha_method = method_override or config.captcha_method
        previous = self._get_captcha_request_context()
        if previous.get("method") == "browser" and previous.get("owner") is asyncio.current_task():
            await self._notify_browser_captcha_request_finished(previous.get("browser_ref"))
        self._set_captcha_request_context({
            "method": captcha_method, "token_id": token_id, "project_id": project_id,
            "owner": asyncio.current_task(),
        })
        debug_logger.log_info(
            f"[reCAPTCHA] 开始获取 token: method={captcha_method}, project_id={project_id}, action={action}"
        )

        if captcha_method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService

                service = await ExtensionCaptchaService.get_instance(self.db)
                extension_timeout = 45 if action == "VIDEO_GENERATION" else 25
                token = await service.get_token(
                    project_id, action, timeout=extension_timeout, token_id=token_id
                )
                self._set_request_fingerprint(None)
                return token, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Extension] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None

        # 内置浏览器打码 (nodriver)
        if captcha_method == "personal":
            debug_logger.log_info(f"[reCAPTCHA] 使用 personal 模式")
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                debug_logger.log_info(f"[reCAPTCHA] 导入 BrowserCaptchaService 成功")
                service = await BrowserCaptchaService.get_instance(self.db)
                debug_logger.log_info(
                    f"[reCAPTCHA] 获取服务实例成功，准备调用 get_token"
                )
                solve_bundle = None
                get_token_bundle = getattr(service, "get_token_bundle", None)
                if callable(get_token_bundle):
                    solve_bundle = await get_token_bundle(
                        project_id,
                        action,
                        token_id=token_id,
                    )
                    token = str((solve_bundle or {}).get("token") or "").strip() or None
                else:
                    get_token_with_metadata = getattr(
                        service, "get_token_with_metadata", None
                    )
                    if callable(get_token_with_metadata):
                        (
                            token,
                            _slot_id,
                            _cookie_source_token_id,
                        ) = await get_token_with_metadata(
                            project_id,
                            action,
                            token_id=token_id,
                        )
                    else:
                        token = await service.get_token(
                            project_id, action, token_id=token_id
                        )
                    solve_bundle = (
                        {
                        "token": token,
                            "fingerprint": service.get_last_fingerprint()
                            if token
                            else None,
                        }
                        if token
                        else None
                    )
                debug_logger.log_info(f"[reCAPTCHA] 取码完成: success={bool(token)}")
                fingerprint = (
                    solve_bundle.get("fingerprint")
                    if isinstance(solve_bundle, dict)
                    and isinstance(solve_bundle.get("fingerprint"), dict)
                    else None
                )
                harvest_session_id = (
                    str((solve_bundle or {}).get("session_id") or "").strip() or None
                )
                if isinstance(solve_bundle, dict) and token:
                    session_cookies = solve_bundle.get("session_cookies")
                    proxy_url = str(solve_bundle.get("proxy_url") or "").strip()
                    next_fingerprint = dict(fingerprint or {})
                    if isinstance(session_cookies, dict) and session_cookies:
                        next_fingerprint["session_cookies"] = dict(session_cookies)
                    if (
                        proxy_url
                        and not str(next_fingerprint.get("proxy_url") or "").strip()
                    ):
                        next_fingerprint["proxy_url"] = proxy_url
                    next_fingerprint["project_id"] = project_id
                    next_fingerprint.setdefault("origin", "https://flow.google.com")
                    next_fingerprint.setdefault(
                        "referer", self._build_flow_project_page_url(project_id)
                    )
                    fingerprint = next_fingerprint or None
                if token:
                    effective_ua = str(
                        (fingerprint or {}).get("user_agent") or ""
                    ).strip()
                    effective_lang = str(
                        (fingerprint or {}).get("accept_language") or ""
                    ).strip()
                    if not effective_ua:
                        debug_logger.log_warning(
                            "[reCAPTCHA Personal] 已拿到 token，但未提取到浏览器指纹 UA；"
                            "为避免协议提交与打码环境失配，丢弃本次 token 并触发重试"
                        )
                        self._set_request_fingerprint(None)
                        return None, None
                    debug_logger.log_info(
                        "[reCAPTCHA Personal] 使用浏览器指纹: "
                        f"UA={effective_ua[:120]}, Accept-Language={effective_lang or '<empty>'}"
                    )
                self._set_request_fingerprint(fingerprint if token else None)
                browser_ref = (
                    f"personal:{harvest_session_id}"
                    if token and harvest_session_id
                    else None
                )
                if token:
                    self._set_captcha_request_context({
                        "method": "personal", "browser_ref": browser_ref,
                        "token_id": token_id, "project_id": project_id,
                        "slot_id": (solve_bundle or {}).get("slot_id"),
                        "issued_at": (solve_bundle or {}).get("issued_at"),
                        "owner": asyncio.current_task(),
                    })
                return token, browser_ref
            except RuntimeError as e:
                # 捕获 Docker 环境或依赖缺失的明确错误
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Personal] {error_msg}")
                print(f"[reCAPTCHA] ❌ 内置浏览器打码失败: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 导入失败: {str(e)}")
                print(f"[reCAPTCHA] ❌ nodriver 未安装，请运行: pip install nodriver")
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # 有头浏览器打码 (playwright)
        elif captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                token, browser_id, fingerprint = await service.get_token(
                    project_id, action, token_id=token_id
                )
                if token and action == "CHAT_GENERATION" and not self._extract_browser_captcha_session_id(
                    browser_id
                ):
                    debug_logger.log_warning(
                        "[reCAPTCHA Browser] harvest 未返回会话 id，生成请求可能被拒"
                    )
                self._set_request_fingerprint(fingerprint if token else None)
                if token:
                    self._set_captcha_request_context({
                        "method": "browser", "browser_ref": browser_id,
                        "token_id": token_id, "project_id": project_id,
                        "owner": asyncio.current_task(),
                    })
                return token, browser_id
            except RuntimeError as e:
                from .browser_captcha import BrowserCaptchaQueueTimeout
                if isinstance(e, BrowserCaptchaQueueTimeout):
                    raise
                # 捕获 Docker 环境或依赖缺失的明确错误
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Browser] {error_msg}")
                print(f"[reCAPTCHA] ❌ 有头浏览器打码失败: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] 导入失败: {str(e)}")
                print(
                    f"[reCAPTCHA] ❌ playwright 未安装，请运行: pip install playwright && python -m playwright install chromium"
                )
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        elif captcha_method == "remote_browser":
            try:
                solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                payload = await self._call_remote_browser_service(
                    method="POST",
                    path="/api/v1/solve",
                    json_data={
                        "project_id": project_id,
                        "action": action,
                        "token_id": token_id,
                    },
                    timeout_override=solve_timeout,
                )
                token = payload.get("token")
                session_id = payload.get("session_id")
                fingerprint = (
                    payload.get("fingerprint")
                    if isinstance(payload.get("fingerprint"), dict)
                    else None
                )
                self._set_request_fingerprint(fingerprint if token else None)
                if not token or not session_id:
                    raise RuntimeError(
                        f"remote_browser 返回缺少 token/session_id: {payload}"
                    )
                return token, str(session_id)
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA RemoteBrowser] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # API打码服务
        elif captcha_method in [
            "yescaptcha",
            "flow2api_team",
            "captcharun",
            "twocaptcha",
            "capmonster",
            "ezcaptcha",
            "capsolver",
        ]:
            existing_fingerprint = self.get_request_fingerprint() or {}
            proxy_url = str(existing_fingerprint.get("proxy_url") or "").strip()
            if not proxy_url and self.proxy_manager:
                try:
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                except Exception as e:
                    debug_logger.log_warning(
                        f"[reCAPTCHA] Failed to get proxy for API captcha: {e}"
                    )

            api_captcha_ua = str(
                existing_fingerprint.get("user_agent") or ""
            ).strip() or self._generate_user_agent(
                str(token_id or project_id or captcha_method)
            )
            next_fingerprint = dict(existing_fingerprint)
            next_fingerprint.update(
                self._build_fingerprint_from_user_agent(
                    api_captcha_ua,
                    accept_language=self._get_primary_accept_language(),
                    proxy_url=proxy_url,
                )
            )
            self._set_request_fingerprint(next_fingerprint)
            captcha_website_url = str(
                website_url or ""
            ).strip() or self._build_flow_project_page_url(project_id)
            parsed_captcha_url = urlparse(captcha_website_url)
            captcha_origin = (
                f"{parsed_captcha_url.scheme}://{parsed_captcha_url.netloc}"
                if parsed_captcha_url.scheme and parsed_captcha_url.netloc
                else "https://flow.google.com"
            )
            self._merge_request_fingerprint(
                {
                    "project_id": project_id,
                    "origin": captcha_origin,
                    "referer": captcha_website_url,
                }
            )

            api_result = await self._get_api_captcha_token(
                captcha_method,
                project_id,
                action,
                proxy_url=proxy_url,
                user_agent=api_captcha_ua,
                website_url=captcha_website_url,
                token_id=token_id,
            )
            if api_result is None:
                return None, None
            token, captcha_user_agent = api_result
            # 把打码服务返回的 userAgent 合并到 fingerprint, 让 Flow API 提交请求沿用同一 UA。
            # 否则 Google reCAPTCHA V3 评估会因 UA 不一致判定 UNUSUAL_ACTIVITY 并返回
            # "reCAPTCHA evaluation failed"。其他 Client Hint (sec-ch-ua-platform 等)
            # 由 _make_request 根据 UA 自动推断 (Windows → "Windows", etc.)。
            if captcha_user_agent:
                existing_fp = self._request_fingerprint_ctx.get()
                merged_fp = dict(existing_fp) if isinstance(existing_fp, dict) else {}
                merged_fp["user_agent"] = captcha_user_agent
                self._set_request_fingerprint(merged_fp)
                debug_logger.log_info(
                    f"[reCAPTCHA {captcha_method}] 已将打码 UA 注入请求指纹: "
                    f"{captcha_user_agent[:80]}"
                )
            return token, None
        else:
            debug_logger.log_info(f"[reCAPTCHA] 未知的打码方式: {captcha_method}")
            self._set_request_fingerprint(None)
            return None, None

    async def _get_api_captcha_token(
        self,
        method: str,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        website_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Optional[tuple[str, str]]:
        """通用API打码服务

        Args:
            method: 打码服务类型
            project_id: 项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)
            proxy_url: 预解析后的代理地址，确保打码与后续提交走同一出口
            user_agent: 显式传给打码服务的 UA，避免 solve / submit 环境不一致

        Returns:
            (gRecaptchaResponse, userAgent) 元组, 或 None (失败时)。
            返回 userAgent 是为了让后续 Flow API 请求沿用打码时的 UA,
            避免 reCAPTCHA V3 评估因 UA 不一致判定 UNUSUAL_ACTIVITY。
        """
        # 获取配置
        if method == "flow2api_team":
            # 内置团队打码：地址与密钥写死，不接受配置覆盖。
            client_key = FLOW2API_TEAM_CAPTCHA_CLIENT_KEY
            base_url = FLOW2API_TEAM_CAPTCHA_BASE_URL
        elif method == "yescaptcha":
            client_key = config.yescaptcha_api_key
            base_url = config.yescaptcha_base_url
        elif method == "capmonster":
            client_key = config.capmonster_api_key
            base_url = config.capmonster_base_url
        elif method == "ezcaptcha":
            client_key = config.ezcaptcha_api_key
            base_url = config.ezcaptcha_base_url
        elif method == "capsolver":
            client_key = config.capsolver_api_key
            base_url = config.capsolver_base_url
        elif method == "captcharun":
            client_key = config.captcharun_api_key
            base_url = config.captcharun_base_url
        elif method == "twocaptcha":
            client_key = config.twocaptcha_api_key
            base_url = config.twocaptcha_base_url
        else:
            debug_logger.log_error(f"[reCAPTCHA] Unknown API method: {method}")
            return None

        if not client_key:
            debug_logger.log_info(
                f"[reCAPTCHA] {method} API key not configured, skipping"
            )
            return None

        runtime_settings = self._resolve_recaptcha_runtime_settings(method, action)
        task_type = runtime_settings["task_type"]
        min_score = runtime_settings["min_score"]
        website_key = runtime_settings["website_key"]
        website_url = str(
            website_url or ""
        ).strip() or self._build_flow_project_page_url(project_id)
        page_action = runtime_settings["page_action"]

        try:
            # 获取代理配置，让打码API请求也走代理
            # 注意：curl_cffi 对 SOCKS5 使用 proxy 参数，HTTP 代理使用 proxies 参数
            proxy = None
            proxies = None
            resolved_proxy_url = str(proxy_url or "").strip()
            if not resolved_proxy_url and self.proxy_manager:
                try:
                    resolved_proxy_url = str(
                        await self.proxy_manager.get_request_proxy_url() or ""
                    ).strip()
                    if resolved_proxy_url:
                        if resolved_proxy_url.startswith("socks5://"):
                            # curl_cffi 对 SOCKS5 使用 proxy 参数
                            proxy = resolved_proxy_url
                        else:
                            # HTTP/HTTPS 代理使用 proxies 字典
                            proxies = {
                                "http": resolved_proxy_url,
                                "https": resolved_proxy_url,
                            }
                except Exception as e:
                    debug_logger.log_warning(
                        f"[reCAPTCHA {method}] Failed to get proxy: {e}"
                    )
            elif resolved_proxy_url:
                if resolved_proxy_url.startswith("socks5://"):
                    proxy = resolved_proxy_url
                else:
                    proxies = {"http": resolved_proxy_url, "https": resolved_proxy_url}

            effective_user_agent = str(
                user_agent
                or (self.get_request_fingerprint() or {}).get("user_agent")
                or self._generate_user_agent(str(project_id or method))
            ).strip()
            if method == "twocaptcha":
                from .twocaptcha import solve_recaptcha_v3

                solution = await solve_recaptcha_v3(
                    client_key, website_url, website_key, page_action,
                    enterprise=True, base_url=base_url,
                    proxy_url=resolved_proxy_url or None,
                )
                return solution["gRecaptchaResponse"], solution["userAgent"]
            if method == "captcharun":
                headers = {
                    "Authorization": f"Bearer {client_key}",
                    "Content-Type": "application/json",
                }
                task = {
                    "captchaType": "ReCaptchaV3",
                    "siteKey": website_key,
                    "siteReferer": website_url,
                    "siteAction": page_action,
                }
                async with AsyncSession() as session:
                    request_options = {
                        "headers": headers,
                        "impersonate": "chrome",
                        "timeout": 30,
                    }
                    if proxy:
                        request_options["proxy"] = proxy
                    elif proxies:
                        request_options["proxies"] = proxies
                    result = await session.post(
                        f"{base_url.rstrip('/')}/v2/tasks",
                        json=task,
                        **request_options,
                    )
                    payload = result.json()
                    task_id = str(payload.get("taskId") or payload.get("id") or "")
                    if result.status_code >= 400 or not task_id:
                        debug_logger.log_error(
                            f"[reCAPTCHA captcharun] create task failed: {payload}"
                        )
                        return None
                    for _ in range(60):
                        await asyncio.sleep(3)
                        result = await session.get(
                            f"{base_url.rstrip('/')}/v2/tasks/{task_id}",
                            **request_options,
                        )
                        payload = result.json()
                        status = str(payload.get("status") or "").lower()
                        if status == "success":
                            solution = payload.get("response") or {}
                            token = solution.get("gRecaptchaResponse")
                            if token:
                                return str(token), effective_user_agent
                            return None
                        if status in {"failed", "failure", "error", "cancelled"}:
                            debug_logger.log_error(
                                f"[reCAPTCHA captcharun] task failed: {payload}"
                            )
                            return None
                return None

            async with AsyncSession() as session:
                create_url = f"{base_url}/createTask"
                create_data = {
                    "clientKey": client_key,
                    "task": {
                        "websiteURL": website_url,
                        "websiteKey": website_key,
                        "type": task_type,
                        "pageAction": page_action,
                    },
                }
                if is_yes_style_captcha_method(method):
                    create_data["softID"] = "33424"
                if effective_user_agent:
                    create_data["task"]["userAgent"] = effective_user_agent
                if min_score is not None:
                    create_data["task"]["minScore"] = min_score
                if method == FLOW2API_TEAM_CAPTCHA_METHOD:
                    # 团队打码额外支持把账号 Cookie 与出口代理一起下发，
                    # 打码侧用同一登录态 + 同一出口 IP 取 token，分数更稳。
                    team_cookie_map = await self._build_team_captcha_cookie_map(
                        token_id
                    )
                    if team_cookie_map:
                        create_data["task"]["cookie"] = team_cookie_map
                    if resolved_proxy_url:
                        create_data["task"]["proxy"] = resolved_proxy_url

                task_id = None
                last_create_error = None
                max_create_attempts = (
                    len(self.YESCAPTCHA_SLOT_BACKOFF_SECONDS)
                    if is_yes_style_captcha_method(method)
                    else 1
                )
                for create_attempt in range(max_create_attempts):
                    if proxy:
                        result = await session.post(
                            create_url,
                            json=create_data,
                            impersonate="chrome",
                            proxy=proxy,
                        )
                    else:
                        result = await session.post(
                            create_url,
                            json=create_data,
                            impersonate="chrome",
                            proxies=proxies,
                        )

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] createTask response status: {result.status_code}"
                    )
                    result_json = result.json()
                    task_id = result_json.get("taskId")

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] created task_id: {task_id}, response: {result_json}"
                    )

                    if task_id:
                        break

                    error_code = str(result_json.get("errorCode") or "").strip()
                    error_desc = result_json.get("errorDescription", "Unknown error")
                    last_create_error = result_json
                    if is_yes_style_captcha_method(method) and error_code in {
                        "ERROR_NO_SLOT_AVAILABLE",
                        "ERROR_NO_SLOT_AVAILABLE_BLOCK",
                    }:
                        delay = self._get_retry_delay_seconds(
                            error_code, create_attempt
                        )
                        if create_attempt < max_create_attempts - 1:
                            debug_logger.log_warning(
                                f"[reCAPTCHA {method}] createTask 资源不足 ({error_code})，将在 {delay} 秒后重试 "
                                f"({create_attempt + 2}/{max_create_attempts})"
                            )
                            await asyncio.sleep(delay)
                            continue
                    debug_logger.log_error(
                        f"[reCAPTCHA {method}] Failed to create task: {error_desc}"
                    )
                    return None

                if not task_id:
                    error_desc = (last_create_error or {}).get(
                        "errorDescription", "Unknown error"
                    )
                    debug_logger.log_error(
                        f"[reCAPTCHA {method}] Failed to create task: {error_desc}"
                    )
                    return None

                get_url = f"{base_url}/getTaskResult"
                for i in range(40):
                    get_data = {"clientKey": client_key, "taskId": task_id}
                    # 根据代理类型使用不同参数
                    if proxy:
                        result = await session.post(
                            get_url, json=get_data, impersonate="chrome", proxy=proxy
                        )
                    else:
                        result = await session.post(
                            get_url,
                            json=get_data,
                            impersonate="chrome",
                            proxies=proxies,
                        )
                    result_json = result.json()

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] polling #{i + 1}: {result_json}"
                    )

                    status = result_json.get("status")
                    if status == "ready":
                        solution = result_json.get("solution", {})
                        response = solution.get("gRecaptchaResponse")
                        if response:
                            self._merge_request_fingerprint(
                                self._build_fingerprint_from_user_agent(
                                    solution.get("userAgent"),
                                    accept_language=self._get_primary_accept_language(),
                                    proxy_url=resolved_proxy_url,
                                )
                            )
                            debug_logger.log_info(f"[reCAPTCHA {method}] Token获取成功")
                            # 同时返回 userAgent, 调用方会注入到 fingerprint context,
                            # 保证 Flow API 提交请求时使用与打码一致的 UA。
                            user_agent = solution.get("userAgent")
                            return response, user_agent

                    await asyncio.sleep(3)

                debug_logger.log_error(
                    f"[reCAPTCHA {method}] Timeout waiting for token"
                )
                return None

        except Exception as e:
            debug_logger.log_error(f"[reCAPTCHA {method}] error: {str(e)}")
            return None


class FlowClient(CurrentFlowClientMixin, _FlowClientBase):
    """Flow client backed exclusively by the current flow.google.com RPC layer."""

    @staticmethod
    def _get_runtime_config():
        return config
