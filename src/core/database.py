"""Database storage layer for Flow2API"""
import aiosqlite
import json
from datetime import datetime
from typing import Optional, List
from pathlib import Path
from .models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, GenerationConfig, CacheConfig, Project, CaptchaConfig, PluginConfig


class Database:
    """SQLite database manager"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            # Store database in data directory
            data_dir = Path(__file__).parent.parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "flow.db")
        self.db_path = db_path

    def db_exists(self) -> bool:
        """Check if database file exists"""
        return Path(self.db_path).exists()

    async def _table_exists(self, db, table_name: str) -> bool:
        """Check if a table exists in the database"""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        result = await cursor.fetchone()
        return result is not None

    async def _column_exists(self, db, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table"""
        try:
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = await cursor.fetchall()
            return any(col[1] == column_name for col in columns)
        except:
            return False

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """Ensure all config tables have their default rows

        Args:
            db: Database connection
            config_dict: Configuration dictionary from setting.toml (optional)
                        If None, use default values instead of reading from TOML.
        """
        # Ensure admin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM admin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            admin_username = "admin"
            admin_password = "admin"
            api_key = "han1234"
            error_ban_threshold = 3

            if config_dict:
                global_config = config_dict.get("global", {})
                admin_username = global_config.get("admin_username", "admin")
                admin_password = global_config.get("admin_password", "admin")
                api_key = global_config.get("api_key", "han1234")

                admin_config = config_dict.get("admin", {})
                error_ban_threshold = admin_config.get("error_ban_threshold", 3)

            await db.execute("""
                INSERT INTO admin_config (id, username, password, api_key, error_ban_threshold)
                VALUES (1, ?, ?, ?, ?)
            """, (admin_username, admin_password, api_key, error_ban_threshold))

        # Ensure proxy_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM proxy_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            proxy_enabled = False
            proxy_url = None

            if config_dict:
                proxy_config = config_dict.get("proxy", {})
                proxy_enabled = proxy_config.get("proxy_enabled", False)
                proxy_url = proxy_config.get("proxy_url", "")
                proxy_url = proxy_url if proxy_url else None

            await db.execute("""
                INSERT INTO proxy_config (id, enabled, proxy_url)
                VALUES (1, ?, ?)
            """, (proxy_enabled, proxy_url))

        # Ensure generation_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM generation_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            image_timeout = 300
            video_timeout = 1500

            if config_dict:
                generation_config = config_dict.get("generation", {})
                image_timeout = generation_config.get("image_timeout", 300)
                video_timeout = generation_config.get("video_timeout", 1500)

            await db.execute("""
                INSERT INTO generation_config (id, image_timeout, video_timeout)
                VALUES (1, ?, ?)
            """, (image_timeout, video_timeout))

        # Ensure cache_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM cache_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            cache_enabled = False
            cache_timeout = 7200
            cache_base_url = None

            if config_dict:
                cache_config = config_dict.get("cache", {})
                cache_enabled = cache_config.get("enabled", False)
                cache_timeout = cache_config.get("timeout", 7200)
                cache_base_url = cache_config.get("base_url", "")
                # Convert empty string to None
                cache_base_url = cache_base_url if cache_base_url else None

            await db.execute("""
                INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                VALUES (1, ?, ?, ?)
            """, (cache_enabled, cache_timeout, cache_base_url))

        # Ensure debug_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM debug_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            debug_enabled = False
            log_requests = True
            log_responses = True
            mask_token = True

            if config_dict:
                debug_config = config_dict.get("debug", {})
                debug_enabled = debug_config.get("enabled", False)
                log_requests = debug_config.get("log_requests", True)
                log_responses = debug_config.get("log_responses", True)
                mask_token = debug_config.get("mask_token", True)

            await db.execute("""
                INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                VALUES (1, ?, ?, ?, ?)
            """, (debug_enabled, log_requests, log_responses, mask_token))

        # Ensure captcha_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM captcha_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            captcha_method = "browser"
            yescaptcha_api_key = ""
            yescaptcha_base_url = "https://api.yescaptcha.com"

            if config_dict:
                captcha_config = config_dict.get("captcha", {})
                captcha_method = captcha_config.get("captcha_method", "browser")
                yescaptcha_api_key = captcha_config.get("yescaptcha_api_key", "")
                yescaptcha_base_url = captcha_config.get("yescaptcha_base_url", "https://api.yescaptcha.com")

            await db.execute("""
                INSERT INTO captcha_config (id, captcha_method, yescaptcha_api_key, yescaptcha_base_url)
                VALUES (1, ?, ?, ?)
            """, (captcha_method, yescaptcha_api_key, yescaptcha_base_url))

        # Ensure plugin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM plugin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            await db.execute("""
                INSERT INTO plugin_config (id, connection_token)
                VALUES (1, '')
            """)

    async def check_and_migrate_db(self, config_dict: dict = None):
        """Check database integrity and perform migrations if needed

        This method is called during upgrade mode to:
        1. Create missing tables (if they don't exist)
        2. Add missing columns to existing tables
        3. Ensure all config tables have default rows

        Args:
            config_dict: Configuration dictionary from setting.toml (optional)
                        Used only to initialize missing config rows with default values.
                        Existing config rows will NOT be overwritten.
        """
        async with aiosqlite.connect(self.db_path) as db:
            print("Checking database integrity and performing migrations...")

            # ========== Step 1: Create missing tables ==========
            # Check and create cache_config table if missing
            if not await self._table_exists(db, "cache_config"):
                print("  ✓ Creating missing table: cache_config")
                await db.execute("""
                    CREATE TABLE cache_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        cache_enabled BOOLEAN DEFAULT 0,
                        cache_timeout INTEGER DEFAULT 7200,
                        cache_base_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create captcha_config table if missing
            if not await self._table_exists(db, "captcha_config"):
                print("  ✓ Creating missing table: captcha_config")
                await db.execute("""
                    CREATE TABLE captcha_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        captcha_method TEXT DEFAULT 'browser',
                        yescaptcha_api_key TEXT DEFAULT '',
                        yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                        capmonster_api_key TEXT DEFAULT '',
                        capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                        ezcaptcha_api_key TEXT DEFAULT '',
                        ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                        capsolver_api_key TEXT DEFAULT '',
                        capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                        website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                        page_action TEXT DEFAULT 'FLOW_GENERATION',
                        browser_proxy_enabled BOOLEAN DEFAULT 0,
                        browser_proxy_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create plugin_config table if missing
            if not await self._table_exists(db, "plugin_config"):
                print("  ✓ Creating missing table: plugin_config")
                await db.execute("""
                    CREATE TABLE plugin_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        connection_token TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # ========== Step 2: Add missing columns to existing tables ==========
            # Check and add missing columns to tokens table
            if await self._table_exists(db, "tokens"):
                columns_to_add = [
                    ("at", "TEXT"),  # Access Token
                    ("at_expires", "TIMESTAMP"),  # AT expiration time
                    ("credits", "INTEGER DEFAULT 0"),  # Balance
                    ("user_paygate_tier", "TEXT"),  # User tier
                    ("current_project_id", "TEXT"),  # Current project UUID
                    ("current_project_name", "TEXT"),  # Project name
                    ("image_enabled", "BOOLEAN DEFAULT 1"),
                    ("video_enabled", "BOOLEAN DEFAULT 1"),
                    ("image_concurrency", "INTEGER DEFAULT -1"),
                    ("video_concurrency", "INTEGER DEFAULT -1"),
                    ("ban_reason", "TEXT"),  # 禁用原因
                    ("banned_at", "TIMESTAMP"),  # 禁用时间
                ]

                for col_name, col_type in columns_to_add:
                    if not await self._column_exists(db, "tokens", col_name):
                        try:
                            await db.execute(f"ALTER TABLE tokens ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to tokens table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to admin_config table
            if await self._table_exists(db, "admin_config"):
                if not await self._column_exists(db, "admin_config", "error_ban_threshold"):
                    try:
                        await db.execute("ALTER TABLE admin_config ADD COLUMN error_ban_threshold INTEGER DEFAULT 3")
                        print("  ✓ Added column 'error_ban_threshold' to admin_config table")
                    except Exception as e:
                        print(f"  ✗ Failed to add column 'error_ban_threshold': {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("browser_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("browser_proxy_url", "TEXT"),
                    ("capmonster_api_key", "TEXT DEFAULT ''"),
                    ("capmonster_base_url", "TEXT DEFAULT 'https://api.capmonster.cloud'"),
                    ("ezcaptcha_api_key", "TEXT DEFAULT ''"),
                    ("ezcaptcha_base_url", "TEXT DEFAULT 'https://api.ez-captcha.com'"),
                    ("capsolver_api_key", "TEXT DEFAULT ''"),
                    ("capsolver_base_url", "TEXT DEFAULT 'https://api.capsolver.com'"),
                    ("browser_count", "INTEGER DEFAULT 1"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to token_stats table
            if await self._table_exists(db, "token_stats"):
                stats_columns_to_add = [
                    ("today_image_count", "INTEGER DEFAULT 0"),
                    ("today_video_count", "INTEGER DEFAULT 0"),
                    ("today_error_count", "INTEGER DEFAULT 0"),
                    ("today_date", "DATE"),
                    ("consecutive_error_count", "INTEGER DEFAULT 0"),  # 🆕 连续错误计数
                ]

                for col_name, col_type in stats_columns_to_add:
                    if not await self._column_exists(db, "token_stats", col_name):
                        try:
                            await db.execute(f"ALTER TABLE token_stats ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to token_stats table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to plugin_config table
            if await self._table_exists(db, "plugin_config"):
                plugin_columns_to_add = [
                    ("auto_enable_on_update", "BOOLEAN DEFAULT 1"),  # 默认开启
                ]

                for col_name, col_type in plugin_columns_to_add:
                    if not await self._column_exists(db, "plugin_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE plugin_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to plugin_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # ========== Step 3: Ensure all config tables have default rows ==========
            # Note: This will NOT overwrite existing config rows
            # It only ensures missing rows are created with default values from setting.toml
            await self._ensure_config_rows(db, config_dict=config_dict)

            await db.commit()
            print("Database migration check completed.")

    async def init_db(self):
        """Initialize database tables"""
        async with aiosqlite.connect(self.db_path) as db:
            # Tokens table (Flow2API版本)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    st TEXT UNIQUE NOT NULL,
                    at TEXT,
                    at_expires TIMESTAMP,
                    email TEXT NOT NULL,
                    name TEXT,
                    remark TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    credits INTEGER DEFAULT 0,
                    user_paygate_tier TEXT,
                    current_project_id TEXT,
                    current_project_name TEXT,
                    image_enabled BOOLEAN DEFAULT 1,
                    video_enabled BOOLEAN DEFAULT 1,
                    image_concurrency INTEGER DEFAULT -1,
                    video_concurrency INTEGER DEFAULT -1,
                    ban_reason TEXT,
                    banned_at TIMESTAMP
                )
            """)

            # Projects table (新增)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    project_name TEXT NOT NULL,
                    tool_name TEXT DEFAULT 'PINHOLE',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Token stats table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Tasks table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    progress INTEGER DEFAULT 0,
                    result_urls TEXT,
                    error_message TEXT,
                    scene_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Request logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Admin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT DEFAULT 'admin',
                    password TEXT DEFAULT 'admin',
                    api_key TEXT DEFAULT 'han1234',
                    error_ban_threshold INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Proxy config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proxy_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    proxy_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Generation config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS generation_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    image_timeout INTEGER DEFAULT 300,
                    video_timeout INTEGER DEFAULT 1500,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Cache config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    cache_enabled BOOLEAN DEFAULT 0,
                    cache_timeout INTEGER DEFAULT 7200,
                    cache_base_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Debug config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS debug_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    log_requests BOOLEAN DEFAULT 1,
                    log_responses BOOLEAN DEFAULT 1,
                    mask_token BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Captcha config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS captcha_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    captcha_method TEXT DEFAULT 'browser',
                    yescaptcha_api_key TEXT DEFAULT '',
                    yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                    capmonster_api_key TEXT DEFAULT '',
                    capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                    ezcaptcha_api_key TEXT DEFAULT '',
                    ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                    capsolver_api_key TEXT DEFAULT '',
                    capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                    website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                    page_action TEXT DEFAULT 'FLOW_GENERATION',
                    browser_proxy_enabled BOOLEAN DEFAULT 0,
                    browser_proxy_url TEXT,
                    browser_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Plugin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plugin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    connection_token TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON tasks(task_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_st ON tokens(st)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_project_id ON projects(project_id)")

            # Migrate request_logs table if needed
            await self._migrate_request_logs(db)

            await db.commit()

    async def _migrate_request_logs(self, db):
        """Migrate request_logs table from old schema to new schema"""
        try:
            # Check if old columns exist
            has_model = await self._column_exists(db, "request_logs", "model")
            has_operation = await self._column_exists(db, "request_logs", "operation")

            if has_model and not has_operation:
                # Old schema detected, need migration
                print("🔄 检测到旧的request_logs表结构,开始迁移...")

                # Rename old table
                await db.execute("ALTER TABLE request_logs RENAME TO request_logs_old")

                # Create new table with new schema
                await db.execute("""
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id INTEGER,
                        operation TEXT NOT NULL,
                        request_body TEXT,
                        response_body TEXT,
                        status_code INTEGER NOT NULL,
                        duration FLOAT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (token_id) REFERENCES tokens(id)
                    )
                """)

                # Migrate data from old table (basic migration)
                await db.execute("""
                    INSERT INTO request_logs (token_id, operation, request_body, status_code, duration, created_at)
                    SELECT
                        token_id,
                        model as operation,
                        json_object('model', model, 'prompt', substr(prompt, 1, 100)) as request_body,
                        CASE
                            WHEN status = 'completed' THEN 200
                            WHEN status = 'failed' THEN 500
                            ELSE 0
                        END as status_code,
                        response_time as duration,
                        created_at
                    FROM request_logs_old
                """)

                # Drop old table
                await db.execute("DROP TABLE request_logs_old")

                print("✅ request_logs表迁移完成")
        except Exception as e:
            print(f"⚠️ request_logs表迁移失败: {e}")
            # Continue even if migration fails

    # Token operations
    async def add_token(self, token: Token) -> int:
        """Add a new token"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                INSERT INTO tokens (st, at, at_expires, email, name, remark, is_active,
                                   credits, user_paygate_tier, current_project_id, current_project_name,
                                   image_enabled, video_enabled, image_concurrency, video_concurrency)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (token.st, token.at, token.at_expires, token.email, token.name, token.remark,
                  token.is_active, token.credits, token.user_paygate_tier,
                  token.current_project_id, token.current_project_name,
                  token.image_enabled, token.video_enabled,
                  token.image_concurrency, token.video_concurrency))
            await db.commit()
            token_id = cursor.lastrowid

            # Create stats entry
            await db.execute("""
                INSERT INTO token_stats (token_id) VALUES (?)
            """, (token_id,))
            await db.commit()

            return token_id

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_token_by_st(self, st: str) -> Optional[Token]:
        """Get token by ST"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE st = ?", (st,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_token_by_email(self, email: str) -> Optional[Token]:
        """Get token by email"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE email = ?", (email,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [Token(**dict(row)) for row in rows]

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE is_active = 1 ORDER BY last_used_at ASC")
            rows = await cursor.fetchall()
            return [Token(**dict(row)) for row in rows]

    async def update_token(self, token_id: int, **kwargs):
        """Update token fields"""
        async with aiosqlite.connect(self.db_path) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                params.append(token_id)
                query = f"UPDATE tokens SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()

    async def delete_token(self, token_id: int):
        """Delete token and related data"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM token_stats WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM projects WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            await db.commit()

    # Project operations
    async def add_project(self, project: Project) -> int:
        """Add a new project"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                INSERT INTO projects (project_id, token_id, project_name, tool_name, is_active)
                VALUES (?, ?, ?, ?, ?)
            """, (project.project_id, project.token_id, project.project_name,
                  project.tool_name, project.is_active))
            await db.commit()
            return cursor.lastrowid

    async def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Get project by UUID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
            row = await cursor.fetchone()
            if row:
                return Project(**dict(row))
            return None

    async def get_projects_by_token(self, token_id: int) -> List[Project]:
        """Get all projects for a token"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE token_id = ? ORDER BY created_at DESC",
                (token_id,)
            )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def delete_project(self, project_id: str):
        """Delete project"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            await db.commit()

    # Task operations
    async def create_task(self, task: Task) -> int:
        """Create a new task"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                INSERT INTO tasks (task_id, token_id, model, prompt, status, progress, scene_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (task.task_id, task.token_id, task.model, task.prompt,
                  task.status, task.progress, task.scene_id))
            await db.commit()
            return cursor.lastrowid

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            if row:
                task_dict = dict(row)
                # Parse result_urls from JSON
                if task_dict.get("result_urls"):
                    task_dict["result_urls"] = json.loads(task_dict["result_urls"])
                return Task(**task_dict)
            return None

    async def update_task(self, task_id: str, **kwargs):
        """Update task"""
        async with aiosqlite.connect(self.db_path) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    # Convert list to JSON string for result_urls
                    if key == "result_urls" and isinstance(value, list):
                        value = json.dumps(value)
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                params.append(task_id)
                query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"
                await db.execute(query, params)
                await db.commit()

    # Token stats operations (kept for compatibility, now delegates to specific methods)
    async def increment_token_stats(self, token_id: int, stat_type: str):
        """Increment token statistics (delegates to specific methods)"""
        if stat_type == "image":
            await self.increment_image_count(token_id)
        elif stat_type == "video":
            await self.increment_video_count(token_id)
        elif stat_type == "error":
            await self.increment_error_count(token_id)

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return TokenStats(**dict(row))
            return None

    async def increment_image_count(self, token_id: int):
        """Increment image generation count with daily reset"""
        from datetime import date
        async with aiosqlite.connect(self.db_path) as db:
            today = str(date.today())
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset today's count
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = today_image_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_video_count(self, token_id: int):
        """Increment video generation count with daily reset"""
        from datetime import date
        async with aiosqlite.connect(self.db_path) as db:
            today = str(date.today())
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset today's count
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = today_video_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_error_count(self, token_id: int):
        """Increment error count with daily reset

        Updates two counters:
        - error_count: Historical total errors (never reset)
        - consecutive_error_count: Consecutive errors (reset on success/enable)
        - today_error_count: Today's errors (reset on date change)
        """
        from datetime import date
        async with aiosqlite.connect(self.db_path) as db:
            today = str(date.today())
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset today's error count
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment all counters
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = today_error_count + 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count (only reset consecutive_error_count, keep error_count and today_error_count)

        This is called when:
        - Token is manually enabled by admin
        - Request succeeds (resets consecutive error counter)

        Note: error_count (total historical errors) is NEVER reset
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?
            """, (token_id,))
            await db.commit()

    # Config operations
    async def get_admin_config(self) -> Optional[AdminConfig]:
        """Get admin configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return AdminConfig(**dict(row))
            return None

    async def update_admin_config(self, **kwargs):
        """Update admin configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                query = f"UPDATE admin_config SET {', '.join(updates)} WHERE id = 1"
                await db.execute(query, params)
                await db.commit()

    async def get_proxy_config(self) -> Optional[ProxyConfig]:
        """Get proxy configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return ProxyConfig(**dict(row))
            return None

    async def update_proxy_config(self, enabled: bool, proxy_url: Optional[str] = None):
        """Update proxy configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE proxy_config
                SET enabled = ?, proxy_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (enabled, proxy_url))
            await db.commit()

    async def get_generation_config(self) -> Optional[GenerationConfig]:
        """Get generation configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return GenerationConfig(**dict(row))
            return None

    async def update_generation_config(self, image_timeout: int, video_timeout: int):
        """Update generation configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE generation_config
                SET image_timeout = ?, video_timeout = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (image_timeout, video_timeout))
            await db.commit()

    # Request log operations
    async def add_request_log(self, log: RequestLog):
        """Add request log"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO request_logs (token_id, operation, request_body, response_body, status_code, duration)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (log.token_id, log.operation, log.request_body, log.response_body,
                  log.status_code, log.duration))
            await db.commit()

    async def get_logs(self, limit: int = 100, token_id: Optional[int] = None):
        """Get request logs with token email"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if token_id:
                cursor = await db.execute("""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.operation,
                        rl.request_body,
                        rl.response_body,
                        rl.status_code,
                        rl.duration,
                        rl.created_at,
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    WHERE rl.token_id = ?
                    ORDER BY rl.created_at DESC
                    LIMIT ?
                """, (token_id, limit))
            else:
                cursor = await db.execute("""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.operation,
                        rl.request_body,
                        rl.response_body,
                        rl.status_code,
                        rl.duration,
                        rl.created_at,
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    ORDER BY rl.created_at DESC
                    LIMIT ?
                """, (limit,))

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def clear_all_logs(self):
        """Clear all request logs"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM request_logs")
            await db.commit()

    async def init_config_from_toml(self, config_dict: dict, is_first_startup: bool = True):
        """
        Initialize database configuration from setting.toml

        Args:
            config_dict: Configuration dictionary from setting.toml
            is_first_startup: If True, initialize all config rows from setting.toml.
                            If False (upgrade mode), only ensure missing config rows exist with default values.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if is_first_startup:
                # First startup: Initialize all config tables with values from setting.toml
                await self._ensure_config_rows(db, config_dict)
            else:
                # Upgrade mode: Only ensure missing config rows exist (with default values, not from TOML)
                await self._ensure_config_rows(db, config_dict=None)

            await db.commit()

    async def reload_config_to_memory(self):
        """
        Reload all configuration from database to in-memory Config instance.
        This should be called after any configuration update to ensure hot-reload.

        Includes:
        - Admin config (username, password, api_key)
        - Cache config (enabled, timeout, base_url)
        - Generation config (image_timeout, video_timeout)
        - Proxy config will be handled by ProxyManager
        """
        from .config import config

        # Reload admin config
        admin_config = await self.get_admin_config()
        if admin_config:
            config.set_admin_username_from_db(admin_config.username)
            config.set_admin_password_from_db(admin_config.password)
            config.api_key = admin_config.api_key

        # Reload cache config
        cache_config = await self.get_cache_config()
        if cache_config:
            config.set_cache_enabled(cache_config.cache_enabled)
            config.set_cache_timeout(cache_config.cache_timeout)
            config.set_cache_base_url(cache_config.cache_base_url or "")

        # Reload generation config
        generation_config = await self.get_generation_config()
        if generation_config:
            config.set_image_timeout(generation_config.image_timeout)
            config.set_video_timeout(generation_config.video_timeout)

        # Reload debug config
        debug_config = await self.get_debug_config()
        if debug_config:
            config.set_debug_enabled(debug_config.enabled)

        # Reload captcha config
        captcha_config = await self.get_captcha_config()
        if captcha_config:
            config.set_captcha_method(captcha_config.captcha_method)
            config.set_yescaptcha_api_key(captcha_config.yescaptcha_api_key)
            config.set_yescaptcha_base_url(captcha_config.yescaptcha_base_url)

    # Cache config operations
    async def get_cache_config(self) -> CacheConfig:
        """Get cache configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CacheConfig(**dict(row))
            # Return default if not found
            return CacheConfig(cache_enabled=False, cache_timeout=7200)

    async def update_cache_config(self, enabled: bool = None, timeout: int = None, base_url: Optional[str] = None):
        """Update cache configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("cache_enabled", False)
                new_timeout = timeout if timeout is not None else current.get("cache_timeout", 7200)
                new_base_url = base_url if base_url is not None else current.get("cache_base_url")

                # If base_url is explicitly set to empty string, treat as None
                if base_url == "":
                    new_base_url = None

                await db.execute("""
                    UPDATE cache_config
                    SET cache_enabled = ?, cache_timeout = ?, cache_base_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_timeout, new_base_url))
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_timeout = timeout if timeout is not None else 7200
                new_base_url = base_url if base_url is not None else None

                await db.execute("""
                    INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                    VALUES (1, ?, ?, ?)
                """, (new_enabled, new_timeout, new_base_url))

            await db.commit()

    # Debug config operations
    async def get_debug_config(self) -> 'DebugConfig':
        """Get debug configuration"""
        from .models import DebugConfig
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return DebugConfig(**dict(row))
            # Return default if not found
            return DebugConfig(enabled=False, log_requests=True, log_responses=True, mask_token=True)

    async def update_debug_config(
        self,
        enabled: bool = None,
        log_requests: bool = None,
        log_responses: bool = None,
        mask_token: bool = None
    ):
        """Update debug configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("enabled", False)
                new_log_requests = log_requests if log_requests is not None else current.get("log_requests", True)
                new_log_responses = log_responses if log_responses is not None else current.get("log_responses", True)
                new_mask_token = mask_token if mask_token is not None else current.get("mask_token", True)

                await db.execute("""
                    UPDATE debug_config
                    SET enabled = ?, log_requests = ?, log_responses = ?, mask_token = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_log_requests, new_log_responses, new_mask_token))
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_log_requests = log_requests if log_requests is not None else True
                new_log_responses = log_responses if log_responses is not None else True
                new_mask_token = mask_token if mask_token is not None else True

                await db.execute("""
                    INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                    VALUES (1, ?, ?, ?, ?)
                """, (new_enabled, new_log_requests, new_log_responses, new_mask_token))

            await db.commit()

    # Captcha config operations
    async def get_captcha_config(self) -> CaptchaConfig:
        """Get captcha configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CaptchaConfig(**dict(row))
            return CaptchaConfig()

    async def update_captcha_config(
        self,
        captcha_method: str = None,
        yescaptcha_api_key: str = None,
        yescaptcha_base_url: str = None,
        capmonster_api_key: str = None,
        capmonster_base_url: str = None,
        ezcaptcha_api_key: str = None,
        ezcaptcha_base_url: str = None,
        capsolver_api_key: str = None,
        capsolver_base_url: str = None,
        browser_proxy_enabled: bool = None,
        browser_proxy_url: str = None,
        browser_count: int = None
    ):
        """Update captcha configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_method = captcha_method if captcha_method is not None else current.get("captcha_method", "yescaptcha")
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else current.get("yescaptcha_api_key", "")
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else current.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else current.get("capmonster_api_key", "")
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else current.get("capmonster_base_url", "https://api.capmonster.cloud")
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else current.get("ezcaptcha_api_key", "")
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else current.get("ezcaptcha_base_url", "https://api.ez-captcha.com")
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else current.get("capsolver_api_key", "")
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else current.get("capsolver_base_url", "https://api.capsolver.com")
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else current.get("browser_proxy_enabled", False)
                new_proxy_url = browser_proxy_url if browser_proxy_url is not None else current.get("browser_proxy_url")
                new_browser_count = browser_count if browser_count is not None else current.get("browser_count", 1)

                await db.execute("""
                    UPDATE captcha_config
                    SET captcha_method = ?, yescaptcha_api_key = ?, yescaptcha_base_url = ?,
                        capmonster_api_key = ?, capmonster_base_url = ?,
                        ezcaptcha_api_key = ?, ezcaptcha_base_url = ?,
                        capsolver_api_key = ?, capsolver_base_url = ?,
                        browser_proxy_enabled = ?, browser_proxy_url = ?, browser_count = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_method, new_yes_key, new_yes_url, new_cap_key, new_cap_url,
                      new_ez_key, new_ez_url, new_cs_key, new_cs_url, new_proxy_enabled, new_proxy_url, new_browser_count))
            else:
                new_method = captcha_method if captcha_method is not None else "yescaptcha"
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else ""
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else "https://api.yescaptcha.com"
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else ""
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else "https://api.capmonster.cloud"
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else ""
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else "https://api.ez-captcha.com"
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else ""
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else "https://api.capsolver.com"
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else False
                new_proxy_url = browser_proxy_url
                new_browser_count = browser_count if browser_count is not None else 1

                await db.execute("""
                    INSERT INTO captcha_config (id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                        capmonster_api_key, capmonster_base_url, ezcaptcha_api_key, ezcaptcha_base_url,
                        capsolver_api_key, capsolver_base_url, browser_proxy_enabled, browser_proxy_url, browser_count)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (new_method, new_yes_key, new_yes_url, new_cap_key, new_cap_url,
                      new_ez_key, new_ez_url, new_cs_key, new_cs_url, new_proxy_enabled, new_proxy_url, new_browser_count))

            await db.commit()

    # Plugin config operations
    async def get_plugin_config(self) -> PluginConfig:
        """Get plugin configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return PluginConfig(**dict(row))
            return PluginConfig()

    async def update_plugin_config(self, connection_token: str, auto_enable_on_update: bool = True):
        """Update plugin configuration"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                await db.execute("""
                    UPDATE plugin_config
                    SET connection_token = ?, auto_enable_on_update = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (connection_token, auto_enable_on_update))
            else:
                await db.execute("""
                    INSERT INTO plugin_config (id, connection_token, auto_enable_on_update)
                    VALUES (1, ?, ?)
                """, (connection_token, auto_enable_on_update))

            await db.commit()
