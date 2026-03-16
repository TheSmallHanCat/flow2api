"""Generation handler for Flow2API"""
import asyncio
import base64
import json
import time
from typing import Optional, AsyncGenerator, List, Dict, Any
from ..core.logger import debug_logger
from ..core.config import config
from ..core.models import Task, RequestLog
from ..core.account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .file_cache import FileCache


# Model configuration
MODEL_CONFIG = {
    # image generation - GEM_PIX (Gemini 2.5 Flash)
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

    # image generation - GEM_PIX_2 (Gemini 3.0 Pro)
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
    "gemini-3.0-pro-image-square": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "gemini-3.0-pro-image-four-three": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "gemini-3.0-pro-image-three-four": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },

    # image generation - GEM_PIX_2 (Gemini 3.0 Pro) 2K upscaled
    "gemini-3.0-pro-image-landscape-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-portrait-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-square-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-four-three-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-three-four-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },

    # image generation - GEM_PIX_2 (Gemini 3.0 Pro) 4K upscaled
    "gemini-3.0-pro-image-landscape-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-portrait-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-square-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-four-three-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-three-four-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },

    # image generation - IMAGEN_3_5 (imagen 4.0)
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

    # image generation - NARWHAL (new version)
    "gemini-3.1-flash-image-landscape": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-3.1-flash-image-portrait": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },
    "gemini-3.1-flash-image-square": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "gemini-3.1-flash-image-four-three": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "gemini-3.1-flash-image-three-four": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },
    "gemini-3.1-flash-image-landscape-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-portrait-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-square-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-four-three-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-three-four-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-landscape-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-portrait-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-square-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-four-three-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-three-four-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },

    # ========== Text-to-video (T2V) ==========
    # Does not support image upload, uses text prompt only

    # veo_3_1_t2v_fast_portrait (portrait)
    # Upstream model name: veo_3_1_t2v_fast_portrait
    "veo_3_1_t2v_fast_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    # veo_3_1_t2v_fast_landscape (landscape)
    # Upstream model name: veo_3_1_t2v_fast
    "veo_3_1_t2v_fast_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_2_1_fast_d_15_t2v (landscape/portrait)
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

    # veo_2_0_t2v (landscape/portrait)
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

    # veo_3_1_t2v_fast_ultra (landscape/portrait)
    "veo_3_1_t2v_fast_portrait_ultra": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_fast_ultra": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_3_1_t2v_fast_ultra_relaxed (landscape/portrait)
    "veo_3_1_t2v_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_3_1_t2v (landscape/portrait)
    "veo_3_1_t2v_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # ========== Frame-based (I2V - image to video) ==========
    # Supports 1-2 images: 1 as first frame, 2 as first and last frames

    # veo_3_1_i2v_s_fast_fl (landscape/portrait)
    "veo_3_1_i2v_s_fast_portrait_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_2_1_fast_d_15_i2v (landscape/portrait)
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

    # veo_2_0_i2v (landscape/portrait)
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

    # veo_3_1_i2v_s_fast_ultra (landscape/portrait)
    "veo_3_1_i2v_s_fast_portrait_ultra_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_ultra_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s_fast_ultra_relaxed (landscape/portrait)
    "veo_3_1_i2v_s_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s (landscape/portrait)
    "veo_3_1_i2v_s_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # ========== Multi-image (R2V - Reference images to video) ==========
    # Upstream protocol supports up to 3 reference images

    # veo_3_1_r2v_fast (landscape/portrait)
    "veo_3_1_r2v_fast_portrait": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # veo_3_1_r2v_fast_ultra (landscape/portrait)
    "veo_3_1_r2v_fast_portrait_ultra": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast_ultra": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # veo_3_1_r2v_fast_ultra_relaxed (landscape/portrait)
    "veo_3_1_r2v_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # ========== video Upsampler ==========
    # Only 3.1 supports this, requires video generation first then upsampling, may take 30 minutes

    # T2V 4K upscaled
    "veo_3_1_t2v_fast_portrait_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_portrait_ultra_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_ultra_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # T2V 1080P upscaled
    "veo_3_1_t2v_fast_portrait_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_portrait_ultra_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_ultra_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # I2V 4K upscaled
    "veo_3_1_i2v_s_fast_portrait_ultra_fl_4k": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_i2v_s_fast_ultra_fl_4k": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # I2V 1080P upscaled
    "veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_i2v_s_fast_ultra_fl_1080p": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # R2V 4K upscaled
    "veo_3_1_r2v_fast_portrait_ultra_4k": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_r2v_fast_ultra_4k": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # R2V 1080P upscaled
    "veo_3_1_r2v_fast_portrait_ultra_1080p": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_r2v_fast_ultra_1080p": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    }
}


class GenerationHandler:
    """Unified generation handler"""

    def __init__(self, flow_client, token_manager, load_balancer, db, concurrency_manager, proxy_manager):
        self.flow_client = flow_client
        self.token_manager = token_manager
        self.load_balancer = load_balancer
        self.db = db
        self.concurrency_manager = concurrency_manager
        self.file_cache = FileCache(
            cache_dir="tmp",
            default_timeout=config.cache_timeout,
            proxy_manager=proxy_manager
        )
        self._last_generated_url = None
        self._last_generation_assets = None

    def _create_generation_result(self) -> Dict[str, Any]:
        """Create a new generation result tracker."""
        return dict(success=False, error_message=None, error_emitted=False)

    def _mark_generation_failed(self, generation_result: Optional[Dict[str, Any]], error_message: str):
        """Mark the generation result as failed with an error message."""
        if isinstance(generation_result, dict):
            generation_result["success"] = False
            generation_result["error_message"] = error_message
            generation_result["error_emitted"] = True

    def _mark_generation_succeeded(self, generation_result: Optional[Dict[str, Any]]):
        """Mark the generation result as succeeded."""
        if isinstance(generation_result, dict):
            generation_result["success"] = True
            generation_result["error_message"] = None
            generation_result["error_emitted"] = False

    def _normalize_error_message(self, error_message: Any, max_length: int = 1000) -> str:
        """Normalize error text to avoid writing excessively long content."""
        text = str(error_message or "").strip() or "Unknown error"
        if len(text) <= max_length:
            return text
        return f"{text[:max_length - 3]}..."

    async def _fail_video_task(self, operations: Optional[List[Dict[str, Any]]], error_message: str):
        """Transition video task to failed state to avoid lingering processing status."""
        if not operations:
            return

        operation = operations[0] if operations else {}
        task_id = (operation.get("operation") or {}).get("name")
        if not task_id:
            return

        try:
            await self.db.update_task(
                task_id,
                status="failed",
                error_message=self._normalize_error_message(error_message),
                completed_at=time.time()
            )
        except Exception as exc:
            debug_logger.log_error(f"[VIDEO] Failed to update task failure status: {exc}")

    async def check_token_availability(self, is_image: bool, is_video: bool) -> bool:
        """Check token availability

        Args:
            is_image: Whether to check image generation token
            is_video: Whether to check video generation token

        Returns:
            True if a token is available, False otherwise
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
        """Unified generation entry point

        Args:
            model: Model name
            prompt: prompt
            images: image list (bytes format)
            stream: Whether to use streaming output
        """
        start_time = time.time()
        token = None
        generation_type = None
        pending_token_state = {"active": False}
        request_id = f"gen-{int(start_time * 1000)}-{id(asyncio.current_task())}"
        perf_trace: Dict[str, Any] = {
            "request_id": request_id,
            "model": model,
            "status": "processing",
        }
        generation_result = self._create_generation_result()
        request_log_state: Dict[str, Any] = {"id": None, "progress": 0}
        self._last_generated_url = None
        self._last_generation_assets = None

        # Prevent concurrent requests from reusing the previous request's fingerprint context
        if hasattr(self.flow_client, "clear_request_fingerprint"):
            self.flow_client.clear_request_fingerprint()

        # 1. Validate model
        if model not in MODEL_CONFIG:
            error_msg = f"Unsupported model: {model}"
            debug_logger.log_error(error_msg)
            yield self._create_error_response(error_msg, status_code=400)
            return

        model_config = MODEL_CONFIG[model]
        generation_type = model_config["type"]
        request_operation = f"generate_{generation_type}"
        prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
        request_payload = {
            "model": model,
            "prompt": prompt_for_log,
            "has_images": images is not None and len(images) > 0,
        }
        debug_logger.log_info(f"[GENERATION] Starting generation - model: {model}, type: {generation_type}, prompt: {prompt[:50]}...")

        # Show start message to user
        if stream:
            yield self._create_stream_chunk(
                f"✨ {'video' if generation_type == 'video' else 'image'}generation task started\n",
                role="assistant"
            )
            request_log_state["id"] = await self._log_request(
                token_id=None,
                operation=request_operation,
                request_data=request_payload,
                response_data={"status": "processing", "status_text": "started", "progress": 0, "request_id": request_id},
                status_code=102,
                duration=0,
                status_text="started",
                progress=0,
            )

        # 2. Select token
        debug_logger.log_info(f"[GENERATION] Selecting available token...")
        token_select_started_at = time.time()

        if generation_type == "image":
            token = await self.load_balancer.select_token(
                for_image_generation=True,
                model=model,
                reserve=False,
                enforce_concurrency_filter=False,
                track_pending=True,
            )
        else:
            token = await self.load_balancer.select_token(
                for_video_generation=True,
                model=model,
                reserve=False,
                enforce_concurrency_filter=False,
                track_pending=True,
            )
        perf_trace["token_select_ms"] = int((time.time() - token_select_started_at) * 1000)

        if not token:
            error_msg = self._get_no_token_error_message(generation_type)
            debug_logger.log_error(f"[GENERATION] {error_msg}")
            await self._log_request(
                token_id=None,
                operation=request_operation,
                request_data=request_payload,
                response_data={"error": error_msg, "performance": perf_trace},
                status_code=503,
                duration=time.time() - start_time,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            if stream:
                yield self._create_stream_chunk(f"❌ {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=503)
            return

        debug_logger.log_info(f"[GENERATION] Token selected: {token.id} ({token.email})")
        await self._update_request_log_progress(
            request_log_state,
            token_id=token.id,
            status_text="token_selected",
            progress=8,
            response_extra={"token_email": token.email},
        )

        try:
            # 3. Ensure AT is valid
            debug_logger.log_info(f"[GENERATION] Checking token AT validity...")
            if stream:
                yield self._create_stream_chunk("Initializing generation environment...\n")

            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="token_ready",
                progress=15,
            )
            ensure_at_started_at = time.time()
            token = await self.token_manager.ensure_valid_token(token)
            perf_trace["ensure_at_ms"] = int((time.time() - ensure_at_started_at) * 1000)
            if not token:
                error_msg = "Token AT invalid or refresh failed"
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                if stream:
                    yield self._create_stream_chunk(f"❌ {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=503)
                return

            # 4. Ensure project exists
            debug_logger.log_info(f"[GENERATION] Checking/creating project...")

            if not supports_model_for_tier(model, token.user_paygate_tier):
                required_tier = get_required_paygate_tier_for_model(model)
                error_msg = "Current model requires " + get_paygate_tier_label(required_tier) + " account: " + model
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                if stream:
                    yield self._create_stream_chunk(f"❌ {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=403)
                return

            ensure_project_started_at = time.time()
            project_id = await self.token_manager.ensure_project_exists(token.id)
            perf_trace["ensure_project_ms"] = int((time.time() - ensure_project_started_at) * 1000)
            debug_logger.log_info(f"[GENERATION] Project ID: {project_id}")
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="project_ready",
                progress=22,
                response_extra={"project_id": project_id},
            )

            # 5. Process by type
            generation_pipeline_started_at = time.time()
            if generation_type == "image":
                debug_logger.log_info(f"[GENERATION] Starting image generation pipeline...")
                async for chunk in self._handle_image_generation(
                    token, project_id, model_config, prompt, images, stream,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state
                ):
                    yield chunk
            else:  # video
                debug_logger.log_info(f"[GENERATION] Starting video generation pipeline...")
                async for chunk in self._handle_video_generation(
                    token, project_id, model_config, prompt, images, stream,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state
                ):
                    yield chunk
            perf_trace["generation_pipeline_ms"] = int((time.time() - generation_pipeline_started_at) * 1000)

            # 6. Record usage
            if not generation_result.get("success"):
                error_msg = generation_result.get("error_message") or "Generation did not complete successfully"
                debug_logger.log_warning(f"[GENERATION] Generation failed, not counting usage: {error_msg}")
                if token:
                    await self.token_manager.record_error(token.id)
                duration = time.time() - start_time
                perf_trace["status"] = "failed"
                perf_trace["total_ms"] = int(duration * 1000)
                perf_trace["error"] = error_msg
                prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
                await self._log_request(
                    token.id if token else None,
                    request_operation,
                    request_payload,
                    {"error": error_msg, "performance": perf_trace},
                    500,
                    duration,
                    log_id=request_log_state.get("id"),
                    status_text="failed",
                    progress=request_log_state.get("progress", 0),
                )
                self._last_generated_url = None
                self._last_generation_assets = None
                if not generation_result.get("error_emitted"):
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=500)
                return

            is_video = (generation_type == "video")
            await self.token_manager.record_usage(token.id, is_video=is_video)

            # Reset error count (clear consecutive error count on success)
            await self.token_manager.record_success(token.id)

            debug_logger.log_info(f"[GENERATION] ✅ Generation completed successfully")

            # 7. Log success
            duration = time.time() - start_time
            perf_trace["status"] = "success"
            perf_trace["total_ms"] = int(duration * 1000)
            # Keep more complete prompt in logs to avoid management page showing truncated content
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"

            # Build response data containing generated URL
            response_data = {
                "status": "success",
                "model": model,
                "prompt": prompt_for_log,
                "performance": perf_trace
            }

            # Add generated URL (if available)
            if hasattr(self, '_last_generated_url') and self._last_generated_url:
                response_data["url"] = self._last_generated_url
            if hasattr(self, "_last_generation_assets") and self._last_generation_assets:
                response_data["generated_assets"] = self._last_generation_assets

            # Clear temporary storage to avoid contaminating subsequent requests
            self._last_generated_url = None
            self._last_generation_assets = None
            image_perf = perf_trace.get("image_generation", {}) if isinstance(perf_trace, dict) else {}
            video_perf = perf_trace.get("video_generation", {}) if isinstance(perf_trace, dict) else {}
            debug_logger.log_info(
                f"[PERF] [{request_id}] total={perf_trace.get('total_ms', 0)}ms, "
                f"select={perf_trace.get('token_select_ms', 0)}ms, "
                f"ensure_at={perf_trace.get('ensure_at_ms', 0)}ms, "
                f"project={perf_trace.get('ensure_project_ms', 0)}ms, "
                f"pipeline={perf_trace.get('generation_pipeline_ms', 0)}ms, "
                f"slot_wait={image_perf.get('slot_wait_ms', 0)}ms, "
                f"launch_queue={image_perf.get('launch_queue_wait_ms', 0)}ms, "
                f"launch_stagger={image_perf.get('launch_stagger_wait_ms', 0)}ms, "
                f"video_slot_wait={video_perf.get('slot_wait_ms', 0)}ms"
            )

            await self._log_request(
                token.id,
                request_operation,
                request_payload,
                response_data,
                200,
                duration,
                log_id=request_log_state.get("id"),
                status_text="completed",
                progress=100,
            )

        except asyncio.CancelledError:
            error_msg = "Generation cancelled: client connection disconnected"
            debug_logger.log_warning(f"[GENERATION] ⚠️ {error_msg}")
            duration = time.time() - start_time
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                request_operation if generation_type else "generate_unknown",
                request_payload if 'request_payload' in locals() else {"model": model},
                {"error": error_msg, "performance": perf_trace},
                499,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            raise
        except Exception as e:
            error_msg = f"Generation failed: {str(e)}"
            debug_logger.log_error(f"[GENERATION] ❌ {error_msg}")
            if token:
                # Record error (all errors handled uniformly, no special 429 handling)
                await self.token_manager.record_error(token.id)

            # Persist final failure state to database before returning error response, avoiding log stuck at 102.
            duration = time.time() - start_time
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                request_operation if generation_type else "generate_unknown",
                request_payload if 'request_payload' in locals() else {"model": model},
                {"error": error_msg, "performance": perf_trace},
                500,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            if stream:
                yield self._create_stream_chunk(f"❌ {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=500)
        finally:
            if pending_token_state.get("active") and token and self.load_balancer:
                await self.load_balancer.release_pending(
                    token.id,
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                )
                pending_token_state["active"] = False


    def _get_no_token_error_message(self, generation_type: str) -> str:
        """Get detailed error message when no token is available"""
        if generation_type == "image":
            return "No available token for image generation. All tokens are disabled, cooling down, locked, or expired."
        else:
            return "No available token for video generation. All tokens are disabled, cooling down, quota exhausted, or expired."

    async def _handle_image_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None
    ) -> AsyncGenerator:
        """Handle image generation (synchronous return)"""

        image_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            image_trace = perf_trace.setdefault("image_generation", {})
            image_trace["input_image_count"] = len(images) if images else 0

        # Do not wait locally for image hard concurrency slot; submit to upstream as soon as request arrives.
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        if image_trace is not None:
            image_trace["slot_wait_ms"] = 0

        if images and len(images) > 0:
            await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="uploading_images", progress=28)
        else:
            await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="submitting_image", progress=28)

        try:
            # Upload images (if any)
            upload_started_at = time.time()
            image_inputs = []
            if images and len(images) > 0:
                if stream:
                    yield self._create_stream_chunk(f"Uploading {len(images)} reference image(s)...\n")

                # Support multi-image input
                for idx, image_bytes in enumerate(images):
                    media_id = await self.flow_client.upload_image(
                        token.at,
                        image_bytes,
                        model_config["aspect_ratio"],
                        project_id=project_id
                    )
                    image_inputs.append({
                        "name": media_id,
                        "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"
                    })
                    if stream:
                        yield self._create_stream_chunk(f"Uploaded image {idx + 1}/{len(images)}  image(s)\n")
            if image_trace is not None:
                image_trace["upload_images_ms"] = int((time.time() - upload_started_at) * 1000)

            # Call generation API
            if stream:
                yield self._create_stream_chunk("Generating image...\n")

            generate_started_at = time.time()
            result, generation_session_id, upstream_trace = await self.flow_client.generate_image(
                at=token.at,
                project_id=project_id,
                prompt=prompt,
                model_name=model_config["model_name"],
                aspect_ratio=model_config["aspect_ratio"],
                image_inputs=image_inputs,
                token_id=token.id,
                token_image_concurrency=token.image_concurrency,
            )
            if image_trace is not None:
                image_trace["generate_api_ms"] = int((time.time() - generate_started_at) * 1000)
                image_trace["upstream_trace"] = upstream_trace
                attempts = upstream_trace.get("generation_attempts") if isinstance(upstream_trace, dict) else None
                if isinstance(attempts, list) and attempts:
                    first_attempt = attempts[0] if isinstance(attempts[0], dict) else {}
                    image_trace["launch_queue_wait_ms"] = int(first_attempt.get("launch_queue_ms") or 0)
                    image_trace["launch_stagger_wait_ms"] = int(first_attempt.get("launch_stagger_ms") or 0)

            # Extract URL and mediaId
            media = result.get("media", [])
            if not media:
                self._mark_generation_failed(generation_result, "Generation result is empty")
                yield self._create_error_response("Generation result is empty", status_code=502)
                return

            image_url = media[0]["image"]["generatedImage"]["fifeUrl"]
            media_id = media[0].get("name")  # for upsample
            self._last_generation_assets = {
                "type": "image",
                "origin_image_url": image_url
            }

            # Check if upsample is needed
            upsample_resolution = model_config.get("upsample")
            if upsample_resolution and media_id:
                upsample_started_at = time.time()
                resolution_name = "4K" if "4K" in upsample_resolution else "2K"
                await self._update_request_log_progress(request_log_state, token_id=token.id, status_text=f"upsampling_{resolution_name.lower()}", progress=82)
                if stream:
                    yield self._create_stream_chunk(f"Upscaling image to {resolution_name}...\n")

                # 4K/2K image retry logic - up to 3 retries
                max_retries = 3
                for retry_attempt in range(max_retries):
                    try:
                        # Call upsample API
                        encoded_image = await self.flow_client.upsample_image(
                            at=token.at,
                            project_id=project_id,
                            media_id=media_id,
                            target_resolution=upsample_resolution,
                            user_paygate_tier=normalized_tier,
                            session_id=generation_session_id,
                            token_id=token.id
                        )

                        if encoded_image:
                            debug_logger.log_info(f"[UPSAMPLE] Image upscaled to {resolution_name}")

                            if stream:
                                yield self._create_stream_chunk(f"✅ Image upscaled to {resolution_name}\n")

                            # Cache upscaled image (if enabled)
                            # Log records original image URL + 2K/4K info
                            self._last_generated_url = image_url
                            self._last_generation_assets = {
                                "type": "image",
                                "origin_image_url": image_url,
                                "upscaled_image": {
                                    "resolution": resolution_name,
                                    "base64": encoded_image
                                }
                            }

                            if config.cache_enabled:
                                try:
                                    if stream:
                                        yield self._create_stream_chunk(f"Caching {resolution_name} image...\n")
                                    cached_filename = await self.file_cache.cache_base64_image(encoded_image, resolution_name)
                                    local_url = f"{self._get_base_url()}/tmp/{cached_filename}"
                                    self._last_generation_assets["upscaled_image"]["local_url"] = local_url
                                    self._last_generation_assets["upscaled_image"]["url"] = local_url
                                    self._mark_generation_succeeded(generation_result)
                                    if stream:
                                        yield self._create_stream_chunk(f"✅ {resolution_name} image cached successfully\n")
                                        yield self._create_stream_chunk(
                                            f"![Generated image]({local_url})",
                                            finish_reason="stop"
                                        )
                                    else:
                                        yield self._create_completion_response(
                                            local_url,
                                            media_type="image"
                                        )
                                    if image_trace is not None:
                                        image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)
                                    return
                                except Exception as e:
                                    debug_logger.log_error(f"Failed to cache {resolution_name} image: {str(e)}")
                                    if stream:
                                        cache_error = self._normalize_error_message(e, max_length=120)
                                        yield self._create_stream_chunk(f"⚠️ Cache failed: {cache_error}，, returning base64...\n")

                            # Cache not enabled or cache failed, return base64 format
                            base64_url = f"data:image/jpeg;base64,{encoded_image}"
                            self._last_generation_assets["upscaled_image"]["local_url"] = None
                            self._last_generation_assets["upscaled_image"]["url"] = base64_url
                            self._mark_generation_succeeded(generation_result)
                            if stream:
                                yield self._create_stream_chunk(
                                    f"![Generated image]({base64_url})",
                                    finish_reason="stop"
                                )
                            else:
                                yield self._create_completion_response(
                                    base64_url,
                                    media_type="image"
                                )
                            if image_trace is not None:
                                image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)
                            return
                        else:
                            debug_logger.log_warning("[UPSAMPLE] Return result is empty")
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ Upscaling failed, returning original image...\n")
                            break  # empty result, no retry

                    except Exception as e:
                        error_str = str(e)
                        debug_logger.log_error(f"[UPSAMPLE] Upscaling failed (attempt {retry_attempt + 1}/{max_retries}): {error_str}")
                        
                        # Check if error is retryable (403, reCAPTCHA, timeout, etc.)
                        retry_reason = self.flow_client._get_retry_reason(error_str)
                        if retry_reason and retry_attempt < max_retries - 1:
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ Upscaling encountered {retry_reason}, retrying ({retry_attempt + 2}/{max_retries})...\n")
                            # wait briefly before retry
                            await asyncio.sleep(1)
                            continue
                        else:
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ Upscaling failed: {error_str}，returning original image...\n")
                            break
                if image_trace is not None:
                    image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)

            # 1K original image returns official direct link directly, avoiding extra download cache dependency on curl/wget.
            local_url = image_url
            if stream:
                yield self._create_stream_chunk("Returning official image link...\n")
            if image_trace is not None:
                image_trace["cache_image_ms"] = 0

            # Return result
            # Store URL for log recording
            self._last_generated_url = local_url
            self._last_generation_assets = {
                "type": "image",
                "origin_image_url": image_url,
                "final_image_url": local_url
            }
            self._mark_generation_succeeded(generation_result)

            if stream:
                yield self._create_stream_chunk(
                    f"![Generated image]({local_url})",
                    finish_reason="stop"
                )
            else:
                yield self._create_completion_response(
                    local_url,  # pass URL directly,let method format internally
                    media_type="image"
                )

        finally:
            pass

    async def _handle_video_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None
    ) -> AsyncGenerator:
        """Handle video generation (async polling)"""

        video_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            video_trace = perf_trace.setdefault("video_generation", {})
            video_trace["input_image_count"] = len(images) if images else 0

        # Do not wait locally for video hard concurrency slot; submit to upstream as soon as request arrives.
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        if video_trace is not None:
            video_trace["slot_wait_ms"] = 0

        await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="preparing_video", progress=24)

        try:
            # Get model type and configuration
            video_type = model_config.get("video_type")
            supports_images = model_config.get("supports_images", False)
            min_images = model_config.get("min_images", 0)
            max_images = model_config.get("max_images", 0)

            # Auto-adjust model key based on account tier
            model_key = model_config["model_key"]
            user_tier = normalized_tier

            # TIER_TWO account requires ultra model version
            if user_tier == "PAYGATE_TIER_TWO":
                # If model key does not contain ultra, add automatically
                if "ultra" not in model_key:
                    # veo_3_1_i2v_s_fast_fl -> veo_3_1_i2v_s_fast_ultra_fl
                    # veo_3_1_i2v_s_fast_portrait_fl -> veo_3_1_i2v_s_fast_portrait_ultra_fl
                    # veo_3_1_t2v_fast -> veo_3_1_t2v_fast_ultra
                    # veo_3_1_t2v_fast_portrait -> veo_3_1_t2v_fast_portrait_ultra
                    # veo_3_1_r2v_fast_landscape -> veo_3_1_r2v_fast_landscape_ultra
                    if "_fl" in model_key:
                        model_key = model_key.replace("_fl", "_ultra_fl")
                    else:
                        # Directly append _ultra to the end
                        model_key = model_key + "_ultra"
                    
                    if stream:
                        yield self._create_stream_chunk(f"TIER_TWO account auto-switching to ultra model: {model_key}\n")
                    debug_logger.log_info(f"[VIDEO] TIER_TWO account，Model auto-adjusted: {model_config['model_key']} -> {model_key}")

            # TIER_ONE account requires non-ultra version
            elif user_tier == "PAYGATE_TIER_ONE":
                # If model key contains ultra, remove it (prevent user error)
                if "ultra" in model_key:
                    # veo_3_1_i2v_s_fast_ultra_fl -> veo_3_1_i2v_s_fast_fl
                    # veo_3_1_t2v_fast_ultra -> veo_3_1_t2v_fast
                    # veo_3_1_r2v_fast_landscape_ultra -> veo_3_1_r2v_fast_landscape
                    model_key = model_key.replace("_ultra_fl", "_fl").replace("_ultra", "")
                    
                    if stream:
                        yield self._create_stream_chunk(f"TIER_ONE account auto-switching to standard model: {model_key}\n")
                    debug_logger.log_info(f"[VIDEO] TIER_ONE account，Model auto-adjusted: {model_config['model_key']} -> {model_key}")

            # Update model_key in model_config
            model_config = dict(model_config)  # Create copy to avoid modifying original config
            model_config["model_key"] = model_key

            # image count
            image_count = len(images) if images else 0

            # ========== Validate and process images ==========

            # T2V: text-to-video - does not support images
            if video_type == "t2v":
                if image_count > 0:
                    if stream:
                        yield self._create_stream_chunk("⚠️ Text-to-video model does not support image upload, ignoring images and using text prompt only\n")
                    debug_logger.log_warning(f"[T2V] model {model_config['model_key']} does not support images,ignored {image_count}  image(s)")
                images = None  # Clear images
                image_count = 0

            # I2V: first/last frame model - requires1-2 image(s)
            elif video_type == "i2v":
                if image_count < min_images or image_count > max_images:
                    error_msg = f"❌ First/last frame model requires {min_images}-{max_images} image(s), but {image_count} provided  "
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # R2V: multi-image generation - upstream protocol supports up to 3 reference images
            elif video_type == "r2v":
                if max_images is not None and image_count > max_images:
                    error_msg = f"❌ Multi-image video model supports up to {max_images} reference images, but {image_count} provided  "
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # ========== upload images ==========
            start_media_id = None
            end_media_id = None
            reference_images = []

            # I2V: first/last frame processing
            if video_type == "i2v" and images:
                if image_count == 1:
                    # Only 1 image: used as first frame only
                    if stream:
                        yield self._create_stream_chunk("uploadfirst frameimage...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] onlyuploadfirst frame: {start_media_id}")

                elif image_count == 2:
                    # 2 image(s): first + last frame
                    if stream:
                        yield self._create_stream_chunk("uploadfirst and last frameimage...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    end_media_id = await self.flow_client.upload_image(
                        token.at, images[1], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] uploadfirst/last frame: {start_media_id}, {end_media_id}")

            # R2V: Multi-image processing
            elif video_type == "r2v" and images:
                if stream:
                    yield self._create_stream_chunk(f"Uploading {image_count} reference image(s)...\n")

                for img in images:
                    media_id = await self.flow_client.upload_image(
                        token.at, img, model_config["aspect_ratio"], project_id=project_id
                    )
                    reference_images.append({
                        "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                        "mediaId": media_id
                    })
                debug_logger.log_info(f"[R2V] Uploaded {len(reference_images)} reference image(s)")

            # ========== Call generation API ==========
            if stream:
                yield self._create_stream_chunk("Submitting video generation task...\n")
            submit_started_at = time.time()

            # I2V: first/last framegeneration
            if video_type == "i2v" and start_media_id:
                if end_media_id:
                    # Has first/last frames
                    result = await self.flow_client.generate_video_start_end(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=model_config["model_key"],
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        end_media_id=end_media_id,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )
                else:
                    # Only first frame - need to remove _fl from model_key
                    # Case 1: _fl_ in the middle (e.g. veo_3_1_i2v_s_fast_fl_ultra_relaxed -> veo_3_1_i2v_s_fast_ultra_relaxed)
                    # Case 2: _fl at the end (e.g. veo_3_1_i2v_s_fast_ultra_fl -> veo_3_1_i2v_s_fast_ultra)
                    actual_model_key = model_config["model_key"].replace("_fl_", "_")
                    if actual_model_key.endswith("_fl"):
                        actual_model_key = actual_model_key[:-3]
                    debug_logger.log_info(f"[I2V] Single-frame mode, model_key: {model_config['model_key']} -> {actual_model_key}")
                    result = await self.flow_client.generate_video_start_image(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=actual_model_key,
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )

            # R2V: multi-image generation
            elif video_type == "r2v" and reference_images:
                result = await self.flow_client.generate_video_reference_images(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    reference_images=reference_images,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )

            # T2V or R2V without images: pure text generation
            else:
                result = await self.flow_client.generate_video_text(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )
            if video_trace is not None:
                video_trace["submit_generation_ms"] = int((time.time() - submit_started_at) * 1000)

            # Get task_id and operations
            operations = result.get("operations", [])
            if not operations:
                self._mark_generation_failed(generation_result, "Generation task creation failed")
                yield self._create_error_response("Generation task creation failed", status_code=502)
                return

            operation = operations[0]
            task_id = operation["operation"]["name"]
            scene_id = operation.get("sceneId")

            # Save task to database
            task = Task(
                task_id=task_id,
                token_id=token.id,
                model=model_config["model_key"],
                prompt=prompt,
                status="processing",
                scene_id=scene_id
            )
            await self.db.create_task(task)
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="video_submitted",
                progress=45,
                response_extra={"task_id": task_id, "scene_id": scene_id},
            )

            # Poll results
            if stream:
                yield self._create_stream_chunk(f"Video generating...\n")

            # Check if upsample is needed
            upsample_config = model_config.get("upsample")

            async for chunk in self._poll_video_result(token, project_id, operations, stream, upsample_config, generation_result, request_log_state):
                yield chunk

        finally:
            pass

    async def _poll_video_result(
        self,
        token,
        project_id: str,
        operations: List[Dict],
        stream: bool,
        upsample_config: Optional[Dict] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None
    ) -> AsyncGenerator:
        """Poll video generation result
        
        Args:
            upsample_config: upscaleconfiguration {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
        """

        max_attempts = config.max_poll_attempts
        poll_interval = config.poll_interval
        
        # If upsample needed, triple poll attempts (upsample may take 30 minutes)
        if upsample_config:
            max_attempts = max_attempts * 3  # upsample needs more time

        consecutive_poll_errors = 0
        last_poll_error: Optional[Exception] = None
        max_consecutive_poll_errors = 3

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            try:
                result = await self.flow_client.check_video_status(token.at, operations)
                checked_operations = result.get("operations", [])
                consecutive_poll_errors = 0
                last_poll_error = None

                if not checked_operations:
                    continue

                operation = checked_operations[0]
                status = operation.get("status")

                # Status update - report every ~20 seconds (poll_interval=3s, ~7 polls per 20s)
                progress_update_interval = 7  # every 7 polls = 21 seconds
                if stream and attempt % progress_update_interval == 0:  # report every ~20 seconds
                    progress = min(int((attempt / max_attempts) * 100), 95)
                    await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="video_polling", progress=max(45, progress), response_extra={"upstream_status": status})
                    yield self._create_stream_chunk(f"Generation progress: {progress}%\n")

                # Check status
                if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                    # success
                    metadata = operation["operation"].get("metadata", {})
                    video_info = metadata.get("video", {})
                    video_url = video_info.get("fifeUrl")
                    video_media_id = video_info.get("mediaGenerationId")
                    aspect_ratio = video_info.get("aspectRatio", "VIDEO_ASPECT_RATIO_LANDSCAPE")

                    if not video_url:
                        error_msg = "Video generation failed: video URL is empty"
                        await self._fail_video_task(checked_operations, error_msg)
                        self._mark_generation_failed(generation_result, error_msg)
                        yield self._create_error_response(error_msg, status_code=502)
                        return

                    # ========== Video upsample processing ==========
                    if upsample_config and video_media_id:
                        if stream:
                            resolution_name = "4K" if "4K" in upsample_config["resolution"] else "1080P"
                            yield self._create_stream_chunk(f"\nVideo generation complete, starting {resolution_name} upsample... (may take 30 minutes)\n")
                        
                        try:
                            # Submit upsample task
                            upsample_result = await self.flow_client.upsample_video(
                                at=token.at,
                                project_id=project_id,
                                video_media_id=video_media_id,
                                aspect_ratio=aspect_ratio,
                                resolution=upsample_config["resolution"],
                                model_key=upsample_config["model_key"],
                                token_id=token.id,
                                token_video_concurrency=token.video_concurrency,
                            )
                            
                            upsample_operations = upsample_result.get("operations", [])
                            if upsample_operations:
                                if stream:
                                    yield self._create_stream_chunk("Upsample task submitted, continuing to poll...\n")
                                
                                # Recursively poll upsample result (no further upsample)
                                async for chunk in self._poll_video_result(
                                    token, project_id, upsample_operations, stream, None, generation_result, request_log_state
                                ):
                                    yield chunk
                                return
                            else:
                                if stream:
                                    yield self._create_stream_chunk("⚠️ Upsample task creation failed, returning original video\n")
                        except Exception as e:
                            debug_logger.log_error(f"video upsample failed: {str(e)}")
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ Upscaling failed: {str(e)}, returning original video\n")

                    # Cache video (if enabled)
                    local_url = video_url
                    if config.cache_enabled:
                        await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="caching_video", progress=92)
                        try:
                            if stream:
                                yield self._create_stream_chunk("Caching video file...\n")
                            cached_filename = await self.file_cache.download_and_cache(video_url, "video")
                            local_url = f"{self._get_base_url()}/tmp/{cached_filename}"
                            if stream:
                                yield self._create_stream_chunk("✅ Video cached successfully, preparing cached URL...\n")
                        except Exception as e:
                            debug_logger.log_error(f"Failed to cache video: {str(e)}")
                            # Cache failure does not affect result, use original URL
                            local_url = video_url
                            if stream:
                                cache_error = self._normalize_error_message(e, max_length=120)
                                yield self._create_stream_chunk(f"⚠️ Cache failed: {cache_error}\nReturning source link...\n")
                    else:
                        if stream:
                            yield self._create_stream_chunk("Cache disabled, returning source link...\n")

                    # Update database
                    task_id = operation["operation"]["name"]
                    await self.db.update_task(
                        task_id,
                        status="completed",
                        progress=100,
                        result_urls=[local_url],
                        completed_at=time.time()
                    )

                    # Store URL for log recording
                    self._last_generated_url = local_url
                    self._last_generation_assets = {
                        "type": "video",
                        "final_video_url": local_url
                    }

                    # Return result
                    self._mark_generation_succeeded(generation_result)

                    if stream:
                        yield self._create_stream_chunk(
                            f"<video src='{local_url}' controls style='max-width:100%'></video>",
                            finish_reason="stop"
                        )
                    else:
                        yield self._create_completion_response(
                            local_url,  # pass URL directly,let method format internally
                            media_type="video"
                        )
                    return

                elif status == "MEDIA_GENERATION_STATUS_FAILED":
                    # Generation failed - extract error info
                    error_info = operation.get("operation", {}).get("error", {})
                    error_code = error_info.get("code", "unknown")
                    error_message = error_info.get("message", "Unknown error")
                    
                    # Update database task status
                    await self._fail_video_task(
                        checked_operations,
                        f"{error_message} (code: {error_code})"
                    )
                    
                    # Return friendly error message, suggest user retry
                    friendly_error = f"Video generation failed: {error_message}. Please try again"
                    self._mark_generation_failed(generation_result, friendly_error)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {friendly_error}\n")
                    yield self._create_error_response(friendly_error, status_code=502)
                    return

                elif status.startswith("MEDIA_GENERATION_STATUS_ERROR"):
                    # Generation error status
                    error_msg = f"Video generation failed: {status}"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=502)
                    return

            except Exception as e:
                last_poll_error = e
                consecutive_poll_errors += 1
                debug_logger.log_error(f"Poll error: {str(e)}")
                if consecutive_poll_errors >= max_consecutive_poll_errors:
                    error_msg = f"Video status query failed: {self._normalize_error_message(e)}"
                    await self._fail_video_task(operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=502)
                    return
                continue

        # Timeout
        if last_poll_error is not None:
            error_msg = f"Video status query persistently failed: {self._normalize_error_message(last_poll_error)}"
        else:
            error_msg = f"Video generation timeout (polled {max_attempts} times)"
        await self._fail_video_task(operations, error_msg)
        self._mark_generation_failed(generation_result, error_msg)
        yield self._create_error_response(error_msg, status_code=504)

    # ========== Response formatting ==========

    def _create_stream_chunk(self, content: str, role: str = None, finish_reason: str = None) -> str:
        """Create streaming response chunk"""
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
        """Create non-streaming response

        Args:
            content: Media URL or plain text message
            media_type: Media type ("image" or "video")
            is_availability_check: Whether this is an availability check response (plain text message)

        Returns:
            JSON formatted response
        """
        import json
        import time

        # Availability check: return plain text message
        if is_availability_check:
            formatted_content = content
        else:
            # Media generation: format content as Markdown based on media type
            if media_type == "video":
                formatted_content = f"```html\n<video src='{content}' controls></video>\n```"
            else:  # image
                formatted_content = f"![Generated image]({content})"

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

    def _create_error_response(self, error_message: str, status_code: int = 500) -> str:
        """Create error response"""
        import json

        error = {
            "error": {
                "message": error_message,
                "type": "server_error" if status_code >= 500 else "invalid_request_error",
                "code": "generation_failed",
                "status_code": status_code,
            }
        }

        return json.dumps(error, ensure_ascii=False)

    def _get_base_url(self) -> str:
        """Get base URL for cache file access"""
        # Prefer configured cache_base_url
        if config.cache_base_url:
            return config.cache_base_url
        # Otherwise use server address
        return f"http://{config.server_host}:{config.server_port}"

    async def _update_request_log_progress(
        self,
        request_log_state: Optional[Dict[str, Any]],
        *,
        token_id: Optional[int] = None,
        status_text: str,
        progress: int,
        response_extra: Optional[Dict[str, Any]] = None,
    ):
        """Update request log progress."""
        if not isinstance(request_log_state, dict):
            return
        log_id = request_log_state.get("id")
        if not log_id:
            return

        safe_progress = max(0, min(100, int(progress)))
        request_log_state["progress"] = safe_progress
        payload = {
            "status": "processing",
            "status_text": status_text,
            "progress": safe_progress,
        }
        if isinstance(response_extra, dict):
            payload.update(response_extra)

        try:
            await self.db.update_request_log(
                log_id,
                token_id=token_id,
                response_body=json.dumps(payload, ensure_ascii=False),
                status_code=102,
                duration=0,
                status_text=status_text,
                progress=safe_progress,
            )
        except Exception as e:
            debug_logger.log_error(f"Failed to update request log progress: {e}")

    async def _log_request(
        self,
        token_id: Optional[int],
        operation: str,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        status_code: int,
        duration: float,
        log_id: Optional[int] = None,
        status_text: Optional[str] = None,
        progress: Optional[int] = None,
    ):
        """Log request, updating existing log if log_id is provided."""
        try:
            effective_status_text = status_text or (
                "completed" if status_code == 200 else "failed" if status_code >= 400 else "processing"
            )
            effective_progress = progress
            if effective_progress is None:
                effective_progress = 100 if status_code == 200 else 0 if status_code >= 400 else 0
            effective_progress = max(0, min(100, int(effective_progress)))

            request_body = json.dumps(request_data, ensure_ascii=False)
            response_body = json.dumps(response_data, ensure_ascii=False)

            if log_id:
                await self.db.update_request_log(
                    log_id,
                    token_id=token_id,
                    operation=operation,
                    request_body=request_body,
                    response_body=response_body,
                    status_code=status_code,
                    duration=duration,
                    status_text=effective_status_text,
                    progress=effective_progress,
                )
                return log_id

            log = RequestLog(
                token_id=token_id,
                operation=operation,
                request_body=request_body,
                response_body=response_body,
                status_code=status_code,
                duration=duration,
                status_text=effective_status_text,
                progress=effective_progress,
            )
            return await self.db.add_request_log(log)
        except Exception as e:
            debug_logger.log_error(f"Failed to log request: {e}")
            return None
