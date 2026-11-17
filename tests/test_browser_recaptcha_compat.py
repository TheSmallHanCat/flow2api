"""reCAPTCHA loading coverage with intercepted browser requests."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlparse

with patch.dict(os.environ, {"DOCKER_CONTAINER": "1", "ALLOW_DOCKER_HEADED_CAPTCHA": "false"}):
    from src.services.browser_captcha import TokenBrowser


class RecaptchaReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_page_needs_no_injection(self):
        browser = object.__new__(TokenBrowser)
        browser.token_id = 7
        browser._inject_recaptcha_script = AsyncMock()
        page = SimpleNamespace(evaluate=AsyncMock(return_value=True))
        self.assertTrue(await browser._wait_for_enterprise_ready(
            page, "site-key", "https://www.google.com", "https://www.recaptcha.net"
        ))
        browser._inject_recaptcha_script.assert_not_awaited()

    async def test_poll_recovers_after_navigation_error(self):
        browser = object.__new__(TokenBrowser)
        browser.token_id = 7
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[RuntimeError("context destroyed"), False, True]),
            is_closed=Mock(return_value=False),
            wait_for_function=AsyncMock(),
        )
        self.assertTrue(await browser._poll_enterprise_ready(page, 1000))
        page.wait_for_function.assert_not_awaited()

    async def test_poll_times_out_and_cancels_stalled_evaluation(self):
        browser = object.__new__(TokenBrowser)
        browser.token_id = 7
        cancelled = asyncio.Event()

        async def stalled_evaluate(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=stalled_evaluate),
            is_closed=Mock(return_value=False),
        )
        result = await asyncio.wait_for(
            browser._poll_enterprise_ready(page, 10), timeout=1.0
        )
        self.assertFalse(result)
        self.assertTrue(cancelled.is_set())
        page.evaluate.assert_awaited_once()

    async def test_poll_skips_evaluation_when_budget_is_exhausted(self):
        browser = object.__new__(TokenBrowser)
        browser.token_id = 7
        page = SimpleNamespace(evaluate=AsyncMock(return_value=True))
        self.assertFalse(await browser._poll_enterprise_ready(page, 0))
        page.evaluate.assert_not_awaited()

    async def test_cancelling_poll_cancels_pending_evaluation(self):
        browser = object.__new__(TokenBrowser)
        browser.token_id = 7
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def stalled_evaluate(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=stalled_evaluate),
            is_closed=Mock(return_value=False),
        )
        task = asyncio.create_task(browser._poll_enterprise_ready(page, 1000))
        try:
            await asyncio.wait_for(started.wait(), timeout=1.0)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())

    async def test_custom_captcha_stops_when_runtime_is_not_ready(self):
        for enterprise in (False, True):
            with self.subTest(enterprise=enterprise):
                browser = object.__new__(TokenBrowser)
                browser.token_id = 7
                browser._browser_proxy_active = False
                browser._apply_browser_environment_patch = AsyncMock()
                browser._wait_for_enterprise_ready = AsyncMock(return_value=False)
                browser._capture_page_fingerprint = AsyncMock()
                page = SimpleNamespace(
                    add_init_script=AsyncMock(), on=Mock(), goto=AsyncMock(),
                    evaluate=AsyncMock(return_value="complete"), bring_to_front=AsyncMock(),
                    mouse=SimpleNamespace(move=AsyncMock(), wheel=AsyncMock()), close=AsyncMock(),
                )
                context = SimpleNamespace(new_page=AsyncMock(return_value=page))
                with patch("src.services.browser_captcha.asyncio.sleep", new=AsyncMock()):
                    result = await browser._execute_custom_captcha(
                        context, "https://captcha.invalid/", "site-key", "login", enterprise=enterprise
                    )
                self.assertIsNone(result)
                self.assertEqual(browser._wait_for_enterprise_ready.call_args.kwargs["enterprise"], enterprise)
                self.assertTrue(all("grecaptcha" not in call.args[0] for call in page.evaluate.call_args_list))
                browser._capture_page_fingerprint.assert_not_awaited()
                page.close.assert_awaited_once()


class RecaptchaCspBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        if not Path(self.playwright.chromium.executable_path).is_file():
            await self.playwright.stop()
            if os.name == "nt":
                cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ms-playwright"
            elif sys.platform == "darwin":
                cache = Path.home() / "Library/Caches/ms-playwright"
            else:
                cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"
            with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}):
                self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        if not Path(self.playwright.chromium.executable_path).is_file():
            self.skipTest("Playwright Chromium is not installed")
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.solver = object.__new__(TokenBrowser)
        self.solver.token_id = 7

    async def make_page(self, *, failed_hosts=(), native_failed_script=False, enterprise=True):
        page = await self.browser.new_page()
        requests = []
        primary = "https://www.google.com/recaptcha/enterprise.js?render=site-key"
        html = '<!doctype html><html><head><script nonce="captcha-test">window.loaded = true;</script>'
        if native_failed_script:
            html += f'<script nonce="captcha-test" src="{primary}"></script>'
        html += "</head><body></body></html>"

        async def route_request(route):
            parsed = urlparse(route.request.url)
            if route.request.url == "https://captcha.invalid/":
                await route.fulfill(content_type="text/html", body=html, headers={
                    "Content-Security-Policy": "require-trusted-types-for 'script'; script-src 'nonce-captcha-test'",
                })
            elif parsed.path in ("/recaptcha/enterprise.js", "/recaptcha/api.js"):
                requests.append(route.request.url)
                if parsed.hostname in failed_hosts:
                    await route.abort("failed")
                else:
                    body = "window.grecaptcha={enterprise:{execute:()=>Promise.resolve('captcha')}}" if enterprise else (
                        "window.grecaptcha={execute:()=>Promise.resolve('captcha')}"
                    )
                    await route.fulfill(content_type="text/javascript", body=body)
            else:
                await route.abort()

        await page.route("**/*", route_request)
        await page.goto("https://captcha.invalid/")
        return page, requests

    async def test_nonce_and_trusted_types_allow_injected_script(self):
        page, requests = await self.make_page()
        self.assertTrue(await self.solver._inject_recaptcha_script(
            page, "https://www.google.com/recaptcha/enterprise.js?render=site-key", timeout_ms=1000
        ))
        self.assertTrue(await self.solver._poll_enterprise_ready(page, 100))
        self.assertEqual(len(requests), 1)
        self.assertEqual(await page.locator("script[src]").evaluate("node => node.nonce"), "captcha-test")

    async def test_failed_native_tag_does_not_suppress_secondary_host(self):
        page, requests = await self.make_page(failed_hosts=("www.google.com",), native_failed_script=True)
        self.assertTrue(await self.solver._wait_for_enterprise_ready(
            page, "site-key", "https://www.google.com", "https://www.recaptcha.net", timeout_ms=1000
        ))
        self.assertEqual([urlparse(url).hostname for url in requests], [
            "www.google.com", "www.google.com", "www.recaptcha.net"
        ])
        # Only the original failed native tag and the successful fallback remain.
        self.assertEqual(await page.locator("script[src]").count(), 2)

    async def test_both_hosts_failed_return_false_and_remove_owned_tags(self):
        page, requests = await self.make_page(failed_hosts=("www.google.com", "www.recaptcha.net"))
        self.assertFalse(await self.solver._wait_for_enterprise_ready(
            page, "site-key", "https://www.google.com", "https://www.recaptcha.net", timeout_ms=1000
        ))
        self.assertEqual([urlparse(url).hostname for url in requests], ["www.google.com", "www.recaptcha.net"])
        self.assertEqual(await page.locator("script[src]").count(), 0)

    async def test_standard_api_uses_its_own_readiness_and_script(self):
        page, requests = await self.make_page(enterprise=False)
        self.assertTrue(await self.solver._wait_for_enterprise_ready(
            page, "site-key", "https://www.google.com", "https://www.recaptcha.net", timeout_ms=1000,
            enterprise=False,
        ))
        self.assertEqual(urlparse(requests[0]).path, "/recaptcha/api.js")
        self.assertFalse(await page.evaluate("() => !!window.grecaptcha.enterprise"))

    async def test_script_load_timeout_removes_owned_tag(self):
        page, _ = await self.make_page()
        release = asyncio.Event()

        async def hold_request(route):
            await release.wait()
            await route.abort()

        await page.route("**/recaptcha/enterprise.js*", hold_request)
        try:
            self.assertFalse(await self.solver._inject_recaptcha_script(
                page, "https://www.google.com/recaptcha/enterprise.js?render=site-key", timeout_ms=100
            ))
            self.assertEqual(await page.locator("script[src]").count(), 0)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
