"""Current flow.google.com frontend RPC transport and response adapters."""

import json
import contextvars
import random
import re
import uuid
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlparse, urljoin

from curl_cffi import CurlInfo, CurlOpt
from curl_cffi.requests import AsyncSession, Cookies
from curl_cffi.requests.cookies import CurlMorsel

from ..core.logger import debug_logger
from .browser_cookie_utils import serialize_cookie_header_for_url
from .flow_cookies import FlowCookieJar


class FlowFrontendMixin:
    """Frontend RPC helpers shared by project and media generation flows."""

    def get_updated_flow_cookies(self, original: Optional[str]) -> str:
        """Return the latest matching cookie snapshot in this async context."""
        storage = str(original or "").strip()
        context = getattr(self, "_flow_cookie_storage_ctx", None)
        snapshot = context.get() if context is not None else None
        if snapshot and storage in snapshot:
            return snapshot[1]
        return storage

    def _remember_flow_cookies(self, original: str, cookie_jar: FlowCookieJar) -> None:
        if not hasattr(self, "_flow_cookie_storage_ctx"):
            self._flow_cookie_storage_ctx = contextvars.ContextVar(
                "flow_cookie_storage", default=None
            )
        self._flow_cookie_storage_ctx.set((original, cookie_jar.export_storage()))

    STREAM_CHAT_RPC_PATH = (
        "/_/AiSandboxAngularFrontend/data/"
        "google.internal.labs.aisandbox.proto.flow.agent.v1."
        "FlowCreationAgentService/StreamChat"
    )

    @staticmethod
    def _build_flow_frontend_project_page_url(project_id: str) -> str:
        return f"https://flow.google.com/project/{project_id}"

    @staticmethod
    def _parse_flow_cookie_storage(raw: Optional[str]) -> str:
        return serialize_cookie_header_for_url(raw)

    async def _resolve_flow_frontend_cookie_storage(
        self,
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> str:
        cookie_storage = str(google_cookies or "").strip()
        if not cookie_storage and token_id is not None and self.db is not None:
            token = await self.db.get_token(int(token_id))
            cookie_storage = (
                str(getattr(token, "google_cookies", "") or "").strip()
                if token
                else ""
            )
        return cookie_storage

    @staticmethod
    def _parse_batchexecute_frames(response_text: str) -> List[List[Any]]:
        frames: List[List[Any]] = []
        for line in str(response_text or "").splitlines():
            line = line.strip()
            if not line.startswith("["):
                continue
            try:
                frame = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(frame, list):
                frames.append(frame)
        return frames

    @staticmethod
    def _extract_batchexecute_payload(
        frames: List[List[Any]],
        rpc_id: str,
    ) -> Optional[Any]:
        for frame in frames:
            for item in frame:
                if not isinstance(item, list) or len(item) < 3:
                    continue
                if item[0] != "wrb.fr" or item[1] != rpc_id:
                    continue
                try:
                    return json.loads(item[2])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _extract_frontend_rpc_error_code(value: Any) -> str:
        if isinstance(value, str):
            return value if value.startswith(("PUBLIC_ERROR_", "ERROR_")) else ""
        if isinstance(value, (list, tuple)):
            for item in value:
                code = FlowFrontendMixin._extract_frontend_rpc_error_code(item)
                if code:
                    return code
        return ""

    @classmethod
    def _raise_batchexecute_error(
        cls,
        frames: List[List[Any]],
        rpc_id: str,
    ) -> None:
        for frame in frames:
            for item in frame:
                if not isinstance(item, list) or len(item) < 3:
                    continue
                if item[0] == "er":
                    code = item[5] if len(item) > 5 and item[5] is not None else None
                    if code is None and len(item) > 6:
                        code = item[6]
                    code = code if code is not None else "unknown"
                    if str(code) == "401":
                        raise RuntimeError(
                            "Flow 登录认证失败：Google Cookie 已失效、不完整，或来自不同账号 "
                            f"(rpc={rpc_id}, code=401)"
                        )
                    raise RuntimeError(
                        f"Flow frontend RPC rejected: rpc={rpc_id}, code={code}"
                    )
                if item[0] != "wrb.fr" or item[1] != rpc_id:
                    continue
                error_detail = item[5] if len(item) > 5 else None
                error_code = cls._extract_frontend_rpc_error_code(error_detail)
                if error_code:
                    raise RuntimeError(
                        f"Flow frontend RPC rejected: rpc={rpc_id}, error={error_code}"
                    )
                if item[2] is None and error_detail is not None:
                    raise RuntimeError(
                        f"Flow frontend RPC rejected: rpc={rpc_id}, code={error_detail}"
                    )

    async def _resolve_flow_frontend_cookie_header(
        self,
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
        target_url: str = "https://flow.google.com/projects",
    ) -> str:
        cookie_storage = await self._resolve_flow_frontend_cookie_storage(
            google_cookies=google_cookies,
            token_id=token_id,
        )
        cookie_header = serialize_cookie_header_for_url(cookie_storage, target_url)
        if not cookie_header:
            raise RuntimeError(
                "Flow frontend generation requires Google session cookies"
            )
        return cookie_header

    @staticmethod
    def _map_frontend_image_aspect_ratio(aspect_ratio: str) -> int:
        mapping = {
            "IMAGE_ASPECT_RATIO_SQUARE": 1,
            "IMAGE_ASPECT_RATIO_PORTRAIT": 2,
            "IMAGE_ASPECT_RATIO_16_9": 3,
            "IMAGE_ASPECT_RATIO_LANDSCAPE": 3,
            "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": 5,
            "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": 4,
        }
        try:
            return mapping[str(aspect_ratio or "").strip().upper()]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported Flow image aspect ratio: {aspect_ratio}"
            ) from exc

    @staticmethod
    def _frontend_project_context(
        project_id: str,
        recaptcha_token: str,
    ) -> List[Any]:
        return [
            None,
            22,
            None,
            None,
            None,
            project_id,
            None,
            None,
            None,
            None,
            [recaptcha_token, 1],
        ]

    def _build_frontend_image_generation_argument(
        self,
        *,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        recaptcha_token: str,
        session_id: str,
        image_inputs: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Any]:
        reference_media_ids = [
            str(image_input.get("name") or image_input.get("mediaId") or "").strip()
            for image_input in image_inputs or []
            if isinstance(image_input, dict)
        ]
        reference_media_ids = [item for item in reference_media_ids if item]
        client_context = self._frontend_project_context(project_id, recaptcha_token)
        seed = random.randint(1, 2147483647)
        request: List[Any] = [
            None,
            None,
            [[media_id, None, None, None, 1] for media_id in reference_media_ids] or None,
            seed,
            self._map_frontend_image_aspect_ratio(aspect_ratio),
            model_name,
            None,
            client_context,
            [[[prompt]]],
            None,
            None,
            None,
            session_id,
            str(uuid.uuid4()).upper(),
        ]
        return [
            None,
            [request],
            1,
            client_context,
            [session_id],
        ]

    @staticmethod
    def _map_frontend_video_aspect_ratio(aspect_ratio: str) -> int:
        normalized = str(aspect_ratio or "").strip().upper()
        if "PORTRAIT" in normalized:
            return 1
        if "SQUARE" in normalized:
            return 0
        return 2

    @staticmethod
    def _frontend_prompt(prompt: str) -> List[Any]:
        return [[[str(prompt or "")]]]

    @staticmethod
    def _frontend_generation_metadata(session_id: str) -> List[Any]:
        return [None, None, None, None, session_id, str(uuid.uuid4()).upper()]

    @staticmethod
    def _frontend_media_reference(media_id: str) -> List[Any]:
        return [None, str(media_id or "").strip()]

    @staticmethod
    def _frontend_video_resolution(resolution: Optional[str]) -> Optional[int]:
        normalized = str(resolution or "").strip().upper()
        if not normalized:
            return None
        if "4K" in normalized:
            return 4
        if "1080" in normalized:
            return 3
        if "720" in normalized:
            return 2
        if "360" in normalized:
            return 1
        return None

    @classmethod
    def _find_flow_content_url(cls, value: Any, media_kind: str) -> str:
        if isinstance(value, str):
            marker = f"/{media_kind}/"
            return value if value.startswith("https://") and marker in value else ""
        if isinstance(value, list):
            for item in value:
                url = cls._find_flow_content_url(item, media_kind)
                if url:
                    return url
        elif isinstance(value, dict):
            for item in value.values():
                url = cls._find_flow_content_url(item, media_kind)
                if url:
                    return url
        return ""

    @staticmethod
    def _is_uuid(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                value,
                re.IGNORECASE,
            )
        )

    @classmethod
    def _normalize_frontend_image_generation_response(
        cls, payload: Any
    ) -> Dict[str, Any]:
        media_items = (
            payload[0]
            if isinstance(payload, list) and payload and isinstance(payload[0], list)
            else []
        )
        media: List[Dict[str, Any]] = []
        for item in media_items:
            if not isinstance(item, list) or not item:
                continue
            media_id = str(item[0] or "").strip()
            image_url = cls._find_flow_content_url(item, "image")
            if media_id and image_url:
                media.append(
                    {
                        "name": media_id,
                        "image": {"generatedImage": {"fifeUrl": image_url}},
                    }
                )
        if not media:
            raise RuntimeError("Flow frontend image RPC returned no downloadable media")
        return {"media": media, "frontendRpc": "ogiZ0b"}

    def _build_frontend_video_generation_argument(
        self,
        *,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        recaptcha_token: str,
        session_id: str,
        mode: str = "text",
        reference_media_ids: Optional[List[str]] = None,
        start_media_id: Optional[str] = None,
        end_media_id: Optional[str] = None,
        video_media_id: Optional[str] = None,
        resolution: Optional[str] = None,
        output_spec: Optional[Any] = None,
    ) -> tuple[str, List[Any]]:
        prompt_value = self._frontend_prompt(prompt)
        prompt_message = [None, None, prompt_value]
        aspect_value = self._map_frontend_video_aspect_ratio(aspect_ratio)
        metadata = self._frontend_generation_metadata(session_id)
        resolution_value = self._frontend_video_resolution(resolution)
        normalized_mode = str(mode or "text").strip().lower()

        if normalized_mode == "references":
            references = [
                self._frontend_media_reference(media_id)
                for media_id in (reference_media_ids or [])
                if str(media_id or "").strip()
            ]
            rpc_id = "MZZa6b"
            request = [
                prompt_message,
                references,
                model_key,
                aspect_value,
                None,
                metadata,
                None,
                None,
                output_spec,
                None,
                None,
                resolution_value,
            ]
        elif normalized_mode == "start_end":
            rpc_id = "nprQif"
            request = [
                prompt_message,
                model_key,
                aspect_value,
                None,
                self._frontend_media_reference(start_media_id or ""),
                self._frontend_media_reference(end_media_id or ""),
                metadata,
                None,
                output_spec,
            ]
        elif normalized_mode == "start":
            rpc_id = "eb1hJf"
            request = [
                prompt_message,
                model_key,
                aspect_value,
                None,
                self._frontend_media_reference(start_media_id or ""),
                metadata,
                None,
                None,
                output_spec,
                resolution_value,
            ]
        elif normalized_mode == "extend":
            rpc_id = "fZytfe"
            request = [
                [None, str(video_media_id or "").strip()],
                prompt_message,
                model_key,
                aspect_value,
                None,
                metadata,
                None,
                output_spec,
            ]
        elif normalized_mode == "upsample":
            rpc_id = "p0UkFb"
            request = [
                [None, str(video_media_id or "").strip()],
                None,
                aspect_value,
                random.randint(1, 2147483647),
                metadata,
                None,
                resolution_value,
                output_spec,
            ]
            if model_key:
                request.extend([None] * (31 - len(request)))
                request.append(model_key)
        else:
            rpc_id = "YhhmEf"
            request = [
                prompt_message,
                model_key,
                aspect_value,
                None,
                metadata,
                None,
                output_spec,
                resolution_value,
            ]
        return rpc_id, [
            [request],
            self._frontend_project_context(project_id, recaptcha_token),
            [str(uuid.uuid4()).upper(), 2],
        ]

    @classmethod
    def _extract_frontend_video_records(
        cls,
        payload: Any,
        project_id: Optional[str] = None,
    ) -> List[List[Any]]:
        records: List[List[Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, list):
                if (
                    len(value) >= 3
                    and cls._is_uuid(value[0])
                    and (project_id is None or str(value[1] or "") == project_id)
                    and cls._is_uuid(value[2])
                ):
                    records.append(value)
                    return
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)

        walk(payload)
        unique: List[List[Any]] = []
        seen = set()
        for record in records:
            key = str(record[0])
            if key not in seen:
                seen.add(key)
                unique.append(record)
        return unique

    @classmethod
    def _normalize_frontend_video_submission(
        cls,
        payload: Any,
        *,
        project_id: str,
        aspect_ratio: str,
        model_key: str,
        rpc_id: str = "YhhmEf",
    ) -> Dict[str, Any]:
        records = cls._extract_frontend_video_records(payload, project_id)
        operations: List[Dict[str, Any]] = []
        for record in records:
            operation_id = str(record[0])
            media_name = (
                str(record[2])
                if len(record) > 2 and cls._is_uuid(record[2])
                else operation_id
            )
            operations.append(
                {
                    "operation": {"name": operation_id},
                    "name": operation_id,
                    "mediaName": media_name,
                    "projectId": project_id,
                    "status": "MEDIA_GENERATION_STATUS_ACTIVE",
                    "aspectRatio": aspect_ratio,
                    "modelKey": model_key,
                    "frontendRpc": rpc_id,
                }
            )
        video_url = cls._find_flow_content_url(payload, "video")
        if video_url:
            return {
                "direct_media": True,
                "video_url": video_url,
                "projectId": project_id,
                "frontendRpc": rpc_id,
            }
        if not operations:
            raise RuntimeError(
                "Flow frontend video RPC returned no generation operations"
            )
        return {
            "operations": operations,
            "projectId": project_id,
            "frontendRpc": rpc_id,
        }

    @classmethod
    def _normalize_frontend_video_status(
        cls,
        payload: Any,
        operations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        existing = {
            str(
                (operation or {}).get("name")
                or ((operation or {}).get("operation") or {}).get("name")
                or ""
            ): operation
            for operation in operations
            if isinstance(operation, dict)
        }
        records = cls._extract_frontend_video_records(payload)
        normalized: List[Dict[str, Any]] = []
        for record in records:
            operation_id = str(record[0])
            previous = existing.get(operation_id, {})
            previous_operation = (
                (previous.get("operation") or {}) if isinstance(previous, dict) else {}
            )
            media_name = (
                str(record[2])
                if len(record) > 2 and cls._is_uuid(record[2])
                else str((previous or {}).get("mediaName") or operation_id)
            )
            video_url = cls._find_flow_content_url(record, "video")
            error_code = cls._extract_frontend_rpc_error_code(record)
            status = "MEDIA_GENERATION_STATUS_SUCCESSFUL" if video_url else (
                "MEDIA_GENERATION_STATUS_FAILED"
                if error_code
                else "MEDIA_GENERATION_STATUS_ACTIVE"
            )
            metadata = dict(previous_operation.get("metadata") or {})
            video_info = (
                dict(metadata.get("video") or {})
                if isinstance(metadata.get("video"), dict)
                else {}
            )
            if video_url:
                video_info["fifeUrl"] = video_url
            video_info.setdefault("mediaName", media_name)
            video_info.setdefault("mediaGenerationId", operation_id)
            video_info.setdefault(
                "aspectRatio",
                previous.get("aspectRatio") if isinstance(previous, dict) else None,
            )
            video_info.setdefault(
                "model",
                previous.get("modelKey") if isinstance(previous, dict) else None,
            )
            metadata["video"] = video_info
            error_message = {
                "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED": (
                    "提示词或参考素材触发了人物安全过滤"
                ),
                "PUBLIC_ERROR_UNSAFE_GENERATION": "提示词或参考素材触发了内容安全过滤",
            }.get(error_code, f"Flow 上游拒绝视频生成：{error_code}")
            operation_body = {"name": operation_id, "metadata": metadata}
            if error_code:
                operation_body["error"] = {
                    "code": error_code,
                    "message": error_message,
                }
            normalized.append(
                {
                    "operation": operation_body,
                    "name": operation_id,
                    "mediaName": media_name,
                    "projectId": (
                        str(record[1])
                        if record[1] is not None
                        else previous.get("projectId", "")
                    ),
                    "status": status,
                    "progress": 100 if video_url else 45,
                    "frontendRpc": "jwpduf",
                }
            )
        if not normalized:
            error_code = cls._extract_frontend_rpc_error_code(payload)
            if error_code:
                error_message = f"Flow 上游拒绝视频生成：{error_code}"
                for previous in operations:
                    operation_id = str(
                        (previous or {}).get("name")
                        or ((previous or {}).get("operation") or {}).get("name")
                        or ""
                    )
                    normalized.append(
                        {
                            **previous,
                            "operation": {
                                **((previous or {}).get("operation") or {}),
                                "name": operation_id,
                                "error": {
                                    "code": error_code,
                                    "message": error_message,
                                },
                            },
                            "status": "MEDIA_GENERATION_STATUS_FAILED",
                            "progress": 45,
                            "frontendRpc": "jwpduf",
                        }
                    )
        return {"operations": normalized or operations, "frontendRpc": "jwpduf"}

    @staticmethod
    def _update_flow_cookie_jar(cookie_jar: CookieJar, response: Any) -> None:
        # A full libcurl snapshot also honors deletions and redirect Set-Cookie.
        # The session's Python jar only merges cookies and can retain deleted ones.
        refreshed = Cookies()
        refreshed.update_cookies_from_curl(
            [
                CurlMorsel.from_curl_format(line)
                for line in response.infos[CurlInfo.COOKIELIST]
            ]
        )
        if isinstance(cookie_jar, FlowCookieJar):
            cookie_jar.replace_snapshot(refreshed.jar, response)
            return
        cookie_jar.clear()
        for cookie in refreshed.jar:
            cookie_jar.set_cookie(cookie)

    async def _load_flow_frontend_bootstrap(
        self,
        page_url: str,
        cookie_header: str,
        timeout: int,
        cookie_jar: Optional[CookieJar] = None,
    ) -> str:
        proxy_url = None
        if self.proxy_manager:
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                proxy_url = await self.proxy_manager.get_request_proxy_url()
            else:
                proxy_url = await self.proxy_manager.get_proxy_url()
        fingerprint = self.get_request_fingerprint() or {}
        if "proxy_url" in fingerprint:
            proxy_url = str(fingerprint.get("proxy_url") or "").strip() or None
        if isinstance(cookie_jar, FlowCookieJar):
            cookie_jar.add_runtime_cookies(fingerprint.get("session_cookies"), page_url)
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": cookie_header,
            "Referer": "https://flow.google.com/",
            "User-Agent": self._get_effective_request_user_agent(),
        }
        session_kwargs: Dict[str, Any] = {"trust_env": False}
        if cookie_jar is not None:
            session_kwargs.update(cookies=cookie_jar, curl_infos=[CurlInfo.COOKIELIST])
            headers.pop("Cookie", None)
        async with AsyncSession(**session_kwargs) as session:
            for _ in range(10):
                extra = (
                    cookie_jar.extra_cookie_header(page_url)
                    if isinstance(cookie_jar, FlowCookieJar) else ""
                )
                session.curl_options[CurlOpt.COOKIE] = extra
                response = await session.get(
                    page_url,
                    headers=headers,
                    proxy=proxy_url,
                    timeout=max(10, min(int(timeout or 30), 30)),
                    impersonate=self._resolve_runtime_impersonate(),
                    allow_redirects=not bool(extra),
                )
                if cookie_jar is not None:
                    self._update_flow_cookie_jar(cookie_jar, response)
                location = response.headers.get("location")
                if not extra or not (300 <= response.status_code < 400) or not location:
                    break
                page_url = urljoin(page_url, location)
                redirect = urlparse(page_url)
                if redirect.scheme != "https" or redirect.hostname != "flow.google.com":
                    raise RuntimeError("Flow 登录认证失败：登录态被重定向")
            else:
                raise RuntimeError("Flow frontend bootstrap exceeded redirect limit")
        response_host = str(urlparse(str(response.url)).hostname or "").lower()
        if response.status_code in {401, 403} or (
            response_host and response_host != "flow.google.com"
        ):
            raise RuntimeError(
                "Flow 登录认证失败：Google Cookie 已失效、不完整，或登录态被重定向 "
                f"(code={response.status_code})"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Flow frontend bootstrap failed: HTTP {response.status_code}"
            )
        return response.text

    async def _call_flow_frontend_rpc(
        self,
        rpc_id: str,
        argument: List[Any],
        cookie_header: str,
        timeout: int,
        project_id: Optional[str] = None,
        source_path: Optional[str] = None,
        cookie_storage: Optional[str] = None,
    ) -> Any:
        resolved_source_path = source_path or (
            f"/project/{project_id}" if project_id else "/projects"
        )
        page_url = f"https://flow.google.com{resolved_source_path}"
        bootstrap_cookie_header = (
            serialize_cookie_header_for_url(cookie_storage, page_url)
            if str(cookie_storage or "").strip()
            else cookie_header
        )
        if not bootstrap_cookie_header:
            raise RuntimeError("Flow frontend RPC requires Google session cookies")
        original_cookies = (
            cookie_storage if str(cookie_storage or "").strip() else cookie_header
        )
        cookie_jar = FlowCookieJar(self.get_updated_flow_cookies(original_cookies))
        bootstrap_text = await self._load_flow_frontend_bootstrap(
            page_url,
            bootstrap_cookie_header,
            timeout,
            cookie_jar=cookie_jar,
        )
        self._remember_flow_cookies(original_cookies, cookie_jar)
        bootloader_match = re.search(
            r"boq_labs-ai-sandbox-frontend_[A-Za-z0-9_.-]+",
            bootstrap_text,
        )
        session_match = re.search(r'"FdrFJe":"(-?[0-9]+)"', bootstrap_text)
        if not bootloader_match or not session_match:
            raise RuntimeError("Flow frontend bootstrap metadata is unavailable")
        bootloader_id = bootloader_match.group(0)
        frontend_session_id = session_match.group(1)
        request_counter = random.randint(10_000, 90_000)
        url = (
            "https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute"
            f"?rpcids={quote(rpc_id, safe='')}"
            f"&source-path={quote(resolved_source_path, safe='')}"
            f"&bl={quote(bootloader_id, safe='')}"
            f"&f.sid={frontend_session_id}&hl=en"
            f"&_reqid={request_counter}&rt=c"
        )
        rpc_cookie_header = serialize_cookie_header_for_url(
            cookie_jar.export_storage(), url
        )
        if not rpc_cookie_header:
            raise RuntimeError("Flow frontend RPC requires Google session cookies")
        headers = {
            "Accept": "*/*",
            "Accept-Language": self._get_primary_accept_language(
                fallback="en-US,en;q=0.9"
            ),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Cookie": rpc_cookie_header,
            "Origin": "https://flow.google.com",
            "Referer": "https://flow.google.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-same-domain": "1",
        }
        f_request = self._compact_json_dumps(
            [[[rpc_id, self._compact_json_dumps(argument), None, "generic"]]]
        )
        request_fields = {"f.req": f_request}
        at_token = self._extract_stream_chat_at_token(bootstrap_text)
        if at_token:
            request_fields["at"] = at_token
        request_body = urlencode(request_fields)
        response_text = await self._make_text_request(
            method="POST",
            url=url,
            headers=headers,
            raw_body=request_body,
            timeout=timeout,
            apply_default_client_headers=False,
            return_error_response=True,
            redact_sensitive_logs=True,
            cookie_jar=cookie_jar,
        )
        xsrf_match = re.search(r'\["xsrf"\s*,\s*"((?:\\.|[^"\\])*)"', response_text)
        if xsrf_match:
            request_fields["at"] = json.loads('"' + xsrf_match.group(1) + '"')
            response_text = await self._make_text_request(
                method="POST",
                url=url,
                headers=headers,
                raw_body=urlencode(request_fields),
                timeout=timeout,
                apply_default_client_headers=False,
                return_error_response=True,
                redact_sensitive_logs=True,
                cookie_jar=cookie_jar,
            )

        frames = self._parse_batchexecute_frames(response_text)
        self._raise_batchexecute_error(frames, rpc_id)
        payload = self._extract_batchexecute_payload(frames, rpc_id)
        if payload is None:
            raise RuntimeError(f"Flow frontend RPC returned no payload: rpc={rpc_id}")
        self._remember_flow_cookies(original_cookies, cookie_jar)
        return payload

    @staticmethod
    def _extract_stream_chat_at_token(bootstrap_text: str) -> str:
        """从 bootstrap HTML 提取 WIZ_global_data.SNlM0e（XSRF at token）。"""
        for pattern in (
            r'"SNlM0e"\s*:\s*"((?:\\.|[^"\\])+)"',
            r"SNlM0e['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_:\-.]+)['\"]",
        ):
            match = re.search(pattern, bootstrap_text)
            if match:
                try:
                    return json.loads('"' + match.group(1) + '"')
                except ValueError:
                    return match.group(1)
        return ""

    def _build_stream_chat_request(
        self,
        *,
        project_id: str,
        prompt: str,
        recaptcha_token: str,
        session_id: str,
    ) -> str:
        """构造 StreamChat f.req（结构复刻自新版前端真实请求）。

        outer = [null, "<inner json>"]
        inner = [session_id, [[[[prompt]]]],
                 ["projects/<pid>", null, [token, 1], null, null, 3]]
        """
        inner = [
            str(session_id),
            [[[[str(prompt)]]]],
            [f"projects/{project_id}", None, [recaptcha_token, 1], None, None, 1],
        ]
        return self._compact_json_dumps(
            [None, self._compact_json_dumps(inner)]
        )

    @classmethod
    def _extract_stream_chat_media_ids(cls, payloads: Any) -> List[str]:
        """从 StreamChat 响应帧中提取 generate_image 结果的 media_id。

        batchexecute 帧形如 ["wrb.fr", null, "<inner json>"]，业务数据在
        内层 JSON 字符串里，需要先 json.loads 再递归查找
        ["media_id", [null, null, "<uuid>"]] 模式。
        """
        found: List[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, list):
                if (
                    len(value) == 2
                    and value[0] == "media_id"
                    and isinstance(value[1], list)
                    and len(value[1]) > 2
                    and cls._is_uuid(value[1][2])
                ):
                    found.append(value[1][2])
                    return
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)

        for payload in payloads if isinstance(payloads, list) else [payloads]:
            if isinstance(payload, list):
                for item in payload:
                    if (
                        isinstance(item, list)
                        and len(item) >= 3
                        and item[0] == "wrb.fr"
                        and isinstance(item[2], str)
                    ):
                        try:
                            walk(json.loads(item[2]))
                        except (TypeError, ValueError):
                            continue
            walk(payload)
        return list(dict.fromkeys(found))

    async def _call_flow_stream_chat(
        self,
        *,
        project_id: str,
        prompt: str,
        recaptcha_token: str,
        session_id: str,
        cookie_header: str,
        timeout: int,
        cookie_storage: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新版 Flow 图片生成入口：FlowCreationAgentService/StreamChat。"""
        page_url = f"https://flow.google.com/project/{project_id}"
        bootstrap_cookie_header = (
            serialize_cookie_header_for_url(cookie_storage, page_url)
            if str(cookie_storage or "").strip()
            else cookie_header
        )
        if not bootstrap_cookie_header:
            raise RuntimeError("Flow StreamChat requires Google session cookies")
        original_cookies = (
            cookie_storage if str(cookie_storage or "").strip() else cookie_header
        )
        cookie_jar = FlowCookieJar(self.get_updated_flow_cookies(original_cookies))
        bootstrap_text = await self._load_flow_frontend_bootstrap(
            page_url,
            bootstrap_cookie_header,
            timeout,
            cookie_jar=cookie_jar,
        )
        self._remember_flow_cookies(original_cookies, cookie_jar)
        bootloader_match = re.search(
            r"boq_labs-ai-sandbox-frontend_[A-Za-z0-9_.-]+",
            bootstrap_text,
        )
        session_match = re.search(r'"FdrFJe":"(-?[0-9]+)"', bootstrap_text)
        if not bootloader_match or not session_match:
            raise RuntimeError("Flow frontend bootstrap metadata is unavailable")
        at_token = self._extract_stream_chat_at_token(bootstrap_text)
        if not at_token:
            raise RuntimeError("Flow frontend bootstrap XSRF token is unavailable")

        url = (
            f"https://flow.google.com{self.STREAM_CHAT_RPC_PATH}"
            f"?bl={quote(bootloader_match.group(0), safe='')}"
            f"&f.sid={session_match.group(1)}&hl=en"
            f"&_reqid={random.randint(100000, 999999)}&rt=c"
        )
        rpc_cookie_header = serialize_cookie_header_for_url(
            cookie_jar.export_storage(), url
        )
        if not rpc_cookie_header:
            raise RuntimeError("Flow StreamChat requires Google session cookies")
        headers = {
            "Accept": "*/*",
            "Accept-Language": self._get_primary_accept_language(
                fallback="en-US,en;q=0.9"
            ),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Cookie": rpc_cookie_header,
            "Origin": "https://flow.google.com",
            "Referer": "https://flow.google.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-client-deadline-ms": "600000",
            "x-same-domain": "1",
        }
        f_request = self._build_stream_chat_request(
            project_id=project_id,
            prompt=prompt,
            recaptcha_token=recaptcha_token,
            session_id=session_id,
        )
        request_body = (
            "f.req="
            + quote(f_request, safe="")
            + "&at="
            + quote(at_token, safe="")
        )
        response_text = await self._make_text_request(
            method="POST",
            url=url,
            headers=headers,
            raw_body=request_body,
            timeout=timeout,
            apply_default_client_headers=False,
            return_error_response=True,
            redact_sensitive_logs=True,
            cookie_jar=cookie_jar,
        )
        frames = self._parse_batchexecute_frames(response_text)
        error_match = re.search(r"PUBLIC_ERROR_[A-Z_]+", response_text)
        if error_match:
            raise RuntimeError(
                f"Flow StreamChat rejected: error={error_match.group(0)}"
            )
        media_ids = self._extract_stream_chat_media_ids(frames)
        if not media_ids:
            debug_logger.log_warning(
                "[StreamChat] response contained no media id "
                f"(response_chars={len(response_text)}, frames={len(frames)})"
            )
        media_urls = list(
            dict.fromkeys(
                url
                for url in re.findall(
                    r"https://flow-content\.google/(?:image|video)/[^\"\\\s]+",
                    response_text,
                )
            )
        )
        self._remember_flow_cookies(original_cookies, cookie_jar)
        return {
            "frames": frames,
            "mediaIds": media_ids,
            "mediaUrls": media_urls,
            "rawLength": len(response_text),
            "responseText": response_text,
        }

    @classmethod
    def _extract_flow_project_id(cls, payload: Any) -> str:
        if isinstance(payload, str) and cls._is_uuid(payload):
            return payload
        if isinstance(payload, list):
            for item in payload:
                project_id = cls._extract_flow_project_id(item)
                if project_id:
                    return project_id
        return ""
