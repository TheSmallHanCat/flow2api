"""Data models for Flow2API"""

from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Union, Any, Literal
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

    # 打码代理（token 级，可覆盖全局浏览器打码代理）
    captcha_proxy_url: Optional[str] = None

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
    status_text: Optional[str] = None
    progress: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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
    enabled: bool = False  # 请求代理开关
    proxy_url: Optional[str] = None  # 请求代理地址
    media_proxy_enabled: bool = False  # 图片上传/下载代理开关
    media_proxy_url: Optional[str] = None  # 图片上传/下载代理地址


class GenerationConfig(BaseModel):
    """Generation timeout configuration"""

    id: int = 1
    image_timeout: int = 300  # seconds
    video_timeout: int = 1500  # seconds
    max_retries: int = 3  # 请求最大重试次数


class CallLogicConfig(BaseModel):
    """Token selection call logic configuration"""

    id: int = 1
    call_mode: str = "default"
    polling_mode_enabled: bool = False
    updated_at: Optional[datetime] = None


class CacheConfig(BaseModel):
    """Cache configuration"""

    id: int = 1
    cache_enabled: bool = False
    cache_timeout: int = 7200  # seconds (2 hours), 0 means never expire
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
    captcha_method: str = "browser"  # yescaptcha/capmonster/ezcaptcha/capsolver/browser/personal/remote_browser
    yescaptcha_api_key: str = ""
    yescaptcha_base_url: str = "https://api.yescaptcha.com"
    capmonster_api_key: str = ""
    capmonster_base_url: str = "https://api.capmonster.cloud"
    ezcaptcha_api_key: str = ""
    ezcaptcha_base_url: str = "https://api.ez-captcha.com"
    capsolver_api_key: str = ""
    capsolver_base_url: str = "https://api.capsolver.com"
    remote_browser_base_url: str = ""
    remote_browser_api_key: str = ""
    remote_browser_timeout: int = 60
    website_key: str = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
    page_action: str = "IMAGE_GENERATION"
    browser_proxy_enabled: bool = False  # 浏览器打码是否启用代理
    browser_proxy_url: Optional[str] = None  # 浏览器打码代理URL
    browser_count: int = 1  # 浏览器打码实例数量
    personal_project_pool_size: int = 4  # 单个 Token 默认维护的项目池数量（仅影响项目轮换）
    personal_max_resident_tabs: int = 5  # 内置浏览器共享打码标签页数量上限
    personal_idle_tab_ttl_seconds: int = 600  # 内置浏览器标签页空闲超时(秒)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PluginConfig(BaseModel):
    """Plugin connection configuration"""

    id: int = 1
    connection_token: str = ""  # 插件连接token
    auto_enable_on_update: bool = True  # 更新token时自动启用（默认开启）
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# OpenAI Compatible Request Models
class ChatMessage(BaseModel):
    """Chat message"""

    role: str
    content: Union[str, List[dict]]  # string or multimodal array


class ImageConfig(BaseModel):
    """Gemini imageConfig parameters"""

    aspectRatio: Optional[str] = None  # "16:9", "9:16", "1:1", "4:3", "3:4"
    imageSize: Optional[str] = None  # "2k", "4k"

    # 兼容 OpenAI/NewAPI 等上游可能透传的 size/quality 或 snake_case 字段
    model_config = ConfigDict(extra="allow")


class GenerationConfigParam(BaseModel):
    """Gemini generationConfig parameters (for model name resolution)"""

    responseModalities: Optional[List[str]] = None  # ["IMAGE", "TEXT"]
    imageConfig: Optional[ImageConfig] = None

    model_config = ConfigDict(extra="allow")


class GeminiInlineData(BaseModel):
    """Gemini inline binary data."""

    mimeType: str
    data: str


class GeminiFileData(BaseModel):
    """Gemini file reference."""

    fileUri: str
    mimeType: Optional[str] = None


class GeminiPart(BaseModel):
    """Gemini content part."""

    text: Optional[str] = None
    inlineData: Optional[GeminiInlineData] = None
    fileData: Optional[GeminiFileData] = None

    model_config = ConfigDict(extra="allow")


class GeminiContent(BaseModel):
    """Gemini content block."""

    role: Optional[Literal["user", "model"]] = None
    parts: List[GeminiPart]


class GeminiGenerateContentRequest(BaseModel):
    """Gemini official generateContent request."""

    contents: List[GeminiContent]
    generationConfig: Optional[GenerationConfigParam] = None
    systemInstruction: Optional[GeminiContent] = None

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    """Chat completion request (OpenAI compatible + Gemini extension)"""

    model: str
    messages: Optional[List[ChatMessage]] = None
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Flow2API specific parameters
    image: Optional[str] = None  # Base64 encoded image (deprecated, use messages)
    video: Optional[str] = None  # Base64 encoded video (deprecated)
    # Gemini extension parameters (from extra_body or top-level)
    generationConfig: Optional[GenerationConfigParam] = None
    contents: Optional[List[Any]] = None  # Gemini native contents

    model_config = ConfigDict(extra="allow")  # Allow extra fields like extra_body passthrough
