import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.generation_handler import (
    MAX_TOKEN_FAILOVER_ATTEMPTS,
    GenerationHandler,
)
from src.services.load_balancer import LoadBalancer


MODEL = "veo-3.1-fast-i2v-6s-landscape"
QUOTA_ERROR = "Flow frontend RPC rejected: rpc=eb1hJf, error=PUBLIC_ERROR_USER_QUOTA_REACHED"
UNUSUAL_ERROR = "Flow frontend RPC rejected: rpc=eb1hJf, error=PUBLIC_ERROR_UNUSUAL_ACTIVITY"


def _token(token_id):
    return SimpleNamespace(
        id=token_id,
        email=f"user{token_id}@example.com",
        user_paygate_tier="PAYGATE_TIER_TWO",
        image_enabled=True,
        video_enabled=True,
        credits=100,
        image_concurrency=-1,
        video_concurrency=-1,
    )


def _build_handler(tokens, quota_token_ids, error=QUOTA_ERROR):
    token_manager = MagicMock()
    token_manager.get_active_tokens = AsyncMock(return_value=tokens)
    token_manager.ensure_valid_token = AsyncMock(side_effect=lambda t: t)
    token_manager.ensure_project_exists = AsyncMock(return_value="project-1")
    token_manager.needs_at_refresh = MagicMock(return_value=False)
    token_manager.record_error = AsyncMock()
    token_manager.record_usage = AsyncMock()
    token_manager.record_success = AsyncMock()

    load_balancer = LoadBalancer(token_manager)
    load_balancer._check_extension_route = AsyncMock(return_value=(True, ""))

    handler = GenerationHandler.__new__(GenerationHandler)
    handler.token_manager = token_manager
    handler.load_balancer = load_balancer
    handler.flow_client = MagicMock()
    handler.flow_client.prefill_remote_browser_pool = AsyncMock()
    handler._log_request = AsyncMock(return_value=1)
    handler._update_request_log_progress = AsyncMock()

    attempted = []

    async def fake_video(token, *args, generation_result=None, **kwargs):
        attempted.append(token.id)
        if token.id in quota_token_ids:
            raise error() if callable(error) else Exception(error)
        handler._mark_generation_succeeded(generation_result)
        yield handler._create_completion_response("http://video", media_type="video")

    handler._handle_video_generation = fake_video
    return handler, attempted


async def _collect(handler):
    with patch("src.services.generation_handler.record_generation_result"):
        return [chunk async for chunk in handler.handle_generation(MODEL, "prompt")]


class TokenQuotaFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_to_another_token_on_quota_error(self):
        handler, attempted = _build_handler([_token(1), _token(2)], {1})

        # 固定随机打散顺序，让 token 1 先被选中
        with patch("src.services.load_balancer.random.random", side_effect=[0.1, 0.9] * 4):
            chunks = await _collect(handler)

        self.assertEqual(attempted, [1, 2])
        self.assertIn("http://video", chunks[-1])
        self.assertNotIn("error", json.loads(chunks[-1]))
        # quota errors must not push the account toward auto-disable
        handler.token_manager.record_error.assert_not_awaited()
        self.assertTrue(handler.load_balancer._in_quota_cooldown(1, "video"))
        self.assertFalse(handler.load_balancer._in_quota_cooldown(2, "video"))

    async def test_gives_up_after_max_attempts(self):
        tokens = [_token(i) for i in range(1, 6)]
        handler, attempted = _build_handler(tokens, {t.id for t in tokens})

        chunks = await _collect(handler)

        self.assertEqual(len(attempted), MAX_TOKEN_FAILOVER_ATTEMPTS)
        self.assertEqual(len(set(attempted)), MAX_TOKEN_FAILOVER_ATTEMPTS)
        error = json.loads(chunks[-1])["error"]
        self.assertIn("PUBLIC_ERROR_USER_QUOTA_REACHED", error["message"])
        self.assertEqual(handler._log_request.await_args.args[4], 500)

    async def test_reports_quota_error_when_no_token_left(self):
        handler, attempted = _build_handler([_token(1), _token(2)], {1, 2})

        chunks = await _collect(handler)

        self.assertEqual(sorted(attempted), [1, 2])
        error = json.loads(chunks[-1])["error"]
        self.assertIn("PUBLIC_ERROR_USER_QUOTA_REACHED", error["message"])
        self.assertIn("已尝试 2 个账号", error["message"])

    async def test_cooled_down_token_is_tried_last(self):
        handler, attempted = _build_handler([_token(1), _token(2)], set())
        handler.load_balancer.mark_quota_cooldown(1, "video")

        with patch("src.services.load_balancer.random.random", side_effect=[0.1, 0.9] * 4):
            await _collect(handler)

        self.assertEqual(attempted, [2])


class TokenUnusualActivityFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_to_another_token_on_unusual_activity(self):
        handler, attempted = _build_handler([_token(1), _token(2)], {1}, UNUSUAL_ERROR)

        with patch("src.services.load_balancer.random.random", side_effect=[0.1, 0.9] * 4):
            chunks = await _collect(handler)

        self.assertEqual(attempted, [1, 2])
        self.assertNotIn("error", json.loads(chunks[-1]))
        handler.token_manager.record_error.assert_not_awaited()
        # 风控冷却较短，且同样让该 token 排到后面
        self.assertTrue(handler.load_balancer._in_quota_cooldown(1, "video"))

    async def test_risk_cooldown_does_not_shorten_quota_cooldown(self):
        handler, _ = _build_handler([_token(1)], set())
        lb = handler.load_balancer
        lb.mark_quota_cooldown(1, "video")
        quota_expiry = lb._quota_cooldown[(1, "video")]
        lb.mark_risk_cooldown(1, "video")
        self.assertEqual(lb._quota_cooldown[(1, "video")], quota_expiry)

    async def test_uncertain_submission_is_not_failed_over(self):
        def uncertain():
            error = Exception(UNUSUAL_ERROR)
            error.submission_uncertain = True
            return error

        handler, attempted = _build_handler([_token(1), _token(2)], {1, 2}, uncertain)

        chunks = await _collect(handler)

        self.assertEqual(len(attempted), 1)
        self.assertIn("PUBLIC_ERROR_UNUSUAL_ACTIVITY", json.loads(chunks[-1])["error"]["message"])


if __name__ == "__main__":
    unittest.main()
