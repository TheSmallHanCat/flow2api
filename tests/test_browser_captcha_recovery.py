"""Concurrency and request-lifetime coverage without contacting Google."""

import asyncio
import json
import os
import time
import types
import unittest
from unittest.mock import MagicMock, AsyncMock, PropertyMock, patch

with patch.dict(
    os.environ, {"DOCKER_CONTAINER": "1", "ALLOW_DOCKER_HEADED_CAPTCHA": "false"}
):
    from src.services.browser_captcha import (
        BrowserCaptchaQueueTimeout,
        BrowserCaptchaService,
        _SlotRecovery,
    )

from src.core.config import config
from src.services.flow_client import FlowClient


class BrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()
        self.service._check_available = lambda: None
        self.service.RECOVERY_BACKOFF_SECONDS = (0.01, 0.02, 0.03)
        self.browser = types.SimpleNamespace(
            _runtime_generation=0,
            _shared_bound_token_id=7,
            is_busy=lambda: False,
            has_shared_browser=lambda: True,
            get_last_fingerprint=lambda: {"user_agent": "agent"},
            submit_flow_request=AsyncMock(return_value={"status": 200, "text": "{}"}),
            force_close_pending_browser=AsyncMock(),
        )

        async def solve(*args, **kwargs):
            self.browser._shared_bound_token_id = kwargs.get("token_id")
            return "captcha", "FLOW-SESSION"

        async def recycle(**kwargs):
            self.browser._runtime_generation += 1
            self.browser._shared_bound_token_id = None

        self.browser.get_token = AsyncMock(side_effect=solve)
        self.browser.recycle_browser = AsyncMock(side_effect=recycle)
        self.service._browsers[0] = self.browser

    async def asyncTearDown(self):
        await self.service.close()

    async def lease(self):
        token, ref, _ = await self.service.get_token("project", token_id=7)
        self.assertEqual(token, "captcha")
        return ref

    async def test_all_busy_has_bounded_wait_without_extra_reservations(self):
        await self.service._select_browser_id("project", token_id=7)
        with self.assertRaises(BrowserCaptchaQueueTimeout):
            await self.service._select_browser_id("project", timeout_seconds=0.01)
        self.assertEqual(self.service._slot_reservations, {0: 1})
        self.assertEqual(self.service.get_stats()["queue_timeout_count"], 1)

    async def test_released_slot_wakes_waiter(self):
        await self.service._select_browser_id("project")
        waiter = asyncio.create_task(
            self.service._select_browser_id("project", timeout_seconds=0.2)
        )
        await asyncio.sleep(0)
        await self.service._release_slot_reservation(0)
        self.assertEqual(await waiter, 0)

    async def test_cancelling_waiter_does_not_release_another_reservation(self):
        await self.service._select_browser_id("project")
        waiter = asyncio.create_task(self.service._select_browser_id("project"))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(self.service._slot_reservations, {0: 1})

    async def test_token_lease_preserves_session_and_account_until_submission(self):
        ref = await self.lease()
        self.assertEqual(
            FlowClient._extract_browser_captcha_session_id(ref), "FLOW-SESSION"
        )
        with self.assertRaises(BrowserCaptchaQueueTimeout):
            await self.service._select_browser_id(
                "other", token_id=8, timeout_seconds=0.01
            )
        response, submitted_ref, _ = await self.service.submit_flow_request(
            "project",
            "IMAGE_GENERATION",
            7,
            "https://example.com",
            "access",
            {},
            1,
            browser_ref=ref,
        )
        self.assertEqual(response["status"], 200)
        self.assertEqual(submitted_ref, ref)
        self.assertFalse(self.service._requests)
        self.assertFalse(self.service._slot_reservations)
        self.browser.get_token.assert_awaited_once()
        await self.service.report_request_finished(ref)
        self.assertFalse(self.service._slot_reservations)

    async def test_error_isolates_only_failed_slot_and_merges_recovery(self):
        self.service._browser_count = 2
        ref = await self.lease()
        await self.service.report_error(ref, "HTTP 403: reCAPTCHA evaluation failed")
        state = self.service._slot_recovery[0]
        recovery = state.task
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        self.assertIs(state.task, recovery)
        self.assertEqual(self.service._stats["api_403"], 1)
        self.assertEqual(await self.service._select_browser_id("other", token_id=8), 1)
        self.browser.recycle_browser.assert_not_awaited()
        await self.service.report_request_finished(ref)
        await asyncio.wait_for(recovery, 0.2)
        self.assertEqual(state.phase, "cooldown")
        self.assertEqual(state.recovery_count, 1)
        self.browser.recycle_browser.assert_awaited_once()

    async def test_submission_can_reuse_lease_from_wait_for_child_task(self):
        ref = await self.lease()
        response, submitted_ref, _ = await asyncio.wait_for(
            self.service.submit_flow_request(
                "project",
                "IMAGE_GENERATION",
                7,
                "https://example.com",
                "access",
                {},
                1,
                browser_ref=ref,
            ),
            0.2,
        )
        self.assertEqual(response["status"], 200)
        self.assertEqual(submitted_ref, ref)
        self.assertFalse(self.service._requests)

    async def test_parent_finish_does_not_drop_active_child_submission(self):
        ref = await self.lease()
        entered = asyncio.Event()
        proceed = asyncio.Event()

        async def submit(**kwargs):
            entered.set()
            await proceed.wait()
            return {"status": 200, "text": "{}"}

        self.browser.submit_flow_request.side_effect = submit
        borrower = asyncio.create_task(
            self.service.submit_flow_request(
                "project",
                "IMAGE_GENERATION",
                7,
                "https://example.com",
                "access",
                {},
                1,
                browser_ref=ref,
            )
        )
        await entered.wait()
        await self.service.report_request_finished(ref)
        self.assertIn(ref, self.service._requests)
        proceed.set()
        await borrower
        self.assertFalse(self.service._requests)

    async def test_drain_timeout_keeps_live_request_and_recovers_after_release(self):
        self.service.DRAIN_WAIT_SECONDS = 0.01
        ref = await self.lease()
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        state = self.service._slot_recovery[0]
        await state.task
        self.assertEqual(state.phase, "draining")
        self.browser.recycle_browser.assert_not_awaited()
        self.assertIn(ref, self.service._requests)
        await self.service.report_request_finished(ref)
        await asyncio.wait_for(state.task, 0.2)
        self.browser.recycle_browser.assert_awaited_once()

    async def test_late_report_cannot_restart_reused_slot(self):
        ref = await self.lease()
        await self.service.report_request_finished(ref)
        new_ref = await self.lease()
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        self.assertFalse(self.service._slot_recovery)
        self.assertIn(new_ref, self.service._requests)

    async def test_changed_runtime_or_account_ignores_old_failure(self):
        ref = await self.lease()
        self.browser._runtime_generation += 1
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        self.assertFalse(self.service._slot_recovery)
        self.browser._runtime_generation -= 1
        self.browser._shared_bound_token_id = 8
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        self.assertFalse(self.service._slot_recovery)

    async def test_permission_and_authentication_errors_do_not_recycle(self):
        ref = await self.lease()
        for reason in (
            "HTTP 401",
            "HTTP 403: MODEL_ACCESS_DENIED",
            "content policy rejected",
            "Failed to obtain reCAPTCHA token",
        ):
            await self.service.report_error(ref, reason)
        self.assertFalse(self.service._slot_recovery)
        self.browser.recycle_browser.assert_not_awaited()

    async def test_cooldown_waits_then_allows_single_probe(self):
        self.service._slot_recovery[0] = _SlotRecovery(
            phase="cooldown",
            cooldown_until=time.monotonic() + 0.01,
            failure_streak=2,
        )
        ref = await self.lease()
        self.assertEqual(self.service._slot_recovery[0].phase, "probe")
        with self.assertRaises(BrowserCaptchaQueueTimeout):
            await self.service._select_browser_id("other", timeout_seconds=0.01)
        await self.service.report_request_finished(ref, success=True)
        self.assertEqual(self.service._slot_recovery[0].phase, "ready")
        self.assertEqual(self.service._slot_recovery[0].failure_streak, 0)

    async def test_repeated_probe_failure_increases_backoff(self):
        ref = await self.lease()
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        state = self.service._slot_recovery[0]
        recovery = state.task
        await self.service.report_request_finished(ref)
        await recovery
        ref = await self.lease()
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        recovery = state.task
        await self.service.report_request_finished(ref)
        await recovery
        self.assertEqual(state.failure_streak, 2)
        self.assertGreater(state.cooldown_until - time.monotonic(), 0.01)

    async def test_owner_cancellation_releases_lease_without_finish_callback(self):
        ready = asyncio.Event()

        async def generate():
            await self.lease()
            ready.set()
            await asyncio.Event().wait()

        owner = asyncio.create_task(generate())
        await ready.wait()
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        async with self.service._slot_condition:
            await asyncio.wait_for(
                self.service._slot_condition.wait_for(
                    lambda: not self.service._requests
                ),
                0.2,
            )
        self.assertFalse(self.service._slot_reservations)

    async def test_submission_response_marks_risk_before_slot_release(self):
        self.browser.submit_flow_request.return_value = {
            "status": 403,
            "text": '{"error":{"message":"reCAPTCHA evaluation failed"}}',
        }
        response, ref, _ = await self.service.submit_flow_request(
            "project",
            "IMAGE_GENERATION",
            7,
            "https://example.com",
            "access",
            {},
            1,
        )
        self.assertEqual(response["status"], 403)
        state = self.service._slot_recovery[0]
        if state.task:
            await state.task
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        self.assertEqual(self.service._stats["api_403"], 1)
        self.assertEqual(state.recovery_count, 1)

    async def test_submission_timeout_is_uncertain_and_does_not_replay(self):
        async def hang(**kwargs):
            await asyncio.Event().wait()

        self.browser.submit_flow_request.side_effect = hang
        with self.assertRaises(RuntimeError) as caught:
            await self.service.submit_flow_request(
                "project",
                "IMAGE_GENERATION",
                7,
                "https://example.com",
                "access",
                {},
                0.01,
            )
        self.assertTrue(caught.exception.submission_uncertain)
        self.browser.submit_flow_request.assert_awaited_once()
        self.assertFalse(self.service._requests)
        self.assertFalse(self.service._slot_reservations)

    async def test_shrink_preserves_active_removed_slot_until_release(self):
        self.service._browser_count = 2
        self.service._browsers = {1: self.browser}
        self.service._round_robin_index = 1
        ref = await self.lease()
        self.service.db = types.SimpleNamespace(
            get_captcha_config=AsyncMock(
                return_value=types.SimpleNamespace(browser_count=1),
            )
        )
        self.service._ensure_idle_reaper = AsyncMock()
        await self.service.reload_browser_count()
        self.browser.recycle_browser.assert_not_awaited()
        self.assertIn(1, self.service._browsers)
        recovery = self.service._slot_recovery[1].task
        await self.service.report_request_finished(ref)
        await recovery
        self.assertNotIn(1, self.service._browsers)

    async def test_shrink_still_retires_slot_after_drain_timeout_or_risk_error(self):
        for failure in ("drain_timeout", "recaptcha"):
            with self.subTest(failure=failure):
                self.service._slot_recovery.clear()
                self.browser._shared_bound_token_id = 7
                self.service._browser_count = 2
                self.service._browsers = {1: self.browser}
                self.service._round_robin_index = 1
                self.service.DRAIN_WAIT_SECONDS = 0.01
                ref = await self.lease()
                self.service.db = types.SimpleNamespace(
                    get_captcha_config=AsyncMock(
                        return_value=types.SimpleNamespace(browser_count=1),
                    )
                )
                self.service._ensure_idle_reaper = AsyncMock()
                await self.service.reload_browser_count()
                state = self.service._slot_recovery[1]
                if failure == "drain_timeout":
                    await asyncio.wait_for(state.task, 0.2)
                else:
                    await self.service.report_error(ref, "reCAPTCHA evaluation failed")
                await self.service.report_request_finished(ref)
                await asyncio.wait_for(state.task, 0.2)
                self.assertNotIn(1, self.service._browsers)
                self.assertNotIn(1, self.service._slot_recovery)
                self.assertEqual(
                    self.service.get_stats()["configured_browser_count"], 1
                )

    async def test_shrink_still_retires_slot_after_recycle_failure(self):
        self.service._browser_count = 2
        self.service._browsers = {1: self.browser}
        self.service._round_robin_index = 1
        ref = await self.lease()
        self.service.db = types.SimpleNamespace(
            get_captcha_config=AsyncMock(
                return_value=types.SimpleNamespace(browser_count=1)
            )
        )
        self.service._ensure_idle_reaper = AsyncMock()
        self.browser.recycle_browser.side_effect = [
            RuntimeError("browser close failed"),
            None,
        ]
        await self.service.reload_browser_count()
        state = self.service._slot_recovery[1]
        recovery = state.task
        await self.service.report_request_finished(ref)
        await asyncio.wait_for(recovery, 0.2)
        state.cooldown_until = time.monotonic() - 1
        self.assertEqual(await self.service._select_browser_id("project"), 0)
        await asyncio.wait_for(state.task, 0.2)
        self.assertNotIn(1, self.service._browsers)
        self.assertNotIn(1, self.service._slot_recovery)

    async def _prepare_failed_retirement(self, failure_mode="error"):
        self.service._browser_count = 2
        self.service._browsers = {1: self.browser}
        self.service._round_robin_index = 1
        self.service.MAINTENANCE_INTERVAL_SECONDS = 0.005
        self.service.RECOVERY_TIMEOUT_SECONDS = 0.01
        self.browser.idle_seconds = lambda: 0
        ref = await self.lease()
        self.service.db = types.SimpleNamespace(
            get_captcha_config=AsyncMock(
                return_value=types.SimpleNamespace(browser_count=1)
            )
        )

        async def recycle(**kwargs):
            if self.browser.recycle_browser.await_count == 1:
                if failure_mode == "timeout":
                    await asyncio.Event().wait()
                raise RuntimeError("browser close failed")
            self.browser._runtime_generation += 1
            self.browser._shared_bound_token_id = None

        self.browser.recycle_browser.side_effect = recycle
        await self.service.reload_browser_count()
        state = self.service._slot_recovery[1]
        recovery = state.task
        await self.service.report_request_finished(ref)
        await asyncio.wait_for(recovery, 0.2)
        return state

    async def _wait_for_retirement_without_requests(self):
        async with self.service._slot_condition:
            await asyncio.wait_for(
                self.service._slot_condition.wait_for(
                    lambda: 1 not in self.service._browsers
                ),
                0.3,
            )
        self.assertNotIn(1, self.service._slot_recovery)
        self.assertFalse(self.service._slot_reservations)
        self.assertEqual(self.browser.recycle_browser.await_count, 2)

    async def test_idle_maintenance_retries_failed_retirement_without_requests(self):
        await self._prepare_failed_retirement()
        await self._wait_for_retirement_without_requests()

    async def test_idle_maintenance_retries_timed_out_retirement_without_requests(self):
        await self._prepare_failed_retirement("timeout")
        await self._wait_for_retirement_without_requests()

    async def test_idle_maintenance_and_request_share_one_retirement_retry(self):
        state = await self._prepare_failed_retirement()
        retry_started = asyncio.Event()
        complete_retry = asyncio.Event()
        self.service.RECOVERY_TIMEOUT_SECONDS = 1

        async def recycle(**kwargs):
            retry_started.set()
            await complete_retry.wait()

        self.browser.recycle_browser.side_effect = recycle
        state.cooldown_until = time.monotonic() - 1
        self.assertEqual(await self.service._select_browser_id("project"), 0)
        await asyncio.wait_for(retry_started.wait(), 0.2)
        recovery = state.task
        await asyncio.sleep(0.03)
        self.assertIs(state.task, recovery)
        self.assertEqual(self.browser.recycle_browser.await_count, 2)
        complete_retry.set()
        await asyncio.wait_for(recovery, 0.2)
        self.assertNotIn(1, self.service._browsers)

    async def test_close_during_retirement_cooldown_stops_maintenance_retry(self):
        await self._prepare_failed_retirement()
        await self.service.close()
        attempts_after_close = self.browser.recycle_browser.await_count
        await asyncio.sleep(0.05)
        self.assertTrue(self.service._idle_reaper_task.done())
        self.assertFalse(self.service._browsers)
        self.assertEqual(self.browser.recycle_browser.await_count, attempts_after_close)

    async def test_expand_during_retirement_keeps_configured_slot(self):
        self.service._browser_count = 2
        self.service._browsers = {1: self.browser}
        self.service._round_robin_index = 1
        ref = await self.lease()
        self.service.db = types.SimpleNamespace(
            get_captcha_config=AsyncMock(
                side_effect=[
                    types.SimpleNamespace(browser_count=1),
                    types.SimpleNamespace(browser_count=2),
                ]
            )
        )
        self.service._ensure_idle_reaper = AsyncMock()
        self.service.warmup_browser_slots = AsyncMock()
        await self.service.reload_browser_count()
        state = self.service._slot_recovery[1]
        recovery = state.task
        await self.service.reload_browser_count()
        await self.service.report_request_finished(ref)
        await asyncio.wait_for(recovery, 0.2)
        self.assertIn(1, self.service._browsers)
        self.assertEqual(state.phase, "ready")
        self.assertFalse(state.retiring)

    async def test_expand_after_retiring_slot_risk_preserves_recovery_cooldown(self):
        self.service._browser_count = 2
        self.service._browsers = {1: self.browser}
        self.service._round_robin_index = 1
        ref = await self.lease()
        self.service.db = types.SimpleNamespace(
            get_captcha_config=AsyncMock(
                side_effect=[
                    types.SimpleNamespace(browser_count=1),
                    types.SimpleNamespace(browser_count=2),
                ]
            )
        )
        self.service._ensure_idle_reaper = AsyncMock()
        self.service.warmup_browser_slots = AsyncMock()
        await self.service.reload_browser_count()
        state = self.service._slot_recovery[1]
        recovery = state.task
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        await self.service.reload_browser_count()
        await self.service.report_request_finished(ref)
        await asyncio.wait_for(recovery, 0.2)
        self.assertIn(1, self.service._browsers)
        self.assertFalse(state.retiring)
        self.assertEqual(state.phase, "cooldown")
        self.assertEqual(state.failure_streak, 1)
        self.assertTrue(self.browser.recycle_browser.call_args.kwargs["rotate_profile"])

    async def test_repeated_shrink_expand_shrink_keeps_one_retirement_task(self):
        self.service._browser_count = 2
        self.service._browsers = {1: self.browser}
        self.service._round_robin_index = 1
        ref = await self.lease()
        self.service.db = types.SimpleNamespace(
            get_captcha_config=AsyncMock(
                side_effect=[
                    types.SimpleNamespace(browser_count=1),
                    types.SimpleNamespace(browser_count=2),
                    types.SimpleNamespace(browser_count=1),
                ]
            )
        )
        self.service._ensure_idle_reaper = AsyncMock()
        self.service.warmup_browser_slots = AsyncMock()
        await self.service.reload_browser_count()
        state = self.service._slot_recovery[1]
        recovery = state.task
        await self.service.reload_browser_count()
        await self.service.reload_browser_count()
        self.assertIs(state.task, recovery)
        await self.service.report_request_finished(ref)
        await asyncio.wait_for(recovery, 0.2)
        self.assertNotIn(1, self.service._browsers)
        self.assertNotIn(1, self.service._slot_recovery)
        self.browser.recycle_browser.assert_awaited_once()

    async def test_close_wakes_waiters_and_cancels_recovery(self):
        ref = await self.lease()
        await self.service.report_error(ref, "reCAPTCHA evaluation failed")
        recovery = self.service._slot_recovery[0].task
        waiter = asyncio.create_task(self.service._select_browser_id("project"))
        await asyncio.sleep(0)
        await self.service.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await waiter
        self.assertTrue(recovery.done())
        self.assertFalse(self.service._requests)

    async def test_probe_release_without_acceptance_does_not_reset_failures(self):
        self.service._slot_recovery[0] = _SlotRecovery(phase="probe", failure_streak=2)
        ref = await self.lease()
        await self.service.report_request_finished(ref)
        self.assertEqual(self.service._slot_recovery[0].phase, "probe")
        self.assertEqual(self.service._slot_recovery[0].failure_streak, 2)

    async def test_invalid_lease_can_be_replaced_without_lock_reentrancy(self):
        ref = await self.lease()
        self.browser._runtime_generation += 1
        response, new_ref, _ = await self.service.submit_flow_request(
            "project",
            "IMAGE_GENERATION",
            7,
            "https://example.com",
            "access",
            {},
            1,
            browser_ref=ref,
        )
        self.assertEqual(response["status"], 200)
        self.assertNotEqual(new_ref, ref)
        self.assertFalse(self.service._requests)

    async def test_close_before_registration_prevents_late_borrow(self):
        entered = asyncio.Event()
        proceed = asyncio.Event()
        original = self.service._get_or_create_browser

        async def delayed(slot_id):
            entered.set()
            await proceed.wait()
            return await original(slot_id)

        self.service._get_or_create_browser = delayed
        borrower = asyncio.create_task(self.lease())
        await entered.wait()
        await self.service.close()
        proceed.set()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await borrower
        self.assertFalse(self.service._browsers)
        self.assertFalse(self.service._requests)

    async def test_close_after_browser_lookup_prevents_registration(self):
        entered = asyncio.Event()
        proceed = asyncio.Event()
        original = self.service._get_or_create_browser

        async def delayed(slot_id):
            browser = await original(slot_id)
            entered.set()
            await proceed.wait()
            return browser

        self.service._get_or_create_browser = delayed
        borrower = asyncio.create_task(self.lease())
        await entered.wait()
        await self.service.close()
        proceed.set()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await borrower
        self.assertFalse(self.service._requests)
        self.browser.get_token.assert_not_awaited()

    async def test_close_cancels_inflight_owner(self):
        entered = asyncio.Event()

        async def solve(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.browser.get_token.side_effect = solve
        borrower = asyncio.create_task(self.lease())
        await entered.wait()
        await self.service.close()
        self.assertTrue(borrower.cancelled())
        self.assertFalse(self.service._requests)

    async def test_closed_singleton_can_be_created_again(self):
        with patch.object(BrowserCaptchaService, "_instance", self.service):
            await self.service.close()
            with (
                patch.object(
                    BrowserCaptchaService, "_load_browser_count", new=AsyncMock()
                ),
                patch.object(
                    BrowserCaptchaService, "_ensure_idle_reaper", new=AsyncMock()
                ),
            ):
                replacement = await BrowserCaptchaService.get_instance()
            self.assertIsNot(replacement, self.service)
            self.assertFalse(replacement._closed)
            await replacement.close()

    async def test_concurrent_callers_wait_for_singleton_initialization(self):
        entered = asyncio.Event()
        proceed = asyncio.Event()
        instances = []

        async def load(instance):
            instances.append(instance)
            entered.set()
            await proceed.wait()

        with (
            patch.object(BrowserCaptchaService, "_instance", None),
            patch.object(BrowserCaptchaService, "_lock", asyncio.Lock()),
            patch.object(BrowserCaptchaService, "_load_browser_count", load),
        ):
            first = asyncio.create_task(BrowserCaptchaService.get_instance())
            await entered.wait()
            second = asyncio.create_task(BrowserCaptchaService.get_instance())
            await asyncio.sleep(0)
            try:
                self.assertIsNone(BrowserCaptchaService._instance)
                self.assertFalse(second.done())
            finally:
                proceed.set()
                results = await asyncio.gather(first, second)
                await instances[0].close()
            self.assertIs(results[0], results[1])
            self.assertEqual(len(instances), 1)

    async def test_close_during_initialization_cannot_create_orphan_reaper(self):
        entered = asyncio.Event()
        proceed = asyncio.Event()
        instances = []

        async def load(instance):
            instances.append(instance)
            entered.set()
            await proceed.wait()

        with (
            patch.object(BrowserCaptchaService, "_instance", None),
            patch.object(BrowserCaptchaService, "_lock", asyncio.Lock()),
            patch.object(BrowserCaptchaService, "_load_browser_count", load),
        ):
            initializing = asyncio.create_task(BrowserCaptchaService.get_instance())
            await entered.wait()
            instance = instances[0]
            closing = asyncio.create_task(instance.close())
            await asyncio.sleep(0)
            self.assertTrue(instance._closed)
            proceed.set()
            try:
                results = await asyncio.gather(
                    initializing, closing, return_exceptions=True
                )
                self.assertIsInstance(results[0], RuntimeError)
                reaper = instance._idle_reaper_task
                self.assertTrue(reaper is None or reaper.done())
                self.assertIsNone(BrowserCaptchaService._instance)
            finally:
                if instance._idle_reaper_task:
                    instance._idle_reaper_task.cancel()
                    await asyncio.gather(
                        instance._idle_reaper_task, return_exceptions=True
                    )

    async def test_cancel_initialization_cleans_reaper_and_leaves_no_singleton(self):
        started = asyncio.Event()
        instances = []
        ensure_reaper = BrowserCaptchaService._ensure_idle_reaper

        async def load(instance):
            instances.append(instance)

        async def start_then_wait(instance):
            await ensure_reaper(instance)
            started.set()
            await asyncio.Event().wait()

        with (
            patch.object(BrowserCaptchaService, "_instance", None),
            patch.object(BrowserCaptchaService, "_lock", asyncio.Lock()),
            patch.object(BrowserCaptchaService, "_load_browser_count", load),
            patch.object(BrowserCaptchaService, "_ensure_idle_reaper", start_then_wait),
        ):
            initializing = asyncio.create_task(BrowserCaptchaService.get_instance())
            await started.wait()
            instance = instances[0]
            initializing.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await initializing
                self.assertIsNone(BrowserCaptchaService._instance)
                self.assertTrue(instance._idle_reaper_task.done())
            finally:
                await instance.close()

    async def test_stats_excludes_credentials_and_reports_leased_slot_busy(self):
        await self.lease()
        stats = self.service.get_stats()
        self.assertEqual(stats["busy_browser_count"], 1)
        self.assertEqual(stats["idle_browser_count"], 0)
        self.assertNotIn("FLOW-SESSION", json.dumps(stats))
        self.assertNotIn("captcha", json.dumps(stats))


class CaptchaRequestContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(proxy_manager=None)

    async def test_context_is_isolated_between_concurrent_requests(self):
        barrier = asyncio.Event()

        async def work(token_id):
            self.client._set_captcha_request_context({"token_id": token_id})
            await barrier.wait()
            return self.client._get_captcha_request_context()["token_id"]

        tasks = [asyncio.create_task(work(i)) for i in (7, 8)]
        await asyncio.sleep(0)
        barrier.set()
        self.assertEqual(await asyncio.gather(*tasks), [7, 8])
        self.assertEqual(self.client._get_captcha_request_context(), {})

    async def test_personal_error_reports_exact_slot_and_issue_time(self):
        service = types.SimpleNamespace(report_flow_error=AsyncMock())
        self.client._set_captcha_request_context(
            {
                "method": "personal",
                "browser_ref": "personal:SESSION",
                "token_id": 7,
                "project_id": "project",
                "slot_id": "w1-slot3",
                "issued_at": 100,
            }
        )
        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
            new=AsyncMock(return_value=service),
        ):
            await self.client._notify_browser_captcha_error(
                "personal:SESSION",
                "project",
                "403错误",
                "reCAPTCHA evaluation failed",
            )
        service.report_flow_error.assert_awaited_once_with(
            project_id="project",
            error_reason="403错误",
            error_message="reCAPTCHA evaluation failed",
            token_id=7,
            slot_id="w1-slot3",
            issued_at=100,
        )

    async def test_exception_reference_overrides_earlier_solve_reference(self):
        error = RuntimeError("HTTP 403: reCAPTCHA evaluation failed")
        error.browser_ref = "1:lease-new/SESSION"
        self.client._notify_browser_captcha_error = AsyncMock()
        with patch.object(
            type(config),
            "captcha_method",
            new_callable=PropertyMock,
            return_value="browser",
        ):
            await self.client._handle_retryable_generation_error(
                error, 5, 6, "0:old", "project", "[IMAGE]"
            )
        self.assertEqual(
            self.client._notify_browser_captcha_error.call_args.kwargs["browser_id"],
            error.browser_ref,
        )

    async def test_raw_recaptcha_error_is_preserved_when_classified_as_403(self):
        service = types.SimpleNamespace(report_error=AsyncMock())
        with (
            patch.object(
                type(config),
                "captcha_method",
                new_callable=PropertyMock,
                return_value="browser",
            ),
            patch(
                "src.services.browser_captcha.BrowserCaptchaService.get_instance",
                new=AsyncMock(return_value=service),
            ),
        ):
            await self.client._notify_browser_captcha_error(
                0, "project", "403错误", "HTTP 403: reCAPTCHA evaluation failed"
            )
        service.report_error.assert_awaited_once_with(
            0, error_reason="HTTP 403: reCAPTCHA evaluation failed"
        )

    async def test_unknown_submission_is_not_retried(self):
        error = RuntimeError("network timeout")
        error.submission_uncertain = True
        self.client._notify_browser_captcha_error = AsyncMock()
        should_retry = await self.client._handle_retryable_generation_error(
            error, 0, 3, None, "project", "[IMAGE]"
        )
        self.assertFalse(should_retry)

    async def test_current_image_entry_stops_on_queue_timeout(self):
        self.client._frontend_cookie = AsyncMock(return_value="cookie")
        self.client._resolve_flow_frontend_cookie_storage = AsyncMock(
            return_value="cookie"
        )
        self.client._get_recaptcha_token = AsyncMock(
            side_effect=BrowserCaptchaQueueTimeout("Browser captcha queue wait timeout")
        )
        self.client._notify_browser_captcha_error = AsyncMock()
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        with patch.object(
            type(config),
            "captcha_method",
            new_callable=PropertyMock,
            return_value="browser",
        ):
            with self.assertRaises(BrowserCaptchaQueueTimeout):
                await self.client.generate_image(
                    "access",
                    "project",
                    "prompt",
                    "NARWHAL",
                    "IMAGE_ASPECT_RATIO_LANDSCAPE",
                )
        self.client._get_recaptcha_token.assert_awaited_once()

    async def test_current_image_entry_reports_verified_success(self):
        ref = "0:lease-request/FLOW-SESSION"
        self.client._frontend_cookie = AsyncMock(return_value="cookie")
        self.client._resolve_flow_frontend_cookie_storage = AsyncMock(
            return_value="cookie"
        )
        self.client._get_recaptcha_token = AsyncMock(return_value=("captcha", ref))
        self.client._current_rpc = AsyncMock(return_value={"ok": True})
        self.client._normalize_frontend_image_generation_response = MagicMock(return_value={"media": ["media"]})
        self.client.get_media = AsyncMock(
            return_value={
                "image": {"generatedImage": {"fifeUrl": "https://example.com/image"}}
            }
        )
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        with patch.object(
            type(config),
            "captcha_method",
            new_callable=PropertyMock,
            return_value="browser",
        ):
            await self.client.generate_image(
                "access", "project", "prompt", "NARWHAL", "IMAGE_ASPECT_RATIO_LANDSCAPE"
            )
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with(
            ref, success=True
        )

    async def test_accepted_image_rpc_is_not_retried_on_normalization_failure(self):
        ref = "0:lease-request/FLOW-SESSION"
        self.client._frontend_cookie = AsyncMock(return_value="cookie")
        self.client._resolve_flow_frontend_cookie_storage = AsyncMock(return_value="cookie")
        self.client._get_recaptcha_token = AsyncMock(return_value=("captcha", ref))
        self.client._current_rpc = AsyncMock(return_value={"ok": True})
        self.client._normalize_frontend_image_generation_response = MagicMock(
            side_effect=RuntimeError("HTTP Error 500: normalization failed")
        )
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        with patch.object(type(config), "captcha_method", new_callable=PropertyMock, return_value="browser"):
            with self.assertRaisesRegex(RuntimeError, "normalization failed"):
                await self.client.generate_image(
                    "access", "project", "prompt", "NARWHAL", "IMAGE_ASPECT_RATIO_LANDSCAPE"
                )
        self.client._get_recaptcha_token.assert_awaited_once()
        self.client._current_rpc.assert_awaited_once()
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with(ref, success=True)

    async def test_personal_late_error_does_not_recover_rebound_or_rebuilt_tab(self):
        from src.services.browser_captcha_personal import (
            BrowserCaptchaService as PersonalService,
            ResidentTabInfo,
        )

        service = object.__new__(PersonalService)
        service._resident_lock = asyncio.Lock()
        info = ResidentTabInfo(object(), "slot", "project", token_id=8)
        service._resident_tabs = {"slot": info}
        service._run_resident_recovery_task = AsyncMock()
        await service.report_flow_error(
            "project", "recaptcha", token_id=7, slot_id="slot"
        )
        info.token_id = 7
        await service.report_flow_error(
            "project",
            "recaptcha",
            token_id=7,
            slot_id="slot",
            issued_at=info.created_at - 1,
        )
        service._run_resident_recovery_task.assert_not_awaited()

    def test_new_handle_without_session_is_not_treated_as_flow_session(self):
        self.assertIsNone(
            FlowClient._extract_browser_captcha_session_id("0:lease-123/")
        )
        self.assertEqual(
            FlowClient._extract_browser_captcha_session_id("0:legacy-session"),
            "legacy-session",
        )


class BrowserStatsApiTests(unittest.TestCase):
    def test_stats_requires_admin_session_and_does_not_start_runtime(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.api import admin

        app = FastAPI()
        app.include_router(admin.router)
        client = TestClient(app)
        with (
            patch.object(
                type(config),
                "captcha_method",
                new_callable=PropertyMock,
                return_value="browser",
            ),
            patch.object(BrowserCaptchaService, "_instance", None),
            patch.object(
                BrowserCaptchaService, "get_instance", new=AsyncMock()
            ) as create_service,
            patch.object(admin, "active_admin_tokens", {"admin-session"}),
        ):
            self.assertEqual(client.get("/api/captcha/browser-stats").status_code, 401)
            response = client.get(
                "/api/captcha/browser-stats",
                headers={"Authorization": "Bearer admin-session"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["running"])
            create_service.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
