"""Browser checks use local pages and mocked API responses, without real accounts."""

import asyncio
import base64
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ui_server():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in {"/", "/login", "/manage", "/test"}:
                name = "login" if path == "/" else path[1:]
                self.path = "/static/" + name + ".html"
            elif (
                not path.startswith("/static/")
                and path
                not in {
                    "/extension/options.html",
                    "/extension/options.js",
                    "/extension/locales.json",
                }
            ) or ".." in path.split("/"):
                self.send_error(404)
                return
            super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def browser_page(ui_server):
    with playwright_api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed")
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="en-US")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(
            "https://cdn.tailwindcss.com/**",
            lambda route: route.fulfill(
                content_type="application/javascript",
                body="window.tailwind={};document.head.insertAdjacentHTML('beforeend','<style>.hidden{display:none!important}</style>');",
            ),
        )

        def api_response(route):
            path = urlsplit(route.request.url).path
            data = {"success": True}
            status = 200
            if path == "/api/stats":
                if "/login" in route.request.headers.get("referer", ""):
                    status = 401
                data = {"total_tokens": 1, "active_tokens": 1}
            elif path == "/api/tokens":
                data = [
                    {
                        "id": 1,
                        "email": "account@example.com",
                        "remark": "保存",
                        "is_active": True,
                        "protocol_mode": "protocol",
                        "credits": 100,
                        "image_enabled": True,
                        "video_enabled": True,
                    }
                ]
            elif path == "/api/logs":
                data = [
                    {
                        "id": 1,
                        "operation": "generate_image",
                        "token_email": "account@example.com",
                        "status_code": 200,
                        "progress": 100,
                        "created_at": "2026-10-26T10:00:00Z",
                        "duration": 1.5,
                    }
                ]
            elif path == "/api/logs/1":
                data = {
                    "id": 1,
                    "operation": "generate_image",
                    "status_code": 200,
                    "progress": 100,
                    "created_at": "2026-10-26T10:00:00Z",
                    "duration": 1.5,
                    "request_body": '{"prompt":"保存"}',
                    "response_body": '{"message":"保存"}',
                }
            elif path == "/api/captcha/config":
                data = {"captcha_method": "yescaptcha"}
            elif path == "/api/token-refresh/config":
                data = {"enabled": True, "refresh_interval_minutes": 120}
            route.fulfill(
                status=status, content_type="application/json", body=json.dumps(data)
            )

        page.route("**/api/**", api_response)
        try:
            yield page, ui_server
            assert errors == []
        finally:
            context.close()
            browser.close()


def open_page(page, address):
    page.goto(address)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "window.I18n && document.querySelector('[data-language-select]').value === I18n.locale"
    )


def test_login_uses_json_post_while_translation_catalog_is_pending(browser_page):
    page, base = browser_page
    pending = []
    navigations = []
    page.route("**/static/locales/login.json", lambda route: pending.append(route))
    page.route(
        "**/api/login",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"detail":"Invalid credentials"}',
        ),
    )
    page.on(
        "request",
        lambda request: navigations.append(request.url)
        if request.is_navigation_request()
        else None,
    )
    try:
        page.goto(base + "/login", wait_until="domcontentloaded")
        page.locator("#username").fill("review-user")
        page.locator("#password").fill("review-password")
        assert pending
        with page.expect_request("**/api/login") as submitted:
            page.locator("#loginButton").click()
        assert submitted.value.method == "POST"
        assert submitted.value.post_data_json == {
            "username": "review-user",
            "password": "review-password",
        }
        assert page.url == base + "/login"
        assert all("password=" not in url for url in navigations)
    finally:
        for route in pending:
            route.fulfill(
                content_type="application/json",
                body=(ROOT / "static/locales/login.json").read_text(encoding="utf-8"),
            )
    page.wait_for_load_state("networkidle")
    playwright_api.expect(page.locator("#loginButton")).to_have_text("Sign in")


def test_login_never_puts_credentials_in_url_when_javascript_is_disabled(browser_page):
    owner, base = browser_page
    context = owner.context.browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.route(
            "**/api/login",
            lambda route: route.fulfill(status=400, content_type="text/html", body=""),
        )
        page.goto(base + "/login")
        page.locator("#username").fill("review-user")
        page.locator("#password").fill("review-password")
        with page.expect_request("**/api/login") as submitted:
            page.locator("#loginButton").click()
        assert submitted.value.method == "POST"
        assert urlsplit(submitted.value.url).query == ""
        assert "password=review-password" in submitted.value.post_data
    finally:
        context.close()


def test_login_language_persists_and_failure_restores_button(browser_page):
    page, base = browser_page
    page.route(
        "**/api/login",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"detail":"Invalid credentials"}',
        ),
    )
    open_page(page, base + "/login")
    assert page.title() == "Sign in - Flow2API"
    page.locator("#username").fill("保存")
    page.locator("#password").fill("test-password")
    page.locator("[data-language-select]").select_option("zh-CN")
    assert page.locator("#username").input_value() == "保存"
    assert page.locator("#password").input_value() == "test-password"
    assert page.locator("#loginButton").inner_text() == "登录"
    page.locator("[data-language-select]").select_option("en")
    page.locator("#loginButton").click()
    playwright_api.expect(page.locator("#loginButton")).to_have_text("Sign in")
    playwright_api.expect(
        page.get_by_text("Invalid username or password", exact=True)
    ).to_be_visible()
    page.locator("[data-language-select]").select_option("zh-CN")
    assert page.evaluate("localStorage.getItem('flow2api.language')") == "zh-CN"
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("html").get_attribute("lang") == "zh-CN"
    open_page(page, base + "/test")
    assert page.locator("html").get_attribute("lang") == "zh-CN"


def test_management_switch_preserves_forms_and_log_payload(browser_page):
    page, base = browser_page
    open_page(page, base + "/manage")
    page.wait_for_function("typeof window.openAddModal === 'function'")
    playwright_api.expect(page.locator("#tabTokens")).to_have_text("Token Management")
    page.evaluate("openAddModal()")
    page.locator("#addTokenRemark").fill("保存")
    page.locator("#addTokenGoogleCookies").fill("SID=local-test")
    page.locator("[data-language-select]").select_option("zh-CN")
    assert page.locator("#addTokenRemark").input_value() == "保存"
    assert page.locator("#addTokenGoogleCookies").input_value() == "SID=local-test"
    assert "hidden" not in page.locator("#addModal").get_attribute("class").split()
    page.evaluate("closeAddModal(); switchTab('logs')")
    page.wait_for_selector("#logsTableBody tr")
    page.evaluate("showLogDetail(1)")
    playwright_api.expect(page.locator("#logDetailContent")).to_contain_text(
        '"prompt": "保存"'
    )
    before = page.locator("#logDetailContent pre").first.inner_text()
    page.locator("[data-language-select]").select_option("en")
    assert page.locator("#logDetailContent pre").first.inner_text() == before
    playwright_api.expect(page.locator("#logDetailContent")).to_contain_text(
        "Request data"
    )
    assert "hidden" not in page.locator("#panelLogs").get_attribute("class").split()


def test_log_status_colors_do_not_depend_on_language(browser_page):
    page, base = browser_page
    records = [
        {"id": 1, "status_text": "processing", "status_code": 102},
        {"id": 2, "status_text": "completed", "status_code": 200},
        {"id": 3, "status_text": "failed", "status_code": 500},
        {"id": 4, "status_text": "video_polling", "status_code": 102},
        {"id": 5, "status_text": "", "status_code": 200},
        {"id": 6, "status_text": "", "status_code": 503},
        {"id": 7, "status_text": "unknown", "status_code": None},
        {"id": 8, "status_text": "completed", "status_code": 102},
    ]
    expected = [
        "bg-amber-50 text-amber-700",
        "bg-green-50 text-green-700",
        "bg-red-50 text-red-700",
        "bg-amber-50 text-amber-700",
        "bg-green-50 text-green-700",
        "bg-red-50 text-red-700",
        "bg-gray-100 text-gray-700",
        "bg-green-50 text-green-700",
    ]
    requests = []

    def respond(route):
        requests.append(route.request.url)
        route.fulfill(content_type="application/json", body=json.dumps(records))

    page.route(re.compile(r"/api/logs(?:\?.*)?$"), respond)
    open_page(page, base + "/manage")
    page.evaluate("switchTab('logs')")
    rows = page.locator("#logsTableBody tr")
    playwright_api.expect(rows).to_have_count(len(records))
    for language in ["en", "zh-CN", "en"]:
        page.locator("[data-language-select]").select_option(language)
        for index, classes in enumerate(expected):
            badge = rows.nth(index).locator("td").nth(2).locator("span").first
            for css_class in classes.split():
                playwright_api.expect(badge).to_have_class(
                    re.compile(r"\b" + css_class + r"\b")
                )
    assert len(requests) == 1
    assert page.evaluate("window.allLogs") == records


def test_proxy_validation_hint_follows_language_without_reloading_form(browser_page):
    page, base = browser_page
    requests = []
    page.route("**/api/proxy/test", lambda route: requests.append(route.request))
    open_page(page, base + "/manage")
    page.evaluate("switchTab('settings')")
    page.wait_for_load_state("networkidle")
    page.locator("#cfgAdminUsername").fill("review-name")
    page.locator("#btnTestProxy").click()
    result = page.locator("#proxyTestResult")
    playwright_api.expect(result).to_have_text(
        "Please enter at least one proxy URL first"
    )
    page.locator("[data-language-select]").select_option("zh-CN")
    playwright_api.expect(result).to_have_text("请先填写至少一个代理地址")
    assert page.locator("#cfgAdminUsername").input_value() == "review-name"
    assert requests == []


def test_proxy_results_retranslate_without_repeating_requests(browser_page):
    page, base = browser_page
    diagnostic = "upstream_diagnostic 保存 rpc=as29s"
    requests = []

    def respond(route):
        request = route.request.post_data_json
        requests.append(request)
        if request["proxy_url"].startswith("http:"):
            data = {
                "success": True,
                "message": "Proxy is working",
                "status_code": 200,
                "elapsed_ms": 12,
            }
        else:
            data = {
                "success": False,
                "message": diagnostic,
                "status_code": 502,
                "elapsed_ms": 34,
            }
        route.fulfill(content_type="application/json", body=json.dumps(data))

    page.route("**/api/proxy/test", respond)
    open_page(page, base + "/manage")
    page.evaluate("switchTab('settings')")
    page.wait_for_load_state("networkidle")
    page.locator("#cfgProxyUrl").fill("http://127.0.0.1:18080")
    page.locator("#cfgMediaProxyEnabled").check()
    page.locator("#cfgMediaProxyUrl").fill("socks5://127.0.0.1:18081")
    page.locator("#btnTestProxy").click()
    result = page.locator("#proxyTestResult")
    english = (
        "Request proxy: ✅ Proxy is working (HTTP 200) 12ms | Media proxy: ❌ "
        + diagnostic
        + " (HTTP 502) 34ms"
    )
    chinese = (
        "请求代理: ✅ 代理可用 (HTTP 200) 12ms | 媒体代理: ❌ "
        + diagnostic
        + " (HTTP 502) 34ms"
    )
    playwright_api.expect(result).to_have_text(english)
    page.locator("[data-language-select]").select_option("zh-CN")
    playwright_api.expect(result).to_have_text(chinese)
    page.locator("[data-language-select]").select_option("en")
    playwright_api.expect(result).to_have_text(english)
    assert len(requests) == 2
    assert page.locator("#cfgProxyUrl").input_value() == "http://127.0.0.1:18080"
    assert page.locator("#cfgMediaProxyUrl").input_value() == "socks5://127.0.0.1:18081"


@pytest.mark.parametrize(
    ("action", "network_error", "english", "chinese", "diagnostic"),
    [
        ("add", False, "Add failed", "添加失败", "Invalid proxy URL"),
        ("edit", False, "Update failed", "更新失败", "Invalid proxy URL"),
        ("add", True, "Add failed", "添加失败", "Failed to fetch"),
    ],
)
def test_account_failure_toasts_follow_language(
    browser_page, action, network_error, english, chinese, diagnostic
):
    page, base = browser_page

    def respond(route):
        if route.request.method not in {"POST", "PUT"}:
            route.fallback()
        elif network_error:
            route.abort("failed")
        else:
            route.fulfill(
                status=400,
                content_type="application/json",
                body=json.dumps({"detail": diagnostic}),
            )

    page.route(re.compile(r"/api/tokens(?:/\d+)?$"), respond)
    open_page(page, base + "/manage")
    if action == "add":
        page.evaluate("openAddModal()")
    else:
        page.wait_for_function("document.querySelector('#tokenTableBody tr') !== null")
        page.evaluate("openEditModal(1)")
    page.locator("#" + action + "TokenGoogleCookies").fill("SID=review-placeholder")
    page.locator("#" + action + "TokenLoginAccount").fill("review@example.com")
    page.locator("#" + action + "TokenBtn").click()
    toast = page.locator("body > div.fixed.bottom-4")
    playwright_api.expect(toast).to_have_text(english + ": " + diagnostic)
    page.locator("[data-language-select]").select_option("zh-CN")
    playwright_api.expect(toast).to_have_text(chinese + ": " + diagnostic)
    playwright_api.expect(page.locator("#" + action + "TokenBtn")).to_be_enabled()


def test_historical_log_messages_localize_without_changing_raw_payloads(browser_page):
    page, base = browser_page
    diagnostic = "Flow frontend RPC rejected: rpc=as29s, code=[5]"
    nested_error = "生成失败: 视频生成成功但获取媒体地址失败: " + diagnostic
    model = "gemini-3.1-flash-image-landscape-4k"
    tier_error = "当前模型需要 Ult 账号，但没有可用的 Ult 账号: " + model
    records = [
        {"id": 11, "operation": "generate_video", "error_summary": nested_error},
        {"id": 12, "operation": "generate_image", "error_summary": tier_error},
        {"id": 13, "operation": "custom_operation", "error_summary": "保存"},
    ]
    for record in records:
        record.update(
            status_code=500,
            status_text="failed",
            progress=24,
            request_body=json.dumps(
                {"prompt": "生成失败: 保持原文"}, ensure_ascii=False
            ),
            response_body=json.dumps(
                {"error": {"message": record["error_summary"]}}, ensure_ascii=False
            ),
        )

    def respond(route):
        path = urlsplit(route.request.url).path
        data = (
            records
            if path == "/api/logs"
            else next(
                record for record in records if path == "/api/logs/" + str(record["id"])
            )
        )
        route.fulfill(content_type="application/json", body=json.dumps(data))

    page.route(re.compile(r"/api/logs(?:/\d+)?(?:\?.*)?$"), respond)
    open_page(page, base + "/manage")
    page.evaluate("switchTab('logs')")
    rows = page.locator("#logsTableBody tr")
    playwright_api.expect(rows).to_have_count(3)
    playwright_api.expect(page.locator("#panelLogs thead th").first).to_have_text(
        "Operation"
    )
    playwright_api.expect(rows.first.locator("td").first).to_have_text("Generate video")
    playwright_api.expect(rows.first.locator("button")).to_have_text("Details")
    expected = (
        "Generation failed: Video generation succeeded, but the media URL could not be retrieved: "
        + diagnostic
    )
    assert (
        rows.first.locator("td").nth(5).locator("div").get_attribute("title")
        == expected
    )
    assert rows.nth(1).locator("td").nth(5).locator("div").get_attribute("title") == (
        "This model requires the Ult tier, but no Ult account is available: " + model
    )
    playwright_api.expect(rows.nth(2).locator("td").first).to_have_text(
        "custom_operation"
    )
    playwright_api.expect(rows.nth(2).locator("td").nth(5)).to_have_text("保存")
    assert page.evaluate("window.allLogs[0].error_summary") == nested_error
    assert rows.first.locator("td").first.get_attribute("title") == "generate_video"
    rows.first.locator("button").click()
    error = page.locator("#logDetailContent [data-i18n-error]")
    playwright_api.expect(error).to_have_text(expected)
    raw_payloads = page.locator("#logDetailContent pre").all_text_contents()
    assert any(nested_error in payload for payload in raw_payloads)
    assert any("生成失败: 保持原文" in payload for payload in raw_payloads)
    page.locator("[data-language-select]").select_option("zh-CN")
    playwright_api.expect(error).to_have_text(nested_error)
    playwright_api.expect(rows.first.locator("button")).to_have_text("详情")
    playwright_api.expect(rows.first.locator("td").first).to_have_text("生成视频")
    assert page.locator("#logDetailContent pre").all_text_contents() == raw_payloads


def test_model_switch_preserves_uploads_and_inflight_generation(browser_page):
    page, base = browser_page
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            content_type="application/json",
            body='{"data":[{"id":"gemini-3.1-flash-image","description":"Image generation"}]}',
        ),
    )
    pending = []
    page.route("**/v1/chat/completions", lambda route: pending.append(route))
    open_page(page, base + "/test")
    page.wait_for_selector('.model-item[data-model="gemini-3.1-flash-image"]')
    page.locator('.model-item[data-model="gemini-3.1-flash-image"]').click()
    page.locator("#promptInput").fill("保存")
    page.locator("#apiKey").fill("local-test-key")
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jNekAAAAASUVORK5CYII="
    )
    page.locator("#imageFileInput").set_input_files(
        {"name": "image.png", "mimeType": "image/png", "buffer": image}
    )
    page.wait_for_selector(".image-preview-item")
    page.locator("[data-language-select]").select_option("zh-CN")
    assert page.locator("#promptInput").input_value() == "保存"
    assert page.locator(".image-preview-item").count() == 1

    assert page.locator(".model-item.active").count() == 1
    page.locator("#btnGenerate").click()
    page.wait_for_function("document.getElementById('btnGenerate').disabled")
    page.locator("[data-language-select]").select_option("en")
    playwright_api.expect(page.locator("#btnGenerate")).to_have_text("⏳ Generating...")
    assert page.locator("#btnGenerate").is_disabled()
    page.wait_for_timeout(50)
    assert len(pending) == 1
    assert pending[0].request.headers["accept-language"] == "zh-CN"
    assert (
        pending[0].request.post_data_json["messages"][0]["content"][0]["text"] == "保存"
    )
    events = [
        {"choices": [{"delta": {"reasoning_content": "初始化生成环境...\n"}}]},
        {"choices": [{"delta": {"content": "保存"}}]},
    ]
    pending[0].fulfill(
        content_type="text/event-stream",
        body="".join("data: " + json.dumps(event) + "\n\n" for event in events)
        + "data: [DONE]\n\n",
    )
    playwright_api.expect(page.locator("#btnGenerate")).to_be_enabled()
    playwright_api.expect(page.locator("#outputResult pre")).to_have_text("保存")
    playwright_api.expect(page.locator("#outputLog")).to_contain_text(
        "Preparing generation environment..."
    )
    assert page.locator(".image-preview-item").count() == 1


def test_language_switch_works_when_browser_storage_is_disabled(browser_page):
    page, base = browser_page
    page.add_init_script(
        "Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('Disabled', 'SecurityError')}})"
    )
    open_page(page, base + "/login")
    assert page.locator("html").get_attribute("lang") == "en"
    page.locator("[data-language-select]").select_option("zh-CN")
    playwright_api.expect(page.locator("#loginButton")).to_have_text("登录")


def test_extension_language_preserves_settings_and_translates_status(browser_page):
    page, base = browser_page
    page.add_init_script("""
        const settings = {uiLanguage:'en', routeKey:'保存'};
        Object.defineProperty(window, 'chrome', {value: {
            runtime: {lastError: null},
            storage: {local: {
                get(defaults, callback) {queueMicrotask(() => callback({...defaults,...settings}));},
                set(values, callback) {Object.assign(settings,values); if(callback) queueMicrotask(callback);}
            }}
        }});
    """)
    page.goto(base + "/extension/options.html")
    page.wait_for_load_state("networkidle")
    playwright_api.expect(page.locator("#saveBtn")).to_have_text("Save")
    assert page.locator("#routeKey").input_value() == "保存"
    page.locator("#saveBtn").click()
    playwright_api.expect(page.locator("#status")).to_have_text(
        "Enter your Flow2API API key."
    )
    page.locator("#language").select_option("zh-CN")
    assert page.locator("#routeKey").input_value() == "保存"
    playwright_api.expect(page.locator("#status")).to_have_text(
        "请填写 Flow2API API Key。"
    )
    page.locator("#apiKey").fill("local-test-key")
    page.locator("#saveBtn").click()
    playwright_api.expect(page.locator("#status")).to_have_text(
        "已保存，后台连接会自动重连。"
    )
    page.locator("#language").select_option("en")
    playwright_api.expect(page.locator("#status")).to_have_text(
        "Settings saved. The background connection will reconnect automatically."
    )
    assert page.locator("#routeKey").input_value() == "保存"


def test_upscaled_video_models_keep_reference_uploads(browser_page):
    page, base = browser_page
    open_page(page, base + "/test")
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jNekAAAAASUVORK5CYII="
    )
    for model, limit in [
        ("veo_3_1_i2v_s_4k", "(1-2 images)"),
        ("veo_3_1_i2v_s_1080p", "(1-2 images)"),
        ("veo_3_1_r2v_fast_landscape_ultra_4k", "(0-3 images)"),
        ("veo_3_1_r2v_fast_landscape_ultra_1080p", "(0-3 images)"),
    ]:
        page.locator(f'.model-item[data-model="{model}"]').click()
        playwright_api.expect(page.locator("#imageUploadGroup")).to_be_visible()
        playwright_api.expect(page.locator("#imageLimit")).to_have_text(limit)
        page.locator("#imageFileInput").set_input_files(
            {"name": "image.png", "mimeType": "image/png", "buffer": image}
        )
        playwright_api.expect(page.locator(".image-preview-item")).to_have_count(1)


def test_upsample_heading_keeps_both_translations(browser_page):
    page, base = browser_page
    open_page(page, base + "/test")
    heading = page.locator('.section-title[data-category="视频放大 (Upsample)"]')
    playwright_api.expect(heading).to_contain_text("Video upscaling (Upsample)")
    page.locator("[data-language-select]").select_option("zh-CN")
    playwright_api.expect(heading).to_contain_text("视频放大 (Upsample)")
    page.locator("[data-language-select]").select_option("en")
    playwright_api.expect(heading).to_contain_text("Video upscaling (Upsample)")


def test_extend_requires_source_and_sends_backend_compatible_reference(browser_page):
    from src.api.routes import _extract_prompt_and_images_from_openai_messages
    from src.core.models import ChatMessage

    page, base = browser_page
    models = [
        "veo-3.1-fast-extend-8s-landscape",
        "omni-1.1-flash-4s-landscape",
        "veo-3.1-fast-8s-landscape",
    ]
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            content_type="application/json",
            body=json.dumps({"data": [{"id": model} for model in models]}),
        ),
    )
    requests = []

    def generate_response(route):
        requests.append(route.request.post_data_json)
        route.fulfill(
            content_type="text/event-stream",
            body='data: {"choices":[{"delta":{"content":"completed"}}]}\n\n'
            + "data: [DONE]\n\n",
        )

    page.route("**/v1/chat/completions", generate_response)
    open_page(page, base + "/test")
    page.locator("#apiKey").fill("local-test-key")
    page.locator("#apiKey").press("Tab")
    page.locator(f'.model-item[data-model="{models[0]}"]').click()
    source = page.locator("#videoMediaId")
    playwright_api.expect(source).to_be_visible()
    playwright_api.expect(page.locator("#imageUploadGroup")).to_be_hidden()
    playwright_api.expect(page.locator('label[for="videoMediaId"]')).to_have_text(
        "Source video media ID"
    )
    dialogs = []

    def dismiss_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", dismiss_dialog)
    for language, message in [
        ("en", "Enter the source video's mediaGenerationId"),
        ("zh-CN", "请输入源视频的 mediaGenerationId"),
    ]:
        page.locator("[data-language-select]").select_option(language)
        source.fill("   ")
        page.locator("#btnGenerate").click()
        assert dialogs[-1] == message
        assert requests == []
        playwright_api.expect(page.locator("#btnGenerate")).to_be_enabled()

    source.fill("  source-media-id  ")
    page.locator("#promptInput").fill("Continue the scene")
    page.locator("[data-language-select]").select_option("en")
    assert source.input_value() == "  source-media-id  "
    page.locator("#btnGenerate").click()
    playwright_api.expect(page.locator("#outputResult pre")).to_have_text("completed")
    assert len(requests) == 1
    body = requests[0]
    assert body["model"] == models[0]
    assert body["stream"] is True
    messages = [ChatMessage(**message) for message in body["messages"]]
    with ThreadPoolExecutor(max_workers=1) as executor:
        prompt, images, media_id = executor.submit(
            asyncio.run, _extract_prompt_and_images_from_openai_messages(messages)
        ).result(timeout=5)
    assert (prompt, images, media_id) == ("Continue the scene", [], "source-media-id")

    page.locator(f'.model-item[data-model="{models[1]}"]').click()
    playwright_api.expect(source).to_be_hidden()
    assert source.input_value() == ""
    playwright_api.expect(page.locator("#imageUploadGroup")).to_be_visible()
    playwright_api.expect(page.locator("#imageLimit")).to_have_text("(0-3 images)")
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jNekAAAAASUVORK5CYII="
    )
    page.locator("#imageFileInput").set_input_files(
        {"name": "image.png", "mimeType": "image/png", "buffer": image}
    )
    playwright_api.expect(page.locator(".image-preview-item")).to_have_count(1)
    page.locator("#btnGenerate").click()
    playwright_api.expect(page.locator("#outputResult pre")).to_have_text("completed")
    playwright_api.expect(page.locator("#btnGenerate")).to_be_enabled()
    assert len(requests) == 2
    content = requests[-1]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Continue the scene"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    page.locator(f'.model-item[data-model="{models[2]}"]').click()
    playwright_api.expect(source).to_be_hidden()
    playwright_api.expect(page.locator("#imageUploadGroup")).to_be_hidden()
    page.locator("#btnGenerate").click()
    playwright_api.expect(page.locator("#btnGenerate")).to_be_enabled()
    assert len(requests) == 3
    assert requests[-1]["messages"] == [
        {"role": "user", "content": "Continue the scene"}
    ]


def test_model_tester_displays_all_published_models(browser_page):
    from src.services.generation_handler import MODEL_CONFIG

    page, base = browser_page
    models = [
        {"id": model, "description": config.get("display_name", model)}
        for model, config in MODEL_CONFIG.items()
        if config.get("listed", True)
    ]
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            content_type="application/json", body=json.dumps({"data": models})
        ),
    )
    open_page(page, base + "/test")
    page.locator("#apiKey").fill("local-test-key")
    page.locator("#apiKey").press("Tab")
    items = page.locator(".model-item")
    playwright_api.expect(items).to_have_count(len(models))
    assert sorted(
        items.evaluate_all("items => items.map(item => item.dataset.model)")
    ) == sorted(model["id"] for model in models)
