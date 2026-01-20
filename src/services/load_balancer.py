"""Load balancing module for Flow2API"""
import random
from typing import Optional
from ..core.models import Token
from .concurrency_manager import ConcurrencyManager
from ..core.logger import debug_logger


class LoadBalancer:
    """Token load balancer with random selection"""

    def __init__(self, token_manager, concurrency_manager: Optional[ConcurrencyManager] = None):
        self.token_manager = token_manager
        self.concurrency_manager = concurrency_manager

    async def select_token(
        self,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None
    ) -> Optional[Token]:
        """
        Select a token using random load balancing

        Args:
            for_image_generation: If True, only select tokens with image_enabled=True
            for_video_generation: If True, only select tokens with video_enabled=True
            model: Model name (used to filter tokens for specific models like 2K/4K)

        Returns:
            Selected token or None if no available tokens
        """
        debug_logger.log_info(f"[LOAD_BALANCER] 开始选择Token (图片生成={for_image_generation}, 视频生成={for_video_generation}, 模型={model})")

        active_tokens = await self.token_manager.get_active_tokens()
        debug_logger.log_info(f"[LOAD_BALANCER] 获取到 {len(active_tokens)} 个活跃Token")

        if not active_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有活跃的Token")
            return None

        # Filter tokens based on generation type
        available_tokens = []
        filtered_reasons = {}  # 记录过滤原因

        for token in active_tokens:
            # Check if token has valid AT (not expired)
            if not await self.token_manager.is_at_valid(token.id):
                filtered_reasons[token.id] = "AT无效或已过期"
                continue

            # Check 2K/4K model restrictions
            if model:
                if "-2k" in model.lower() and not token.enable_2k:
                    filtered_reasons[token.id] = "未启用2K模型"
                    continue
                if "-4k" in model.lower() and not token.enable_4k:
                    filtered_reasons[token.id] = "未启用4K模型"
                    continue

            # Filter for image generation
            if for_image_generation:
                if not token.image_enabled:
                    filtered_reasons[token.id] = "图片生成已禁用"
                    continue

                # Check concurrency limit
                if self.concurrency_manager and not await self.concurrency_manager.can_use_image(token.id):
                    filtered_reasons[token.id] = "图片并发已满"
                    continue

            # Filter for video generation
            if for_video_generation:
                if not token.video_enabled:
                    filtered_reasons[token.id] = "视频生成已禁用"
                    continue

                # Check concurrency limit
                if self.concurrency_manager and not await self.concurrency_manager.can_use_video(token.id):
                    filtered_reasons[token.id] = "视频并发已满"
                    continue

            available_tokens.append(token)

        # 输出过滤信息
        if filtered_reasons:
            debug_logger.log_info(f"[LOAD_BALANCER] 已过滤Token:")
            for token_id, reason in filtered_reasons.items():
                debug_logger.log_info(f"[LOAD_BALANCER]   - Token {token_id}: {reason}")

        if not available_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有可用的Token (图片生成={for_image_generation}, 视频生成={for_video_generation})")
            return None

        # Random selection
        selected = random.choice(available_tokens)
        debug_logger.log_info(f"[LOAD_BALANCER] ✅ 已选择Token {selected.id} ({selected.email}) - 余额: {selected.credits}")
        return selected
