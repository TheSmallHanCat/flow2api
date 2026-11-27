import itertools
import json
import os
import types
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from urllib.parse import urlencode

from src.core.config import config
from src.services.flow_client import FlowClient
from src.services.browser_captcha_personal import (
    NativeGenerationOutcomeUnknownError,
    PERSONAL_TRUSTED_RECAPTCHA_HOOK_SOURCE,
    BrowserCaptchaService,
    ResidentTabInfo,
    _build_personal_browser_args,
    _personal_bare_mode_enabled,
    _personal_startup_injection_disabled,
    _PersonalBrowserPoolService,
    _compose_proxy_url,
    _patch_nodriver_connection_instance,
    _redact_proxy_url,
)


class _FakeTab:
    def __init__(self, result):
        self._result = result

    async def evaluate(self, expression, await_promise=False, return_by_value=False):
        return self._result


class _ClosableFakeTab:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True

    async def sleep(self, _seconds):
        return None


class _FakeWebSocket:
    def __init__(self, owner):
        self.owner = owner
        self.close_code = None
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        payload = json.loads(message)
        transaction = self.owner.mapper[payload["id"]]
        transaction(result={"ok": True})


class _ConnectionWithoutClosed:
    def __init__(self):
        self.mapper = {}
        self.handlers = {}
        self.websocket = None
        self.connect_count = 0
        self.register_count = 0
        self.__count__ = itertools.count(0)

    async def send(self, _cdp_obj, _is_update=False):
        raise AssertionError("original send should be patched")

    async def connect(self):
        self.connect_count += 1
        self.websocket = _FakeWebSocket(self)

    async def _register_handlers(self):
        self.register_count += 1


def _fake_cdp_command():
    result = yield {"method": "Runtime.evaluate", "params": {}}
    return result


class BrowserCaptchaPersonalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()

    def test_native_harvest_parser_extracts_token_and_session(self):
        token = "0cAFcW" + ("x" * 120)
        inner = [
            "FLOW-SESSION-1",
            [[[["a red apple on a wooden table"]]]],
            ["projects/project-1", None, [token, 1], None, None, 1],
        ]
        outer = [None, json.dumps(inner), None]
        body = urlencode({"f.req": json.dumps(outer), "at": "xsrf-token"})

        result = self.service._parse_native_harvest_post_data(body)

        self.assertEqual(result, {"token": token, "session_id": "FLOW-SESSION-1"})

    def test_flow_client_extracts_personal_harvest_session(self):
        self.assertEqual(
            FlowClient._extract_browser_captcha_session_id("personal:FLOW-SESSION-1"),
            "FLOW-SESSION-1",
        )
        self.assertIsNone(FlowClient._extract_browser_captcha_session_id("personal:"))

    def test_native_harvest_parser_rejects_invalid_body(self):
        self.assertIsNone(self.service._parse_native_harvest_post_data("at=only"))
        self.assertIsNone(
            self.service._parse_native_harvest_post_data("f.req=not-json")
        )

    async def test_execute_recaptcha_on_tab_accepts_remote_object_success_result(self):
        tab = _FakeTab(self._make_remote_object_result("token-xyz"))
        self.service._wait_for_recaptcha = AsyncMock(return_value=True)

        result = await self.service._execute_recaptcha_on_tab(
            tab, action="IMAGE_GENERATION"
        )

        self.assertEqual(result, {"token": "token-xyz", "session_id": None})

    async def test_direct_chat_solution_generates_flow_session_id(self):
        tab = _FakeTab(self._make_remote_object_result("token-chat"))
        self.service._wait_for_recaptcha = AsyncMock(return_value=True)
        fixed_seed = uuid.UUID("11111111-2222-4333-8444-555555555555")

        with patch("uuid.uuid4", return_value=fixed_seed):
            result = await self.service._execute_recaptcha_on_tab(
                tab, action="CHAT_GENERATION"
            )

        self.assertEqual(result["token"], "token-chat")
        self.assertEqual(
            result["session_id"],
            "cf4c4732-fd3b-4f8a-95b6-0871950a2f22",
        )

    async def test_direct_chat_opens_project_page_for_trusted_closure(self):
        tab = _FakeTab(self._make_remote_object_result("token-project"))
        self.service._navigation_timeout_seconds = 1.0
        self.service._apply_trusted_recaptcha_hook = AsyncMock(return_value=True)
        self.service._tab_get = AsyncMock()
        self.service._wait_for_document_ready = AsyncMock(return_value=True)
        self.service._wait_for_recaptcha = AsyncMock(return_value=True)

        result = await self.service._execute_recaptcha_on_tab(
            tab, action="CHAT_GENERATION", project_id="project-1"
        )

        self.assertEqual(result["token"], "token-project")
        self.assertEqual(
            self.service._tab_get.await_args_list[-1].args[1],
            "https://flow.google.com/project/project-1",
        )
        self.service._wait_for_recaptcha.assert_awaited_once_with(tab)

    def test_trusted_recaptcha_hook_captures_bound_original_execute(self):
        self.assertIn(
            "__flow2apiTrustedRecaptchaExecute",
            PERSONAL_TRUSTED_RECAPTCHA_HOOK_SOURCE,
        )
        self.assertIn("Function.prototype.bind", PERSONAL_TRUSTED_RECAPTCHA_HOOK_SOURCE)

    def test_personal_strategy_selector_respects_explicit_modes(self):
        with patch.object(
            type(config), "personal_solve_strategy", new_callable=PropertyMock
        ) as strategy:
            strategy.return_value = "direct"
            self.assertFalse(self.service._uses_native_harvest("CHAT_GENERATION"))

            strategy.return_value = "harvest"
            self.assertTrue(self.service._uses_native_harvest("CHAT_GENERATION"))
            for action in ("IMAGE_GENERATION", "VIDEO_GENERATION", "UPLOAD_IMAGE"):
                self.assertFalse(self.service._uses_native_harvest(action))

            strategy.return_value = "auto"
            resident_info = ResidentTabInfo(object(), "slot-auto")
            self.assertFalse(
                self.service._uses_native_harvest(
                    "CHAT_GENERATION", resident_info=resident_info
                )
            )
            resident_info.prefer_native_harvest = True
            self.assertTrue(
                self.service._uses_native_harvest(
                    "CHAT_GENERATION", resident_info=resident_info
                )
            )
            self.assertFalse(self.service._uses_native_harvest("IMAGE_GENERATION"))

    async def test_native_harvest_executes_flow_project_submission(self):
        token = "0cAFcW" + ("y" * 120)
        inner = [
            "FLOW-SESSION-2",
            [[[["a red apple on a wooden table"]]]],
            ["projects/project-1", None, [token, 1], None, None, 1],
        ]
        outer = [None, json.dumps(inner), None]
        body = urlencode({"f.req": json.dumps(outer)})
        resident_info = ResidentTabInfo(object(), "slot-1", project_id="project-1")

        self.service._navigation_timeout_seconds = 1.0
        self.service._solve_timeout_seconds = 1.0
        self.service._tab_get = AsyncMock()
        self.service._wait_for_document_ready = AsyncMock(return_value=True)
        self.service._dismiss_native_page_overlays = AsyncMock()
        self.service._wait_for_recaptcha = AsyncMock(return_value=True)
        self.service._wait_for_native_prompt_editor = AsyncMock(return_value=True)
        self.service._install_native_harvest_hook = AsyncMock(return_value=True)
        self.service._submit_native_prompt = AsyncMock()
        self.service._tab_evaluate = AsyncMock(return_value=body)

        result = await self.service._execute_native_harvest_on_tab(
            resident_info, "project-1", "CHAT_GENERATION"
        )

        self.assertEqual(result, {"token": token, "session_id": "FLOW-SESSION-2"})
        self.assertEqual(
            self.service._tab_get.await_args_list[1].args[1],
            "https://flow.google.com/project/project-1",
        )
        self.service._submit_native_prompt.assert_awaited_once()

    def test_solve_bundle_carries_native_harvest_session(self):
        bundle = self.service._build_solve_bundle(
            token="captcha-token",
            project_id="project-1",
            action="CHAT_GENERATION",
            token_id=7,
            slot_id="slot-1",
            session_id="FLOW-SESSION-1",
        )

        self.assertEqual(bundle["token"], "captcha-token")
        self.assertEqual(bundle["session_id"], "FLOW-SESSION-1")

    def test_personal_bare_mode_uses_minimal_browser_args(self):
        with patch.dict(os.environ, {"PERSONAL_BROWSER_BARE_MODE": "true"}):
            self.assertTrue(_personal_bare_mode_enabled())
            args = _build_personal_browser_args(
                headless=False,
                proxy_server_arg="--proxy-server=http://127.0.0.1:7890",
            )

        self.assertEqual(args, ["--proxy-server=http://127.0.0.1:7890"])

    async def test_personal_bare_mode_skips_startup_injection(self):
        service = BrowserCaptchaService()
        service._apply_trusted_recaptcha_hook = AsyncMock()
        service._apply_runtime_profile_to_tab = AsyncMock()
        service._apply_headless_visibility_spoof = AsyncMock()
        service._apply_fingerprint_surface_spoof = AsyncMock()

        with patch.dict(
            os.environ,
            {"PERSONAL_BROWSER_DISABLE_STARTUP_INJECTION": "true"},
        ):
            self.assertTrue(_personal_startup_injection_disabled())
            await service._apply_tab_startup_spoofs(object(), label="unit_test")

        service._apply_trusted_recaptcha_hook.assert_not_awaited()
        service._apply_runtime_profile_to_tab.assert_not_awaited()
        service._apply_headless_visibility_spoof.assert_not_awaited()
        service._apply_fingerprint_surface_spoof.assert_not_awaited()

    def test_authenticated_proxy_url_is_preserved_for_flow_requests(self):
        proxy_url = _compose_proxy_url(
            "http", "proxy.example.com", "8080", "user", "password"
        )

        self.assertEqual(proxy_url, "http://user:password@proxy.example.com:8080")
        self.assertEqual(
            _redact_proxy_url(proxy_url),
            "http://***:***@proxy.example.com:8080",
        )

    @staticmethod
    def _make_remote_object_result(token: str):
        return types.SimpleNamespace(
            type_="object",
            value=None,
            deep_serialized_value=types.SimpleNamespace(
                type_="object",
                value=[
                    ["ok", {"type": "boolean", "value": True}],
                    ["token", {"type": "string", "value": token}],
                ],
            ),
        )

    async def test_tab_evaluate_normalizes_deep_serialized_remote_object(self):
        tab = _FakeTab(self._make_remote_object_result("token-123"))

        result = await self.service._tab_evaluate(
            tab,
            "ignored",
            label="unit_test_tab_evaluate",
            await_promise=True,
            return_by_value=True,
        )

        self.assertEqual(result, {"ok": True, "token": "token-123"})

    async def test_create_resident_tab_returns_none_when_browser_missing(self):
        self.service.browser = None

        resident_info = await self.service._create_resident_tab(
            "slot-1", project_id="project-1"
        )

        self.assertIsNone(resident_info)

    async def test_load_token_cookie_reads_google_cookies_field(self):
        self.service.db = types.SimpleNamespace(
            get_token=AsyncMock(
                return_value=types.SimpleNamespace(
                    google_cookies="OSID=flow-cookie; SID=google-cookie"
                )
            )
        )

        cookie_text = await self.service._load_token_cookie(7)

        self.assertEqual(cookie_text, "OSID=flow-cookie; SID=google-cookie")
        self.service.db.get_token.assert_awaited_once_with(7)

    async def test_persist_context_cookies_updates_google_cookies_field(self):
        resident_info = ResidentTabInfo(
            tab=object(),
            slot_id="slot-1",
            token_id=7,
            browser_context_id="context-1",
        )
        self.service.db = types.SimpleNamespace(update_token=AsyncMock())
        self.service._get_browser_cookies = AsyncMock(
            return_value=[
                {
                    "name": "OSID",
                    "value": "flow-cookie",
                    "domain": "flow.google.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )
        self.service._load_token_cookie = AsyncMock(return_value="SID=google-cookie")

        persisted = await self.service._persist_context_cookies_to_token(
            resident_info,
            7,
            label="unit_test",
        )

        self.assertTrue(persisted)
        self.service.db.update_token.assert_awaited_once()
        args, kwargs = self.service.db.update_token.await_args
        self.assertEqual(args, (7,))
        self.assertIn("google_cookies", kwargs)
        self.assertNotIn("cookie", kwargs)
        self.assertIn("OSID", kwargs["google_cookies"])

    async def test_close_clears_resident_tabs_when_warmup_task_attr_missing(self):
        tab = _ClosableFakeTab()
        self.service._resident_tabs["slot-1"] = ResidentTabInfo(
            tab=tab, slot_id="slot-1"
        )
        if hasattr(self.service, "_resident_warmup_task"):
            delattr(self.service, "_resident_warmup_task")

        await self.service.close()

        self.assertEqual(self.service._resident_tabs, {})
        self.assertTrue(tab.closed)

    async def test_legacy_harvest_session_is_carried_into_bundle(self):
        self.service._get_token_direct = AsyncMock(
            return_value=("captcha-token", "legacy:FLOW-SESSION-LEGACY")
        )

        bundle = await self.service.get_token_bundle(
            "project-1", action="CHAT_GENERATION", token_id=7
        )

        self.assertEqual(bundle["token"], "captcha-token")
        self.assertEqual(bundle["slot_id"], "legacy:FLOW-SESSION-LEGACY")
        self.assertEqual(bundle["session_id"], "FLOW-SESSION-LEGACY")

    def _image_rpc_client(self):
        client = FlowClient(None)
        client._frontend_cookie = AsyncMock()
        client._get_runtime_config = lambda: types.SimpleNamespace(flow_max_retries=3, flow_image_request_timeout=90)
        client._resolve_flow_frontend_cookie_storage = AsyncMock(return_value="SID=session-cookie")
        client._get_recaptcha_token = AsyncMock(return_value=("captcha-token", None))
        client._current_rpc = AsyncMock(return_value=["rpc-result"])
        client._normalize_frontend_image_generation_response = MagicMock(return_value={"media": []})
        client._notify_browser_captcha_request_finished = AsyncMock()
        client._handle_retryable_generation_error = AsyncMock(return_value=True)
        client._call_flow_stream_chat = AsyncMock()
        client._personal_browser_service = types.SimpleNamespace(generate_native_image=AsyncMock())
        return client

    async def test_personal_image_generation_preserves_explicit_model(self):
        client = self._image_rpc_client()
        with patch.object(type(config), "captcha_method", new_callable=PropertyMock, return_value="personal"):
            _, session_id, trace = await client.generate_image(
                at="at-token", project_id="project-1", prompt="a red apple",
                model_name="NARWHAL", aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            )
        request = client._current_rpc.call_args.kwargs["argument"][1][0]
        self.assertEqual(request[5], "BELUGA")
        self.assertEqual(request[12], session_id)
        self.assertEqual(trace["generation_attempts"][0]["image_model_name"], "BELUGA")
        client._personal_browser_service.generate_native_image.assert_not_awaited()
        client._call_flow_stream_chat.assert_not_awaited()
        client._notify_browser_captcha_request_finished.assert_awaited_once_with(None)

    async def test_personal_direct_strategy_keeps_captcha_and_submission_separate(self):
        client = self._image_rpc_client()
        with (
            patch.object(type(config), "captcha_method", new_callable=PropertyMock, return_value="personal"),
            patch.object(type(config), "personal_solve_strategy", new_callable=PropertyMock, return_value="direct"),
        ):
            await client.generate_image(
                at="at-token", project_id="project-1", prompt="a red apple",
                model_name="BELUGA", aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            )
        self.assertIsNone(client._get_recaptcha_token.call_args.kwargs.get("method_override"))
        self.assertEqual(client._get_recaptcha_token.call_args.kwargs["action"], "IMAGE_GENERATION")
        client._personal_browser_service.generate_native_image.assert_not_awaited()
        client._current_rpc.assert_awaited_once()

    async def test_personal_nondefault_image_preserves_reference_and_ratio(self):
        client = self._image_rpc_client()
        with patch.object(type(config), "captcha_method", new_callable=PropertyMock, return_value="personal"):
            await client.generate_image(
                at="at-token", project_id="project-1", prompt="edit the reference",
                model_name="BELUGA", aspect_ratio="IMAGE_ASPECT_RATIO_PORTRAIT",
                image_inputs=[{"mediaId": "reference-1"}],
            )
        request = client._current_rpc.call_args.kwargs["argument"][1][0]
        self.assertEqual(request[4], 2)
        self.assertEqual(request[2], [["reference-1", None, None, None, 1]])
        self.assertIsNone(client._get_recaptcha_token.call_args.kwargs.get("method_override"))
        client._personal_browser_service.generate_native_image.assert_not_awaited()

    async def test_personal_unknown_image_rpc_outcome_is_not_retried(self):
        client = self._image_rpc_client()
        client._current_rpc.side_effect = NativeGenerationOutcomeUnknownError("generation outcome is unknown")
        with patch.object(type(config), "captcha_method", new_callable=PropertyMock, return_value="personal"):
            with self.assertRaisesRegex(NativeGenerationOutcomeUnknownError, "outcome is unknown"):
                await client.generate_image(
                    at="at-token", project_id="project-1", prompt="a red apple",
                    model_name="BELUGA", aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                )
        client._get_recaptcha_token.assert_awaited_once()
        client._current_rpc.assert_awaited_once()
        client._handle_retryable_generation_error.assert_not_awaited()
        client._personal_browser_service.generate_native_image.assert_not_awaited()

    async def test_nonchat_direct_failure_does_not_harvest_chat_token(self):
        for action in ("IMAGE_GENERATION", "VIDEO_GENERATION", "UPLOAD_IMAGE"):
            with self.subTest(action=action):
                info = ResidentTabInfo(object(), "slot", project_id="project")
                self.service._execute_recaptcha_on_tab = AsyncMock(return_value=None)
                self.service._execute_native_harvest_on_tab = AsyncMock(return_value={"token": "wrong-action"})
                with patch.object(type(config), "personal_solve_strategy", new_callable=PropertyMock, return_value="auto"):
                    token = await self.service._solve_with_resident_tab(
                        "slot", "project", info, action, success_label="solve",
                    )
                self.assertIsNone(token)
                self.service._execute_native_harvest_on_tab.assert_not_awaited()

    async def test_atomic_native_auto_prefers_direct_submission(self):
        resident_info = ResidentTabInfo(object(), "slot-1", project_id="project-1")
        service = BrowserCaptchaService()
        service.initialize = AsyncMock()
        service._ensure_resident_tab = AsyncMock(
            return_value=("slot-1", resident_info)
        )
        service._consume_resident_slot_reservation = AsyncMock()
        service._release_resident_slot_reservation = AsyncMock()
        service._execute_recaptcha_on_tab = AsyncMock(
            return_value={"token": "direct-token", "session_id": "session-direct"}
        )
        async def submit_while_locked(*_args, **_kwargs):
            self.assertTrue(resident_info.solve_lock.locked())
            return {
                "responseText": "response",
                "rawLength": 8,
                "session_id": "session-direct",
            }

        service._submit_native_stream_chat_with_resident = AsyncMock(
            side_effect=submit_while_locked
        )
        service._generate_native_image_on_tab = AsyncMock()
        service._refresh_last_fingerprint = AsyncMock(
            return_value={"user_agent": "Chrome"}
        )
        service._cache_session_cookies_for_computed = AsyncMock()
        service._maybe_execute_pending_fresh_profile_restart = AsyncMock()
        service._record_browser_solve_success = MagicMock(return_value=1)
        service._remember_fingerprint = MagicMock()
        service._remember_project_affinity = MagicMock()
        service._remember_token_affinity = MagicMock()
        service._mark_browser_health = MagicMock()

        with patch.object(
            type(config), "personal_solve_strategy", new_callable=PropertyMock
        ) as strategy:
            strategy.return_value = "auto"
            result = await service.generate_native_image(
                project_id="project-1",
                prompt="a red apple",
                token_id=7,
                timeout=90,
            )

        self.assertEqual(result["session_id"], "session-direct")
        service._execute_recaptcha_on_tab.assert_awaited_once()
        service._submit_native_stream_chat_with_resident.assert_awaited_once()
        service._generate_native_image_on_tab.assert_not_awaited()

    async def test_atomic_native_auto_does_not_duplicate_on_network_failure(self):
        resident_info = ResidentTabInfo(object(), "slot-1", project_id="project-1")
        service = BrowserCaptchaService()
        service.initialize = AsyncMock()
        service._ensure_resident_tab = AsyncMock(
            return_value=("slot-1", resident_info)
        )
        service._consume_resident_slot_reservation = AsyncMock()
        service._release_resident_slot_reservation = AsyncMock()
        service._execute_recaptcha_on_tab = AsyncMock(
            return_value={"token": "direct-token", "session_id": "session-direct"}
        )
        service._submit_native_stream_chat_with_resident = AsyncMock(
            side_effect=TimeoutError("network timeout")
        )
        service._generate_native_image_on_tab = AsyncMock()

        with patch.object(
            type(config), "personal_solve_strategy", new_callable=PropertyMock
        ) as strategy:
            strategy.return_value = "auto"
            with self.assertRaisesRegex(TimeoutError, "network timeout"):
                await service.generate_native_image(
                    project_id="project-1",
                    prompt="a red apple",
                    token_id=7,
                    timeout=90,
                )

        service._generate_native_image_on_tab.assert_not_awaited()

    async def test_pool_forwards_stream_chat_to_session_owner(self):
        pool = object.__new__(_PersonalBrowserPoolService)
        first = MagicMock()
        first.submit_native_stream_chat = AsyncMock()
        second = MagicMock()
        second.submit_native_stream_chat = AsyncMock(
            return_value={"responseText": "ok", "rawLength": 2}
        )
        pool._workers = [first, second]
        pool._ensure_workers = AsyncMock()
        pool._native_session_workers = {}
        pool._remember_native_session_worker("session-owner", 1)

        result = await pool.submit_native_stream_chat(
            project_id="project-1",
            prompt="a red apple",
            recaptcha_token="captcha-token",
            session_id="session-owner",
            timeout=90,
        )

        self.assertEqual(result["responseText"], "ok")
        first.submit_native_stream_chat.assert_not_awaited()
        second.submit_native_stream_chat.assert_awaited_once()

    async def test_get_token_bundle_uses_immutable_solve_snapshot(self):
        service = BrowserCaptchaService()
        service._get_token_direct = AsyncMock(
            return_value=("captcha-token", "slot-1")
        )
        service._remember_solve_bundle_snapshot(
            {
                "token": "captcha-token",
                "session_id": "session-a",
                "fingerprint": {"proxy_url": "http://proxy-a:8080"},
                "slot_id": "slot-1",
            }
        )
        resident_info = ResidentTabInfo(object(), "slot-1")
        resident_info.last_harvest_session_id = "session-b"
        resident_info.fingerprint = {"proxy_url": "http://proxy-b:8080"}
        service._resident_tabs["slot-1"] = resident_info

        bundle = await service.get_token_bundle("project-1", token_id=7)

        self.assertEqual(bundle["session_id"], "session-a")
        self.assertEqual(bundle["fingerprint"]["proxy_url"], "http://proxy-a:8080")

    async def test_generation_observer_supports_fetch(self):
        service = BrowserCaptchaService()
        captured = {}

        async def capture(_tab, expression, **_kwargs):
            captured["expression"] = expression
            return True

        service._tab_evaluate = capture

        self.assertTrue(await service._install_native_generation_observer(object()))
        self.assertIn("window.fetch", captured["expression"])
        self.assertIn("response.clone()", captured["expression"])

    def _prepare_resident_solve_assertions(self, resident_info):
        self.service._consume_resident_slot_reservation = AsyncMock()
        self.service._record_browser_solve_success = MagicMock(return_value=1)
        self.service._remember_project_affinity = MagicMock()
        self.service._mark_browser_health = MagicMock()
        self.service._refresh_last_fingerprint = AsyncMock(return_value={})
        self.service._remember_fingerprint = MagicMock()
        self.service._cache_session_cookies_for_computed = AsyncMock()
        self.service._maybe_execute_pending_fresh_profile_restart = AsyncMock()

        async def run_with_timeout(awaitable, **_kwargs):
            return await awaitable

        self.service._run_with_timeout = run_with_timeout
        return resident_info

    async def test_resident_solve_uses_direct_strategy(self):
        resident_info = ResidentTabInfo(object(), "slot-1", project_id="project-1")
        resident_info.recaptcha_ready = True
        resident_info.last_harvest_session_id = "stale-session"
        self._prepare_resident_solve_assertions(resident_info)
        self.service._execute_recaptcha_on_tab = AsyncMock(
            return_value={"token": "direct-token", "session_id": "direct-session"}
        )
        self.service._execute_native_harvest_on_tab = AsyncMock()

        with patch.object(
            type(config), "personal_solve_strategy", new_callable=PropertyMock
        ) as strategy:
            strategy.return_value = "direct"
            token = await self.service._solve_with_resident_tab(
                "slot-1",
                "project-1",
                resident_info,
                "CHAT_GENERATION",
                success_label="unit_test",
            )

        self.assertEqual(token, "direct-token")
        self.assertEqual(resident_info.last_harvest_session_id, "direct-session")
        self.service._execute_recaptcha_on_tab.assert_awaited_once()
        self.service._execute_native_harvest_on_tab.assert_not_awaited()

    async def test_auto_strategy_falls_back_to_harvest_when_js_fails(self):
        resident_info = ResidentTabInfo(object(), "slot-1", project_id="project-1")
        resident_info.recaptcha_ready = True
        self._prepare_resident_solve_assertions(resident_info)
        self.service._execute_recaptcha_on_tab = AsyncMock(return_value=None)
        self.service._execute_native_harvest_on_tab = AsyncMock(
            return_value={"token": "harvest-token", "session_id": "session-auto"}
        )

        with patch.object(
            type(config), "personal_solve_strategy", new_callable=PropertyMock
        ) as strategy:
            strategy.return_value = "auto"
            token = await self.service._solve_with_resident_tab(
                "slot-1",
                "project-1",
                resident_info,
                "CHAT_GENERATION",
                success_label="unit_test",
            )

        self.assertEqual(token, "harvest-token")
        self.assertEqual(resident_info.last_harvest_session_id, "session-auto")
        self.assertTrue(resident_info.last_solve_used_native_harvest)
        self.service._execute_recaptcha_on_tab.assert_awaited_once()
        self.service._execute_native_harvest_on_tab.assert_awaited_once()

    async def test_recaptcha_rejection_makes_auto_prefer_harvest(self):
        resident_info = ResidentTabInfo(object(), "slot-1", project_id="project-1")
        resident_info.recaptcha_ready = True
        resident_info.last_solve_used_native_harvest = False
        self.service._resident_tabs["slot-1"] = resident_info
        self.service._resident_error_streaks.clear()
        self.service._resolve_resident_slot_for_project_locked = (
            lambda *args, **kwargs: ("slot-1", resident_info)
        )
        self.service._is_generation_policy_error = MagicMock(return_value=False)
        self.service._is_external_flow_error = MagicMock(return_value=False)
        self.service._is_recaptcha_cache_reset_error = MagicMock(return_value=True)
        self.service._is_force_fresh_browser_restart_error = MagicMock(
            return_value=True
        )
        self.service._initialized = True
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._mark_fresh_profile_restart_pending = MagicMock()
        self.service._maybe_execute_pending_fresh_profile_restart = AsyncMock()
        self.service._mark_resident_slot_unavailable = AsyncMock()

        with patch.object(
            type(config), "personal_solve_strategy", new_callable=PropertyMock
        ) as strategy:
            strategy.return_value = "auto"
            await self.service.report_flow_error(
                "project-1", "reCAPTCHA evaluation failed"
            )

        self.assertTrue(resident_info.prefer_native_harvest)

    async def test_resident_solve_uses_forced_harvest_strategy(self):
        resident_info = ResidentTabInfo(object(), "slot-1", project_id="project-1")
        resident_info.recaptcha_ready = True
        self._prepare_resident_solve_assertions(resident_info)
        self.service._execute_recaptcha_on_tab = AsyncMock(return_value="direct-token")
        self.service._execute_native_harvest_on_tab = AsyncMock(
            return_value={"token": "harvest-token", "session_id": "session-1"}
        )

        with patch.object(
            type(config), "personal_solve_strategy", new_callable=PropertyMock
        ) as strategy:
            strategy.return_value = "harvest"
            token = await self.service._solve_with_resident_tab(
                "slot-1",
                "project-1",
                resident_info,
                "CHAT_GENERATION",
                success_label="unit_test",
            )

        self.assertEqual(token, "harvest-token")
        self.assertEqual(resident_info.last_harvest_session_id, "session-1")
        self.service._execute_recaptcha_on_tab.assert_not_awaited()
        self.service._execute_native_harvest_on_tab.assert_awaited_once()

    async def test_create_resident_tab_cleans_tab_when_initialization_fails(self):
        tab = _ClosableFakeTab()
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._create_isolated_context_tab = AsyncMock(
            return_value=(tab, "context-1")
        )
        self.service._tab_evaluate = AsyncMock(return_value="complete")
        self.service._apply_token_cookie_binding = AsyncMock(
            side_effect=RuntimeError("cookie failed")
        )
        self.service._dispose_browser_context_quietly = AsyncMock()
        self.service._close_tab_quietly = AsyncMock()

        resident_info = await self.service._create_resident_tab(
            "slot-1", project_id="project-1"
        )

        self.assertIsNone(resident_info)
        self.service._dispose_browser_context_quietly.assert_awaited_once_with(
            "context-1"
        )
        self.service._close_tab_quietly.assert_awaited_once_with(tab)

    async def test_restart_browser_for_project_reuses_recent_healthy_runtime(self):
        resident_info = ResidentTabInfo(
            tab=object(), slot_id="slot-1", project_id="project-1"
        )
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._initialized = True
        self.service._mark_runtime_restart()
        self.service._probe_browser_runtime = AsyncMock(return_value=True)
        self.service._ensure_resident_tab = AsyncMock(
            return_value=("slot-1", resident_info)
        )
        self.service._restart_browser_for_project_unlocked = AsyncMock(
            return_value=True
        )

        result = await self.service._restart_browser_for_project("project-1")

        self.assertTrue(result)
        self.service._restart_browser_for_project_unlocked.assert_not_awaited()
        self.service._ensure_resident_tab.assert_awaited_once()

    async def test_wait_for_recaptcha_raises_on_runtime_disconnect(self):
        tab = _ClosableFakeTab()
        runtime_error = ConnectionRefusedError(1225, "远程计算机拒绝网络连接。")
        self.service._inject_recaptcha_bootstrap_script = AsyncMock(
            return_value="remote"
        )
        self.service._tab_evaluate = AsyncMock(side_effect=runtime_error)

        with self.assertRaises(ConnectionRefusedError):
            await self.service._wait_for_recaptcha(tab)

        self.assertFalse(self.service._last_health_probe_ok)
        self.assertEqual(self.service._tab_evaluate.await_count, 1)

    async def test_force_fresh_flow_error_defers_sync_browser_restart_until_drain(self):
        tab = _ClosableFakeTab()
        resident_info = ResidentTabInfo(
            tab=tab,
            slot_id="slot-1",
            project_id="project-1",
            token_id=1,
        )
        resident_info.recaptcha_ready = True
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._initialized = True
        self.service._resident_tabs["slot-1"] = resident_info
        self.service._project_resident_affinity["project-1"] = "slot-1"
        self.service._token_resident_affinity["1"] = "slot-1"
        self.service._maybe_execute_pending_fresh_profile_restart = AsyncMock(
            return_value=False
        )
        self.service._restart_browser_for_project = AsyncMock(return_value=True)

        await self.service.report_flow_error(
            "project-1",
            "reCAPTCHA 验证失败",
            error_message="Flow API request failed: PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed",
            token_id=1,
            slot_id="slot-1",
        )

        self.assertIn("slot-1", self.service._resident_unavailable_slots)
        self.assertTrue(self.service._fresh_profile_restart_pending)
        self.assertTrue(self.service._fresh_profile_restart_force_pending)
        self.service._restart_browser_for_project.assert_not_awaited()
        self.service._maybe_execute_pending_fresh_profile_restart.assert_awaited_once()

    async def test_pending_fresh_restart_task_is_preserved_during_runtime_shutdown(
        self,
    ):
        async def runner():
            self.service._fresh_profile_restart_task = asyncio.current_task()
            await self.service._cancel_background_runtime_tasks(reason="unit_test")
            self.assertIs(
                self.service._fresh_profile_restart_task, asyncio.current_task()
            )

        import asyncio

        task = asyncio.create_task(runner())
        await task

    async def test_get_token_waits_for_pending_fresh_restart_before_resident_pick(self):
        events = []
        tab = _ClosableFakeTab()
        resident_info = ResidentTabInfo(
            tab=tab,
            slot_id="slot-1",
            project_id="project-1",
            token_id=1,
        )
        resident_info.recaptcha_ready = True
        self.service._fresh_profile_restart_every_n_solves = 5
        self.service._fresh_profile_restart_pending = True
        self.service._fresh_profile_restart_pending_reason = "unit:5/5"
        self.service._has_active_browser_work = AsyncMock(return_value=False)

        async def restart_unlocked(project_id, token_id=None, *, fresh_profile=False):
            events.append("fresh_restart")
            self.assertEqual(project_id, "project-1")
            self.assertTrue(fresh_profile)
            self.service._reset_browser_rotation_budget()
            return True

        async def initialize():
            events.append("initialize")

        async def ensure_resident(*args, **kwargs):
            events.append("ensure_resident")
            return "slot-1", resident_info

        async def solve_resident(*args, **kwargs):
            events.append("solve_resident")
            return "token-1"

        self.service._restart_browser_for_project_unlocked = AsyncMock(
            side_effect=restart_unlocked
        )
        self.service.initialize = AsyncMock(side_effect=initialize)
        self.service._ensure_resident_tab = AsyncMock(side_effect=ensure_resident)
        self.service._ensure_resident_token_binding = AsyncMock(return_value=True)
        self.service._solve_with_resident_tab = AsyncMock(side_effect=solve_resident)

        token, slot_id = await self.service._get_token_direct(
            "project-1",
            token_id=1,
            return_slot_id=True,
        )

        self.assertEqual((token, slot_id), ("token-1", "slot-1"))
        self.assertEqual(
            events, ["fresh_restart", "initialize", "ensure_resident", "solve_resident"]
        )
        self.assertFalse(self.service._fresh_profile_restart_pending)
        self.assertIsNone(self.service._fresh_profile_restart_task)

    async def test_wait_for_pending_fresh_restart_awaits_existing_task(self):
        import asyncio

        events = []
        self.service._fresh_profile_restart_pending = True

        async def restart_task():
            events.append("restart_start")
            await asyncio.sleep(0.01)
            self.service._fresh_profile_restart_pending = False
            events.append("restart_done")
            return True

        task = asyncio.create_task(restart_task())
        self.service._fresh_profile_restart_task = task

        result = (
            await self.service._wait_for_pending_fresh_profile_restart_before_solve(
                "project-1",
                token_id=1,
                source="unit_test",
            )
        )

        self.assertTrue(result)
        self.assertEqual(events, ["restart_start", "restart_done"])
        self.assertTrue(task.done())

    async def test_runtime_surface_profile_contains_extended_browser_environment(self):
        profile = self.service._get_runtime_surface_profile()

        self.assertIn("webgpu", profile)
        self.assertIn("mediaQueries", profile)
        self.assertIn("storage", profile)
        self.assertIn("behavior", profile)
        self.assertIn("visualViewport", profile["window"])
        self.assertIn("supportedExtensions", profile["graphics"])
        self.assertIn(
            "WEBGL_debug_renderer_info", profile["graphics"]["supportedExtensions"]
        )

        source = self.service._build_tab_fingerprint_spoof_source(
            types.SimpleNamespace(target_id="unit-tab")
        )
        for marker in (
            "ensureWebGpuEnvironment",
            "ensureMatchMediaEnvironment",
            "ensureVisualViewportEnvironment",
            "navigator.storage",
            "getSupportedConstraints",
            "userActivation",
        ):
            self.assertIn(marker, source)

    async def test_pool_tab_limits_use_browser_count_times_per_worker_tabs(self):
        pool = _PersonalBrowserPoolService()

        self.assertEqual(pool._build_worker_tab_limits(5, 10), [5] * 10)

        capped_limits = pool._build_worker_tab_limits(5, 20)
        self.assertEqual(len(capped_limits), 20)
        self.assertEqual(sum(capped_limits), 50)
        self.assertLessEqual(max(capped_limits), 5)

        warmup_limits = pool._build_worker_tab_limits(
            5,
            10,
            total_limit=5,
            allow_zero=True,
        )
        self.assertEqual(len(warmup_limits), 10)
        self.assertEqual(sum(warmup_limits), 5)
        self.assertEqual(sum(1 for item in warmup_limits if item > 0), 5)

    async def test_pool_dispatch_prefers_cold_idle_worker_over_busy_live_worker(self):
        pool = _PersonalBrowserPoolService()
        live_worker = BrowserCaptchaService(
            browser_instance_id=1, max_resident_tabs_override=5
        )
        cold_worker = BrowserCaptchaService(
            browser_instance_id=2, max_resident_tabs_override=5
        )
        live_worker._initialized = True
        live_worker.browser = types.SimpleNamespace(stopped=False)
        pool._workers = [live_worker, cold_worker]
        pool._worker_dispatch_reservations = {0: 1}

        self.assertLess(
            pool._worker_dispatch_score(1, cold_worker),
            pool._worker_dispatch_score(0, live_worker),
        )

    async def test_nodriver_send_patch_handles_connection_without_closed_attr(self):
        connection = _ConnectionWithoutClosed()

        _patch_nodriver_connection_instance(connection)
        result = await connection.send(_fake_cdp_command())

        self.assertEqual(result, {"ok": True})
        self.assertEqual(connection.connect_count, 1)
        self.assertEqual(connection.register_count, 1)
        self.assertTrue(getattr(connection, "_flow2api_send_patched", False))


class PersonalCookieTargetTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _flow_cookie_export():
        return json.dumps(
            [
                {
                    "name": "__Secure-OSID",
                    "value": "osid-secure",
                    "domain": "flow.google.com",
                    "hostOnly": True,
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "SID",
                    "value": "sid",
                    "domain": ".google.com",
                    "hostOnly": False,
                    "path": "/",
                    "secure": True,
                },
            ]
        )

    @staticmethod
    def _targets(cookies, name):
        return sorted(
            cookie.get("url") or cookie.get("domain")
            for cookie in cookies
            if cookie["name"] == name
        )

    def test_host_only_flow_cookie_is_injected_for_flow(self):
        for builder in (
            BrowserCaptchaService._build_personal_cookie_targets,
            BrowserCaptchaService._build_configured_browser_cookie_targets,
        ):
            with self.subTest(builder=builder.__name__):
                cookies = builder(self._flow_cookie_export())
                self.assertIn(
                    "https://flow.google.com/",
                    self._targets(cookies, "__Secure-OSID"),
                )
                self.assertIn(".google.com", self._targets(cookies, "SID"))

    def test_raw_cookie_header_reaches_flow_google_and_recaptcha(self):
        cookies = BrowserCaptchaService._build_personal_cookie_targets("SID=sid")
        self.assertEqual(
            self._targets(cookies, "SID"),
            [
                "https://flow.google.com/",
                "https://www.google.com/",
                "https://www.recaptcha.net/",
            ],
        )
        google_cookies = [
            cookie
            for cookie in cookies
            if cookie["url"] != "https://flow.google.com/"
        ]
        self.assertTrue(all(c["sameSite"] == "None" for c in google_cookies))

    async def test_warmup_initializes_browser_and_binds_project_token(self):
        db = MagicMock()
        db.get_project_by_id = AsyncMock(
            side_effect=lambda pid: types.SimpleNamespace(token_id=7)
            if pid == "p1"
            else None
        )
        service = BrowserCaptchaService(db)
        service.initialize = AsyncMock()
        service._ensure_resident_tab = AsyncMock(
            side_effect=[("slot-1", object()), ("slot-2", object())]
        )

        warmed = await service.warmup_resident_tabs(["p1", "p2"], limit=2)

        self.assertEqual(warmed, ["slot-1", "slot-2"])
        service.initialize.assert_awaited_once()
        self.assertEqual(
            [c.kwargs["token_id"] for c in service._ensure_resident_tab.await_args_list],
            [7, None],
        )


if __name__ == "__main__":
    unittest.main()
