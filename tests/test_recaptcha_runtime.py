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


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the JavaScript contract")
class RecaptchaExecutionContractTests(unittest.TestCase):
    def run_contract(self, setup, assertions, *, virtual_time=False, timeout_ms=150):
        source = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
  let now = 0;
  const sandbox = { setTimeout, clearTimeout, location: { href: 'https://flow.google.com/project/example' } };
  if (input.virtualTime) {
    sandbox.Date = { now: () => now };
    sandbox.setTimeout = (callback, delay) => setTimeout(() => { now += delay; callback(); }, 0);
  }
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`
    globalThis.nodes = [];
    globalThis.document = {
      createElement: () => ({
        children: [], style: {}, setAttribute() {},
        remove() { nodes.splice(nodes.indexOf(this), 1); }
      }),
      body: { appendChild(node) { nodes.push(node); } },
      documentElement: { children: ['head', 'body'] }
    };
  `, sandbox);
  vm.runInContext(input.setup, sandbox);
  sandbox.result = await vm.runInContext(input.script, sandbox);
  sandbox.elapsed = now;
  vm.runInContext(input.assertions, sandbox);
})().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            input=json.dumps({
                "setup": setup,
                "assertions": assertions,
                "script": build_recaptcha_execute_script('site-key"\\value', "IMAGE_GENERATION", timeout_ms),
                "virtualTime": virtual_time,
            }),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_capture_calls_direct_execute_with_receiver_and_action(self):
        self.run_contract(
            r"""
            globalThis.calls = [];
            globalThis.grecaptcha = { enterprise: {
              execute(key, options) {
                calls.push({ receiver: this === grecaptcha.enterprise, key, action: options.action });
                return Promise.resolve(' captcha ');
              },
              render() { throw Error('direct execute must precede render'); }
            }};
            """,
            r"""
            if (!result.ok || result.token !== 'captcha') throw Error(JSON.stringify(result));
            if (calls.length !== 1 || !calls[0].receiver) throw Error('incorrect execute receiver');
            if (calls[0].key !== 'site-key"\\value' || calls[0].action !== 'IMAGE_GENERATION') throw Error('changed arguments');
            """,
        )

    def test_empty_captured_and_rejected_direct_fall_back_to_fresh_render(self):
        self.run_contract(
            r"""
            globalThis.targets = [];
            globalThis.__flow2apiTrustedRecaptchaExecute = () => Promise.resolve('  ');
            globalThis.grecaptcha = { enterprise: {
              execute(target, options) {
                if (this !== grecaptcha.enterprise || options.action !== 'IMAGE_GENERATION') throw Error('changed execute context');
                targets.push(target);
                if (target === 7) return Promise.resolve('captcha');
                return Promise.reject(new Error('direct unavailable'));
              },
              render(container, options) {
                if (container === document.documentElement || container.children.length) throw Error('container is not empty');
                if (!nodes.includes(container) || options.sitekey !== 'site-key"\\value') throw Error('invalid render');
                if (options.size !== 'invisible') throw Error('widget must remain invisible');
                return 7;
              }
            }};
            """,
            r"""
            if (!result.ok || result.token !== 'captcha') throw Error(JSON.stringify(result));
            if (targets.length !== 2 || targets[1] !== 7) throw Error('render fallback was not executed');
            if (nodes.length) throw Error('render container leaked');
            """,
        )

    def test_missing_action_guard_blocks_both_public_paths(self):
        self.run_contract(
            r"""
            globalThis.calls = 0;
            globalThis.grecaptcha = { enterprise: {
              execute() { calls += 1; return Promise.resolve('extension_hijack_detected'); },
              render() { calls += 1; return 7; }
            }};
            """,
            r"""
            if (result.ok || !result.error.includes('action guard')) throw Error(JSON.stringify(result));
            if (calls !== 0) throw Error('unguarded public execution was attempted');
            """,
        )

    def test_trusted_capture_can_succeed_without_public_action_guard(self):
        self.run_contract(
            r"""
            globalThis.__flow2apiTrustedRecaptchaExecute = () => Promise.resolve('captcha');
            globalThis.grecaptcha = { enterprise: {
              execute() { throw Error('extension_hijack_detected'); },
              render() { throw Error('public render was attempted'); }
            }};
            """,
            "if (!result.ok || result.token !== 'captcha') throw Error(JSON.stringify(result));",
        )

    def test_hung_strategies_leave_render_within_total_budget(self):
        self.run_contract(
            r"""
            globalThis.__flow2apiTrustedRecaptchaExecute = () => new Promise(() => {});
            globalThis.grecaptcha = { enterprise: {
              execute(target) { return target === 7 ? Promise.resolve('captcha') : new Promise(() => {}); },
              render() { return 7; }
            }};
            """,
            r"""
            if (!result.ok || result.token !== 'captcha') throw Error(JSON.stringify(result));
            if (elapsed >= 150) throw Error('fallback exhausted the total budget: ' + elapsed);
            if (nodes.length) throw Error('render container leaked');
            """,
            virtual_time=True,
        )

    def test_late_captured_success_survives_failed_fallbacks(self):
        self.run_contract(
            r"""
            globalThis.__flow2apiTrustedRecaptchaExecute = () => new Promise(
              resolve => setTimeout(() => resolve(' captcha '), 300)
            );
            globalThis.grecaptcha = { enterprise: {
              execute() { return Promise.reject(new Error('public unavailable')); },
              render() { return 7; }
            }};
            """,
            r"""
            if (!result.ok || result.token !== 'captcha') throw Error(JSON.stringify(result));
            if (nodes.length) throw Error('render container leaked');
            """,
            timeout_ms=600,
        )

    def test_late_captured_success_interrupts_pending_fallback(self):
        self.run_contract(
            r"""
            globalThis.renders = 0;
            globalThis.__flow2apiTrustedRecaptchaExecute = () => new Promise(
              resolve => setTimeout(() => resolve('captcha'), 300)
            );
            globalThis.grecaptcha = { enterprise: {
              execute() { return new Promise(() => {}); },
              render() { renders += 1; return 7; }
            }};
            """,
            r"""
            if (!result.ok || result.token !== 'captcha') throw Error(JSON.stringify(result));
            if (renders !== 0) throw Error('render launched after a valid captured result');
            """,
            timeout_ms=600,
        )

    def test_late_direct_success_survives_render_failure(self):
        self.run_contract(
            r"""
            globalThis.__flow2apiTrustedRecaptchaExecute = () => new Promise(() => {});
            globalThis.grecaptcha = { enterprise: {
              execute(target) {
                if (target === 7) return Promise.reject(new Error('widget unavailable'));
                return new Promise(resolve => setTimeout(() => resolve('captcha'), 250));
              },
              render() { return 7; }
            }};
            """,
            r"""
            if (!result.ok || result.token !== 'captcha') throw Error(JSON.stringify(result));
            if (nodes.length) throw Error('render container leaked');
            """,
            timeout_ms=600,
        )

    def test_all_pending_strategies_obey_total_timeout_and_clean_container(self):
        self.run_contract(
            r"""
            globalThis.__flow2apiTrustedRecaptchaExecute = () => new Promise(() => {});
            globalThis.grecaptcha = { enterprise: {
              execute() { return new Promise(() => {}); },
              render() { return 7; }
            }};
            """,
            r"""
            if (result.ok || !result.error.includes('timeout')) throw Error(JSON.stringify(result));
            if (elapsed > 150) throw Error('total timeout exceeded: ' + elapsed);
            if (nodes.length) throw Error('timed-out render container leaked');
            """,
            virtual_time=True,
        )

    def test_late_rejection_does_not_break_successful_fallback(self):
        self.run_contract(
            r"""
            globalThis.__flow2apiTrustedRecaptchaExecute = () => new Promise(
              (resolve, reject) => setTimeout(() => reject(new Error('late capture failure')), 300)
            );
            globalThis.grecaptcha = { enterprise: {
              execute() { return Promise.resolve('captcha'); },
              render() { throw Error('render must not run'); }
            }};
            """,
            "if (!result.ok || result.token !== 'captcha') throw Error(JSON.stringify(result));",
            timeout_ms=600,
        )

    def test_failures_preserve_each_strategy_error_and_clean_container(self):
        self.run_contract(
            r"""
            globalThis.__flow2apiTrustedRecaptchaExecute = () => { throw Error('capture failed'); };
            globalThis.grecaptcha = { enterprise: {
              execute(target) { return Promise.reject(new Error(target === 7 ? 'widget failed' : 'direct failed')); },
              render() { return 7; }
            }};
            """,
            r"""
            if (result.ok) throw Error('failed execution returned success');
            for (const message of ['capture failed', 'direct failed', 'widget failed']) {
              if (!result.error.includes(message)) throw Error('lost error: ' + message);
            }
            if (nodes.length) throw Error('failed render container leaked');
            """,
        )


if __name__ == "__main__":
    unittest.main()
