"""API routes - OpenAI compatible endpoints"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from typing import List, Optional, Dict, Any, Tuple
import base64
import re
import json
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession
from ..core.auth import verify_api_key_header
from ..core.models import ChatCompletionRequest
from ..services.generation_handler import GenerationHandler, MODEL_CONFIG
from ..services.semantic_probe import SemanticProbeService
from ..core.logger import debug_logger

router = APIRouter()
# Dependency injection will be set up in main.py
generation_handler: GenerationHandler = None
semantic_probe_service = SemanticProbeService()


def _normalize_quality_from_image_quality(image_quality: Optional[str]) -> Optional[str]:
    value = (image_quality or "").strip().lower().replace("_", "-")
    mapping = {
        "standard": "standard",
        "normal": "standard",
        "default": "standard",
        "hd": "ultra",
        "high": "ultra",
        "ultra": "ultra",
        "ultra-relaxed": "ultra_relaxed",
        "relaxed": "ultra_relaxed"
    }
    return mapping.get(value)


def _parse_size(size: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    if not size:
        return None, None
    match = re.match(r"^\s*(\d{2,5})\s*[xX]\s*(\d{2,5})\s*$", size)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _infer_aspect_ratio(width: Optional[int], height: Optional[int]) -> Optional[str]:
    if not width or not height or width <= 0 or height <= 0:
        return None

    ratio = width / height
    if abs(ratio - 1.0) <= 0.08:
        return "square"
    if abs(ratio - (4 / 3)) <= 0.08:
        return "four-three"
    if abs(ratio - (3 / 4)) <= 0.08:
        return "three-four"
    return "landscape" if width > height else "portrait"


def _infer_resolution(width: Optional[int], height: Optional[int]) -> Optional[str]:
    if not width or not height or width <= 0 or height <= 0:
        return None
    max_side = max(width, height)
    if max_side >= 3000:
        return "4k"
    if max_side >= 1800:
        return "2k"
    if max_side >= 1000:
        return "1080p"
    return None


async def _resolve_generation_params(
    request: ChatCompletionRequest,
    prompt: str,
    has_images: bool
) -> Dict[str, Any]:
    # 1) 显式参数（最高优先级）
    width = request.width
    height = request.height

    size_w, size_h = _parse_size(request.size)
    if width is None and size_w is not None:
        width = size_w
    if height is None and size_h is not None:
        height = size_h

    derived_aspect = _infer_aspect_ratio(width, height)
    derived_resolution = _infer_resolution(width, height)
    mapped_quality = _normalize_quality_from_image_quality(request.image_quality)

    current = {
        "aspect_ratio": request.aspect_ratio or derived_aspect,
        "resolution": request.resolution or derived_resolution,
        "quality": request.quality or mapped_quality,
        "video_type": request.video_type
    }

    # 2) 语意探查仅补全缺失字段
    probed: Dict[str, Optional[str]] = {}
    try:
        probed = await semantic_probe_service.infer(
            prompt=prompt,
            has_images=has_images,
            current=current
        )
    except Exception as e:
        debug_logger.log_warning(f"[SEMANTIC_PROBE] 路由推断异常，继续本地逻辑: {str(e)}")

    final_aspect = request.aspect_ratio or derived_aspect or probed.get("aspect_ratio")
    final_resolution = request.resolution or derived_resolution or probed.get("resolution")
    final_quality = request.quality or mapped_quality or probed.get("quality")
    final_video_type = request.video_type or probed.get("video_type")

    image_count = request.n if isinstance(request.n, int) and request.n > 0 else 1
    image_count = min(image_count, 4)

    return {
        "aspect_ratio": final_aspect,
        "resolution": final_resolution,
        "quality": final_quality,
        "video_type": final_video_type,
        "image_count": image_count,
        "image_style": request.style,
        "image_seed": request.seed
    }


def set_generation_handler(handler: GenerationHandler):
    """Set generation handler instance"""
    global generation_handler
    generation_handler = handler


async def retrieve_image_data(url: str) -> Optional[bytes]:
    """
    智能获取图片数据：
    1. 优先检查是否为本地 /tmp/ 缓存文件，如果是则直接读取磁盘
    2. 如果本地不存在或是外部链接，则进行网络下载
    """
    # 优先尝试本地读取
    try:
        if "/tmp/" in url and generation_handler and generation_handler.file_cache:
            path = urlparse(url).path
            filename = path.split("/tmp/")[-1]
            local_file_path = generation_handler.file_cache.cache_dir / filename

            if local_file_path.exists() and local_file_path.is_file():
                data = local_file_path.read_bytes()
                if data:
                    return data
    except Exception as e:
        debug_logger.log_warning(f"[CONTEXT] 本地缓存读取失败: {str(e)}")

    # 回退逻辑：网络下载
    try:
        async with AsyncSession() as session:
            response = await session.get(url, timeout=30, impersonate="chrome110", verify=False)
            if response.status_code == 200:
                return response.content
            else:
                debug_logger.log_warning(f"[CONTEXT] 图片下载失败，状态码: {response.status_code}")
    except Exception as e:
        debug_logger.log_error(f"[CONTEXT] 图片下载异常: {str(e)}")

    return None


@router.get("/v1/models")
async def list_models(api_key: str = Depends(verify_api_key_header)):
    """List available models"""
    models = []

    for model_id, config in MODEL_CONFIG.items():
        description = f"{config['type'].capitalize()} generation"
        if config['type'] == 'image':
            description += f" - {config['model_name']}"
        else:
            description += f" - {config['model_key']}"

        models.append({
            "id": model_id,
            "object": "model",
            "owned_by": "flow2api",
            "description": description
        })

    return {
        "object": "list",
        "data": models
    }


@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    api_key: str = Depends(verify_api_key_header)
):
    """Create chat completion (unified endpoint for image and video generation)"""
    try:
        # Extract prompt from messages
        if not request.messages:
            raise HTTPException(status_code=400, detail="Messages cannot be empty")

        last_message = request.messages[-1]
        content = last_message.content

        # Handle both string and array format (OpenAI multimodal)
        prompt = ""
        images: List[bytes] = []

        if isinstance(content, str):
            # Simple text format
            prompt = content
        elif isinstance(content, list):
            # Multimodal format
            for item in content:
                if item.get("type") == "text":
                    prompt = item.get("text", "")
                elif item.get("type") == "image_url":
                    # Extract image from URL or base64
                    image_url = item.get("image_url", {}).get("url", "")
                    if image_url.startswith("data:image"):
                        # Parse base64
                        match = re.search(r"base64,(.+)", image_url)
                        if match:
                            image_base64 = match.group(1)
                            image_bytes = base64.b64decode(image_base64)
                            images.append(image_bytes)
                    elif image_url.startswith("http://") or image_url.startswith("https://"):
                        # Download remote image URL
                        debug_logger.log_info(f"[IMAGE_URL] 下载远程图片: {image_url}")
                        try:
                            downloaded_bytes = await retrieve_image_data(image_url)
                            if downloaded_bytes and len(downloaded_bytes) > 0:
                                images.append(downloaded_bytes)
                                debug_logger.log_info(f"[IMAGE_URL] ✅ 远程图片下载成功: {len(downloaded_bytes)} 字节")
                            else:
                                debug_logger.log_warning(f"[IMAGE_URL] ⚠️ 远程图片下载失败或为空: {image_url}")
                        except Exception as e:
                            debug_logger.log_error(f"[IMAGE_URL] ❌ 远程图片下载异常: {str(e)}")

        # Fallback to deprecated image parameter
        if request.image and not images:
            if request.image.startswith("data:image"):
                match = re.search(r"base64,(.+)", request.image)
                if match:
                    image_base64 = match.group(1)
                    image_bytes = base64.b64decode(image_base64)
                    images.append(image_bytes)

        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        # 参数映射与语意探查（显式参数优先，探查仅补缺）
        effective_params = await _resolve_generation_params(
            request=request,
            prompt=prompt,
            has_images=(len(images) > 0)
        )

        # 自动参考图：仅对图片模型生效（支持通用模型推断）
        model_config = None
        try:
            _, model_config = generation_handler.resolve_model(
                model=request.model,
                images=images if images else None,
                aspect_ratio=effective_params["aspect_ratio"],
                resolution=effective_params["resolution"],
                quality=effective_params["quality"],
                video_type=effective_params["video_type"]
            )
        except Exception:
            model_config = MODEL_CONFIG.get(request.model)

        if model_config and model_config["type"] == "image" and len(request.messages) > 1:
            debug_logger.log_info(f"[CONTEXT] 开始查找历史参考图，消息数量: {len(request.messages)}")

            # 查找上一次 assistant 回复的图片
            for msg in reversed(request.messages[:-1]):
                if msg.role == "assistant" and isinstance(msg.content, str):
                    # 匹配 Markdown 图片格式: ![...](http...)
                    matches = re.findall(r"!\[.*?\]\((.*?)\)", msg.content)
                    if matches:
                        last_image_url = matches[-1]

                        if last_image_url.startswith("http"):
                            try:
                                downloaded_bytes = await retrieve_image_data(last_image_url)
                                if downloaded_bytes and len(downloaded_bytes) > 0:
                                    # 将历史图片插入到最前面
                                    images.insert(0, downloaded_bytes)
                                    debug_logger.log_info(f"[CONTEXT] ✅ 添加历史参考图: {last_image_url}")
                                    break
                                else:
                                    debug_logger.log_warning(f"[CONTEXT] 图片下载失败或为空，尝试下一个: {last_image_url}")
                            except Exception as e:
                                debug_logger.log_error(f"[CONTEXT] 处理参考图时出错: {str(e)}")
                                # 继续尝试下一个图片

        # Call generation handler
        if request.stream:
            # Streaming response
            async def generate():
                async for chunk in generation_handler.handle_generation(
                    model=request.model,
                    prompt=prompt,
                    images=images if images else None,
                    stream=True,
                    aspect_ratio=effective_params["aspect_ratio"],
                    resolution=effective_params["resolution"],
                    quality=effective_params["quality"],
                    video_type=effective_params["video_type"],
                    image_count=effective_params["image_count"],
                    image_style=effective_params["image_style"],
                    image_seed=effective_params["image_seed"]
                ):
                    yield chunk

                # Send [DONE] signal
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no"
                }
            )
        else:
            # Non-streaming response
            result = None
            async for chunk in generation_handler.handle_generation(
                model=request.model,
                prompt=prompt,
                images=images if images else None,
                stream=False,
                aspect_ratio=effective_params["aspect_ratio"],
                resolution=effective_params["resolution"],
                quality=effective_params["quality"],
                video_type=effective_params["video_type"],
                image_count=effective_params["image_count"],
                image_style=effective_params["image_style"],
                image_seed=effective_params["image_seed"]
            ):
                result = chunk

            if result:
                # Parse the result JSON string
                try:
                    result_json = json.loads(result)
                    return JSONResponse(content=result_json)
                except json.JSONDecodeError:
                    # If not JSON, return as-is
                    return JSONResponse(content={"result": result})
            else:
                raise HTTPException(status_code=500, detail="Generation failed: No response from handler")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
