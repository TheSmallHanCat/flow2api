import asyncio
import json
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from curl_cffi import CurlInfo
from curl_cffi.requests.cookies import CurlMorsel

from src.core.models import Token
from src.core.database import Database
from src.services.protocol_login import ProtocolLogin, _cookie_jar, _export_cookie_jar
from src.services.token_manager import TokenManager
from src.services.browser_captcha import TokenBrowser, BrowserCaptchaService
from src.services.browser_captcha_personal import (
    BrowserCaptchaService as PersonalService,
    _PersonalBrowserPoolService,
)
from src.services.browser_cookie_utils import (
    build_browser_cookie_targets,
    cookie_pairs_for_url,
    parse_browser_cookie_payload,
    validate_flow_cookie_storage,
)

EMAIL = "account@example.com"
COOKIES = json.dumps(
    [
        {
            "name": "OSID",
            "value": "session-old",
            "domain": "flow.google.com",
            "hostOnly": True,
            "path": "/",
            "secure": True,
            "httpOnly": True,
        },
        {
            "name": "SID",
            "value": "account-cookie",
            "domain": ".google.com",
            "path": "/",
            "secure": True,
        },
        {
            "name": "deleted",
            "value": "removed",
            "domain": "flow.google.com",
            "path": "/project",
            "hostOnly": True,
        },
    ]
)
NEW_COOKIES = COOKIES.replace("session-old", "session-new")
PAGE = 'boq_labs-ai-sandbox-frontend_ "oPEP7c":"account@example.com"'


def make_failing_personal_pool(error):
    pool = _PersonalBrowserPoolService.__new__(_PersonalBrowserPoolService)
    worker = MagicMock()
    worker.refresh_google_cookies = AsyncMock(side_effect=error)
    pool._workers = [worker]
    pool._ensure_workers = AsyncMock()
    pool._acquire_worker = AsyncMock(return_value=(0, worker))
    pool._release_worker_reservation = AsyncMock()
    return pool


class ProtocolCookieRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def call_login(
        self,
        *,
        page=PAGE,
        url="https://flow.google.com/projects",
        status=200,
        raw=COOKIES,
    ):
        jar = _cookie_jar(NEW_COOKIES)
        jar.clear("flow.google.com", "/project", "deleted")
        lines = [
            (
                ("#HttpOnly_" if c.has_nonstandard_attr("HttpOnly") else "")
                + CurlMorsel.from_cookiejar_cookie(c).to_curl_format()
            ).encode()
            for c in jar
        ]
        response = types.SimpleNamespace(
            text=page, url=url, status_code=status, infos={CurlInfo.COOKIELIST: lines}
        )
        session = MagicMock()
        session.__aenter__.return_value = session
        session.get = AsyncMock(return_value=response)
        with patch(
            "src.services.protocol_login.AsyncSession", return_value=session
        ) as factory:
            result = await ProtocolLogin().login(
                raw, proxy="127.0.0.1:7890", email=EMAIL
            )
        return result, factory, session

    async def test_rotated_and_deleted_cookies_are_exported_with_scope(self):
        result, factory, session = await self.call_login()
        self.assertTrue(result["success"])
        cookies = {c["name"]: c for c in json.loads(result["google_cookies"])}
        self.assertEqual(cookies["OSID"]["value"], "session-new")
        self.assertTrue(cookies["OSID"]["hostOnly"])
        self.assertTrue(cookies["OSID"]["httpOnly"])
        self.assertFalse(cookies["SID"]["hostOnly"])
        self.assertNotIn("deleted", cookies)
        self.assertEqual(factory.call_args.kwargs["proxy"], "http://127.0.0.1:7890")
        # 续期请求必须显式带上浏览器会发的那份 Cookie 头：
        # path=/project 的 deleted 不属于 /projects 的作用域，不应出现在这里。
        self.assertEqual(
            session.get.call_args.kwargs["headers"]["Cookie"],
            "OSID=session-old; SID=account-cookie",
        )

    async def test_anonymous_frontend_does_not_count_as_refresh(self):
        result, _, _ = await self.call_login(page="boq_labs-ai-sandbox-frontend_")
        self.assertFalse(result["success"])

    async def test_wrong_account_is_reported_instead_of_failing(self):
        """账号不匹配时不再报错，而是把 Flow 页面的真实邮箱回传，由调用方自动更正。"""
        result, _, _ = await self.call_login(
            page=PAGE.replace(EMAIL, "other@example.com")
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["email"], "other@example.com")
        self.assertIn("google_cookies", result)

    async def test_login_redirect_is_rejected(self):
        result, _, _ = await self.call_login(url="https://accounts.google.com/signin")
        self.assertFalse(result["success"])

    async def test_plain_cookie_header_remains_supported(self):
        jar = _cookie_jar("OSID=session; SID=account")
        self.assertEqual({c.name for c in jar}, {"OSID", "SID"})
        self.assertTrue(all(not c.domain_specified for c in jar))

    async def test_multiline_cookie_text_preserves_separate_names_and_values(self):
        for raw in (
            "OSID=session\nSID=account",
            "Cookie: OSID=session\r\nSID=account",
            "OSID=session;\n SID=account;",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    validate_flow_cookie_storage(raw), "OSID=session; SID=account"
                )
                cookies = parse_browser_cookie_payload(raw)
                self.assertEqual(
                    {cookie["name"]: cookie["value"] for cookie in cookies},
                    {"OSID": "session", "SID": "account"},
                )
                jar = _cookie_jar(raw)
                self.assertEqual(
                    {cookie.name: cookie.value for cookie in jar},
                    {"OSID": "session", "SID": "account"},
                )
                self.assertFalse(
                    any("\n" in cookie.value or "\r" in cookie.value for cookie in jar)
                )
                targets = build_browser_cookie_targets(raw)
                self.assertEqual(
                    {cookie["name"]: cookie["value"] for cookie in targets},
                    {"OSID": "session", "SID": "account"},
                )

    async def test_protocol_login_seeds_complete_multiline_cookie_header(self):
        result, factory, _ = await self.call_login(raw="OSID=session\nSID=account")
        self.assertTrue(result["success"])
        jar = factory.call_args.kwargs["cookies"]
        self.assertEqual(
            {cookie.name: cookie.value for cookie in jar},
            {"OSID": "session", "SID": "account"},
        )

    async def test_cookie_name_value_json_map_remains_supported(self):
        jar = _cookie_jar('{"OSID":"session","SID":"account"}')
        self.assertEqual({c.name for c in jar}, {"OSID", "SID"})

    async def test_existing_browser_security_metadata_survives_curl_snapshot(self):
        original = json.loads(COOKIES)
        original[0]["sameSite"] = "None"
        # Seed cookies in curl_cffi lose HttpOnly/SameSite unless the server resets them.
        raw = json.dumps(original)
        jar = _cookie_jar(raw)
        for cookie in jar:
            cookie._rest = {"http_only": "False"}
        exported = json.loads(_export_cookie_jar(jar, raw))
        session = next(c for c in exported if c["name"] == "OSID")
        self.assertTrue(session["httpOnly"])
        self.assertEqual(session["sameSite"], "None")

    async def test_partitioned_cookie_cannot_replace_normal_flow_session(self):
        for marker in (
            {"partitionKey": {"topLevelSite": "https://other.example"}},
            {"partitionKey": "https://other.example"},
            {"partitionKey": {"topLevelSite": "https://flow.google.com"}},
            {"partitionKeyOpaque": True},
        ):
            with self.subTest(marker=marker):
                cookies = json.loads(COOKIES)
                cookies.append({**cookies[0], "value": "partition-session", **marker})
                raw = json.dumps(cookies)
                jar = _cookie_jar(raw)
                session = next(cookie for cookie in jar if cookie.name == "OSID")
                self.assertEqual(session.value, "session-old")
                stored = json.loads(_export_cookie_jar(jar, raw))
                self.assertEqual(
                    next(c for c in stored if c["name"] == "OSID")["value"],
                    "session-old",
                )

    async def test_partitioned_only_payload_does_not_fall_back_to_plain_pairs(self):
        cookie = {
            **json.loads(COOKIES)[0],
            "partitionKey": {"topLevelSite": "https://flow.google.com"},
        }
        for payload in ([cookie], {"cookies": [cookie]}, cookie):
            with self.subTest(payload_type=type(payload).__name__):
                self.assertEqual(list(_cookie_jar(json.dumps(payload))), [])

    async def test_null_partition_metadata_preserves_normal_cookies(self):
        cookies = json.loads(COOKIES)
        for cookie in cookies:
            cookie["partitionKey"] = None
            cookie["partitionKeyOpaque"] = False
        jar = _cookie_jar(json.dumps(cookies))
        self.assertEqual({cookie.name for cookie in jar}, {"OSID", "SID", "deleted"})

    async def test_partitioned_only_flow_session_is_rejected_before_network_request(
        self,
    ):
        for marker in (
            {"partitionKey": {"topLevelSite": "https://flow.google.com"}},
            {"partitionKey": "https://other.example"},
            {"partitionKeyOpaque": True},
        ):
            with self.subTest(marker=marker):
                original = json.loads(COOKIES)
                raw = json.dumps([{**original[0], **marker}, original[1]])
                with self.assertRaisesRegex(ValueError, "OSID"):
                    validate_flow_cookie_storage(raw)
                with patch("src.services.protocol_login.AsyncSession") as factory:
                    result = await ProtocolLogin().login(raw)
                self.assertFalse(result["success"])
                factory.assert_not_called()

    async def test_partitioned_cookies_are_not_seeded_into_browser_or_saved_as_normal(
        self,
    ):
        cookies = json.loads(COOKIES)
        cookies.append(
            {
                "name": "partition-only",
                "value": "partition-value",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "partitionKey": {"topLevelSite": "https://other.example"},
            }
        )
        raw = json.dumps({"cookies": cookies})
        self.assertNotIn(
            "partition-only",
            {cookie["name"] for cookie in build_browser_cookie_targets(raw)},
        )
        stored = json.loads(_export_cookie_jar(_cookie_jar(raw), raw))
        self.assertNotIn("partition-only", {cookie["name"] for cookie in stored})


class TokenCookieRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=self.temp.name + "/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(
                st="frontend-old",
                at="old-at",
                email=EMAIL,
                google_cookies=COOKIES,
                login_account=EMAIL,
                protocol_mode="session",
                proxy_url="http://127.0.0.1:7890",
            )
        )
        self.flow = MagicMock()
        self.flow.get_request_fingerprint.return_value = None
        self.flow.st_to_at = AsyncMock(
            return_value={
                "access_token": "flow-frontend-cookie",
                "expires": "2026-12-01T00:00:00Z",
                "credits": 42,
                "userPaygateTier": "PAYGATE_TIER_ONE",
            }
        )
        self.manager = TokenManager(self.db, self.flow)
        self.login = patch(
            "src.services.protocol_login.protocol_loginer.login", new_callable=AsyncMock
        )
        self.login_mock = self.login.start()
        self.login_mock.return_value = {
            "success": True,
            "google_cookies": NEW_COOKIES,
            "email": EMAIL,
        }

    async def asyncTearDown(self):
        self.login.stop()
        self.temp.cleanup()

    async def test_validation_rotations_are_persisted_after_protocol_refresh(self):
        import hashlib

        final_cookies = NEW_COOKIES.replace("session-new", "session-validated")
        self.flow.st_to_at.return_value["google_cookies"] = final_cookies
        token = await self.db.get_token(self.token_id)

        await self.manager._save_refreshed_cookies(
            self.token_id, token, {"google_cookies": NEW_COOKIES, "email": EMAIL}
        )

        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.google_cookies, final_cookies)
        self.assertEqual(
            saved.st, "frontend-" + hashlib.sha256(final_cookies.encode()).hexdigest()[:32]
        )

    def prepare_at_fallback(self):
        self.flow.FRONTEND_ACCESS_TOKEN = "flow-frontend-cookie"
        self.flow.st_to_at.return_value["google_cookies"] = NEW_COOKIES
        self.flow.get_credits = AsyncMock(
            return_value={"credits": 42, "userPaygateTier": "PAYGATE_TIER_ONE"}
        )

    async def test_scheduled_fallback_saves_rotated_cookies(self):
        import hashlib

        self.prepare_at_fallback()
        await self.db.update_token(self.token_id, at="flow-frontend-cookie")
        with patch.object(self.manager, "_try_refresh_st", AsyncMock(return_value=None)):
            self.assertTrue(await self.manager._refresh_at_inner(self.token_id))

        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.google_cookies, NEW_COOKIES)
        self.assertEqual(
            saved.st, "frontend-" + hashlib.sha256(NEW_COOKIES.encode()).hexdigest()[:32]
        )
        self.assertEqual(saved.last_st_refresh_result, "success")

    async def test_fallback_verifies_and_saves_the_latest_cookie_snapshot(self):
        self.prepare_at_fallback()
        final_cookies = NEW_COOKIES.replace("session-new", "session-final")
        self.flow.get_updated_flow_cookies.side_effect = (
            lambda original: final_cookies if original == NEW_COOKIES else original
        )

        self.assertTrue(await self.manager._do_refresh_at(self.token_id, "frontend-old"))

        self.assertEqual(
            self.flow.get_credits.call_args.kwargs["google_cookies"], NEW_COOKIES
        )
        self.assertEqual(
            (await self.db.get_token(self.token_id)).google_cookies, final_cookies
        )

    async def test_fallback_cannot_overwrite_a_concurrent_plugin_push(self):
        self.prepare_at_fallback()

        async def push_during_verification(*args, **kwargs):
            await self.db.update_token(
                self.token_id,
                google_cookies="newer-plugin-push",
                st="newer-st",
                at="newer-at",
            )
            return {"credits": 42}

        self.flow.get_credits.side_effect = push_during_verification

        self.assertFalse(await self.manager._do_refresh_at(self.token_id, "frontend-old"))

        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.google_cookies, "newer-plugin-push")
        self.assertEqual(saved.st, "newer-st")
        self.assertEqual(saved.at, "newer-at")

    async def test_fallback_auth_rejection_does_not_save_candidate_credentials(self):
        self.prepare_at_fallback()
        self.flow.get_credits.side_effect = RuntimeError("rpc=nzlxg, code=401")

        self.assertFalse(await self.manager._do_refresh_at(self.token_id, "frontend-old"))

        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.google_cookies, COOKIES)
        self.assertEqual(saved.st, "frontend-old")
        self.assertEqual(saved.at, "old-at")

    async def test_fallback_network_error_keeps_the_authenticated_cookie_snapshot(self):
        self.prepare_at_fallback()
        self.flow.get_credits.side_effect = TimeoutError("connection timed out")

        self.assertTrue(await self.manager._do_refresh_at(self.token_id, "frontend-old"))

        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.google_cookies, NEW_COOKIES)
        self.assertEqual(saved.at, "flow-frontend-cookie")

    async def test_legacy_at_refresh_preserves_cookie_and_session_fields(self):
        self.flow.st_to_at.return_value = {"access_token": "legacy-at"}
        self.flow.get_credits = AsyncMock(return_value={"credits": 42})

        self.assertTrue(await self.manager._do_refresh_at(self.token_id, "frontend-old"))

        saved = await self.db.get_token(self.token_id)
        self.assertEqual(saved.google_cookies, COOKIES)
        self.assertEqual(saved.st, "frontend-old")
        self.assertEqual(saved.at, "legacy-at")

    async def test_manual_refresh_saves_cookies_and_validates_new_snapshot(self):
        self.assertTrue(await self.manager.refresh_cookies(self.token_id))
        latest = await self.db.get_token(self.token_id)
        self.assertEqual(latest.google_cookies, NEW_COOKIES)
        self.assertEqual(latest.at, "flow-frontend-cookie")
        self.assertEqual(latest.credits, 42)
        self.assertEqual(latest.last_st_refresh_result, "success")
        self.assertEqual(
            self.flow.st_to_at.call_args.kwargs["google_cookies"], NEW_COOKIES
        )
        self.assertEqual(self.flow.st_to_at.call_args.kwargs["token_id"], self.token_id)
        self.assertEqual(
            self.login_mock.call_args.kwargs["proxy"], "http://127.0.0.1:7890"
        )

    async def test_rpc_rejection_does_not_commit_candidate(self):
        self.flow.st_to_at.side_effect = RuntimeError("rpc=nzlxg, code=401")
        token = await self.db.get_token(self.token_id)
        self.assertIsNone(
            await self.manager._try_protocol_refresh_st(self.token_id, token)
        )
        latest = await self.db.get_token(self.token_id)
        self.assertEqual(latest.google_cookies, COOKIES)
        self.assertEqual(latest.st, "frontend-old")
        self.assertEqual(latest.at, "old-at")
        self.assertIn("401", latest.last_st_refresh_result)

    async def test_concurrent_push_is_not_overwritten(self):
        async def push_during_validation(*args, **kwargs):
            await self.db.update_token(
                self.token_id,
                google_cookies="newer-plugin-push",
                st="newer-st",
                last_st_refresh_result="pushed",
            )
            return {"access_token": "flow-frontend-cookie"}

        self.flow.st_to_at.side_effect = push_during_validation
        token = await self.db.get_token(self.token_id)
        self.assertIsNone(
            await self.manager._try_protocol_refresh_st(self.token_id, token)
        )
        latest = await self.db.get_token(self.token_id)
        self.assertEqual(latest.google_cookies, "newer-plugin-push")
        self.assertEqual(latest.st, "newer-st")
        self.assertEqual(latest.last_st_refresh_result, "pushed")

    async def test_unchanged_valid_cookies_are_successful(self):
        self.login_mock.return_value["google_cookies"] = COOKIES
        self.assertTrue(await self.manager.refresh_cookies(self.token_id))
        self.assertTrue(await self.manager.refresh_cookies(self.token_id))
        self.assertEqual(
            (await self.db.get_token(self.token_id)).last_st_refresh_result, "success"
        )

    async def test_scheduler_refreshes_current_cookies_on_legacy_mode_rows(self):
        with patch.object(
            self.db,
            "get_token_refresh_config",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    enabled=True, refresh_interval_minutes=1
                )
            ),
        ):
            await self.manager.run_protocol_refresh_once()
        self.assertEqual(
            (await self.db.get_token(self.token_id)).google_cookies, NEW_COOKIES
        )

    async def test_browser_fallback_passes_account_id_and_requires_validated_cookies(
        self,
    ):
        self.login_mock.return_value = {
            "success": False,
            "error": "frontend unavailable",
        }
        browser = MagicMock()
        browser.refresh_google_cookies = AsyncMock(
            return_value={"google_cookies": NEW_COOKIES, "email": EMAIL}
        )
        with (
            patch("src.services.token_manager.config") as config,
            patch.object(
                BrowserCaptchaService, "get_instance", AsyncMock(return_value=browser)
            ),
        ):
            config.captcha_method = "browser"

            self.assertTrue(await self.manager.refresh_cookies(self.token_id))
        browser.refresh_google_cookies.assert_awaited_once_with(
            None, token_id=self.token_id
        )
        self.assertEqual(
            self.flow.st_to_at.call_args.kwargs["google_cookies"], NEW_COOKIES
        )

    async def test_identity_mismatch_is_adopted_from_flow_page(self):
        """Cookie 属于另一个账号时，刷新应以 Flow 页面为准自动更正邮箱。"""
        self.login_mock.return_value["email"] = "other@example.com"
        token = await self.db.get_token(self.token_id)
        self.assertTrue(await self.manager._try_protocol_refresh_st(self.token_id, token))
        latest = await self.db.get_token(self.token_id)
        self.assertEqual(latest.email, "other@example.com")
        self.assertEqual(latest.login_account, "other@example.com")
        self.assertEqual(latest.google_cookies, NEW_COOKIES)
        self.assertEqual(latest.last_st_refresh_result, "success")

    async def test_identity_conflict_with_other_token_is_rejected(self):
        await self.db.add_token(
            Token(
                st="frontend-other",
                email="other@example.com",
                login_account="other@example.com",
                google_cookies=COOKIES,
            )
        )
        self.login_mock.return_value["email"] = "other@example.com"
        token = await self.db.get_token(self.token_id)
        self.assertIsNone(
            await self.manager._try_protocol_refresh_st(self.token_id, token)
        )
        latest = await self.db.get_token(self.token_id)
        # 不覆盖本地账号邮箱，也不保存别家账号的 Cookie
        self.assertEqual(latest.email, EMAIL)
        self.assertEqual(latest.google_cookies, COOKIES)
        self.assertIn("已被 Token", latest.last_st_refresh_result)

    async def test_personal_network_failures_keep_account_active(self):
        self.flow._is_proxy_connection_error.return_value = False
        self.flow._is_retryable_network_error.return_value = False
        self.flow.st_to_at.side_effect = TimeoutError("认证请求响应超时")
        self.login_mock.return_value = {
            "success": False,
            "error": "connection timed out",
        }
        for error in (
            TimeoutError(),
            TimeoutError("页面响应超时"),
            ConnectionError("connection reset"),
            RuntimeError("proxy connection failed"),
        ):
            with self.subTest(error=type(error).__name__, message=str(error)):
                pool = make_failing_personal_pool(error)
                with (
                    patch("src.services.token_manager.config") as config,
                    patch.object(
                        PersonalService, "get_instance", AsyncMock(return_value=pool)
                    ),
                ):
                    config.captcha_method = "personal"
                    self.assertFalse(
                        await self.manager._refresh_at_inner(self.token_id)
                    )
                latest = await self.db.get_token(self.token_id)
                self.assertTrue(latest.is_active)
                self.assertIn(type(error).__name__, latest.last_st_refresh_result)
                if str(error):
                    self.assertIn(str(error), latest.last_st_refresh_result)
                pool._release_worker_reservation.assert_awaited_once_with(0)

    async def test_authentication_failure_is_not_classified_as_network_error(self):
        self.flow._is_proxy_connection_error.return_value = False
        self.flow._is_retryable_network_error.return_value = False
        self.flow.st_to_at.side_effect = RuntimeError("rpc=nzlxg, code=401")
        self.login_mock.return_value = {
            "success": False,
            "error": "rpc=nzlxg, code=401",
        }
        pool = make_failing_personal_pool(RuntimeError("Flow 登录已失效"))
        with (
            patch("src.services.token_manager.config") as config,
            patch.object(PersonalService, "get_instance", AsyncMock(return_value=pool)),
        ):
            config.captcha_method = "personal"
            self.assertFalse(await self.manager._refresh_at_inner(self.token_id))
        self.assertFalse((await self.db.get_token(self.token_id)).is_active)


class CookieRefreshScheduleTests(unittest.IsolatedAsyncioTestCase):
    """The Flow frontend AT is static, so refresh must follow the Cookie interval."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=self.temp.name + "/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(
                st="frontend-old",
                at="flow-frontend-cookie",
                at_expires=datetime.now(timezone.utc) + timedelta(minutes=55),
                email=EMAIL,
                google_cookies=COOKIES,
                login_account=EMAIL,
                refresh_interval_minutes=120,
                last_st_refresh_at=datetime.now(timezone.utc),
                last_st_refresh_result="success",
            )
        )
        self.flow = MagicMock()
        self.flow.FRONTEND_ACCESS_TOKEN = "flow-frontend-cookie"
        self.flow.proxy_manager = None
        self.flow.get_request_fingerprint.return_value = None
        self.flow.st_to_at = AsyncMock(
            return_value={"access_token": "flow-frontend-cookie"}
        )
        self.flow.get_credits = AsyncMock(
            return_value={"credits": 7, "userPaygateTier": None}
        )
        self.manager = TokenManager(self.db, self.flow)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_newly_refreshed_cookie_is_not_refreshed_again(self):
        token = await self.db.get_token(self.token_id)
        self.assertFalse(self.manager.needs_at_refresh(token))
        refreshed = await self.manager.ensure_valid_token(token)
        self.assertIsNotNone(refreshed)
        self.flow.st_to_at.assert_not_awaited()

    async def test_interval_elapsed_marks_cookie_for_refresh(self):
        await self.db.update_token(
            self.token_id,
            last_st_refresh_at=datetime.now(timezone.utc) - timedelta(minutes=121),
        )
        token = await self.db.get_token(self.token_id)
        self.assertTrue(self.manager.needs_at_refresh(token))

    async def test_due_interval_rotates_cookies_instead_of_revalidating_at(self):
        await self.db.update_token(
            self.token_id,
            last_st_refresh_at=datetime.now(timezone.utc) - timedelta(minutes=121),
        )
        token = await self.db.get_token(self.token_id)
        with patch(
            "src.services.protocol_login.protocol_loginer.login",
            new_callable=AsyncMock,
        ) as login:
            login.return_value = {
                "success": True,
                "google_cookies": NEW_COOKIES,
                "email": EMAIL,
            }
            refreshed = await self.manager.ensure_valid_token(token)
        self.assertIsNotNone(refreshed)
        latest = await self.db.get_token(self.token_id)
        self.assertEqual(latest.google_cookies, NEW_COOKIES)
        self.assertEqual(latest.last_st_refresh_result, "success")
        self.assertEqual(
            self.flow.st_to_at.call_args.kwargs["google_cookies"], NEW_COOKIES
        )
        self.assertFalse(self.manager.needs_at_refresh(latest))

    async def test_disabled_auto_refresh_never_marks_cookie_for_refresh(self):
        await self.db.update_token(
            self.token_id,
            auto_refresh_enabled=False,
            last_st_refresh_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        token = await self.db.get_token(self.token_id)
        self.assertFalse(self.manager.needs_at_refresh(token))

    async def test_recent_validation_skips_repeated_credits_lookup(self):
        token = await self.db.get_token(self.token_id)
        await self.manager.ensure_valid_token(token)
        self.manager._mark_at_valid(self.token_id)
        await self.manager.ensure_valid_token(token)
        self.flow.get_credits.assert_awaited_once()


class PlaceholderIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=self.temp.name + "/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(
                st="frontend-old",
                at="flow-frontend-cookie",
                email="account-1234@flow.local",
                google_cookies=COOKIES,
                login_account="",
            )
        )
        self.flow = MagicMock()
        self.flow.FRONTEND_ACCESS_TOKEN = "flow-frontend-cookie"
        self.flow.proxy_manager = None
        self.flow.get_request_fingerprint.return_value = None
        self.flow.st_to_at = AsyncMock(
            return_value={"access_token": "flow-frontend-cookie"}
        )
        self.manager = TokenManager(self.db, self.flow)
        self.login = patch(
            "src.services.protocol_login.protocol_loginer.login", new_callable=AsyncMock
        )
        self.login_mock = self.login.start()
        self.login_mock.return_value = {
            "success": True,
            "google_cookies": NEW_COOKIES,
            "email": EMAIL,
        }

    async def asyncTearDown(self):
        self.login.stop()
        self.temp.cleanup()

    async def test_refresh_replaces_placeholder_email_with_real_account(self):
        self.assertTrue(await self.manager.refresh_cookies(self.token_id))
        latest = await self.db.get_token(self.token_id)
        self.assertEqual(latest.email, EMAIL)
        self.assertEqual(latest.login_account, EMAIL)
        self.assertEqual(latest.google_cookies, NEW_COOKIES)

    async def test_detect_account_identity_reads_email_and_refreshed_cookies(self):
        identity = await self.manager.detect_account_identity(COOKIES)
        self.assertEqual(identity["email"], EMAIL)
        self.assertEqual(identity["google_cookies"], NEW_COOKIES)
        self.assertTrue(identity["st"].startswith("frontend-"))

    async def test_add_token_uses_detected_email_instead_of_placeholder(self):
        self.flow.get_credits = AsyncMock(
            return_value={"credits": 5, "userPaygateTier": None}
        )
        self.flow.create_project = AsyncMock(side_effect=lambda *a, **k: "project-1")
        with patch("src.services.token_manager.config") as config:
            config.personal_project_pool_size = 1
            token = await self.manager.add_token(st="", google_cookies=COOKIES)
        self.assertEqual(token.email, EMAIL)
        self.assertEqual(token.login_account, EMAIL)
        self.assertTrue(token.auto_refresh_enabled)
        stored = await self.db.get_token(token.id)
        self.assertEqual(stored.last_st_refresh_result, "success")
        self.assertIsNotNone(stored.last_st_refresh_at)


class ImportPayloadTests(unittest.TestCase):
    def test_login_account_is_optional_for_cookie_only_import(self):
        from src.api.admin import ImportTokenItem, ImportTokensRequest

        item = ImportTokenItem(google_cookies=COOKIES)
        self.assertIsNone(item.login_account)
        request = ImportTokensRequest(tokens=[item, ImportTokenItem(google_cookies=NEW_COOKIES)])
        self.assertEqual(len(request.tokens), 2)

    def test_explicit_login_account_is_normalized(self):
        from src.api.admin import ImportTokenItem

        item = ImportTokenItem(
            google_cookies=COOKIES, login_account="User@Example.com "
        )
        self.assertEqual(item.login_account, "user@example.com")


class RefreshIntervalDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=self.temp.name + "/flow.db")
        await self.db.init_db()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_default_refresh_interval_is_30_minutes(self):
        self.assertEqual(Token(st="frontend-x", email=EMAIL).refresh_interval_minutes, 30)
        config_row = await self.db.get_token_refresh_config()
        self.assertEqual(config_row.refresh_interval_minutes, 30)

    async def test_scheduler_uses_30_minutes_when_interval_is_missing(self):
        token_id = await self.db.add_token(
            Token(
                st="frontend-x",
                at="flow-frontend-cookie",
                email=EMAIL,
                google_cookies=COOKIES,
            )
        )
        flow = MagicMock()
        flow.FRONTEND_ACCESS_TOKEN = "flow-frontend-cookie"
        flow.proxy_manager = None
        flow.get_request_fingerprint.return_value = None
        manager = TokenManager(self.db, flow)
        self.assertEqual(manager._normalize_refresh_interval(None), 30)
        token = await self.db.get_token(token_id)
        self.assertEqual(token.refresh_interval_minutes, 30)
        # 29 分钟前校验过 -> 还未到期
        token.last_st_refresh_at = datetime.now(timezone.utc) - timedelta(minutes=29)
        self.assertFalse(manager.needs_at_refresh(token))
        # 31 分钟前校验过 -> 到期
        token.last_st_refresh_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        self.assertTrue(manager.needs_at_refresh(token))

    async def test_global_interval_can_be_applied_to_every_account(self):
        first = await self.db.add_token(Token(st="frontend-a", email="a@example.com"))
        second = await self.db.add_token(Token(st="frontend-b", email="b@example.com"))
        updated = await self.db.update_all_tokens_refresh_interval(15)
        self.assertEqual(updated, 2)
        for token_id in (first, second):
            self.assertEqual(
                (await self.db.get_token(token_id)).refresh_interval_minutes, 15
            )

    async def test_interval_under_30_minutes_is_accepted(self):
        from src.api.admin import ImportTokenItem, TokenRefreshConfigRequest

        self.assertEqual(
            ImportTokenItem(google_cookies=COOKIES, refresh_interval_minutes=10)
            .refresh_interval_minutes,
            10,
        )
        self.assertEqual(
            TokenRefreshConfigRequest(
                refresh_interval_minutes=5, apply_to_tokens=True
            ).refresh_interval_minutes,
            5,
        )
        with self.assertRaises(Exception):
            TokenRefreshConfigRequest(refresh_interval_minutes=0)


class BrowserCookieRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_personal_host_cookie_preserves_original_origin(self):
        for scope in (
            {"domain": "accounts.google.com"},
            {"url": "https://accounts.google.com/signin"},
            {"url": "http://accounts.google.com/signin"},
        ):
            with self.subTest(scope=scope):
                cookie = {
                    "name": "__Host-GAPS",
                    "value": "account-cookie",
                    "path": "/signin",
                    "secure": False,
                    "httpOnly": True,
                    "expires": 2030000000,
                    **scope,
                }
                for source in (cookie, types.SimpleNamespace(**cookie)):
                    stored = PersonalService._serialize_browser_cookie_for_storage(
                        source
                    )
                    self.assertEqual(stored["url"], "https://accounts.google.com/")
                    self.assertNotIn("domain", stored)
                    self.assertEqual(stored["path"], "/")
                    self.assertTrue(stored["secure"])
                    self.assertTrue(stored["httpOnly"])
                    self.assertEqual(stored["expires"], 2030000000)
                    self.assertEqual(
                        cookie_pairs_for_url(
                            [stored], "https://flow.google.com/projects"
                        ),
                        [],
                    )
                    self.assertEqual(
                        cookie_pairs_for_url(
                            [stored], "https://sub.accounts.google.com/"
                        ),
                        [],
                    )
                    self.assertEqual(
                        cookie_pairs_for_url([stored], "https://accounts.google.com/"),
                        [("__Host-GAPS", "account-cookie")],
                    )

    async def test_personal_pool_preserves_original_error_and_releases_worker(self):
        error = TimeoutError("connection timed out")
        pool = make_failing_personal_pool(error)
        with self.assertRaises(TimeoutError) as caught:
            await pool.refresh_google_cookies(None, token_id=9)
        self.assertIs(caught.exception, error)
        pool._release_worker_reservation.assert_awaited_once_with(0)

    async def test_personal_export_rejects_partitioned_cdp_cookies(self):
        for marker in (
            {"partitionKey": "https://other.example"},
            {"partition_key": object()},
            {"partition_key_opaque": True},
        ):
            with self.subTest(marker=marker):
                cookie = {**json.loads(COOKIES)[0], **marker}
                self.assertIsNone(
                    PersonalService._serialize_browser_cookie_for_storage(cookie)
                )
                cdp_cookie = types.SimpleNamespace(**cookie)
                self.assertIsNone(
                    PersonalService._serialize_browser_cookie_for_storage(cdp_cookie)
                )

    async def test_playwright_export_excludes_partitioned_cookies(self):
        browser = TokenBrowser.__new__(TokenBrowser)
        browser._semaphore = asyncio.Semaphore(1)
        browser._solve_inflight = 0
        browser.note_idle = MagicMock()
        page = MagicMock(url="https://flow.google.com/projects")
        page.goto = AsyncMock(return_value=types.SimpleNamespace(status=200))
        page.evaluate = AsyncMock(return_value=EMAIL)
        page.close = AsyncMock()
        cookies = json.loads(COOKIES)
        cookies.append(
            {
                **cookies[0],
                "value": "partition-session",
                "partitionKey": "https://other.example",
            }
        )
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.cookies = AsyncMock(return_value=cookies)
        browser._get_or_create_shared_browser = AsyncMock(
            return_value=(None, None, context)
        )
        result = await browser.refresh_google_cookies(token_id=9)
        sessions = [
            cookie
            for cookie in json.loads(result["google_cookies"])
            if cookie["name"] == "OSID"
        ]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["value"], "session-old")
        page.close.assert_awaited_once()

    async def test_playwright_refresh_closes_page_on_failure(self):
        browser = TokenBrowser.__new__(TokenBrowser)
        browser._semaphore = asyncio.Semaphore(1)
        browser._solve_inflight = 0
        browser.note_idle = MagicMock()
        page = MagicMock(url="https://flow.google.com/projects")
        page.goto = AsyncMock(side_effect=RuntimeError("timeout"))
        page.close = AsyncMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        browser._get_or_create_shared_browser = AsyncMock(
            return_value=(None, None, context)
        )
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            await browser.refresh_google_cookies(token_id=9)
        page.close.assert_awaited_once()
        self.assertEqual(browser._solve_inflight, 0)
        self.assertEqual(browser._semaphore._value, 1)

    async def test_personal_refresh_exports_bound_context_without_next_auth_cookie(
        self,
    ):
        service = PersonalService.__new__(PersonalService)
        resident = types.SimpleNamespace(
            tab=MagicMock(),
            solve_lock=asyncio.Lock(),
            token_id=9,
            browser_context_id="account-context",
            recaptcha_ready=True,
        )
        service._mark_runtime_active = MagicMock()
        service.initialize = AsyncMock()
        service._ensure_resident_tab = AsyncMock(return_value=("slot", resident))
        service._consume_resident_slot_reservation = AsyncMock()
        service._release_resident_slot_reservation = AsyncMock()
        service._apply_token_cookie_binding = AsyncMock(return_value=True)
        service._tab_get = AsyncMock()
        service._tab_evaluate = AsyncMock(
            return_value={
                "host": "flow.google.com",
                "ready": "complete",
                "email": EMAIL,
            }
        )
        cookies = json.loads(NEW_COOKIES)
        cookies.append(
            {
                "name": "__Host-GAPS",
                "value": "account-cookie",
                "domain": "accounts.google.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
        )
        service._get_browser_cookies = AsyncMock(return_value=cookies)
        result = await service.refresh_google_cookies(None, token_id=9)
        self.assertEqual(result["email"], EMAIL)
        self.assertIn("OSID", result["google_cookies"])
        stored_cookies = json.loads(result["google_cookies"])
        account_cookie = next(
            cookie for cookie in stored_cookies if cookie["name"] == "__Host-GAPS"
        )
        self.assertEqual(account_cookie["url"], "https://accounts.google.com/")
        self.assertNotIn(
            "__Host-GAPS",
            {name for name, _ in cookie_pairs_for_url(result["google_cookies"])},
        )
        self.assertIn(
            "__Host-GAPS",
            {
                name
                for name, _ in cookie_pairs_for_url(
                    result["google_cookies"], "https://accounts.google.com/"
                )
            },
        )
        self.assertEqual(
            service._get_browser_cookies.call_args.kwargs["browser_context_id"],
            "account-context",
        )
        service._consume_resident_slot_reservation.assert_awaited_once()
        service._release_resident_slot_reservation.assert_not_awaited()

    async def test_personal_cancel_while_waiting_releases_reservation(self):
        service = PersonalService.__new__(PersonalService)
        lock = asyncio.Lock()
        await lock.acquire()
        resident = types.SimpleNamespace(tab=MagicMock(), solve_lock=lock)
        service._mark_runtime_active = MagicMock()
        service.initialize = AsyncMock()
        service._ensure_resident_tab = AsyncMock(return_value=("slot", resident))
        service._release_resident_slot_reservation = AsyncMock()
        task = asyncio.create_task(service.refresh_google_cookies(None, token_id=9))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        service._release_resident_slot_reservation.assert_awaited_once_with(
            "slot", resident
        )
        lock.release()


if __name__ == "__main__":
    unittest.main()
