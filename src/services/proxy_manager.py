"""Proxy management module"""
import asyncio
from typing import Optional
from ..core.database import Database
from ..core.models import ProxyConfig

class ProxyManager:
    """Proxy configuration manager with pool support"""

    def __init__(self, db: Database):
        self.db = db
        self._current_index = 0
        self._lock = asyncio.Lock()

    async def get_proxy_url(self) -> Optional[str]:
        """Get proxy URL - supports both single proxy and pool rotation"""
        # Check if pool is enabled
        pool_config = await self.db.get_proxy_pool_config()
        
        if pool_config.get("pool_enabled"):
            # Use proxy pool with rotation
            return await self._get_next_pool_proxy()
        else:
            # Use single proxy config
            config = await self.db.get_proxy_config()
            if config and config.enabled and config.proxy_url:
                return config.proxy_url
            return None

    async def _get_next_pool_proxy(self) -> Optional[str]:
        """Get next proxy from pool using round-robin"""
        async with self._lock:
            proxies = await self.db.get_enabled_proxy_pool_items()
            if not proxies:
                return None
            
            # Round-robin selection
            proxy = proxies[self._current_index % len(proxies)]
            self._current_index = (self._current_index + 1) % len(proxies)
            
            return proxy.get("proxy_url")

    async def get_proxy_with_id(self) -> tuple[Optional[str], Optional[int]]:
        """Get proxy URL with its ID for tracking usage"""
        pool_config = await self.db.get_proxy_pool_config()
        
        if pool_config.get("pool_enabled"):
            async with self._lock:
                proxies = await self.db.get_enabled_proxy_pool_items()
                if not proxies:
                    return None, None
                
                proxy = proxies[self._current_index % len(proxies)]
                self._current_index = (self._current_index + 1) % len(proxies)
                
                return proxy.get("proxy_url"), proxy.get("id")
        else:
            config = await self.db.get_proxy_config()
            if config and config.enabled and config.proxy_url:
                return config.proxy_url, None
            return None, None

    async def record_proxy_result(self, proxy_id: Optional[int], success: bool):
        """Record proxy usage result for statistics"""
        if proxy_id is not None:
            await self.db.record_proxy_usage(proxy_id, success)

    async def update_proxy_config(self, enabled: bool, proxy_url: Optional[str]):
        """Update proxy configuration"""
        await self.db.update_proxy_config(enabled, proxy_url)

    async def get_proxy_config(self) -> ProxyConfig:
        """Get proxy configuration"""
        return await self.db.get_proxy_config()

    # ========== Proxy Pool Management ==========

    async def add_pool_proxy(self, proxy_url: str, name: str = None) -> int:
        """Add a proxy to the pool"""
        return await self.db.add_proxy_pool_item(proxy_url, name)

    async def get_all_pool_proxies(self) -> list:
        """Get all proxies in the pool"""
        return await self.db.get_all_proxy_pool_items()

    async def update_pool_proxy(self, proxy_id: int, **kwargs):
        """Update a proxy in the pool"""
        await self.db.update_proxy_pool_item(proxy_id, **kwargs)

    async def delete_pool_proxy(self, proxy_id: int):
        """Delete a proxy from the pool"""
        await self.db.delete_proxy_pool_item(proxy_id)

    async def get_pool_config(self) -> dict:
        """Get proxy pool configuration"""
        return await self.db.get_proxy_pool_config()

    async def update_pool_config(self, pool_enabled: bool = None, rotation_mode: str = None):
        """Update proxy pool configuration"""
        await self.db.update_proxy_pool_config(pool_enabled, rotation_mode)

    async def batch_update_proxies(self, proxy_urls: list):
        """Batch update proxies - clear all and add new ones"""
        await self.db.batch_update_proxy_pool(proxy_urls)
