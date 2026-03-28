"""
ExtensionCaptchaService — 浏览器插件 WebSocket 打码服务

通过 WebSocket 与 Chrome Extension 通信，按需获取 reCAPTCHA token。
协议: JSON 格式
- auth: 客户端认证
- solve: 服务端发送求解请求
- result: 客户端返回 token
- config: 服务端下发配置
- ping/pong: 心跳保活
"""

import asyncio
import uuid
import time
import logging
from typing import Optional, Dict, Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ClientInfo:
    """单个 WebSocket 客户端的元数据"""

    def __init__(self, ws: WebSocket, client_id: int):
        self.ws = ws
        self.client_id = client_id
        # 客户端上报的名称（Docker 实例 ID / 手动标注）
        self.name: str = ""
        # 来源标识：docker / manual
        self.source: str = "unknown"
        # 连接时间
        self.connected_at: float = time.time()
        # 是否启用（管理员可禁用）
        self.enabled: bool = True
        # 已认证
        self.authed: bool = False
        # 统计
        self.solve_count: int = 0
        self.error_count: int = 0


class ExtensionCaptchaService:
    """浏览器插件打码服务（单例）"""

    _instance: Optional["ExtensionCaptchaService"] = None

    def __init__(self):
        # 客户端信息：{client_id: ClientInfo}
        self._clients: Dict[int, ClientInfo] = {}
        # 等待响应的请求：{request_id: asyncio.Future}
        self._pending: Dict[str, asyncio.Future] = {}
        # 认证密钥（空字符串表示不认证）
        self._auth_key: str = ""
        # 轮询索引
        self._round_robin_idx: int = 0
        # 标签页刷新配置
        self._refresh_interval: int = 2
        # 全局统计
        self.stats = {"total_requests": 0, "success": 0, "errors": 0, "clients": 0}

    @classmethod
    def get_instance(cls) -> "ExtensionCaptchaService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_auth_key(self, key: str):
        """设置认证密钥"""
        self._auth_key = key or ""

    @property
    def refresh_interval(self) -> int:
        return self._refresh_interval

    @refresh_interval.setter
    def refresh_interval(self, value: int):
        self._refresh_interval = max(1, value)

    @property
    def has_clients(self) -> bool:
        """是否有可用的插件客户端"""
        return any(c.enabled for c in self._get_authed_clients())

    def get_clients_info(self) -> list[Dict[str, Any]]:
        """获取所有客户端信息（供管理后台展示）"""
        result = []
        for info in self._clients.values():
            result.append({
                "client_id": info.client_id,
                "name": info.name or f"client-{info.client_id}",
                "source": info.source,
                "enabled": info.enabled,
                "authed": info.authed,
                "connected_at": info.connected_at,
                "uptime_seconds": int(time.time() - info.connected_at),
                "solve_count": info.solve_count,
                "error_count": info.error_count,
            })
        return result

    def set_client_enabled(self, client_id: int, enabled: bool) -> bool:
        """启用/禁用指定客户端"""
        info = self._clients.get(client_id)
        if info is None:
            return False
        info.enabled = enabled
        logger.info(f"[ExtCaptcha] 客户端 {client_id} {'启用' if enabled else '禁用'}")
        return True

    async def broadcast_config(self):
        """向所有客户端下发当前配置"""
        config_msg = {
            "type": "config",
            "refreshInterval": self._refresh_interval,
        }
        for info in self._clients.values():
            try:
                await info.ws.send_json(config_msg)
            except Exception:
                pass

    # ==================== WebSocket 连接管理 ====================

    async def handle_websocket(self, websocket: WebSocket):
        """处理 WebSocket 连接（由 FastAPI 路由调用）"""
        await websocket.accept()
        client_id = id(websocket)
        info = ClientInfo(websocket, client_id)

        # 从 URL query params 解析客户端元数据
        query_params = dict(websocket.query_params)
        info.name = query_params.get("name", "")
        info.source = query_params.get("source", "unknown")

        # 无需认证时默认已认证
        if not self._auth_key:
            info.authed = True

        self._clients[client_id] = info
        self.stats["clients"] = len(self._clients)
        logger.info(f"[ExtCaptcha] 插件客户端连接: {client_id} "
                    f"(name={info.name}, source={info.source}), "
                    f"当前 {len(self._clients)} 个")

        # 连接后立即下发配置
        try:
            await websocket.send_json({
                "type": "config",
                "refreshInterval": self._refresh_interval,
            })
        except Exception:
            pass

        try:
            while True:
                data = await websocket.receive_json()
                await self._handle_message(info, data)
        except WebSocketDisconnect:
            logger.info(f"[ExtCaptcha] 插件客户端断开: {client_id}")
        except Exception as e:
            logger.error(f"[ExtCaptcha] WebSocket 错误: {e}")
        finally:
            self._clients.pop(client_id, None)
            self.stats["clients"] = len(self._clients)

    async def _handle_message(self, client: ClientInfo, msg: dict):
        """处理来自插件的消息"""
        msg_type = msg.get("type")

        if msg_type == "auth":
            key = msg.get("key", "")
            if not self._auth_key or key == self._auth_key:
                client.authed = True
                await client.ws.send_json({"type": "auth_ok"})
                logger.info(f"[ExtCaptcha] 客户端 {client.client_id} 认证成功")
            else:
                await client.ws.send_json({"type": "auth_fail", "error": "密钥错误"})
                logger.warning(f"[ExtCaptcha] 客户端 {client.client_id} 认证失败")

        elif msg_type == "ping":
            await client.ws.send_json({"type": "pong"})

        elif msg_type == "result":
            req_id = msg.get("id")
            if req_id and req_id in self._pending:
                future = self._pending.pop(req_id)
                if not future.done():
                    if msg.get("error"):
                        future.set_exception(RuntimeError(msg["error"]))
                    else:
                        future.set_result(msg.get("token"))

    # ==================== Token 获取 ====================

    async def get_token(
        self,
        action: str = "IMAGE_GENERATION",
        site_key: str = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
        timeout: float = 15.0,
    ) -> str:
        """
        向插件请求 reCAPTCHA token

        Args:
            action: reCAPTCHA action 名称
            site_key: 站点密钥
            timeout: 超时秒数

        Returns:
            reCAPTCHA token 字符串

        Raises:
            RuntimeError: 无可用客户端、超时或求解失败
        """
        if not self.has_clients:
            raise RuntimeError("无可用的浏览器插件客户端")

        client = self._pick_client()
        if client is None:
            raise RuntimeError("无已认证且启用的浏览器插件客户端")

        req_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        self.stats["total_requests"] += 1

        try:
            await client.ws.send_json({
                "type": "solve",
                "id": req_id,
                "action": action,
                "siteKey": site_key,
            })

            token = await asyncio.wait_for(future, timeout=timeout)
            self.stats["success"] += 1
            client.solve_count += 1
            logger.info(f"[ExtCaptcha] Token 获取成功, 长度: {len(token) if token else 0}")
            return token

        except asyncio.TimeoutError:
            self.stats["errors"] += 1
            client.error_count += 1
            raise RuntimeError(f"插件求解超时 ({timeout}s)")
        except Exception:
            self.stats["errors"] += 1
            client.error_count += 1
            raise
        finally:
            self._pending.pop(req_id, None)

    # ==================== 客户端选择（Round-Robin） ====================

    def _get_authed_clients(self) -> list[ClientInfo]:
        """获取已认证的客户端列表"""
        if not self._auth_key:
            return list(self._clients.values())
        return [c for c in self._clients.values() if c.authed]

    def _pick_client(self) -> Optional[ClientInfo]:
        """轮询选择可用的客户端（已认证 + 已启用）"""
        available = [c for c in self._get_authed_clients() if c.enabled]
        if not available:
            return None
        self._round_robin_idx = self._round_robin_idx % len(available)
        client = available[self._round_robin_idx]
        self._round_robin_idx += 1
        return client
