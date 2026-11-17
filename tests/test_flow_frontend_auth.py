import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.services.browser_cookie_utils import (
    serialize_cookie_header_for_url,
    validate_flow_cookie_storage,
)
from src.services.browser_captcha import BrowserCaptchaService, TokenBrowser
from src.services.flow_client import FlowClient


class FlowFrontendCookieTests(unittest.TestCase):
    def test_structured_export_keeps_matching_scopes_and_duplicate_names(self):
        cookies = [
            {
                "name": "SID",
                "value": "google-session",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
            },
            {
                "name": "OSID",
                "value": "parent-session",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
            },
            {
                "name": "OSID",
                "value": "flow-session",
                "domain": "flow.google.com",
                "path": "/",
                "hostOnly": True,
                "secure": True,
            },
            {
                "name": "SID",
                "value": "wrong-host",
                "domain": "accounts.google.com",
                "path": "/",
                "hostOnly": True,
                "secure": True,
            },
        ]

        header = serialize_cookie_header_for_url(json.dumps(cookies))

        self.assertIn("SID=google-session", header)
        self.assertIn("OSID=parent-session", header)
        self.assertIn("OSID=flow-session", header)
        self.assertNotIn("wrong-host", header)
        self.assertEqual(header.count("OSID="), 2)

    def test_browser_binding_accepts_structured_cookie_export(self):
        cookies = json.dumps(
            [
                {
                    "name": "SID",
                    "value": "google-session",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                },
                {
                    "name": "OSID",
                    "value": "flow-session",
                    "domain": "flow.google.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                },
            ]
        )

        targets = TokenBrowser._build_google_cookie_targets(cookies)

        self.assertEqual([item["name"] for item in targets], ["SID", "OSID"])
        self.assertTrue(all(item.get("httpOnly") for item in targets))

    def test_validation_rejects_flow_only_cookie_export(self):
        cookies = json.dumps(
            [
                {
                    "name": "OSID",
                    "value": "flow-session",
                    "domain": "flow.google.com",
                    "path": "/",
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "Google Cookies 不完整"):
            validate_flow_cookie_storage(cookies)

    def test_plain_cookie_header_preserves_duplicate_names(self):
        header = serialize_cookie_header_for_url(
            "SID=google-session; OSID=parent-session; OSID=flow-session"
        )

        self.assertEqual(header.count("OSID="), 2)


class FlowFrontendErrorTests(unittest.TestCase):
    def test_batchexecute_auth_error_reads_http_status_slot(self):
        frames = [["er", None, None, None, None, 401, None, None, None, 16]]

        with self.assertRaisesRegex(RuntimeError, r"code=401"):
            FlowClient._raise_batchexecute_error([frames], "nzlxg")


class BrowserHarvestSessionTests(unittest.TestCase):
    def test_session_id_is_carried_by_request_ref(self):
        browser_ref = BrowserCaptchaService._compose_browser_ref(2, "session-a")

        self.assertEqual(browser_ref, "2:session-a")
        self.assertEqual(
            FlowClient._extract_browser_captcha_session_id(browser_ref),
            "session-a",
        )
        self.assertIsNone(FlowClient._extract_browser_captcha_session_id(2))


class BrowserCaptchaRequestSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_returns_fingerprint_with_token_and_session(self):
        service = BrowserCaptchaService(db=None)
        browser = MagicMock()
        browser.get_token = AsyncMock(return_value=("captcha-token", "session-a"))
        browser.get_last_fingerprint.return_value = {
            "proxy_url": "http://account-proxy:8080"
        }
        service._check_available = MagicMock()
        service._select_browser_id = AsyncMock(return_value=0)
        service._get_or_create_browser = AsyncMock(return_value=browser)
        service._release_slot_reservation = AsyncMock()
        service._resolve_token_proxy_url = AsyncMock(return_value=None)
        service._log_stats = MagicMock()

        token, browser_ref, fingerprint = await service.get_token(
            "project-a", token_id=1
        )

        try:
            self.assertEqual(token, "captcha-token")
            browser_id, request_ref = service._parse_browser_ref(browser_ref)
            self.assertEqual(browser_id, 0)
            self.assertTrue(request_ref.startswith("lease-"))
            self.assertIn(browser_ref, service._requests)
            self.assertEqual(
                FlowClient._extract_browser_captcha_session_id(browser_ref), "session-a"
            )
            self.assertEqual(fingerprint["proxy_url"], "http://account-proxy:8080")
        finally:
            await service.report_request_finished(browser_ref)
        self.assertFalse(service._requests)

    async def test_third_party_captcha_preserves_account_proxy(self):
        proxy_manager = MagicMock()
        proxy_manager.get_request_proxy_url = AsyncMock(
            return_value="http://global-proxy:8080"
        )
        client = FlowClient(proxy_manager=proxy_manager)
        client._set_request_fingerprint(
            {"proxy_url": "http://account-proxy:8080"}
        )
        client._get_api_captcha_token = AsyncMock(
            return_value=("captcha-token", "Mozilla/5.0 Chrome/136.0.0.0")
        )

        token, _ = await client._get_recaptcha_token(
            "project-a",
            token_id=1,
            method_override="yescaptcha",
        )

        self.assertEqual(token, "captcha-token")
        self.assertEqual(
            client.get_request_fingerprint()["proxy_url"],
            "http://account-proxy:8080",
        )
        self.assertEqual(
            client._get_api_captcha_token.await_args.kwargs["proxy_url"],
            "http://account-proxy:8080",
        )
        proxy_manager.get_request_proxy_url.assert_not_awaited()


class FlowFrontendLoginValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_st_to_at_rejects_incomplete_cookie_before_rpc(self):
        client = FlowClient(proxy_manager=None)

        with self.assertRaisesRegex(ValueError, "Google Cookies 不完整"):
            await client.st_to_at(google_cookies="OSID=flow-session")

    async def test_rpc_serializes_cookies_for_each_actual_url(self):
        client = FlowClient(proxy_manager=None)
        bootstrap_headers = []
        rpc_headers = []

        async def load_bootstrap(page_url, cookie_header, timeout, *, cookie_jar=None):
            bootstrap_headers.append(cookie_header)
            return (
                'boq_labs-ai-sandbox-frontend_test '
                '"FdrFJe":"123456789"'
            )

        async def make_request(*args, **kwargs):
            rpc_headers.append(kwargs["headers"]["Cookie"])
            return json.dumps([["wrb.fr", "nzlxg", "[]"]])

        client._load_flow_frontend_bootstrap = AsyncMock(side_effect=load_bootstrap)
        client._make_text_request = AsyncMock(side_effect=make_request)
        cookies = json.dumps(
            [
                {
                    "name": "SID",
                    "value": "root",
                    "domain": ".google.com",
                    "path": "/",
                },
                {
                    "name": "PROJECT_ONLY",
                    "value": "project",
                    "domain": "flow.google.com",
                    "path": "/projects",
                },
                {
                    "name": "RPC_ONLY",
                    "value": "rpc",
                    "domain": "flow.google.com",
                    "path": "/_/AiSandboxAngularFrontend/data/batchexecute",
                },
            ]
        )

        await client._call_flow_frontend_rpc(
            rpc_id="nzlxg",
            argument=[],
            cookie_header="",
            timeout=10,
            cookie_storage=cookies,
        )

        self.assertIn("PROJECT_ONLY=project", bootstrap_headers[0])
        self.assertNotIn("RPC_ONLY=rpc", bootstrap_headers[0])
        self.assertIn("RPC_ONLY=rpc", rpc_headers[0])
        self.assertNotIn("PROJECT_ONLY=project", rpc_headers[0])


if __name__ == "__main__":
    unittest.main()
