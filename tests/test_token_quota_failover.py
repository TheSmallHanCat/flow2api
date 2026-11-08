import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from src.core.config import config
from src.services.flow_client import FlowClient

from src.services.generation_handler import (
    MAX_RISK_FAILOVER_ATTEMPTS,
    MAX_TOKEN_FAILOVER_ATTEMPTS,
    GenerationHandler,
)
from src.services.load_balancer import (
    RISK_COOLDOWN_MAX_SECONDS,
    RISK_COOLDOWN_SECONDS,
    LoadBalancer,
)
from src.services.token_manager import TokenManager


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


def _build_handler(tokens, quota_token_ids, error=QUOTA_ERROR, errors_by_token=None):
    token_manager = MagicMock()
    token_manager.get_active_tokens = AsyncMock(return_value=tokens)
    token_manager.ensure_valid_token = AsyncMock(side_effect=lambda t: t)
    token_manager.ensure_project_exists = AsyncMock(return_value="project-1")
    token_manager.needs_at_refresh = MagicMock(return_value=False)
    token_manager.record_error = AsyncMock()
    token_manager.record_usage = AsyncMock()
    token_manager.record_success = AsyncMock()
    token_manager.refresh_credits = AsyncMock(return_value=10)

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
        if errors_by_token and token.id in errors_by_token:
            raise Exception(errors_by_token[token.id])
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
        tokens = [_token(i) for i in range(1, MAX_TOKEN_FAILOVER_ATTEMPTS + 3)]
        handler, attempted = _build_handler(tokens, {t.id for t in tokens})

        chunks = await _collect(handler)

        self.assertEqual(len(attempted), MAX_TOKEN_FAILOVER_ATTEMPTS)
        self.assertEqual(len(set(attempted)), MAX_TOKEN_FAILOVER_ATTEMPTS)
        error = json.loads(chunks[-1])["error"]
        self.assertIn("PUBLIC_ERROR_USER_QUOTA_REACHED", error["message"])
        self.assertIn(f"已尝试 {MAX_TOKEN_FAILOVER_ATTEMPTS} 个账号均失败", error["message"])
        self.assertEqual(handler._log_request.await_args.args[4], 500)
        # 最后一个失败账号也要记录在性能追踪里
        perf = handler._log_request.await_args.args[3]["performance"]
        self.assertEqual(len(perf["token_failovers"]), MAX_TOKEN_FAILOVER_ATTEMPTS)

    async def test_quota_error_records_refreshed_credits(self):
        handler, _ = _build_handler([_token(1), _token(2)], {1})

        with patch("src.services.load_balancer.random.random", side_effect=[0.1, 0.9] * 4):
            await _collect(handler)

        handler.token_manager.refresh_credits.assert_awaited_once_with(
            1, raise_on_error=True
        )
        lb = handler.load_balancer
        self.assertTrue(lb._in_quota_cooldown(1, "video", 10))
        # 余额回升（充值/额度刷新）后提前解除冷却
        self.assertFalse(lb._in_quota_cooldown(1, "video", 500))

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

    async def test_risk_failures_have_their_own_budget(self):
        tokens = [_token(i) for i in range(1, 7)]
        handler, attempted = _build_handler(
            tokens, set(), errors_by_token={t.id: UNUSUAL_ERROR for t in tokens}
        )

        chunks = await _collect(handler)

        self.assertEqual(len(attempted), MAX_RISK_FAILOVER_ATTEMPTS)
        error = json.loads(chunks[-1])["error"]["message"]
        self.assertIn("PUBLIC_ERROR_UNUSUAL_ACTIVITY", error)
        self.assertIn("触发风控", error)

    async def test_quota_failures_do_not_use_risk_budget(self):
        # 复现线上请求：两个账号风控、一个账号额度不足，第四个账号可用
        errors = {7: UNUSUAL_ERROR, 9: UNUSUAL_ERROR, 1: QUOTA_ERROR}
        tokens = [_token(i) for i in (7, 9, 1, 5)]
        handler, attempted = _build_handler(tokens, set(), errors_by_token=errors)

        # 固定打散顺序为 7 -> 9 -> 1 -> 5（每轮只对未排除的候选取随机数）
        order = [0.1, 0.2, 0.3, 0.9, 0.2, 0.3, 0.9, 0.3, 0.9, 0.9]
        with patch("src.services.load_balancer.random.random", side_effect=order):
            chunks = await _collect(handler)

        self.assertEqual(attempted, [7, 9, 1, 5])
        self.assertNotIn("error", json.loads(chunks[-1]))

    async def test_failure_summary_lists_each_account(self):
        errors = {7: UNUSUAL_ERROR, 1: QUOTA_ERROR}
        handler, _ = _build_handler(
            [_token(7), _token(1)], set(), errors_by_token=errors
        )

        chunks = await _collect(handler)

        error = json.loads(chunks[-1])["error"]["message"]
        self.assertIn("已尝试 2 个账号均失败", error)
        self.assertIn("#7 触发风控", error)
        self.assertIn("#1 配额耗尽", error)

    async def test_risk_cooldown_escalates_and_resets_on_success(self):
        handler, _ = _build_handler([_token(1)], set())
        lb = handler.load_balancer
        durations = [lb.mark_risk_cooldown(1, "video") for _ in range(5)]
        self.assertEqual(durations[0], RISK_COOLDOWN_SECONDS)
        self.assertGreater(durations[1], durations[0])
        self.assertEqual(durations[-1], RISK_COOLDOWN_MAX_SECONDS)

        lb.clear_quota_cooldown(1, "video")
        self.assertFalse(lb._in_quota_cooldown(1, "video"))
        self.assertEqual(lb.mark_risk_cooldown(1, "video"), RISK_COOLDOWN_SECONDS)

    async def test_uncertain_submission_is_not_failed_over(self):
        def uncertain():
            error = Exception(UNUSUAL_ERROR)
            error.submission_uncertain = True
            return error

        handler, attempted = _build_handler([_token(1), _token(2)], {1, 2}, uncertain)

        chunks = await _collect(handler)

        self.assertEqual(len(attempted), 1)
        self.assertIn("PUBLIC_ERROR_UNUSUAL_ACTIVITY", json.loads(chunks[-1])["error"]["message"])


class TokenFailoverIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_account_prepares_project_without_previous_fingerprint(self):
        for error in (QUOTA_ERROR, UNUSUAL_ERROR):
            with self.subTest(error=error):
                handler, attempted = _build_handler(
                    [_token(1), _token(2)], {1}, error
                )
                flow_client = FlowClient(proxy_manager=None)
                flow_client.prefill_remote_browser_pool = AsyncMock()
                handler.flow_client = flow_client
                project_fingerprints = {}

                async def prepare_project(token_id):
                    project_fingerprints[token_id] = (
                        flow_client.get_request_fingerprint()
                    )
                    return f"project-{token_id}"

                generate_video = handler._handle_video_generation

                async def video_with_fingerprint(token, *args, **kwargs):
                    flow_client._set_request_fingerprint(
                        {
                            "session_cookies": {"OSID": f"session-{token.id}"},
                            "proxy_url": f"http://proxy-{token.id}:8080",
                        }
                    )
                    async for chunk in generate_video(token, *args, **kwargs):
                        yield chunk

                handler.token_manager.ensure_project_exists = AsyncMock(
                    side_effect=prepare_project
                )
                handler._handle_video_generation = video_with_fingerprint
                with patch(
                    "src.services.load_balancer.random.random",
                    side_effect=[0.1, 0.9] * 4,
                ):
                    chunks = await _collect(handler)

                self.assertEqual(attempted, [1, 2])
                self.assertNotIn("error", json.loads(chunks[-1]))
                self.assertEqual(project_fingerprints, {1: None, 2: None})


class TokenCreditRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.token = _token(1)
        self.token.at = "unused"
        self.token.google_cookies = ""
        self.manager = TokenManager.__new__(TokenManager)
        self.manager.db = MagicMock()
        self.manager.db.get_token = AsyncMock(return_value=self.token)
        self.manager.db.update_token = AsyncMock()
        self.manager.ensure_valid_token = AsyncMock(return_value=self.token)
        self.manager.flow_client = MagicMock()
        self.manager.flow_client.get_credits = AsyncMock()
        self.handler = GenerationHandler.__new__(GenerationHandler)
        self.handler.token_manager = self.manager

    async def test_refresh_failure_keeps_cooldown_with_cached_balance(self):
        self.manager.flow_client.get_credits.side_effect = OSError("connection failed")

        credits = await self.handler._refresh_token_credits(self.token)
        lb = LoadBalancer(self.manager)
        lb.mark_quota_cooldown(self.token.id, "video", credits=credits)

        self.assertEqual(credits, self.token.credits)
        self.manager.db.update_token.assert_not_awaited()
        self.assertTrue(
            lb._in_quota_cooldown(self.token.id, "video", self.token.credits)
        )

    async def test_successful_refresh_preserves_real_zero_balance(self):
        self.manager.flow_client.get_credits.return_value = {
            "credits": 0,
            "userPaygateTier": "PAYGATE_TIER_ONE",
        }

        credits = await self.handler._refresh_token_credits(self.token)

        self.assertEqual(credits, 0)
        self.manager.db.update_token.assert_awaited_once_with(
            self.token.id, credits=0, user_paygate_tier="PAYGATE_TIER_ONE"
        )

    async def test_default_refresh_keeps_existing_failure_contract(self):
        self.manager.flow_client.get_credits.side_effect = OSError("connection failed")

        self.assertEqual(await self.manager.refresh_credits(self.token.id), 0)
        self.manager.db.update_token.assert_not_awaited()


class TokenCooldownPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_rotates_between_accounts_outside_cooldown(self):
        for cooldown_method in ("mark_quota_cooldown", "mark_risk_cooldown"):
            with self.subTest(cooldown_method=cooldown_method):
                handler, _ = _build_handler([_token(i) for i in range(1, 5)], set())
                lb = handler.load_balancer
                for token_id in (2, 3):
                    getattr(lb, cooldown_method)(token_id, "video")
                lb._round_robin_state["video"] = 1

                with patch.object(
                    type(config),
                    "call_logic_mode",
                    new_callable=PropertyMock,
                    return_value="polling",
                ):
                    selected = [
                        (await lb.select_token(for_video_generation=True)).id
                        for _ in range(4)
                    ]

                self.assertEqual(selected, [4, 1, 4, 1])

    async def test_polling_keeps_cooled_accounts_as_fallback(self):
        handler, _ = _build_handler([_token(1), _token(2)], set())
        lb = handler.load_balancer
        for token_id in (1, 2):
            lb.mark_quota_cooldown(token_id, "video")

        with patch.object(
            type(config),
            "call_logic_mode",
            new_callable=PropertyMock,
            return_value="polling",
        ):
            selected = [
                (await lb.select_token(for_video_generation=True)).id
                for _ in range(4)
            ]

        self.assertEqual(selected, [1, 2, 1, 2])


if __name__ == "__main__":
    unittest.main()
