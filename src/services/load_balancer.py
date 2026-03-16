"""Load balancing module for Flow2API"""
import asyncio
import random
from typing import Optional, Dict
from ..core.models import Token
from ..core.account_tiers import (
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .concurrency_manager import ConcurrencyManager
from ..core.logger import debug_logger


class LoadBalancer:
    """Token load balancer with load-aware selection"""

    def __init__(self, token_manager, concurrency_manager: Optional[ConcurrencyManager] = None):
        self.token_manager = token_manager
        self.concurrency_manager = concurrency_manager
        self._image_pending: Dict[int, int] = {}
        self._video_pending: Dict[int, int] = {}
        self._pending_lock = asyncio.Lock()

    async def _get_pending_count(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> int:
        async with self._pending_lock:
            if for_image_generation:
                return max(0, int(self._image_pending.get(token_id, 0)))
            if for_video_generation:
                return max(0, int(self._video_pending.get(token_id, 0)))
            return 0

    async def _add_pending(self, token_id: int, for_image_generation: bool, for_video_generation: bool):
        async with self._pending_lock:
            if for_image_generation:
                self._image_pending[token_id] = max(0, int(self._image_pending.get(token_id, 0))) + 1
            elif for_video_generation:
                self._video_pending[token_id] = max(0, int(self._video_pending.get(token_id, 0))) + 1

    async def release_pending(self, token_id: int, for_image_generation: bool = False, for_video_generation: bool = False):
        async with self._pending_lock:
            if for_image_generation:
                current = max(0, int(self._image_pending.get(token_id, 0)))
                if current <= 1:
                    self._image_pending.pop(token_id, None)
                else:
                    self._image_pending[token_id] = current - 1
            elif for_video_generation:
                current = max(0, int(self._video_pending.get(token_id, 0)))
                if current <= 1:
                    self._video_pending.pop(token_id, None)
                else:
                    self._video_pending[token_id] = current - 1

    async def _get_token_load(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> tuple[int, Optional[int]]:
        """Get token current load.

        Returns:
            (inflight, remaining)
            remaining is None means unlimited
        """
        if not self.concurrency_manager:
            return 0, None

        if for_image_generation:
            inflight = await self.concurrency_manager.get_image_inflight(token_id)
            remaining = await self.concurrency_manager.get_image_remaining(token_id)
            pending = await self._get_pending_count(token_id, True, False)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        if for_video_generation:
            inflight = await self.concurrency_manager.get_video_inflight(token_id)
            remaining = await self.concurrency_manager.get_video_remaining(token_id)
            pending = await self._get_pending_count(token_id, False, True)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        return 0, None

    async def _reserve_slot(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> bool:
        """Try to reserve a generation slot for the current token."""
        if not self.concurrency_manager:
            return True

        if for_image_generation:
            return await self.concurrency_manager.acquire_image(token_id)

        if for_video_generation:
            return await self.concurrency_manager.acquire_video(token_id)

        return True

    async def select_token(
        self,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        reserve: bool = False,
        enforce_concurrency_filter: bool = True,
        track_pending: bool = False,
    ) -> Optional[Token]:
        """
        Select a token using load-aware balancing

        Args:
            for_image_generation: If True, only select tokens with image_enabled=True
            for_video_generation: If True, only select tokens with video_enabled=True
            model: Model name (used to filter tokens for specific models)
            reserve: Whether to atomically reserve one concurrency slot for the selected token
            enforce_concurrency_filter:
                Whether to pre-filter tokens by current inflight/remaining capacity.
                For reserve=False generation paths, this should usually be False so
                requests can enter the downstream wait queue instead of failing fast.
            track_pending:
                Whether to count the selected token as a queued request immediately.
                This smooths burst distribution before the hard concurrency slot is acquired.

        Returns:
            Selected token or None if no available tokens
        """
        debug_logger.log_info(
            f"[LOAD_BALANCER] Starting selectionToken (image generation={for_image_generation}, "
            f"video generation={for_video_generation}, model={model}, reserve_slot={reserve})"
        )

        active_tokens = await self.token_manager.get_active_tokens()
        debug_logger.log_info(f"[LOAD_BALANCER] Found {len(active_tokens)} active tokens")

        if not active_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ No active tokens")
            return None

        available_tokens = []
        filtered_reasons = {}
        required_tier = get_required_paygate_tier_for_model(model)

        for token in active_tokens:
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                filtered_reasons[token.id] = 'Account tier insufficient, requires ' + get_paygate_tier_label(required_tier)
                continue
            if for_image_generation:
                if not token.image_enabled:
                    filtered_reasons[token.id] = "image generationdisabled"
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_image(token.id)
                ):
                    filtered_reasons[token.id] = "Image concurrency full"
                    continue

            if for_video_generation:
                if not token.video_enabled:
                    filtered_reasons[token.id] = "video generationdisabled"
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_video(token.id)
                ):
                    filtered_reasons[token.id] = "Video concurrency full"
                    continue

            inflight, remaining = await self._get_token_load(
                token.id,
                for_image_generation=for_image_generation,
                for_video_generation=for_video_generation
            )
            available_tokens.append({
                "token": token,
                "inflight": inflight,
                "remaining": remaining,
                "random": random.random()
            })

        if filtered_reasons:
            debug_logger.log_info(f"[LOAD_BALANCER] Filtered tokens:")
            for token_id, reason in filtered_reasons.items():
                debug_logger.log_info(f"[LOAD_BALANCER]   - Token {token_id}: {reason}")

        if not available_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ No available token (image generation={for_image_generation}, video generation={for_video_generation})")
            return None

        # Lowest in-flight first; with concurrency limit, prefer tokens with more remaining slots; then random
        available_tokens.sort(
            key=lambda item: (
                item["inflight"],
                0 if item["remaining"] is None else 1,
                -(item["remaining"] or 0),
                item["random"]
            )
        )

        debug_logger.log_info("[LOAD_BALANCER] Candidate token loads:")
        for item in available_tokens:
            token = item["token"]
            remaining = "unlimited" if item["remaining"] is None else item["remaining"]
            debug_logger.log_info(
                f"[LOAD_BALANCER]   - Token {token.id} ({token.email}) "
                f"inflight={item['inflight']}, remaining={remaining}, credits={token.credits}"
            )

        # Only check AT for tokens actually tried in candidate list, avoiding scanning all tokens per request
        for item in available_tokens:
            token = item["token"]
            token_id = token.id

            token = await self.token_manager.ensure_valid_token(token)
            if not token:
                debug_logger.log_info(f"[LOAD_BALANCER] Skipping token {token_id}: AT invalid or expired")
                continue

            if reserve and not await self._reserve_slot(token.id, for_image_generation, for_video_generation):
                debug_logger.log_info(f"[LOAD_BALANCER] Skipping token {token.id}: slot reservation failed")
                continue

            debug_logger.log_info(
                f"[LOAD_BALANCER] ✅ Token selected {token.id} ({token.email}) - "
                f"credits: {token.credits}, inflight={item['inflight']}"
            )
            return token

        debug_logger.log_info(f"[LOAD_BALANCER] ❌ All candidate tokens unavailable (image_generation={for_image_generation}, video_generation={for_video_generation})")
        return None
