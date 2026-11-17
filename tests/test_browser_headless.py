"""Docker availability checks for headed and headless browser captcha."""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.core.config import config


class BrowserHeadlessTests(unittest.TestCase):
    def load_browser_module(self, *, headless=False, allow_headed=False):
        source = Path(__file__).parents[1] / "src/services/browser_captcha.py"
        spec = importlib.util.spec_from_file_location(
            "src.services._browser_headless_tests", source
        )
        module = importlib.util.module_from_spec(spec)
        playwright = types.ModuleType("playwright")
        playwright.__path__ = []
        api = types.ModuleType("playwright.async_api")
        api.async_playwright = Mock()
        api.Route = object
        api.BrowserContext = object
        environment = {"DOCKER_CONTAINER": "1"}
        if allow_headed:
            environment["ALLOW_DOCKER_HEADED_CAPTCHA"] = "true"

        with (
            patch.dict(config._config, {"captcha": {"browser_headless": headless}}),
            patch.dict(os.environ, environment, clear=True),
            patch.dict(
                sys.modules,
                {
                    spec.name: module,
                    "playwright": playwright,
                    "playwright.async_api": api,
                },
            ),
            patch(
                "subprocess.run",
                return_value=types.SimpleNamespace(
                    returncode=0, stdout=str(source), stderr=""
                ),
            ) as run,
            patch("builtins.print"),
        ):
            spec.loader.exec_module(module)

        # Keep each imported module's runtime configuration independent.
        module.config = types.SimpleNamespace(browser_headless=headless)
        return module, run

    def test_docker_headless_imports_and_runs_without_display(self):
        for allow_headed in (False, True):
            with self.subTest(allow_headed=allow_headed):
                module, run = self.load_browser_module(
                    headless=True, allow_headed=allow_headed
                )
                self.assertTrue(module.PLAYWRIGHT_AVAILABLE)
                self.assertIsNotNone(module.async_playwright)
                # Browser detection is mocked; no package/browser installation runs.
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][1], "-c")
                with patch.dict(os.environ, {"DISPLAY": ""}):
                    module.BrowserCaptchaService._check_available(None)

    def test_docker_headed_stays_blocked_without_permission(self):
        module, run = self.load_browser_module()
        self.assertFalse(module.PLAYWRIGHT_AVAILABLE)
        run.assert_not_called()
        with patch.dict(os.environ, {"DISPLAY": ":99"}):
            with self.assertRaisesRegex(RuntimeError, "ALLOW_DOCKER_HEADED_CAPTCHA"):
                module.BrowserCaptchaService._check_available(None)

    def test_docker_headed_still_requires_display(self):
        module, _ = self.load_browser_module(allow_headed=True)
        self.assertTrue(module.PLAYWRIGHT_AVAILABLE)
        with patch.dict(os.environ, {"DISPLAY": ""}):
            with self.assertRaisesRegex(RuntimeError, "DISPLAY"):
                module.BrowserCaptchaService._check_available(None)
        with patch.dict(os.environ, {"DISPLAY": ":99"}):
            module.BrowserCaptchaService._check_available(None)

    def test_headless_still_requires_playwright(self):
        module, _ = self.load_browser_module(headless=True)
        with patch.dict(os.environ, {"DISPLAY": ""}):
            for available, runtime in ((False, object()), (True, None)):
                with self.subTest(available=available, runtime=runtime):
                    module.PLAYWRIGHT_AVAILABLE = available
                    module.async_playwright = runtime
                    with self.assertRaisesRegex(RuntimeError, "playwright"):
                        module.BrowserCaptchaService._check_available(None)

    def test_switching_to_headed_rechecks_docker_permission(self):
        module, _ = self.load_browser_module(headless=True)
        module.config.browser_headless = False
        with patch.dict(os.environ, {"DISPLAY": ":99"}):
            with self.assertRaisesRegex(RuntimeError, "ALLOW_DOCKER_HEADED_CAPTCHA"):
                module.BrowserCaptchaService._check_available(None)


if __name__ == "__main__":
    unittest.main()
