"""Load balancing module for Flow2API"""

import asyncio
import random
import time
from typing import Optional, Dict, Iterable
from ..core.models import Token
from ..core.config import config
from ..core.account_tiers import (
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .concurrency_manager import ConcurrencyManager
from ..core.logger import debug_logger

# 配额耗尽 (PUBLIC_ERROR_USER_QUOTA_REACHED) 后降低该 token 优先级的最长时长；
# 期间余额回升（充值/额度刷新）会提前解除
QUOTA_COOLDOWN_SECONDS = 6 * 3600
# 风控拒绝 (PUBLIC_ERROR_UNUSUAL_ACTIVITY) 后降低该 token 优先级的时长：
# 连续被拒按倍数递增 (5m -> 15m -> 45m -> 60m)，成功一次即重置
RISK_COOLDOWN_SECONDS = 300
RISK_COOLDOWN_MULTIPLIER = 3
RISK_COOLDOWN_MAX_SECONDS = 3600


class LoadBalancer:
    """Token load balancer with load-aware selection"""

    def __init__(
        self, token_manager, concurrency_manager: Optional[ConcurrencyManager] = None
    ):
        self.token_manager = token_manager
        self.concurrency_manager = concurrency_manager
        self._image_pending: Dict[int, int] = {}
        self._video_pending: Dict[int, int] = {}
        self._pending_lock = asyncio.Lock()
        self._round_robin_state: Dict[str, Optional[int]] = {
            "image": None,
            "video": None,
            "default": None,
        }
        self._rr_lock = asyncio.Lock()
        # (token_id, "image"/"video") -> monotonic expiry; quota-exhausted tokens sort last
        self._quota_cooldown: Dict[tuple[int, str], float] = {}
        # 配额耗尽时记录的余额；余额高于该值说明已充值，解除冷却
        self._quota_credits: Dict[tuple[int, str], int] = {}
        # 连续风控次数，用于递增风控冷却
        self._risk_streak: Dict[tuple[int, str], int] = {}

    @staticmethod
    def _scope(for_image_generation: bool, for_video_generation: bool) -> Optional[str]:
        if for_image_generation:
            return "image"
        if for_video_generation:
            return "video"
        return None

    def mark_quota_cooldown(
        self,
        token_id: int,
        scope: str,
        seconds: float = QUOTA_COOLDOWN_SECONDS,
        credits: Optional[int] = None,
    ):
        """配额耗尽后让该 token 排在候选末尾（不剔除，避免全部冷却时无号可用）。

        传入当时的余额后，余额回升即提前解除冷却。
        """
        key = (token_id, scope)
        self._quota_cooldown[key] = time.monotonic() + seconds
        if credits is None:
            self._quota_credits.pop(key, None)
        else:
            self._quota_credits[key] = credits
        debug_logger.log_warning(
            f"[LOAD_BALANCER] Token {token_id} {scope} 配额耗尽 (余额: {credits})，"
            f"冷却 {int(seconds)}s 内优先使用其他账号"
        )

    def mark_risk_cooldown(
        self, token_id: int, scope: str, seconds: Optional[float] = None
    ) -> float:
        """风控拒绝后让该 token 排在候选末尾，连续被拒时冷却递增；不会缩短已有的配额冷却。"""
        key = (token_id, scope)
        streak = self._risk_streak.get(key, 0) + 1
        self._risk_streak[key] = streak
        if seconds is None:
            seconds = min(
                RISK_COOLDOWN_MAX_SECONDS,
                RISK_COOLDOWN_SECONDS * RISK_COOLDOWN_MULTIPLIER ** (streak - 1),
            )
        expires_at = time.monotonic() + seconds
        if self._quota_cooldown.get(key, 0) < expires_at:
            self._quota_cooldown[key] = expires_at
        debug_logger.log_warning(
            f"[LOAD_BALANCER] Token {token_id} {scope} 连续第 {streak} 次触发风控，"
            f"冷却 {int(seconds)}s 内优先使用其他账号"
        )
        return seconds

    def clear_quota_cooldown(self, token_id: int, scope: Optional[str] = None):
        for state in (self._quota_cooldown, self._quota_credits, self._risk_streak):
            for key in list(state):
                if key[0] == token_id and (scope is None or key[1] == scope):
                    state.pop(key, None)

    def _in_quota_cooldown(
        self, token_id: int, scope: Optional[str], credits: Optional[int] = None
    ) -> bool:
        if scope is None:
            return False
        key = (token_id, scope)
        expires_at = self._quota_cooldown.get(key)
        if expires_at is None:
            return False
        if time.monotonic() >= expires_at:
            self._quota_cooldown.pop(key, None)
            self._quota_credits.pop(key, None)
            return False
        exhausted_credits = self._quota_credits.get(key)
        if (
            exhausted_credits is not None
            and credits is not None
            and credits > exhausted_credits
        ):
            # 余额比耗尽时高，说明已充值或额度已刷新
            self._quota_cooldown.pop(key, None)
            self._quota_credits.pop(key, None)
            return False
        return True

    async def _get_pending_count(
        self, token_id: int, for_image_generation: bool, for_video_generation: bool
    ) -> int:
        async with self._pending_lock:
            if for_image_generation:
                return max(0, int(self._image_pending.get(token_id, 0)))
            if for_video_generation:
                return max(0, int(self._video_pending.get(token_id, 0)))
            return 0

    async def _add_pending(
        self, token_id: int, for_image_generation: bool, for_video_generation: bool
    ):
        async with self._pending_lock:
            if for_image_generation:
                self._image_pending[token_id] = (
                    max(0, int(self._image_pending.get(token_id, 0))) + 1
                )
            elif for_video_generation:
                self._video_pending[token_id] = (
                    max(0, int(self._video_pending.get(token_id, 0))) + 1
                )

    async def release_pending(
        self,
        token_id: int,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
    ):
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

    async def _get_token_load(
        self, token_id: int, for_image_generation: bool, for_video_generation: bool
    ) -> tuple[int, Optional[int]]:
        """获取 token 当前负载。

        Returns:
            (inflight, remaining)
            remaining 为 None 表示无限制
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

    async def _reserve_slot(
        self, token_id: int, for_image_generation: bool, for_video_generation: bool
    ) -> bool:
        """尝试为当前 token 预占一个生成槽位。"""
        if not self.concurrency_manager:
            return True

        if for_image_generation:
            return await self.concurrency_manager.acquire_image(token_id)

        if for_video_generation:
            return await self.concurrency_manager.acquire_video(token_id)

        return True

    async def _select_round_robin(
        self, tokens: list[dict], scenario: str
    ) -> Optional[dict]:
        """Select candidate in round-robin order for the given scenario."""
        if not tokens:
            return None

        tokens_sorted = sorted(tokens, key=lambda item: item["token"].id or 0)
        async with self._rr_lock:
            last_id = self._round_robin_state.get(scenario)
            start_idx = 0
            if last_id is not None:
                for idx, item in enumerate(tokens_sorted):
                    if item["token"].id == last_id:
                        start_idx = (idx + 1) % len(tokens_sorted)
                        break
            selected = tokens_sorted[start_idx]
            self._round_robin_state[scenario] = selected["token"].id
        return selected

    async def _check_extension_route(self, token: Token) -> tuple[bool, str]:
        """Ensure extension captcha requests are routed to the selected account."""
        if config.captcha_method != "extension":
            return True, ""

        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            service = await ExtensionCaptchaService.get_instance(
                getattr(self.token_manager, "db", None)
            )
            has_connection, route_key = await service.has_connection_for_token(token.id)
            if has_connection:
                return True, ""

            available = service.describe_routes() or "none"
            if route_key:
                return False, f"扩展路由 {route_key} 未连接（可用路由: {available}）"
            return False, f"扩展路由未配置或匿名插件未连接（可用路由: {available}）"
        except Exception as exc:
            return False, f"扩展路由检查失败: {exc}"

    async def select_token(
        self,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        reserve: bool = False,
        enforce_concurrency_filter: bool = True,
        track_pending: bool = False,
        exclude_token_ids: Optional[Iterable[int]] = None,
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
            exclude_token_ids: Tokens to skip (e.g. ones that already failed this request).

        Returns:
            Selected token or None if no available tokens
        """
        debug_logger.log_info(
            f"[LOAD_BALANCER] 开始选择Token (图片生成={for_image_generation}, "
            f"视频生成={for_video_generation}, 模型={model}, 预占槽位={reserve})"
        )

        active_tokens = await self.token_manager.get_active_tokens()
        debug_logger.log_info(
            f"[LOAD_BALANCER] 获取到 {len(active_tokens)} 个活跃Token"
        )

        if not active_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有活跃的Token")
            return None

        available_tokens = []
        filtered_reasons = {}
        required_tier = get_required_paygate_tier_for_model(model)
        excluded = set(exclude_token_ids or ())
        scope = self._scope(for_image_generation, for_video_generation)

        for token in active_tokens:
            if token.id in excluded:
                filtered_reasons[token.id] = "本次请求已失败过"
                continue
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                filtered_reasons[token.id] = (
                    "账号等级不足，需要 " + get_paygate_tier_label(required_tier)
                )
                continue
            if for_image_generation:
                if not token.image_enabled:
                    filtered_reasons[token.id] = "图片生成已禁用"
                    continue

                route_ok, route_reason = await self._check_extension_route(token)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_image(token.id)
                ):
                    filtered_reasons[token.id] = "图片并发已满"
                    continue

            if for_video_generation:
                if not token.video_enabled:
                    filtered_reasons[token.id] = "视频生成已禁用"
                    continue

                route_ok, route_reason = await self._check_extension_route(token)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_video(token.id)
                ):
                    filtered_reasons[token.id] = "视频并发已满"
                    continue

            inflight, remaining = await self._get_token_load(
                token.id,
                for_image_generation=for_image_generation,
                for_video_generation=for_video_generation,
            )
            available_tokens.append(
                {
                "token": token,
                "inflight": inflight,
                "remaining": remaining,
                "needs_refresh": self.token_manager.needs_at_refresh(token),
                    "quota_cooldown": self._in_quota_cooldown(
                        token.id, scope, getattr(token, "credits", None)
                    ),
                    "random": random.random(),
                }
            )

        if filtered_reasons:
            debug_logger.log_info(f"[LOAD_BALANCER] 已过滤Token:")
            for token_id, reason in filtered_reasons.items():
                debug_logger.log_info(f"[LOAD_BALANCER]   - Token {token_id}: {reason}")

        if not available_tokens:
            debug_logger.log_info(
                f"[LOAD_BALANCER] ❌ 没有可用的Token (图片生成={for_image_generation}, 视频生成={for_video_generation})"
            )
            return None

        # 最低 in-flight 优先；有并发上限时，剩余槽位更多的 token 优先；最后随机打散
        call_mode = config.call_logic_mode
        if call_mode == "polling":
            scenario = "default"
            if for_image_generation:
                scenario = "image"
            elif for_video_generation:
                scenario = "video"

            ordered_candidates = []
            round_robin_candidates = [
                item for item in available_tokens if not item["quota_cooldown"]
            ] or available_tokens
            first_candidate = await self._select_round_robin(
                round_robin_candidates, scenario
            )
            if first_candidate is not None:
                ordered_candidates.append(first_candidate)
                ordered_candidates.extend(
                    item
                    for item in sorted(
                        available_tokens, key=lambda item: item["token"].id or 0
                    )
                    if item["token"].id != first_candidate["token"].id
                )
            available_tokens = ordered_candidates
        else:
            available_tokens.sort(
                key=lambda item: (
                    1 if item["needs_refresh"] else 0,
                    item["inflight"],
                    0 if item["remaining"] is None else 1,
                    -(item["remaining"] or 0),
                    item["random"],
                )
            )

        ready_candidates = [
            item for item in available_tokens if not item["needs_refresh"]
        ]
        refresh_candidates = [
            item for item in available_tokens if item["needs_refresh"]
        ]
        if ready_candidates and refresh_candidates:
            available_tokens = ready_candidates + refresh_candidates

        # 配额冷却中的 token 排到最后（稳定排序，保持上面的顺序）
        available_tokens.sort(key=lambda item: 1 if item["quota_cooldown"] else 0)

        debug_logger.log_info("[LOAD_BALANCER] 候选Token负载:")
        for item in available_tokens:
            token = item["token"]
            remaining = "unlimited" if item["remaining"] is None else item["remaining"]
            debug_logger.log_info(
                f"[LOAD_BALANCER]   - Token {token.id} ({token.email}) "
                f"inflight={item['inflight']}, remaining={remaining}, "
                f"needs_refresh={item['needs_refresh']}, "
                f"quota_cooldown={item['quota_cooldown']}, credits={token.credits}"
            )

        # 只为候选列表中真正尝试到的 token 做 AT 校验，避免每次请求把所有 token 全扫一遍
        for item in available_tokens:
            token = item["token"]
            token_id = token.id

            token = await self.token_manager.ensure_valid_token(token)
            if not token:
                debug_logger.log_info(
                    f"[LOAD_BALANCER] 跳过 Token {token_id}: AT无效或已过期"
                )
                continue

            if reserve and not await self._reserve_slot(
                token.id, for_image_generation, for_video_generation
            ):
                debug_logger.log_info(
                    f"[LOAD_BALANCER] 跳过 Token {token.id}: 预占槽位失败"
                )
                continue

            if track_pending:
                await self._add_pending(
                    token.id, for_image_generation, for_video_generation
                )

            debug_logger.log_info(
                f"[LOAD_BALANCER] ✅ 已选择Token {token.id} ({token.email}) - "
                f"余额: {token.credits}, inflight={item['inflight']}"
            )
            return token

        debug_logger.log_info(
            f"[LOAD_BALANCER] ❌ 候选Token均不可用 (图片生成={for_image_generation}, 视频生成={for_video_generation})"
        )
        return None

    async def get_unavailable_reason(
        self,
        *,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """给出更明确的“无可用账号”原因，优先用于分辨率/tier 档位提示。"""
        active_tokens = await self.token_manager.get_active_tokens()
        if not active_tokens:
            return None

        required_tier = get_required_paygate_tier_for_model(model)
        supported_tokens = []
        for token in active_tokens:
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                continue
            supported_tokens.append(token)

        if model and not supported_tokens:
            tier_label = get_paygate_tier_label(required_tier)
            return f"当前模型需要 {tier_label} 账号，但没有可用的 {tier_label} 账号: {model}"

        capability_tokens = []
        for token in supported_tokens:
            if for_image_generation and not token.image_enabled:
                continue
            if for_video_generation and not token.video_enabled:
                continue
            capability_tokens.append(token)

        if supported_tokens and not capability_tokens:
            if for_image_generation:
                return "当前有符合档位的账号，但图片生成功能已全部禁用。"
            if for_video_generation:
                return "当前有符合档位的账号，但视频生成功能已全部禁用。"

        return None
