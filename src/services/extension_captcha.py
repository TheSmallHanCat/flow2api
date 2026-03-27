"""
ExtensionCaptchaService — 浏览器插件 WebSocket 打码服务

通过 WebSocket 与 Chrome Extension 通信，按需获取 reCAPTCHA token。
协议: JSON 格式
- auth: 客户端认证
- solve: 服务端发送求解请求
- result: 客户端返回 token
- ping/pong: 心跳保活
"""

import asyncio
import uuid
import logging
from typing import Optional, Dict

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ExtensionCaptchaService:
    """浏览器插件打码服务（单例）"""

    _instance: Optional["ExtensionCaptchaService"] = None

    def __init__(self):
        # 已连接的 WebSocket 客户端列表
        self._clients: list[WebSocket] = []
        # 等待响应的请求：{request_id: asyncio.Future}
        self._pending: Dict[str, asyncio.Future] = {}
        # 认证密钥（空字符串表示不认证）
        self._auth_key: str = ""
        # 已认证的客户端集合
        self._authed: set[int] = set()
        # 轮询索引（多客户端负载均衡）
        self._round_robin_idx: int = 0
        # 统计
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
    def has_clients(self) -> bool:
        """是否有可用的插件客户端"""
        if not self._auth_key:
            return len(self._clients) > 0
        return len(self._authed) > 0

    # ==================== WebSocket 连接管理 ====================

    async def handle_websocket(self, websocket: WebSocket):
        """处理 WebSocket 连接（由 FastAPI 路由调用）"""
        await websocket.accept()
        client_id = id(websocket)
        self._clients.append(websocket)
        self.stats["clients"] = len(self._clients)
        logger.info(f"[ExtCaptcha] 插件客户端连接: {client_id}, 当前 {len(self._clients)} 个")

        try:
            while True:
                data = await websocket.receive_json()
                await self._handle_message(websocket, data)
        except WebSocketDisconnect:
            logger.info(f"[ExtCaptcha] 插件客户端断开: {client_id}")
        except Exception as e:
            logger.error(f"[ExtCaptcha] WebSocket 错误: {e}")
        finally:
            self._clients = [c for c in self._clients if c is not websocket]
            self._authed.discard(client_id)
            self.stats["clients"] = len(self._clients)

    async def _handle_message(self, ws: WebSocket, msg: dict):
        """处理来自插件的消息"""
        msg_type = msg.get("type")
        client_id = id(ws)

        if msg_type == "auth":
            key = msg.get("key", "")
            if not self._auth_key or key == self._auth_key:
                self._authed.add(client_id)
                await ws.send_json({"type": "auth_ok"})
                logger.info(f"[ExtCaptcha] 客户端 {client_id} 认证成功")
            else:
                await ws.send_json({"type": "auth_fail", "error": "密钥错误"})
                logger.warning(f"[ExtCaptcha] 客户端 {client_id} 认证失败")

        elif msg_type == "ping":
            await ws.send_json({"type": "pong"})

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
            action: reCAPTCHA action 名称 (IMAGE_GENERATION / VIDEO_GENERATION)
            site_key: 站点密钥
            timeout: 超时秒数

        Returns:
            reCAPTCHA token 字符串

        Raises:
            RuntimeError: 无可用客户端、超时或求解失败
        """
        if not self.has_clients:
            raise RuntimeError("无可用的浏览器插件客户端")

        ws = self._pick_client()
        if ws is None:
            raise RuntimeError("无已认证的浏览器插件客户端")

        req_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        self.stats["total_requests"] += 1

        try:
            # 发送 solve 请求给插件
            await ws.send_json({
                "type": "solve",
                "id": req_id,
                "action": action,
                "siteKey": site_key,
            })

            # 等待结果
            token = await asyncio.wait_for(future, timeout=timeout)
            self.stats["success"] += 1
            logger.info(f"[ExtCaptcha] Token 获取成功, 长度: {len(token) if token else 0}")
            return token

        except asyncio.TimeoutError:
            self.stats["errors"] += 1
            raise RuntimeError(f"插件求解超时 ({timeout}s)")
        except Exception:
            self.stats["errors"] += 1
            raise
        finally:
            # 无论成功、超时、异常或取消，统一清理 pending 条目
            self._pending.pop(req_id, None)

    # ==================== 客户端选择（Round-Robin） ====================

    def _pick_client(self) -> Optional[WebSocket]:
        """轮询选择可用的客户端"""
        if not self._auth_key:
            if not self._clients:
                return None
            self._round_robin_idx = self._round_robin_idx % len(self._clients)
            client = self._clients[self._round_robin_idx]
            self._round_robin_idx += 1
            return client

        authed_clients = [c for c in self._clients if id(c) in self._authed]
        if not authed_clients:
            return None
        self._round_robin_idx = self._round_robin_idx % len(authed_clients)
        client = authed_clients[self._round_robin_idx]
        self._round_robin_idx += 1
        return client
