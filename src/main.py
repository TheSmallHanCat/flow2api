"""FastAPI application initialization"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse, Response
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
        print("🎉 First startup detected. Initializing database and configuration from setting.toml...")
        await db.init_config_from_toml(config_dict, is_first_startup=True)
        print("✓ Database and configuration initialized successfully.")
    else:
        print("🔄 Existing database detected. Checking for missing tables and columns...")
        await db.check_and_migrate_db(config_dict)
        print("✓ Database migration check completed.")

    # 启动时统一把数据库配置同步到内存，避免 personal/brower 相关运行时配置遗漏。
    await db.reload_config_to_memory()
    generation_handler.file_cache.set_timeout(config.cache_timeout)
    cache_cleanup_enabled = await generation_handler.file_cache.refresh_cleanup_task()
    captcha_config = await db.get_captcha_config()

    # 尽量在浏览器服务启动前就拿到 token 快照，后续并发管理和预热共用。
    tokens = await token_manager.get_all_tokens()

    # Initialize browser captcha service if needed
    browser_service = None
    if captcha_config.captcha_method == "personal":
        from .services.browser_captcha_personal import BrowserCaptchaService
        browser_service = await BrowserCaptchaService.get_instance(db)
        print("✓ Browser captcha service initialized (nodriver mode)")

        warmup_limit = max(1, int(config.personal_max_resident_tabs or 1))
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
            print(
                "⚠ Browser captcha resident warmup failed: "
                f"{type(e).__name__}: {e}"
            )
        if warmed_slots:
            print(
                f"✓ Browser captcha shared resident tabs warmed "
                f"({len(warmed_slots)} slot(s), limit={warmup_limit})"
            )
        elif warmup_error is not None:
            print("⚠ Browser captcha resident warmup skipped for this startup")
        elif tokens:
            print("⚠ Browser captcha resident warmup skipped: no tab warmed successfully")
        else:
            # 没有任何可用 token 时，打开登录窗口供用户手动操作
            await browser_service.open_login_window()
            print("⚠ No active token found, opened login window for manual setup")
    elif captcha_config.captcha_method == "browser":
        from .services.browser_captcha import BrowserCaptchaService
        browser_service = await BrowserCaptchaService.get_instance(db)
        await browser_service.warmup_browser_slots()
        print("? Browser captcha service initialized (headed mode)")

    # Initialize concurrency manager
    await concurrency_manager.initialize(tokens)

    if config.captcha_method == "remote_browser":
        try:
            warmed_projects = await flow_client.prefill_remote_browser_for_tokens(tokens, action="IMAGE_GENERATION")
            print(f"✓ Remote browser pool prefill started for {warmed_projects} project(s)")
        except Exception as e:
            print(f"⚠ Remote browser pool prefill failed: {e}")

    # Start 429 auto-unban task
    import asyncio
    async def auto_unban_task():
        """定时任务：每小时检查并解禁429被禁用的token"""
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时执行一次
                await token_manager.auto_unban_429_tokens()
            except Exception as e:
                print(f"❌ Auto-unban task error: {e}")

    auto_unban_task_handle = asyncio.create_task(auto_unban_task())

    print(f"✓ Database initialized")
    print(f"✓ Total tokens: {len(tokens)}")
    print(f"✓ Cache: {'Enabled' if config.cache_enabled else 'Disabled'} (timeout: {config.cache_timeout}s)")
    if cache_cleanup_enabled:
        print("✓ File cache cleanup task started")
    else:
        print("✓ File cache cleanup task disabled (timeout <= 0)")
    print(f"✓ 429 auto-unban task started (runs every hour)")
    print(f"✓ Server running on http://{config.server_host}:{config.server_port}")
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
    # Close browser if initialized
    if browser_service:
        await browser_service.close()
        print("✓ Browser captcha service closed")
    print("✓ File cache cleanup task stopped")
    print("✓ 429 auto-unban task stopped")


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
    proxy_manager  # 添加 proxy_manager 参数
)

# Set dependencies
routes.set_generation_handler(generation_handler)
admin.set_dependencies(token_manager, proxy_manager, db, concurrency_manager)

# Create FastAPI app
app = FastAPI(
    title="Flow2API",
    description="OpenAI-compatible API for Google VideoFX (Veo)",
    version="1.0.0",
    lifespan=lifespan
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


@app.get("/", response_class=HTMLResponse)
async def index():
    """Redirect to login page"""
    login_file = static_path / "login.html"
    if login_file.exists():
        return FileResponse(str(login_file))
    return HTMLResponse(content="<h1>Flow2API</h1><p>Frontend not found</p>", status_code=404)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Login page"""
    login_file = static_path / "login.html"
    if login_file.exists():
        return FileResponse(str(login_file))
    return HTMLResponse(content="<h1>Login Page Not Found</h1>", status_code=404)


@app.get("/manage", response_class=HTMLResponse)
async def manage_page():
    """Management console page"""
    manage_file = static_path / "manage.html"
    if manage_file.exists():
        return FileResponse(str(manage_file))
    return HTMLResponse(content="<h1>Management Page Not Found</h1>", status_code=404)


@app.get("/test", response_class=HTMLResponse)
async def test_page():
    """Model testing page"""
    test_file = static_path / "test.html"
    if test_file.exists():
        return FileResponse(str(test_file))
    return HTMLResponse(content="<h1>Test Page Not Found</h1>", status_code=404)


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint for the main Flow2API service."""
    payload = await render_main_metrics(db, concurrency_manager=concurrency_manager)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)
