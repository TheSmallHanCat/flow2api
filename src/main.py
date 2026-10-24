"""FastAPI application initialization"""

import asyncio
import os
import sys
import warnings

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pathlib import Path

from .core.config import config
from .core.database import Database
from .core.monitoring import CONTENT_TYPE_LATEST, render_main_metrics
from .services.flow_client import FlowClient
from .services.proxy_manager import ProxyManager
from .services.token_manager import TokenManager
from .services.load_balancer import LoadBalancer
from .services.concurrency_manager import ConcurrencyManager
from .services.generation_handler import GenerationHandler
from .api import routes, admin


_LOCAL_NO_PROXY_HOSTS = ("127.0.0.1", "localhost", "::1")


def _configure_stdio_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _configure_local_no_proxy() -> None:
    for env_name in ("NO_PROXY", "no_proxy"):
        raw_value = str(os.environ.get(env_name, "") or "")
        entries = [
            item.strip()
            for item in raw_value.replace(";", ",").split(",")
            if item.strip()
        ]
        normalized = {item.lower() for item in entries}
        changed = False
        for host in _LOCAL_NO_PROXY_HOSTS:
            if host.lower() not in normalized:
                entries.append(host)
                normalized.add(host.lower())
                changed = True
        if changed or not raw_value:
            os.environ[env_name] = ",".join(entries)


def _configure_asyncio_policy() -> None:
    if os.name != "nt":
        return
    policy_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_cls is None:
        return
    try:
        if not isinstance(asyncio.get_event_loop_policy(), policy_cls):
            asyncio.set_event_loop_policy(policy_cls())
    except Exception:
        pass


def _suppress_known_runtime_warnings() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r".*Proactor event loop does not implement add_reader family of methods required.*",
        category=RuntimeWarning,
    )


def _configure_process_runtime() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _configure_stdio_utf8()
    _configure_local_no_proxy()
    _configure_asyncio_policy()
    _suppress_known_runtime_warnings()


_configure_process_runtime()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    print("=" * 60)
    print("Flow2API Starting...")
    print("=" * 60)

    # Get config from setting.toml
    config_dict = config.get_raw_config()

    # Check if database exists (determine if first startup)
    is_first_startup = not db.db_exists()

    # Initialize database tables structure
    await db.init_db()

    # Handle database initialization based on startup type
    if is_first_startup:
        print(
            "First startup detected. Initializing database and configuration from setting.toml..."
        )
        await db.init_config_from_toml(config_dict, is_first_startup=True)
        print("Database and configuration initialized successfully.")
    else:
        print("Existing database detected. Checking for missing tables and columns...")
        await db.check_and_migrate_db(config_dict)
        print("Database migration check completed.")

    # Synchronize database configuration into memory at startup so personal/browser runtime settings are not missed.
    await db.reload_config_to_memory()
    generation_handler.file_cache.set_timeout(config.cache_timeout)
    cache_cleanup_enabled = await generation_handler.file_cache.refresh_cleanup_task()
    captcha_config = await db.get_captcha_config()

    # Capture a token snapshot before starting the browser service when possible, so concurrency management and warm-up can share it.
    tokens = await token_manager.get_all_tokens()

    # Initialize browser captcha service if needed
    browser_service = None
    if captcha_config.captcha_method == "personal":
        from .services.browser_captcha_personal import (
            BrowserCaptchaService,
            PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS,
            resolve_effective_browser_count,
            resolve_effective_personal_max_resident_tabs,
        )

        browser_service = await BrowserCaptchaService.get_instance(db)
        print("Browser captcha service initialized (nodriver mode)")

        warmup_limit = max(
            1,
            min(
            PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS,
            resolve_effective_browser_count(config.browser_count)
                * resolve_effective_personal_max_resident_tabs(
                    config.personal_max_resident_tabs
                ),
            ),
        )
        warmup_project_ids = await token_manager.get_personal_warmup_project_ids(
            tokens=tokens,
            limit=warmup_limit,
        )

        warmed_slots = []
        warmup_error = None
        try:
            warmed_slots = await browser_service.warmup_resident_tabs(
                warmup_project_ids,
                limit=warmup_limit,
            )
        except Exception as e:
            warmup_error = e
            print(f"Browser captcha resident warmup failed: {type(e).__name__}: {e}")
        if warmed_slots:
            print(
                f"Browser captcha shared resident tabs warmed "
                f"({len(warmed_slots)} slot(s), limit={warmup_limit})"
            )
        elif warmup_error is not None:
            print("Browser captcha resident warmup skipped for this startup")
        elif tokens:
            print("Browser captcha resident warmup skipped: no tab warmed successfully")
        else:
            # If no usable token is available, open a login window for the user to sign in manually.
            await browser_service.open_login_window()
            print("No active token found, opened login window for manual setup")
    elif captcha_config.captcha_method == "browser":
        from .services.browser_captcha import BrowserCaptchaService

        browser_service = await BrowserCaptchaService.get_instance(db)
        await browser_service.warmup_browser_slots()
        print("Browser captcha service initialized (headed mode)")

    # Initialize concurrency manager
    await concurrency_manager.initialize(tokens)

    if config.captcha_method == "remote_browser":
        try:
            warmed_projects = await flow_client.prefill_remote_browser_for_tokens(
                tokens, action="IMAGE_GENERATION"
            )
            print(
                f"Remote browser pool prefill started for {warmed_projects} project(s)"
            )
        except Exception as e:
            print(f"Remote browser pool prefill failed: {e}")

    # Start 429 auto-unban task
    import asyncio

    async def auto_unban_task():
        """Scheduled task: check hourly and re-enable tokens disabled by HTTP 429."""
        while True:
            try:
                await asyncio.sleep(3600)  # Run once per hour
                await token_manager.auto_unban_429_tokens()
            except Exception as e:
                print(f"Auto-unban task error: {e}")

    auto_unban_task_handle = asyncio.create_task(auto_unban_task())
    token_manager.start_protocol_refresher()

    print("Database initialized")
    print(f"Total tokens: {len(tokens)}")
    print(
        f"Cache: {'Enabled' if config.cache_enabled else 'Disabled'} (timeout: {config.cache_timeout}s)"
    )
    if cache_cleanup_enabled:
        print("File cache cleanup task started")
    else:
        print("File cache cleanup task disabled (timeout <= 0)")
    print("429 auto-unban task started (runs every hour)")
    print("Protocol token refresher started (runs every minute)")
    print(f"Server running on http://{config.server_host}:{config.server_port}")
    print("=" * 60)

    yield

    # Shutdown
    print("Flow2API Shutting down...")
    # Stop file cache cleanup task
    await generation_handler.file_cache.stop_cleanup_task()
    # Stop auto-unban task
    auto_unban_task_handle.cancel()
    try:
        await auto_unban_task_handle
    except asyncio.CancelledError:
        pass
    await token_manager.stop_protocol_refresher()
    # Close browser if initialized
    if browser_service:
        await browser_service.close()
        print("Browser captcha service closed")
    print("File cache cleanup task stopped")
    print("429 auto-unban task stopped")
    print("Protocol token refresher stopped")


# Initialize components
db = Database()
proxy_manager = ProxyManager(db)
flow_client = FlowClient(proxy_manager, db)
token_manager = TokenManager(db, flow_client)
concurrency_manager = ConcurrencyManager()
load_balancer = LoadBalancer(token_manager, concurrency_manager)
generation_handler = GenerationHandler(
    flow_client,
    token_manager,
    load_balancer,
    db,
    concurrency_manager,
    proxy_manager,  # Add the proxy_manager parameter
)

# Set dependencies
routes.set_generation_handler(generation_handler)
admin.set_dependencies(token_manager, proxy_manager, db, concurrency_manager)

# Create FastAPI app
app = FastAPI(
    title="Flow2API",
    description="OpenAI-compatible API for Google VideoFX (Veo)",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(routes.router)
app.include_router(admin.router)

# Static files - serve tmp directory for cached files
tmp_dir = Path(__file__).parent.parent / "tmp"
tmp_dir.mkdir(exist_ok=True)
app.mount("/tmp", StaticFiles(directory=str(tmp_dir)), name="tmp")

# HTML routes for frontend
static_path = Path(__file__).parent.parent / "static"
_STATIC_PAGE_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _static_page_response(file_path: Path) -> FileResponse:
    return FileResponse(str(file_path), headers=_STATIC_PAGE_NO_CACHE_HEADERS)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Redirect to login page"""
    login_file = static_path / "login.html"
    if login_file.exists():
        return _static_page_response(login_file)
    return HTMLResponse(
        content="<h1>Flow2API</h1><p>Frontend not found</p>", status_code=404
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Login page"""
    login_file = static_path / "login.html"
    if login_file.exists():
        return _static_page_response(login_file)
    return HTMLResponse(content="<h1>Login Page Not Found</h1>", status_code=404)


@app.get("/manage", response_class=HTMLResponse)
async def manage_page(request: Request):
    """Management console page"""
    guard_response = _ensure_admin_page_session(request)
    if guard_response is not None:
        return guard_response
    manage_file = static_path / "manage.html"
    if manage_file.exists():
        return _static_page_response(manage_file)
    return HTMLResponse(content="<h1>Management Page Not Found</h1>", status_code=404)


@app.get("/test", response_class=HTMLResponse)
async def test_page(request: Request):
    """Model testing page"""
    guard_response = _ensure_admin_page_session(request)
    if guard_response is not None:
        return guard_response
    test_file = static_path / "test.html"
    if test_file.exists():
        return _static_page_response(test_file)
    return HTMLResponse(content="<h1>Test Page Not Found</h1>", status_code=404)


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint for the main Flow2API service."""
    payload = await render_main_metrics(db, concurrency_manager=concurrency_manager)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


def _ensure_admin_page_session(request: Request):
    token = admin.get_admin_token_from_cookie(request)
    if not admin.is_admin_session_token_valid(token):
        return RedirectResponse(url="/login", status_code=302)
    return None
