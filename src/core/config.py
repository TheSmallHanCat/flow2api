"""Configuration management for Flow2API"""
import tomli
from pathlib import Path
from typing import Dict, Any, Optional

class Config:
    """Application configuration"""

    def __init__(self):
        self._config = self._load_config()
        self._admin_username: Optional[str] = None
        self._admin_password: Optional[str] = None

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from setting.toml"""
        config_path = Path(__file__).parent.parent.parent / "config" / "setting.toml"
        with open(config_path, "rb") as f:
            return tomli.load(f)

    def reload_config(self):
        """Reload configuration from file"""
        self._config = self._load_config()

    def get_raw_config(self) -> Dict[str, Any]:
        """Get raw configuration dictionary"""
        return self._config

    @property
    def admin_username(self) -> str:
        # If admin_username is set from database, use it; otherwise fall back to config file
        if self._admin_username is not None:
            return self._admin_username
        return self._config["global"]["admin_username"]

    @admin_username.setter
    def admin_username(self, value: str):
        self._admin_username = value
        self._config["global"]["admin_username"] = value

    def set_admin_username_from_db(self, username: str):
        """Set admin username from database"""
        self._admin_username = username

    # Flow2API specific properties
    @property
    def flow_labs_base_url(self) -> str:
        """Google Labs base URL for project management"""
        return self._config["flow"]["labs_base_url"]

    @property
    def flow_api_base_url(self) -> str:
        """Google AI Sandbox API base URL for generation"""
        return self._config["flow"]["api_base_url"]

    @property
    def flow_timeout(self) -> int:
        timeout = self._config.get("flow", {}).get("timeout", 120)
        try:
            return max(5, int(timeout))
        except Exception:
            return 120

    @property
    def flow_max_retries(self) -> int:
        retries = self._config.get("flow", {}).get("max_retries", 3)
        try:
            return max(1, int(retries))
        except Exception:
            return 3

    def set_flow_max_retries(self, retries: int):
        """Set flow max retries"""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(1, int(retries))
        except Exception:
            normalized = 3
        self._config["flow"]["max_retries"] = normalized

    @property
    def flow_image_request_timeout(self) -> int:
        """图片生成单次 HTTP 请求超时(秒)。"""
        default_timeout = min(self.flow_timeout, 40)
        timeout = self._config.get("flow", {}).get(
            "image_request_timeout",
            default_timeout
        )
        try:
            return max(5, int(timeout))
        except Exception:
            return self.flow_timeout

    @property
    def flow_image_timeout_retry_count(self) -> int:
        """图片生成遇到网络超时时的快速重试次数。"""
        retry_count = self._config.get("flow", {}).get("image_timeout_retry_count", 1)
        try:
            return max(0, min(3, int(retry_count)))
        except Exception:
            return 1

    @property
    def flow_image_timeout_retry_delay(self) -> float:
        """图片生成网络超时重试前等待秒数。"""
        delay = self._config.get("flow", {}).get("image_timeout_retry_delay", 0.8)
        try:
            return max(0.0, min(5.0, float(delay)))
        except Exception:
            return 0.8

    @property
    def flow_image_timeout_use_media_proxy_fallback(self) -> bool:
        """网络超时时是否切换媒体代理重试。"""
        return bool(
            self._config.get("flow", {}).get(
                "image_timeout_use_media_proxy_fallback",
                True
            )
        )

    @property
    def flow_image_prefer_media_proxy(self) -> bool:
        """图片生成是否优先走媒体代理链路。"""
        return bool(
            self._config.get("flow", {}).get(
                "image_prefer_media_proxy",
                False
            )
        )

    @property
    def flow_image_slot_wait_timeout(self) -> float:
        """图片硬并发槽位等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("image_slot_wait_timeout", 120)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 120.0

    @property
    def flow_image_launch_soft_limit(self) -> int:
        """图片生成前置发车软并发上限(0 表示关闭软整形，仅使用硬并发)。"""
        value = self._config.get("flow", {}).get("image_launch_soft_limit", 0)
        try:
            return max(0, min(200, int(value)))
        except Exception:
            return 0

    @property
    def flow_image_launch_wait_timeout(self) -> float:
        """图片前置发车软并发等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("image_launch_wait_timeout", 180)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 180.0

    @property
    def flow_image_launch_stagger_ms(self) -> int:
        """图片请求前置发车间隔(毫秒)，用于平滑同批突发。"""
        value = self._config.get("flow", {}).get("image_launch_stagger_ms", 0)
        try:
            return max(0, min(5000, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_slot_wait_timeout(self) -> float:
        """视频硬并发槽位等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("video_slot_wait_timeout", 120)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 120.0

    @property
    def flow_video_launch_soft_limit(self) -> int:
        """视频生成前置发车软并发上限(0 表示关闭软整形，仅使用硬并发)。"""
        value = self._config.get("flow", {}).get("video_launch_soft_limit", 0)
        try:
            return max(0, min(200, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_launch_wait_timeout(self) -> float:
        """视频前置发车软并发等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("video_launch_wait_timeout", 180)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 180.0

    @property
    def flow_video_launch_stagger_ms(self) -> int:
        """视频请求前置发车间隔(毫秒)，用于平滑同批突发。"""
        value = self._config.get("flow", {}).get("video_launch_stagger_ms", 0)
        try:
            return max(0, min(5000, int(value)))
        except Exception:
            return 0

    @property
    def poll_interval(self) -> float:
        return self._config["flow"]["poll_interval"]

    @property
    def max_poll_attempts(self) -> int:
        return self._config["flow"]["max_poll_attempts"]

    @property
    def server_host(self) -> str:
        return self._config["server"]["host"]

    @property
    def server_port(self) -> int:
        return self._config["server"]["port"]

    @property
    def debug_enabled(self) -> bool:
        return self._config.get("debug", {}).get("enabled", False)

    @property
    def debug_log_requests(self) -> bool:
        return self._config.get("debug", {}).get("log_requests", True)

    @property
    def debug_log_responses(self) -> bool:
        return self._config.get("debug", {}).get("log_responses", True)

    @property
    def debug_mask_token(self) -> bool:
        return self._config.get("debug", {}).get("mask_token", True)

    # Mutable properties for runtime updates
    @property
    def api_key(self) -> str:
        return self._config["global"]["api_key"]

    @api_key.setter
    def api_key(self, value: str):
        self._config["global"]["api_key"] = value

    @property
    def admin_password(self) -> str:
        # If admin_password is set from database, use it; otherwise fall back to config file
        if self._admin_password is not None:
            return self._admin_password
        return self._config["global"]["admin_password"]

    @admin_password.setter
    def admin_password(self, value: str):
        self._admin_password = value
        self._config["global"]["admin_password"] = value

    def set_admin_password_from_db(self, password: str):
        """Set admin password from database"""
        self._admin_password = password

    def set_debug_enabled(self, enabled: bool):
        """Set debug mode enabled/disabled"""
        if "debug" not in self._config:
            self._config["debug"] = {}
        self._config["debug"]["enabled"] = enabled

    @property
    def image_timeout(self) -> int:
        """Get image generation timeout in seconds"""
        return self._config.get("generation", {}).get("image_timeout", 300)

    def set_image_timeout(self, timeout: int):
        """Set image generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["image_timeout"] = timeout

    @property
    def video_timeout(self) -> int:
        """Get video generation timeout in seconds"""
        return self._config.get("generation", {}).get("video_timeout", 1500)

    def set_video_timeout(self, timeout: int):
        """Set video generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["video_timeout"] = timeout

    @property
    def polling_mode_enabled(self) -> bool:
        """Get polling mode enabled status."""
        return self.call_logic_mode == "polling"

    @property
    def call_logic_mode(self) -> str:
        """Get call logic mode (default or polling)."""
        call_logic = self._config.get("call_logic", {})
        mode = call_logic.get("call_mode")
        if mode in ("default", "polling"):
            return mode
        if call_logic.get("polling_mode_enabled", False):
            return "polling"
        return "default"

    def set_polling_mode_enabled(self, enabled: bool):
        """Set polling mode enabled/disabled."""
        self.set_call_logic_mode("polling" if enabled else "default")

    def set_call_logic_mode(self, mode: str):
        """Set call logic mode (default or polling)."""
        normalized = "polling" if mode == "polling" else "default"
        if "call_logic" not in self._config:
            self._config["call_logic"] = {}
        self._config["call_logic"]["call_mode"] = normalized
        self._config["call_logic"]["polling_mode_enabled"] = normalized == "polling"

    @property
    def upsample_timeout(self) -> int:
        """Get upsample (4K/2K) timeout in seconds"""
        return self._config.get("generation", {}).get("upsample_timeout", 300)

    def set_upsample_timeout(self, timeout: int):
        """Set upsample (4K/2K) timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["upsample_timeout"] = timeout

    # Cache configuration
    @property
    def cache_enabled(self) -> bool:
        """Get cache enabled status"""
        return self._config.get("cache", {}).get("enabled", False)

    def set_cache_enabled(self, enabled: bool):
        """Set cache enabled status"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["enabled"] = enabled

    @property
    def cache_timeout(self) -> int:
        """Get cache timeout in seconds"""
        return self._config.get("cache", {}).get("timeout", 7200)

    def set_cache_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["timeout"] = timeout

    @property
    def cache_base_url(self) -> str:
        """Get cache base URL"""
        return self._config.get("cache", {}).get("base_url", "")

    def set_cache_base_url(self, base_url: str):
        """Set cache base URL"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["base_url"] = base_url

    # Captcha configuration
    @property
    def captcha_method(self) -> str:
        """Get captcha method"""
        return self._config.get("captcha", {}).get("captcha_method", "yescaptcha")

    def set_captcha_method(self, method: str):
        """Set captcha method"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["captcha_method"] = method

    @property
    def browser_launch_background(self) -> bool:
        """有头浏览器打码是否默认后台启动，避免抢占前台窗口。"""
        return self._config.get("captcha", {}).get("browser_launch_background", True)

    def set_browser_launch_background(self, enabled: bool):
        """设置有头浏览器打码是否后台启动。"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["browser_launch_background"] = bool(enabled)

    @property
    def browser_recaptcha_settle_seconds(self) -> float:
        """有头打码在 reload/clr 就绪后的额外等待秒数。"""
        value = self._config.get("captcha", {}).get("browser_recaptcha_settle_seconds", 3.0)
        try:
            return max(0.0, min(10.0, float(value)))
        except Exception:
            return 3.0

    @property
    def browser_idle_ttl_seconds(self) -> int:
        value = self._config.get("captcha", {}).get("browser_idle_ttl_seconds", 600)
        try:
            return max(60, int(value))
        except Exception:
            return 600

    @property
    def personal_max_resident_tabs(self) -> int:
        """内置浏览器打码的共享标签页上限"""
        value = self._config.get("captcha", {}).get("personal_max_resident_tabs", 5)
        try:
            return max(1, min(50, int(value)))  # 限制在1-50之间
        except Exception:
            return 5

    @property
    def personal_project_pool_size(self) -> int:
        """单个 Token 默认维护的项目池数量，仅影响项目轮换。"""
        value = self._config.get("captcha", {}).get("personal_project_pool_size", 4)
        try:
            return max(1, min(50, int(value)))
        except Exception:
            return 4

    @property
    def personal_idle_tab_ttl_seconds(self) -> int:
        """内置浏览器打码标签页空闲超时(秒)"""
        value = self._config.get("captcha", {}).get("personal_idle_tab_ttl_seconds", 600)
        try:
            return max(60, int(value))
        except Exception:
            return 600

    def set_personal_max_resident_tabs(self, value: int):
        """设置内置浏览器打码的共享标签页上限"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_max_resident_tabs"] = max(1, min(50, int(value)))

    def set_personal_project_pool_size(self, value: int):
        """设置单个 Token 默认维护的项目池数量，仅影响项目轮换"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_project_pool_size"] = max(1, min(50, int(value)))

    def set_personal_idle_tab_ttl_seconds(self, value: int):
        """设置内置浏览器打码标签页空闲超时(秒)"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_idle_tab_ttl_seconds"] = max(60, int(value))

    @property
    def yescaptcha_api_key(self) -> str:
        """Get YesCaptcha API key"""
        return self._config.get("captcha", {}).get("yescaptcha_api_key", "")

    def set_yescaptcha_api_key(self, api_key: str):
        """Set YesCaptcha API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_api_key"] = api_key

    @property
    def yescaptcha_base_url(self) -> str:
        """Get YesCaptcha base URL"""
        return self._config.get("captcha", {}).get("yescaptcha_base_url", "https://api.yescaptcha.com")

    def set_yescaptcha_base_url(self, base_url: str):
        """Set YesCaptcha base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_base_url"] = base_url

    @property
    def capmonster_api_key(self) -> str:
        """Get CapMonster API key"""
        return self._config.get("captcha", {}).get("capmonster_api_key", "")

    def set_capmonster_api_key(self, api_key: str):
        """Set CapMonster API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_api_key"] = api_key

    @property
    def capmonster_base_url(self) -> str:
        """Get CapMonster base URL"""
        return self._config.get("captcha", {}).get("capmonster_base_url", "https://api.capmonster.cloud")

    def set_capmonster_base_url(self, base_url: str):
        """Set CapMonster base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_base_url"] = base_url

    @property
    def ezcaptcha_api_key(self) -> str:
        """Get EzCaptcha API key"""
        return self._config.get("captcha", {}).get("ezcaptcha_api_key", "")

    def set_ezcaptcha_api_key(self, api_key: str):
        """Set EzCaptcha API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["ezcaptcha_api_key"] = api_key

    @property
    def ezcaptcha_base_url(self) -> str:
        """Get EzCaptcha base URL"""
        return self._config.get("captcha", {}).get("ezcaptcha_base_url", "https://api.ez-captcha.com")

    def set_ezcaptcha_base_url(self, base_url: str):
        """Set EzCaptcha base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["ezcaptcha_base_url"] = base_url

    @property
    def capsolver_api_key(self) -> str:
        """Get CapSolver API key"""
        return self._config.get("captcha", {}).get("capsolver_api_key", "")

    def set_capsolver_api_key(self, api_key: str):
        """Set CapSolver API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capsolver_api_key"] = api_key

    @property
    def capsolver_base_url(self) -> str:
        """Get CapSolver base URL"""
        return self._config.get("captcha", {}).get("capsolver_base_url", "https://api.capsolver.com")

    def set_capsolver_base_url(self, base_url: str):
        """Set CapSolver base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capsolver_base_url"] = base_url

    @property
    def remote_browser_base_url(self) -> str:
        """Get remote browser captcha service base URL"""
        return self._config.get("captcha", {}).get("remote_browser_base_url", "")

    def set_remote_browser_base_url(self, base_url: str):
        """Set remote browser captcha service base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["remote_browser_base_url"] = (base_url or "").strip()

    @property
    def remote_browser_api_key(self) -> str:
        """Get remote browser captcha service API key"""
        return self._config.get("captcha", {}).get("remote_browser_api_key", "")

    def set_remote_browser_api_key(self, api_key: str):
        """Set remote browser captcha service API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["remote_browser_api_key"] = (api_key or "").strip()

    @property
    def remote_browser_timeout(self) -> int:
        """Get remote browser captcha request timeout (seconds)"""
        timeout = self._config.get("captcha", {}).get("remote_browser_timeout", 60)
        try:
            return max(5, int(timeout))
        except Exception:
            return 60

    def set_remote_browser_timeout(self, timeout: int):
        """Set remote browser captcha request timeout (seconds)"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        try:
            normalized = max(5, int(timeout))
        except Exception:
            normalized = 60
        self._config["captcha"]["remote_browser_timeout"] = normalized


# Global config instance
config = Config()
