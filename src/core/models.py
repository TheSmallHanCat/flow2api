"""Data models for Flow2API"""
from pydantic import BaseModel
from typing import Optional, List, Union, Any
from datetime import datetime


class Token(BaseModel):
    """Token model for Flow2API"""
    id: Optional[int] = None

    # 认证信息 (核心)
    st: str  # Session Token (__Secure-next-auth.session-token)
    at: Optional[str] = None  # Access Token (从ST转换而来)
    at_expires: Optional[datetime] = None  # AT过期时间

    # 基础信息
    email: str
    name: Optional[str] = ""
    remark: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    use_count: int = 0

    # VideoFX特有字段
    credits: int = 0  # 剩余credits
    user_paygate_tier: Optional[str] = None  # PAYGATE_TIER_ONE

    # 项目管理
    current_project_id: Optional[str] = None  # 当前使用的项目UUID
    current_project_name: Optional[str] = None  # 项目名称

    # 功能开关
    image_enabled: bool = True
    video_enabled: bool = True

    # 并发限制
    image_concurrency: int = -1  # -1表示无限制
    video_concurrency: int = -1  # -1表示无限制

    # 429禁用相关
    ban_reason: Optional[str] = None  # 禁用原因: "429_rate_limit" 或 None
    banned_at: Optional[datetime] = None  # 禁用时间


class Project(BaseModel):
    """Project model for VideoFX"""
    id: Optional[int] = None
    project_id: str  # VideoFX项目UUID
    token_id: int  # 关联的Token ID
    project_name: str  # 项目名称
    tool_name: str = "PINHOLE"  # 工具名称,固定为PINHOLE
    is_active: bool = True
    created_at: Optional[datetime] = None


class TokenStats(BaseModel):
    """Token statistics"""
    token_id: int
    image_count: int = 0
    video_count: int = 0
    success_count: int = 0
    error_count: int = 0  # Historical total errors (never reset)
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    # 今日统计
    today_image_count: int = 0
    today_video_count: int = 0
    today_error_count: int = 0
    today_date: Optional[str] = None
    # 连续错误计数 (用于自动禁用判断)
    consecutive_error_count: int = 0


class Task(BaseModel):
    """Generation task"""
    id: Optional[int] = None
    task_id: str  # Flow API返回的operation name
    token_id: int
    model: str
    prompt: str
    status: str  # processing, completed, failed
    progress: int = 0  # 0-100
    result_urls: Optional[List[str]] = None
    error_message: Optional[str] = None
    scene_id: Optional[str] = None  # Flow API的sceneId
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class RequestLog(BaseModel):
    """API request log"""
    id: Optional[int] = None
    token_id: Optional[int] = None
    operation: str
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    status_code: int
    duration: float
    created_at: Optional[datetime] = None


class AdminConfig(BaseModel):
    """Admin configuration"""
    id: int = 1
    username: str
    password: str
    api_key: str
    error_ban_threshold: int = 3  # Auto-disable token after N consecutive errors


class ProxyConfig(BaseModel):
    """Proxy configuration"""
    id: int = 1
    enabled: bool = False
    proxy_url: Optional[str] = None


class GenerationConfig(BaseModel):
    """Generation timeout configuration"""
    id: int = 1
    image_timeout: int = 300  # seconds
    video_timeout: int = 1500  # seconds


class CacheConfig(BaseModel):
    """Cache configuration"""
    id: int = 1
    cache_enabled: bool = False
    cache_timeout: int = 7200  # seconds (2 hours)
    cache_base_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DebugConfig(BaseModel):
    """Debug configuration"""
    id: int = 1
    enabled: bool = False
    log_requests: bool = True
    log_responses: bool = True
    mask_token: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CaptchaConfig(BaseModel):
    """Captcha configuration"""
    id: int = 1
    captcha_method: str = "browser"  # yescaptcha 或 browser
    yescaptcha_api_key: str = ""
    yescaptcha_base_url: str = "https://api.yescaptcha.com"
    website_key: str = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
    page_action: str = "FLOW_GENERATION"
    browser_proxy_enabled: bool = False  # 浏览器打码是否启用代理
    browser_proxy_url: Optional[str] = None  # 浏览器打码代理URL
    # Browser refresh configuration (for personal mode)
    refresh_enabled: bool = True  # 是否启用后台刷新
    refresh_min_interval: int = 300  # 最小间隔(秒) - 5分钟
    refresh_max_interval: int = 7200  # 最大间隔(秒) - 2小时
    refresh_visit_duration_min: int = 10  # 每次访问停留时间最小值(秒)
    refresh_visit_duration_max: int = 30  # 每次访问停留时间最大值(秒)
    refresh_scroll_probability: float = 0.7  # 滚动页面的概率 (0-1)
    refresh_mouse_move_probability: float = 0.5  # 移动鼠标的概率 (0-1)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# OpenAI Compatible Request Models
class ChatMessage(BaseModel):
    """Chat message"""
    role: str
    content: Union[str, List[dict]]  # string or multimodal array


class ChatCompletionRequest(BaseModel):
    """Chat completion request (OpenAI compatible)"""
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Flow2API specific parameters
    image: Optional[str] = None  # Base64 encoded image (deprecated, use messages)
    video: Optional[str] = None  # Base64 encoded video (deprecated)
