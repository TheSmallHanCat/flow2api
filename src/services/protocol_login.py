"""Validate exported Google cookies against the current Flow frontend."""

import hashlib
import json
import re
from http.cookiejar import Cookie, CookieJar
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from curl_cffi import CurlInfo
from curl_cffi.requests import AsyncSession, Cookies
from curl_cffi.requests.cookies import CurlMorsel

from ..core.logger import debug_logger
from .browser_cookie_utils import (
    cookie_pairs_for_url,
    normalize_cookie_storage_text,
    parse_browser_cookie_payload,
    serialize_cookie_header_for_url,
    validate_flow_cookie_storage,
)


FLOW_BASE = "https://flow.google.com"
IMPERSONATE = "chrome"


def extract_flow_account_email(html: str) -> str:
    """Read the signed-in identity from the current Flow bootstrap data."""
    match = re.search(r'"oPEP7c"\s*:\s*("(?:[^"\\]|\\.)*")', html or "")
    if not match:
        return ""
    value = json.loads(match.group(1))
    return value.strip() if isinstance(value, str) and "@" in value else ""


def _cookie_jar(raw: str) -> CookieJar:
    jar = CookieJar()
    items = parse_browser_cookie_payload(raw)
    if not items:
        normalized = normalize_cookie_storage_text(raw)
        if normalized[:1] in {"[", "{"}:
            payload = json.loads(normalized)
            if (
                not isinstance(payload, dict)
                or "cookies" in payload
                or "name" in payload
            ):
                return jar
        items = [
            {"name": name, "value": value, "url": f"{FLOW_BASE}/"}
            for name, value in cookie_pairs_for_url(raw)
        ]
    for item in items:
        domain = item.get("domain") or urlparse(item.get("url", FLOW_BASE)).hostname
        domain = str(domain or "").lower()
        if domain.lstrip(".") != "google.com" and not domain.endswith(".google.com"):
            continue
        expires = item.get("expires")
        expires = int(expires) if expires and expires > 0 else None
        rest = {}
        if item.get("httpOnly"):
            rest["HttpOnly"] = None
        if item.get("sameSite"):
            rest["SameSite"] = item["sameSite"]
        jar.set_cookie(
            Cookie(
                version=0,
                name=item["name"],
                value=item["value"],
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(item.get("domain")),
                domain_initial_dot=domain.startswith("."),
                path=item.get("path", "/"),
                path_specified=True,
                secure=item.get("secure", True),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest=rest,
            )
        )
    jar.clear_expired_cookies()
    return jar


def _export_cookie_jar(jar: CookieJar, original: str) -> str:
    # libcurl retains HTTP scope/expiry but does not expose SameSite metadata.
    original_attributes = {
        (
            item["name"],
            str(
                item.get("domain") or urlparse(item.get("url", FLOW_BASE)).hostname
            ).lstrip("."),
            item.get("path", "/"),
        ): item
        for item in parse_browser_cookie_payload(original)
    }
    jar.clear_expired_cookies()
    items = []
    for cookie in jar:
        original_item = original_attributes.get(
            (cookie.name, cookie.domain.lstrip("."), cookie.path), {}
        )
        item = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "hostOnly": not cookie.domain_specified,
            "path": cookie.path,
            "secure": cookie.secure,
            "httpOnly": cookie.has_nonstandard_attr("HttpOnly")
            or str(cookie.get_nonstandard_attr("http_only", "False")).lower() == "true"
            or bool(original_item.get("httpOnly")),
        }
        if cookie.expires is not None:
            item["expires"] = cookie.expires
        same_site = original_item.get("sameSite")
        if same_site:
            item["sameSite"] = same_site
        items.append(item)
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


class ProtocolLogin:
    """Refresh the scoped Google/Flow cookie jar through the current frontend."""

    async def login(
        self,
        google_cookies_raw: str,
        proxy: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            validate_flow_cookie_storage(google_cookies_raw)
            cookie_jar = _cookie_jar(google_cookies_raw)
        except (ValueError, OverflowError) as exc:
            return {
                "success": False,
                "error": str(exc),
            }

        session_kwargs: Dict[str, Any] = {
            "impersonate": IMPERSONATE,
            "trust_env": False,
            "cookies": cookie_jar,
            "timeout": 30,
            "curl_infos": [CurlInfo.COOKIELIST],
        }
        normalized_proxy = str(proxy or "").strip()
        if normalized_proxy:
            if "://" not in normalized_proxy:
                normalized_proxy = f"http://{normalized_proxy}"
            session_kwargs["proxy"] = normalized_proxy

        try:
            cookie_hdr = serialize_cookie_header_for_url(
                google_cookies_raw, f"{FLOW_BASE}/projects"
            )
            req_headers = {"Referer": f"{FLOW_BASE}/"}
            if cookie_hdr:
                req_headers["Cookie"] = cookie_hdr
            async with AsyncSession(**session_kwargs) as session:
                response = await session.get(
                    f"{FLOW_BASE}/projects",
                    headers=req_headers,
                )
                # The Python session jar only merges updates; a fresh snapshot also
                # honors cookies deleted by Set-Cookie, including during redirects.
                refreshed_jar = Cookies()
                refreshed_jar.update_cookies_from_curl(
                    [
                        CurlMorsel.from_curl_format(line)
                        for line in response.infos[CurlInfo.COOKIELIST]
                    ]
                )
                refreshed_cookies = _export_cookie_jar(
                    refreshed_jar.jar, google_cookies_raw
                )
            response_host = str(urlparse(str(response.url)).hostname or "").lower()
            if response_host and response_host != "flow.google.com":
                return {
                    "success": False,
                    "error": "Flow Cookie 已失效或不完整，页面已跳转到 Google 登录",
                }
            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"Flow Cookie 验证失败: HTTP {response.status_code}",
                }
            if "boq_labs-ai-sandbox-frontend_" not in (response.text or ""):
                return {
                    "success": False,
                    "error": "Flow Cookie 验证失败: 未加载当前前端",
                }
            validate_flow_cookie_storage(refreshed_cookies)
            account_email = extract_flow_account_email(response.text)
            if not account_email:
                return {
                    "success": False,
                    "error": "Flow 页面未返回已登录账号，请重新同步 Google Cookies",
                }
            expected_email = str(email or "").strip()
            if (
                expected_email
                and account_email.casefold() != expected_email.casefold()
            ):
                # 邮箱以 Flow 页面为准，调用方据此自动更正本地账号信息。
                debug_logger.log_warning(
                    f"[PROTOCOL_LOGIN] 登录账号已变更: {expected_email} -> {account_email}"
                )
            digest = hashlib.sha256(refreshed_cookies.encode("utf-8")).hexdigest()[:32]
            return {
                "success": True,
                "session_token": f"frontend-{digest}",
                "email": account_email,
                "google_cookies": refreshed_cookies,
            }
        except Exception as exc:
            debug_logger.log_error(f"[PROTOCOL_LOGIN] Flow Cookie 验证异常: {exc}")
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            return {"success": False, "error": error}


protocol_loginer = ProtocolLogin()
