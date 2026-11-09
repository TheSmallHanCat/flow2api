import json
import shutil
import subprocess
import types
import unittest
from unittest.mock import AsyncMock

from src.services.browser_captcha import TokenBrowser
from src.services.recaptcha_runtime import (
    TRUSTED_RECAPTCHA_HOOK_SOURCE,
    build_recaptcha_execute_script,
)


class LocalCaptchaActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_actions_execute_without_native_generation(self):
        for action in ("IMAGE_GENERATION", "VIDEO_GENERATION", "UPLOAD_IMAGE"):
            with self.subTest(action=action):
                browser = object.__new__(TokenBrowser)
                browser.token_id = 7
                browser._apply_browser_environment_patch = AsyncMock()
                browser._prepare_flow_runtime_page = AsyncMock(return_value=True)
                browser._harvest_token_via_ui_submit = AsyncMock()
                page = types.SimpleNamespace(
                    add_init_script=AsyncMock(),
                    evaluate=AsyncMock(return_value={"ok": True, "token": "captcha"}),
                    close=AsyncMock(),
                )
                context = types.SimpleNamespace(new_page=AsyncMock(return_value=page))
                token = await browser._execute_captcha(
                    context, "project", "site-key", action
                )
                self.assertEqual(token, "captcha")
                self.assertIn(json.dumps(action), page.evaluate.call_args.args[0])
                browser._harvest_token_via_ui_submit.assert_not_awaited()
                self.assertIsNone(browser._last_harvest_session_id)
                page.close.assert_awaited_once()

    async def test_execution_failure_closes_page_without_chat_fallback(self):
        browser = object.__new__(TokenBrowser)
        browser.token_id = 7
        browser._apply_browser_environment_patch = AsyncMock()
        browser._prepare_flow_runtime_page = AsyncMock(return_value=True)
        browser._harvest_token_via_ui_submit = AsyncMock()
        page = types.SimpleNamespace(
            add_init_script=AsyncMock(),
            evaluate=AsyncMock(side_effect=RuntimeError("execution failed")),
            close=AsyncMock(),
        )
        token = await browser._execute_captcha(
            types.SimpleNamespace(new_page=AsyncMock(return_value=page)),
            "project",
            "site-key",
            "IMAGE_GENERATION",
        )
        self.assertIsNone(token)
        browser._harvest_token_via_ui_submit.assert_not_awaited()
        page.close.assert_awaited_once()

    @unittest.skipUnless(
        shutil.which("node"), "Node.js is required for the JavaScript contract"
    )
    def test_shared_execute_script_preserves_requested_action(self):
        source = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
  const sandbox = { setTimeout, clearTimeout, location: { href: 'https://flow.google.com/project/example' } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(input.hook, sandbox);
  vm.runInContext(`
    globalThis.calls = [];
    globalThis.grecaptcha = { enterprise: {
      execute: function(key, options) { calls.push({ key, action: options.action }); return Promise.resolve('captcha'); }
    }};
    const original = grecaptcha.enterprise.execute.bind(grecaptcha.enterprise);
    grecaptcha.enterprise.execute = function(key, options) {
      return original(key, Object.assign({}, options, { action: 'extension_hijack_detected' }));
    };
  `, sandbox);
  for (const script of input.scripts) {
    const result = await vm.runInContext(script, sandbox);
    if (!result.ok || result.token !== 'captcha') throw Error('captcha execution failed');
  }
  const actual = sandbox.calls.map(call => call.action);
  if (JSON.stringify(actual) !== JSON.stringify(input.actions)) throw Error('action mismatch');
  if (sandbox.calls.some(call => call.key !== input.siteKey)) throw Error('site key mismatch');
})().catch(error => { process.stderr.write(error.message); process.exitCode = 1; });
"""
        actions = [
            "IMAGE_GENERATION",
            "VIDEO_GENERATION",
            "UPLOAD_IMAGE",
            "CHAT_GENERATION",
        ]
        site_key = 'site-key"\\value'
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            input=json.dumps(
                {
                    "hook": TRUSTED_RECAPTCHA_HOOK_SOURCE,
                    "scripts": [
                        build_recaptcha_execute_script(site_key, action, 1000)
                        for action in actions
                    ],
                    "actions": actions,
                    "siteKey": site_key,
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


    @unittest.skipUnless(
        shutil.which("node"), "Node.js is required for the JavaScript contract"
    )
    def test_shared_execute_script_falls_back_to_direct_execute(self):
        """新版 Flow 不再调用 execute.bind()，hook 捕获不到闭包时必须直接执行。"""
        source = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
  const sandbox = { setTimeout, clearTimeout, location: { href: 'https://flow.google.com/project/example' } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(input.hook, sandbox);
  vm.runInContext(`
    globalThis.calls = [];
    globalThis.grecaptcha = { enterprise: {
      execute: function(key, options) {
        calls.push({ thisOK: this === grecaptcha.enterprise, key, action: options.action });
        return Promise.resolve('captcha');
      }
    }};
  `, sandbox);
  if (sandbox.__flow2apiTrustedRecaptchaExecute) throw Error('closure should not be captured');
  const result = await vm.runInContext(input.script, sandbox);
  if (!result.ok || result.token !== 'captcha') throw Error('captcha execution failed: ' + JSON.stringify(result));
  if (result.debug.captured !== false) throw Error('captured flag should be false');
  if (sandbox.calls.length !== 1) throw Error('expected one execute call, got ' + sandbox.calls.length);
  const call = sandbox.calls[0];
  if (!call.thisOK) throw Error('execute must be invoked with enterprise as receiver');
  if (call.key !== input.siteKey) throw Error('site key mismatch');
  if (call.action !== input.action) throw Error('action mismatch');
})().catch(error => { process.stderr.write(error.message); process.exitCode = 1; });
"""
        site_key = "site-key"
        action = "IMAGE_GENERATION"
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            input=json.dumps(
                {
                    "hook": TRUSTED_RECAPTCHA_HOOK_SOURCE,
                    "script": build_recaptcha_execute_script(site_key, action, 1000),
                    "siteKey": site_key,
                    "action": action,
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
