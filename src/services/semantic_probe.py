"""Semantic probe service for auto-inferring generation parameters via configurable Chat API."""
import json
import re
from typing import Optional, Dict, Any
from curl_cffi.requests import AsyncSession

from ..core.config import config
from ..core.logger import debug_logger


class SemanticProbeService:
    """Use external Chat API to infer aspect_ratio/resolution/video_type/quality from prompt."""

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None

        # Try direct JSON first
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        # Try fenced JSON block
        match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
        if match:
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        # Try first object in text
        match = re.search(r"(\{[\s\S]*\})", text)
        if match:
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        return None

    async def infer(
        self,
        prompt: str,
        has_images: bool,
        current: Dict[str, Optional[str]]
    ) -> Dict[str, Optional[str]]:
        """Infer missing params. Returns empty dict if probe disabled/unavailable/failed."""
        if not config.semantic_probe_enabled:
            return {}

        api_url = config.semantic_probe_api_url
        api_key = config.semantic_probe_api_key
        model = config.semantic_probe_model

        if not api_url or not model:
            return {}

        system_prompt = (
            "你是参数推断器。请根据用户生成意图输出JSON，字段仅包含："
            "aspect_ratio,resolution,quality,video_type。\n"
            "取值约束：\n"
            "- aspect_ratio: landscape|portrait|square|four-three|three-four\n"
            "- resolution: 2k|4k|1080p|null\n"
            "- quality: standard|ultra|ultra_relaxed|null\n"
            "- video_type: t2v|i2v|r2v|null\n"
            "只输出JSON，不要解释。"
        )

        user_prompt = {
            "prompt": prompt,
            "has_images": has_images,
            "current": current
        }

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)}
            ],
            "temperature": 0,
            "stream": False
        }

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with AsyncSession() as session:
                resp = await session.post(
                    api_url,
                    headers=headers,
                    json=payload,
                    timeout=config.semantic_probe_timeout,
                    impersonate="chrome110"
                )

                if resp.status_code >= 400:
                    debug_logger.log_warning(f"[SEMANTIC_PROBE] HTTP {resp.status_code}: {resp.text[:300]}")
                    return {}

                result = resp.json()
                content = (
                    result.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )

                data = self._extract_json(content)
                if not data:
                    return {}

                return {
                    "aspect_ratio": data.get("aspect_ratio"),
                    "resolution": data.get("resolution"),
                    "quality": data.get("quality"),
                    "video_type": data.get("video_type")
                }
        except Exception as e:
            debug_logger.log_warning(f"[SEMANTIC_PROBE] 推断失败，已回退本地逻辑: {str(e)}")
            return {}
