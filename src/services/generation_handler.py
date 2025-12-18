"""Generation handler for Flow2API"""
import asyncio
import base64
import json
import time
from typing import Optional, AsyncGenerator, List, Dict, Any
from ..core.logger import debug_logger
from ..core.config import config
from ..core.models import Task, RequestLog
from .file_cache import FileCache
from .browser_captcha_personal import BrowserCaptchaService


# Model configuration
MODEL_CONFIG = {
    # 图片生成 - GEM_PIX (Gemini 2.5 Flash)
    "gemini-2.5-flash-image-landscape": {
        "type": "image",
        "model_name": "GEM_PIX",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-2.5-flash-image-portrait": {
        "type": "image",
        "model_name": "GEM_PIX",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },

    # 图片生成 - GEM_PIX_2 (Gemini 3.0 Pro)
    "gemini-3.0-pro-image-landscape": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-3.0-pro-image-portrait": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },

    # 图片生成 - IMAGEN_3_5 (Imagen 4.0)
    "imagen-4.0-generate-preview-landscape": {
        "type": "image",
        "model_name": "IMAGEN_3_5",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "imagen-4.0-generate-preview-portrait": {
        "type": "image",
        "model_name": "IMAGEN_3_5",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },

    # ========== 文生视频 (T2V - Text to Video) ==========
    # 不支持上传图片，只使用文本提示词生成

    # veo_3_1_t2v_fast_portrait (竖屏)
    # 上游模型名: veo_3_1_t2v_fast_portrait
    "veo_3_1_t2v_fast_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    # veo_3_1_t2v_fast_landscape (横屏)
    # 上游模型名: veo_3_1_t2v_fast
    "veo_3_1_t2v_fast_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_2_1_fast_d_15_t2v (需要新增横竖屏)
    "veo_2_1_fast_d_15_t2v_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_2_1_fast_d_15_t2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_2_1_fast_d_15_t2v_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_2_1_fast_d_15_t2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_2_0_t2v (需要新增横竖屏)
    "veo_2_0_t2v_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_2_0_t2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_2_0_t2v_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_2_0_t2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # ========== 首尾帧模型 (I2V - Image to Video) ==========
    # 支持1-2张图片：1张作为首帧，2张作为首尾帧

    # veo_3_1_i2v_s_fast_fl (需要新增横竖屏)
    "veo_3_1_i2v_s_fast_fl_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_fl_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_2_1_fast_d_15_i2v (需要新增横竖屏)
    "veo_2_1_fast_d_15_i2v_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_2_1_fast_d_15_i2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_2_1_fast_d_15_i2v_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_2_1_fast_d_15_i2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_2_0_i2v (需要新增横竖屏)
    "veo_2_0_i2v_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_2_0_i2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_2_0_i2v_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_2_0_i2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # ========== 多图生成 (R2V - Reference Images to Video) ==========
    # 支持多张图片,不限制数量

    # veo_3_0_r2v_fast (需要新增横竖屏)
    "veo_3_0_r2v_fast_portrait": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_0_r2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": None  # 不限制
    },
    "veo_3_0_r2v_fast_landscape": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_0_r2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": None  # 不限制
    }
}


class GenerationHandler:
    """统一生成处理器"""

    def __init__(self, flow_client, token_manager, load_balancer, db, concurrency_manager, proxy_manager, browser_service=None):
        self.flow_client = flow_client
        self.token_manager = token_manager
        self.load_balancer = load_balancer
        self.db = db
        self.concurrency_manager = concurrency_manager
        self.browser_service = browser_service
        self.file_cache = FileCache(
            cache_dir="tmp",
            default_timeout=config.cache_timeout,
            proxy_manager=proxy_manager
        )

    async def check_token_availability(self, is_image: bool, is_video: bool) -> bool:
        """检查Token可用性

        Args:
            is_image: 是否检查图片生成Token
            is_video: 是否检查视频生成Token

        Returns:
            True表示有可用Token, False表示无可用Token
        """
        token_obj = await self.load_balancer.select_token(
            for_image_generation=is_image,
            for_video_generation=is_video
        )
        return token_obj is not None

    async def handle_generation(
        self,
        model: str,
        prompt: str,
        images: Optional[List[bytes]] = None,
        stream: bool = False
    ) -> AsyncGenerator:
        """统一生成入口

        Args:
            model: 模型名称
            prompt: 提示词
            images: 图片列表 (bytes格式)
            stream: 是否流式输出
        """
        start_time = time.time()
        token = None

        # 1. 验证模型
        if model not in MODEL_CONFIG:
            error_msg = f"不支持的模型: {model}"
            debug_logger.log_error(error_msg)
            yield self._create_error_response(error_msg)
            return

        model_config = MODEL_CONFIG[model]
        generation_type = model_config["type"]
        debug_logger.log_info(f"[GENERATION] 开始生成 - 模型: {model}, 类型: {generation_type}, Prompt: {prompt[:50]}...")

        # 非流式模式: 只检查可用性
        if not stream:
            is_image = (generation_type == "image")
            is_video = (generation_type == "video")
            available = await self.check_token_availability(is_image, is_video)

            if available:
                if is_image:
                    message = "所有Token可用于图片生成。请启用流式模式使用生成功能。"
                else:
                    message = "所有Token可用于视频生成。请启用流式模式使用生成功能。"
            else:
                if is_image:
                    message = "没有可用的Token进行图片生成"
                else:
                    message = "没有可用的Token进行视频生成"

            yield self._create_completion_response(message, is_availability_check=True)
            return

        # 向用户展示开始信息
        if stream:
            yield self._create_stream_chunk(
                f"✨ {'视频' if generation_type == 'video' else '图片'}生成任务已启动\n",
                role="assistant"
            )

        # 2. 选择Token
        debug_logger.log_info(f"[GENERATION] 正在选择可用Token...")

        if generation_type == "image":
            token = await self.load_balancer.select_token(for_image_generation=True, model=model)
        else:
            token = await self.load_balancer.select_token(for_video_generation=True, model=model)

        if not token:
            # 尝试通过浏览器自动获取新 token
            debug_logger.log_info(f"[GENERATION] 未找到可用Token，尝试通过浏览器自动获取...")
            if stream:
                yield self._create_stream_chunk("🔄 未找到可用Token，正在尝试自动获取新Token...\n")
            
            auto_token_success = await self._try_auto_get_token()
            
            if auto_token_success:
                debug_logger.log_info(f"[GENERATION] 自动获取Token成功，重新选择Token...")
                if stream:
                    yield self._create_stream_chunk("✅ 新Token已添加，重新尝试生成...\n")
                
                # 重新选择 token
                if generation_type == "image":
                    token = await self.load_balancer.select_token(for_image_generation=True, model=model)
                else:
                    token = await self.load_balancer.select_token(for_video_generation=True, model=model)
            
            # 如果还是没有 token，返回错误
            if not token:
                error_msg = self._get_no_token_error_message(generation_type)
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                if stream:
                    yield self._create_stream_chunk(f"❌ {error_msg}\n")
                yield self._create_error_response(error_msg)
                return

        debug_logger.log_info(f"[GENERATION] 已选择Token: {token.id} ({token.email})")

        try:
            # 3. 确保AT有效
            debug_logger.log_info(f"[GENERATION] 检查Token AT有效性...")
            if stream:
                yield self._create_stream_chunk("初始化生成环境...\n")

            if not await self.token_manager.is_at_valid(token.id):
                error_msg = "Token AT无效或刷新失败"
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                if stream:
                    yield self._create_stream_chunk(f"❌ {error_msg}\n")
                yield self._create_error_response(error_msg)
                return

            # 重新获取token (AT可能已刷新)
            token = await self.token_manager.get_token(token.id)

            # 4. 确保Project存在
            debug_logger.log_info(f"[GENERATION] 检查/创建Project...")

            project_id = await self.token_manager.ensure_project_exists(token.id)
            debug_logger.log_info(f"[GENERATION] Project ID: {project_id}")

            # 5. 根据类型处理
            if generation_type == "image":
                debug_logger.log_info(f"[GENERATION] 开始图片生成流程...")
                async for chunk in self._handle_image_generation(
                    token, project_id, model_config, prompt, images, stream
                ):
                    yield chunk
            else:  # video
                debug_logger.log_info(f"[GENERATION] 开始视频生成流程...")
                async for chunk in self._handle_video_generation(
                    token, project_id, model_config, prompt, images, stream
                ):
                    yield chunk

            # 6. 记录使用
            is_video = (generation_type == "video")
            await self.token_manager.record_usage(token.id, is_video=is_video)

            # 重置错误计数 (请求成功时清空连续错误计数)
            await self.token_manager.record_success(token.id)

            debug_logger.log_info(f"[GENERATION] ✅ 生成成功完成")

            # 7. 记录成功日志
            duration = time.time() - start_time
            await self._log_request(
                token.id,
                f"generate_{generation_type}",
                {"model": model, "prompt": prompt[:100], "has_images": images is not None and len(images) > 0},
                {"status": "success"},
                200,
                duration
            )

        except Exception as e:
            error_msg = f"生成失败: {str(e)}"
            debug_logger.log_error(f"[GENERATION] ❌ {error_msg}")
            if stream:
                yield self._create_stream_chunk(f"❌ {error_msg}\n")
            if token:
                # 检测429错误，立即禁用token
                if "429" in str(e) or "HTTP Error 429" in str(e):
                    debug_logger.log_warning(f"[429_BAN] Token {token.id} 遇到429错误，立即禁用")
                    await self.token_manager.ban_token_for_429(token.id)
                else:
                    await self.token_manager.record_error(token.id)
            yield self._create_error_response(error_msg)

            # 记录失败日志
            duration = time.time() - start_time
            await self._log_request(
                token.id if token else None,
                f"generate_{generation_type if model_config else 'unknown'}",
                {"model": model, "prompt": prompt[:100], "has_images": images is not None and len(images) > 0},
                {"error": error_msg},
                500,
                duration
            )

    def _get_no_token_error_message(self, generation_type: str) -> str:
        """获取无可用Token时的详细错误信息"""
        if generation_type == "image":
            return "没有可用的Token进行图片生成。所有Token都处于禁用、冷却、锁定或已过期状态。"
        else:
            return "没有可用的Token进行视频生成。所有Token都处于禁用、冷却、配额耗尽或已过期状态。"

    async def _handle_image_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool
    ) -> AsyncGenerator:
        """处理图片生成 (同步返回)"""

        # 获取并发槽位
        if self.concurrency_manager:
            if not await self.concurrency_manager.acquire_image(token.id):
                yield self._create_error_response("图片并发限制已达上限")
                return

        try:
            # 上传图片 (如果有)
            image_inputs = []
            if images and len(images) > 0:
                if stream:
                    yield self._create_stream_chunk(f"上传 {len(images)} 张参考图片...\n")

                # 支持多图输入
                for idx, image_bytes in enumerate(images):
                    media_id = await self.flow_client.upload_image(
                        token.at,
                        image_bytes,
                        model_config["aspect_ratio"]
                    )
                    image_inputs.append({
                        "name": media_id,
                        "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"
                    })
                    if stream:
                        yield self._create_stream_chunk(f"已上传第 {idx + 1}/{len(images)} 张图片\n")

            # 调用生成API
            if stream:
                yield self._create_stream_chunk("正在生成图片...\n")

            result = await self.flow_client.generate_image(
                at=token.at,
                project_id=project_id,
                prompt=prompt,
                model_name=model_config["model_name"],
                aspect_ratio=model_config["aspect_ratio"],
                image_inputs=image_inputs
            )

            # 提取URL
            media = result.get("media", [])
            if not media:
                yield self._create_error_response("生成结果为空")
                return

            image_url = media[0]["image"]["generatedImage"]["fifeUrl"]

            # 缓存图片 (如果启用)
            local_url = image_url
            if config.cache_enabled:
                try:
                    if stream:
                        yield self._create_stream_chunk("缓存图片中...\n")
                    cached_filename = await self.file_cache.download_and_cache(image_url, "image")
                    local_url = f"{self._get_base_url()}/tmp/{cached_filename}"
                    if stream:
                        yield self._create_stream_chunk("✅ 图片缓存成功,准备返回缓存地址...\n")
                except Exception as e:
                    debug_logger.log_error(f"Failed to cache image: {str(e)}")
                    # 缓存失败不影响结果返回,使用原始URL
                    local_url = image_url
                    if stream:
                        yield self._create_stream_chunk(f"⚠️ 缓存失败: {str(e)}\n正在返回源链接...\n")
            else:
                if stream:
                    yield self._create_stream_chunk("缓存已关闭,正在返回源链接...\n")

            # 返回结果
            if stream:
                yield self._create_stream_chunk(
                    f"![Generated Image]({local_url})",
                    finish_reason="stop"
                )
            else:
                yield self._create_completion_response(
                    local_url,  # 直接传URL,让方法内部格式化
                    media_type="image"
                )

        finally:
            # 释放并发槽位
            if self.concurrency_manager:
                await self.concurrency_manager.release_image(token.id)

    async def _handle_video_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool
    ) -> AsyncGenerator:
        """处理视频生成 (异步轮询)"""

        # 获取并发槽位
        if self.concurrency_manager:
            if not await self.concurrency_manager.acquire_video(token.id):
                yield self._create_error_response("视频并发限制已达上限")
                return

        try:
            # 获取模型类型和配置
            video_type = model_config.get("video_type")
            supports_images = model_config.get("supports_images", False)
            min_images = model_config.get("min_images", 0)
            max_images = model_config.get("max_images", 0)

            # 图片数量
            image_count = len(images) if images else 0

            # ========== 验证和处理图片 ==========

            # T2V: 文生视频 - 不支持图片
            if video_type == "t2v":
                if image_count > 0:
                    if stream:
                        yield self._create_stream_chunk("⚠️ 文生视频模型不支持上传图片,将忽略图片仅使用文本提示词生成\n")
                    debug_logger.log_warning(f"[T2V] 模型 {model_config['model_key']} 不支持图片,已忽略 {image_count} 张图片")
                images = None  # 清空图片
                image_count = 0

            # I2V: 首尾帧模型 - 需要1-2张图片
            elif video_type == "i2v":
                if image_count < min_images or image_count > max_images:
                    error_msg = f"❌ 首尾帧模型需要 {min_images}-{max_images} 张图片,当前提供了 {image_count} 张"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    yield self._create_error_response(error_msg)
                    return

            # R2V: 多图生成 - 支持多张图片,不限制数量
            elif video_type == "r2v":
                # 不再限制最大图片数量
                pass

            # ========== 上传图片 ==========
            start_media_id = None
            end_media_id = None
            reference_images = []

            # I2V: 首尾帧处理
            if video_type == "i2v" and images:
                if image_count == 1:
                    # 只有1张图: 仅作为首帧
                    if stream:
                        yield self._create_stream_chunk("上传首帧图片...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"]
                    )
                    debug_logger.log_info(f"[I2V] 仅上传首帧: {start_media_id}")

                elif image_count == 2:
                    # 2张图: 首帧+尾帧
                    if stream:
                        yield self._create_stream_chunk("上传首帧和尾帧图片...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"]
                    )
                    end_media_id = await self.flow_client.upload_image(
                        token.at, images[1], model_config["aspect_ratio"]
                    )
                    debug_logger.log_info(f"[I2V] 上传首尾帧: {start_media_id}, {end_media_id}")

            # R2V: 多图处理
            elif video_type == "r2v" and images:
                if stream:
                    yield self._create_stream_chunk(f"上传 {image_count} 张参考图片...\n")

                for idx, img in enumerate(images):  # 上传所有图片,不限制数量
                    media_id = await self.flow_client.upload_image(
                        token.at, img, model_config["aspect_ratio"]
                    )
                    reference_images.append({
                        "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                        "mediaId": media_id
                    })
                debug_logger.log_info(f"[R2V] 上传了 {len(reference_images)} 张参考图片")

            # ========== 调用生成API ==========
            if stream:
                yield self._create_stream_chunk("提交视频生成任务...\n")

            # I2V: 首尾帧生成
            if video_type == "i2v" and start_media_id:
                if end_media_id:
                    # 有首尾帧
                    result = await self.flow_client.generate_video_start_end(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=model_config["model_key"],
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        end_media_id=end_media_id,
                        user_paygate_tier=token.user_paygate_tier or "PAYGATE_TIER_ONE"
                    )
                else:
                    # 只有首帧
                    result = await self.flow_client.generate_video_start_image(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=model_config["model_key"],
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        user_paygate_tier=token.user_paygate_tier or "PAYGATE_TIER_ONE"
                    )

            # R2V: 多图生成
            elif video_type == "r2v" and reference_images:
                result = await self.flow_client.generate_video_reference_images(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    reference_images=reference_images,
                    user_paygate_tier=token.user_paygate_tier or "PAYGATE_TIER_ONE"
                )

            # T2V 或 R2V无图: 纯文本生成
            else:
                result = await self.flow_client.generate_video_text(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    user_paygate_tier=token.user_paygate_tier or "PAYGATE_TIER_ONE"
                )

            # 获取task_id和operations
            operations = result.get("operations", [])
            if not operations:
                yield self._create_error_response("生成任务创建失败")
                return

            operation = operations[0]
            task_id = operation["operation"]["name"]
            scene_id = operation.get("sceneId")

            # 保存Task到数据库
            task = Task(
                task_id=task_id,
                token_id=token.id,
                model=model_config["model_key"],
                prompt=prompt,
                status="processing",
                scene_id=scene_id
            )
            await self.db.create_task(task)

            # 轮询结果
            if stream:
                yield self._create_stream_chunk(f"视频生成中...\n")

            async for chunk in self._poll_video_result(token, operations, stream):
                yield chunk

        finally:
            # 释放并发槽位
            if self.concurrency_manager:
                await self.concurrency_manager.release_video(token.id)

    async def _poll_video_result(
        self,
        token,
        operations: List[Dict],
        stream: bool
    ) -> AsyncGenerator:
        """轮询视频生成结果"""

        max_attempts = config.max_poll_attempts
        poll_interval = config.poll_interval

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            try:
                result = await self.flow_client.check_video_status(token.at, operations)
                checked_operations = result.get("operations", [])

                if not checked_operations:
                    continue

                operation = checked_operations[0]
                status = operation.get("status")

                # 状态更新 - 每20秒报告一次 (poll_interval=3秒, 20秒约7次轮询)
                progress_update_interval = 7  # 每7次轮询 = 21秒
                if stream and attempt % progress_update_interval == 0:  # 每20秒报告一次
                    progress = min(int((attempt / max_attempts) * 100), 95)
                    yield self._create_stream_chunk(f"生成进度: {progress}%\n")

                # 检查状态
                if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                    # 成功
                    metadata = operation["operation"].get("metadata", {})
                    video_info = metadata.get("video", {})
                    video_url = video_info.get("fifeUrl")

                    if not video_url:
                        yield self._create_error_response("视频URL为空")
                        return

                    # 缓存视频 (如果启用)
                    local_url = video_url
                    if config.cache_enabled:
                        try:
                            if stream:
                                yield self._create_stream_chunk("正在缓存视频文件...\n")
                            cached_filename = await self.file_cache.download_and_cache(video_url, "video")
                            local_url = f"{self._get_base_url()}/tmp/{cached_filename}"
                            if stream:
                                yield self._create_stream_chunk("✅ 视频缓存成功,准备返回缓存地址...\n")
                        except Exception as e:
                            debug_logger.log_error(f"Failed to cache video: {str(e)}")
                            # 缓存失败不影响结果返回,使用原始URL
                            local_url = video_url
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ 缓存失败: {str(e)}\n正在返回源链接...\n")
                    else:
                        if stream:
                            yield self._create_stream_chunk("缓存已关闭,正在返回源链接...\n")

                    # 更新数据库
                    task_id = operation["operation"]["name"]
                    await self.db.update_task(
                        task_id,
                        status="completed",
                        progress=100,
                        result_urls=[local_url],
                        completed_at=time.time()
                    )

                    # 返回结果
                    if stream:
                        yield self._create_stream_chunk(
                            f"<video src='{local_url}' controls style='max-width:100%'></video>",
                            finish_reason="stop"
                        )
                    else:
                        yield self._create_completion_response(
                            local_url,  # 直接传URL,让方法内部格式化
                            media_type="video"
                        )
                    return

                elif status.startswith("MEDIA_GENERATION_STATUS_ERROR"):
                    # 失败
                    yield self._create_error_response(f"视频生成失败: {status}")
                    return

            except Exception as e:
                debug_logger.log_error(f"Poll error: {str(e)}")
                continue

        # 超时
        yield self._create_error_response(f"视频生成超时 (已轮询{max_attempts}次)")

    # ========== 响应格式化 ==========

    def _create_stream_chunk(self, content: str, role: str = None, finish_reason: str = None) -> str:
        """创建流式响应chunk"""
        import json
        import time

        chunk = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "flow2api",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason
            }]
        }

        if role:
            chunk["choices"][0]["delta"]["role"] = role

        if finish_reason:
            chunk["choices"][0]["delta"]["content"] = content
        else:
            chunk["choices"][0]["delta"]["reasoning_content"] = content

        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    def _create_completion_response(self, content: str, media_type: str = "image", is_availability_check: bool = False) -> str:
        """创建非流式响应

        Args:
            content: 媒体URL或纯文本消息
            media_type: 媒体类型 ("image" 或 "video")
            is_availability_check: 是否为可用性检查响应 (纯文本消息)

        Returns:
            JSON格式的响应
        """
        import json
        import time

        # 可用性检查: 返回纯文本消息
        if is_availability_check:
            formatted_content = content
        else:
            # 媒体生成: 根据媒体类型格式化内容为Markdown
            if media_type == "video":
                formatted_content = f"```html\n<video src='{content}' controls></video>\n```"
            else:  # image
                formatted_content = f"![Generated Image]({content})"

        response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "flow2api",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": formatted_content
                },
                "finish_reason": "stop"
            }]
        }

        return json.dumps(response, ensure_ascii=False)

    def _create_error_response(self, error_message: str) -> str:
        """创建错误响应"""
        import json

        error = {
            "error": {
                "message": error_message,
                "type": "invalid_request_error",
                "code": "generation_failed"
            }
        }

        return json.dumps(error, ensure_ascii=False)

    def _get_base_url(self) -> str:
        """获取基础URL用于缓存文件访问"""
        # 优先使用配置的cache_base_url
        if config.cache_base_url:
            return config.cache_base_url
        # 否则使用服务器地址
        return f"http://{config.server_host}:{config.server_port}"

    async def _log_request(
        self,
        token_id: Optional[int],
        operation: str,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        status_code: int,
        duration: float
    ):
        """记录请求到数据库"""
        try:
            log = RequestLog(
                token_id=token_id,
                operation=operation,
                request_body=json.dumps(request_data, ensure_ascii=False),
                response_body=json.dumps(response_data, ensure_ascii=False),
                status_code=status_code,
                duration=duration
            )
            await self.db.add_request_log(log)
        except Exception as e:
            # 日志记录失败不影响主流程
            debug_logger.log_error(f"Failed to log request: {e}")

    async def _try_auto_get_token(self) -> bool:
        """
        尝试通过浏览器自动获取新 token
        
        Returns:
            bool: 成功返回 True，失败返回 False
        """
        try:
            # 检查是否配置了浏览器服务
            if not self.browser_service:
                debug_logger.log_warning("[AUTO_TOKEN] 浏览器服务未配置，无法自动获取Token")
                return False
            
            debug_logger.log_info("[AUTO_TOKEN] 开始通过浏览器获取 cookies...")
            
            # 获取 cookies
            cookie_result = await self.browser_service.get_flow_cookies()
            
            if not cookie_result:
                debug_logger.log_error("[AUTO_TOKEN] 获取 cookies 失败")
                return False
            
            cookies_simple = cookie_result.get('simple', {})
            
            # 查找 session token (可能的 cookie 名称)
            st = None
            possible_keys = [
                '__Secure-next-auth.session-token',
                'next-auth.session-token',
                '__Secure-session-token',
                'session-token'
            ]
            
            for key in possible_keys:
                if key in cookies_simple:
                    st = cookies_simple[key]
                    debug_logger.log_info(f"[AUTO_TOKEN] 找到 session token (key: {key})")
                    break
            
            if not st:
                debug_logger.log_error(f"[AUTO_TOKEN] 未找到有效的 session token。可用的 cookies: {list(cookies_simple.keys())}")
                return False
            
            # 添加 token
            debug_logger.log_info(f"[AUTO_TOKEN] 尝试添加新 Token (ST: {st[:20]}...)")
            
            try:
                new_token = await self.token_manager.add_token(
                    st=st,
                    remark="Auto-generated from browser",
                    image_enabled=True,
                    video_enabled=True
                )
                debug_logger.log_info(f"[AUTO_TOKEN] ✅ 成功添加新 Token (ID: {new_token.id}, Email: {new_token.email})")
                return True
                
            except ValueError as e:
                # Token 可能已存在
                if "已存在" in str(e):
                    debug_logger.log_warning(f"[AUTO_TOKEN] Token 已存在: {str(e)}")
                    # 尝试启用该 token
                    existing_token = await self.db.get_token_by_st(st)
                    if existing_token and not existing_token.is_active:
                        await self.token_manager.enable_token(existing_token.id)
                        debug_logger.log_info(f"[AUTO_TOKEN] 已启用现有 Token (ID: {existing_token.id})")
                        return True
                    return False
                else:
                    raise
                    
        except Exception as e:
            debug_logger.log_error(f"[AUTO_TOKEN] 自动获取Token失败: {str(e)}")
            import traceback
            debug_logger.log_error(f"[AUTO_TOKEN] 错误详情: {traceback.format_exc()}")
            return False

