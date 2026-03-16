"""Proxy management module"""
from typing import Optional
import re
from ..core.database import Database
from ..core.models import ProxyConfig

class ProxyManager:
    """Proxy configuration manager"""

    def __init__(self, db: Database):
        self.db = db

    def _parse_proxy_line(self, line: str) -> Optional[str]:
        """Convert user-input proxy to standard URL format.

        Supported formats:
        - http://user:pass@host:port
        - https://user:pass@host:port
        - socks5://user:pass@host:port
        - socks5h://user:pass@host:port
        - socks5://host:port:user:pass
        - st5 host:port:user:pass
        - host:port
        - host:port:user:pass
        """
        if not line:
            return None

        line = line.strip()
        if not line:
            return None

        # st5 host:port:user:pass
        st5_match = re.match(r"^st5\s+(.+)$", line, re.IGNORECASE)
        if st5_match:
            rest = st5_match.group(1).strip()
            if "@" in rest:
                return f"socks5://{rest}"
            parts = rest.split(":")
            if len(parts) >= 4 and parts[1].isdigit():
                host = parts[0]
                port = parts[1]
                username = parts[2]
                password = ":".join(parts[3:])
                return f"socks5://{username}:{password}@{host}:{port}"
            return None

        # Protocol prefix format
        if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
            # Convert socks5h to socks5 for unified handling
            if line.startswith("socks5h://"):
                line = "socks5://" + line[len("socks5h://"):]

            # Already standard user:pass@host:port (or host:port)
            if "@" in line:
                return line

            # Compatible with protocol://host:port:user:pass
            try:
                protocol_end = line.index("://") + 3
                protocol = line[:protocol_end]
                rest = line[protocol_end:]
                parts = rest.split(":")
                if len(parts) >= 4 and parts[1].isdigit():
                    host = parts[0]
                    port = parts[1]
                    username = parts[2]
                    password = ":".join(parts[3:])
                    return f"{protocol}{username}:{password}@{host}:{port}"
                if len(parts) == 2 and parts[1].isdigit():
                    return line
            except Exception:
                return None
            return None

        # No protocol, has @: default to http
        if "@" in line:
            return f"http://{line}"

        # No protocol, determine by colon count
        parts = line.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            # host:port
            return f"http://{parts[0]}:{parts[1]}"

        if len(parts) >= 4 and parts[1].isdigit():
            # host:port:user:pass
            host = parts[0]
            port = parts[1]
            username = parts[2]
            password = ":".join(parts[3:])
            return f"http://{username}:{password}@{host}:{port}"

        return None

    def normalize_proxy_url(self, proxy_url: Optional[str]) -> Optional[str]:
        """Normalize proxy URL; returns None for empty, raises ValueError for invalid format."""
        if proxy_url is None:
            return None

        raw = proxy_url.strip()
        if not raw:
            return None

        parsed = self._parse_proxy_line(raw)
        if not parsed:
            raise ValueError(
                "Invalid proxy URL format. Supported examples: "
                "http://user:pass@host:port / "
                "socks5://user:pass@host:port / "
                "host:port:user:pass / st5 host:port:user:pass"
            )
        return parsed

    async def get_proxy_url(self) -> Optional[str]:
        """Backward compatible: return request proxy URL"""
        return await self.get_request_proxy_url()

    async def get_request_proxy_url(self) -> Optional[str]:
        """Get request proxy URL if enabled, otherwise return None"""
        config = await self.db.get_proxy_config()
        if config and config.enabled and config.proxy_url:
            return config.proxy_url
        return None

    async def get_media_proxy_url(self) -> Optional[str]:
        """Get media upload/download proxy URL, fallback to request proxy"""
        config = await self.db.get_proxy_config()
        if config and config.media_proxy_enabled and config.media_proxy_url:
            return config.media_proxy_url
        return await self.get_request_proxy_url()

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str],
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None
    ):
        """Update proxy configuration"""
        normalized_proxy_url = self.normalize_proxy_url(proxy_url)
        normalized_media_proxy_url = self.normalize_proxy_url(media_proxy_url)

        await self.db.update_proxy_config(
            enabled=enabled,
            proxy_url=normalized_proxy_url,
            media_proxy_enabled=media_proxy_enabled,
            media_proxy_url=normalized_media_proxy_url
        )

    async def get_proxy_config(self) -> ProxyConfig:
        """Get proxy configuration"""
        return await self.db.get_proxy_config()
