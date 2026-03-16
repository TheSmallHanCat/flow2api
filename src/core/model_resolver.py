"""Model name resolver - converts simplified model names + generationConfig params to internal MODEL_CONFIG keys.

When upstream services (e.g. New API) send requests with a generic model name
along with generationConfig containing aspectRatio / imageSize, this module
resolves them to the specific internal model name used by flow2api.

Example:
    model = "gemini-3.0-pro-image"
    generationConfig.imageConfig.aspectRatio = "16:9"
    generationConfig.imageConfig.imageSize = "2k"
    → resolved to "gemini-3.0-pro-image-landscape-2k"
"""

from typing import Optional, Dict, Any, Tuple
from ..core.logger import debug_logger

# ──────────────────────────────────────────────
# Simplified model name -> base model name prefix mapping
# ──────────────────────────────────────────────
IMAGE_BASE_MODELS = {
    # Gemini 2.5 Flash (GEM_PIX)
    "gemini-2.5-flash-image": "gemini-2.5-flash-image",
    # Gemini 3.0 Pro (GEM_PIX_2)
    "gemini-3.0-pro-image": "gemini-3.0-pro-image",
    # Gemini 3.1 Flash (NARWHAL)
    "gemini-3.1-flash-image": "gemini-3.1-flash-image",
    # imagen 4.0 (IMAGEN_3_5)
    "imagen-4.0-generate-preview": "imagen-4.0-generate-preview",
}

# ──────────────────────────────────────────────
# aspectRatio conversion mapping
# Supports Gemini native format ("16:9") and internal format ("landscape")
# ──────────────────────────────────────────────
ASPECT_RATIO_MAP = {
    # Gemini standard ratio format
    "16:9": "landscape",
    "9:16": "portrait",
    "1:1": "square",
    "4:3": "four-three",
    "3:4": "three-four",
    # Direct English name mapping
    "landscape": "landscape",
    "portrait": "portrait",
    "square": "square",
    "four-three": "four-three",
    "three-four": "three-four",
    "four_three": "four-three",
    "three_four": "three-four",
    # Uppercase form
    "LANDSCAPE": "landscape",
    "PORTRAIT": "portrait",
    "SQUARE": "square",
}

# Supported aspectRatio list for each base model
# If requested ratio is not in supported list, fall back to default
MODEL_SUPPORTED_ASPECTS = {
    "gemini-2.5-flash-image": ["landscape", "portrait"],
    "gemini-3.0-pro-image": [
        "landscape",
        "portrait",
        "square",
        "four-three",
        "three-four",
    ],
    "gemini-3.1-flash-image": [
        "landscape",
        "portrait",
        "square",
        "four-three",
        "three-four",
    ],
    "imagen-4.0-generate-preview": ["landscape", "portrait"],
}

# Supported imageSize (resolution) list for each base model
MODEL_SUPPORTED_SIZES = {
    "gemini-2.5-flash-image": [],  # does not support upscaling
    "gemini-3.0-pro-image": ["2k", "4k"],
    "gemini-3.1-flash-image": ["2k", "4k"],
    "imagen-4.0-generate-preview": [],  # does not support upscaling
}

# imageSize normalization mapping
IMAGE_SIZE_MAP = {
    "1k": "1k",
    "1K": "1k",
    "2k": "2k",
    "2K": "2k",
    "4k": "4k",
    "4K": "4k",
    "": "",
}

# Default aspectRatio
DEFAULT_ASPECT = "landscape"


# ──────────────────────────────────────────────
# Video model simplified name mapping
# ──────────────────────────────────────────────
VIDEO_BASE_MODELS = {
    # T2V models
    "veo_3_1_t2v_fast": {
        "landscape": "veo_3_1_t2v_fast_landscape",
        "portrait": "veo_3_1_t2v_fast_portrait",
    },
    "veo_2_1_fast_d_15_t2v": {
        "landscape": "veo_2_1_fast_d_15_t2v_landscape",
        "portrait": "veo_2_1_fast_d_15_t2v_portrait",
    },
    "veo_2_0_t2v": {
        "landscape": "veo_2_0_t2v_landscape",
        "portrait": "veo_2_0_t2v_portrait",
    },
    "veo_3_1_t2v_fast_ultra": {
        "landscape": "veo_3_1_t2v_fast_ultra",
        "portrait": "veo_3_1_t2v_fast_portrait_ultra",
    },
    "veo_3_1_t2v_fast_ultra_relaxed": {
        "landscape": "veo_3_1_t2v_fast_ultra_relaxed",
        "portrait": "veo_3_1_t2v_fast_portrait_ultra_relaxed",
    },
    "veo_3_1_t2v": {
        "landscape": "veo_3_1_t2v_landscape",
        "portrait": "veo_3_1_t2v_portrait",
    },
    # I2V models
    "veo_3_1_i2v_s_fast_fl": {
        "landscape": "veo_3_1_i2v_s_fast_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_fl",
    },
    "veo_2_1_fast_d_15_i2v": {
        "landscape": "veo_2_1_fast_d_15_i2v_landscape",
        "portrait": "veo_2_1_fast_d_15_i2v_portrait",
    },
    "veo_2_0_i2v": {
        "landscape": "veo_2_0_i2v_landscape",
        "portrait": "veo_2_0_i2v_portrait",
    },
    "veo_3_1_i2v_s_fast_ultra_fl": {
        "landscape": "veo_3_1_i2v_s_fast_ultra_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
    },
    "veo_3_1_i2v_s_fast_ultra_relaxed": {
        "landscape": "veo_3_1_i2v_s_fast_ultra_relaxed",
        "portrait": "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
    },
    "veo_3_1_i2v_s": {
        "landscape": "veo_3_1_i2v_s_landscape",
        "portrait": "veo_3_1_i2v_s_portrait",
    },
    # R2V models
    "veo_3_1_r2v_fast": {
        "landscape": "veo_3_1_r2v_fast",
        "portrait": "veo_3_1_r2v_fast_portrait",
    },
    "veo_3_1_r2v_fast_ultra": {
        "landscape": "veo_3_1_r2v_fast_ultra",
        "portrait": "veo_3_1_r2v_fast_portrait_ultra",
    },
    "veo_3_1_r2v_fast_ultra_relaxed": {
        "landscape": "veo_3_1_r2v_fast_ultra_relaxed",
        "portrait": "veo_3_1_r2v_fast_portrait_ultra_relaxed",
    },
}


def _extract_generation_params(request) -> Tuple[Optional[str], Optional[str]]:
    """Extract aspectRatio and imageSize parameters from request.

    Priority:
    1. request.generationConfig.imageConfig (top-level Gemini parameter)
    2. extra fields in generationConfig (extra_body passthrough)

    Returns:
        (aspect_ratio, image_size) Normalized values
    """
    aspect_ratio = None
    image_size = None

    # Try to extract from generationConfig
    gen_config = getattr(request, "generationConfig", None)

    # If not at top level, try from extra fields (Pydantic extra="allow")
    if gen_config is None and hasattr(request, "__pydantic_extra__"):
        extra = request.__pydantic_extra__ or {}
        gen_config_raw = extra.get("generationConfig")
        if not isinstance(gen_config_raw, dict):
            extra_body = extra.get("extra_body") or extra.get("extraBody")
            if isinstance(extra_body, dict):
                gen_config_raw = extra_body.get("generationConfig")
        if isinstance(gen_config_raw, dict):
            image_config_raw = gen_config_raw.get("imageConfig", {})
            if isinstance(image_config_raw, dict):
                aspect_ratio = image_config_raw.get("aspectRatio")
                image_size = image_config_raw.get("imageSize")
            return (
                ASPECT_RATIO_MAP.get(aspect_ratio, aspect_ratio)
                if aspect_ratio
                else None,
                IMAGE_SIZE_MAP.get(image_size, image_size) if image_size else None,
            )

    if gen_config is not None:
        image_config = getattr(gen_config, "imageConfig", None)
        if image_config is not None:
            aspect_ratio = getattr(image_config, "aspectRatio", None)
            image_size = getattr(image_config, "imageSize", None)

    # Normalize
    if aspect_ratio:
        aspect_ratio = ASPECT_RATIO_MAP.get(aspect_ratio, aspect_ratio)
    if image_size:
        image_size = IMAGE_SIZE_MAP.get(image_size, image_size)

    return aspect_ratio, image_size


def resolve_model_name(
    model: str, request=None, model_config: Dict[str, Any] = None
) -> str:
    """Resolve simplified model name + generationConfig params to internal MODEL_CONFIG key.

    If model is already a valid MODEL_CONFIG key, return as-is.
    If model is a simplified name (base model name), resolve based on
    aspectRatio / imageSize from generationConfig into the full internal model name.

    Args:
        model: Model name in request
        request: ChatCompletionRequest instance (for extracting generationConfig)
        model_config: MODEL_CONFIG dict (for validating resolved model name)

    Returns:
        Resolved internal model name
    """
    # ────── Image model resolution ──────
    if model in IMAGE_BASE_MODELS:
        base = IMAGE_BASE_MODELS[model]
        aspect_ratio, image_size = (
            _extract_generation_params(request) if request else (None, None)
        )

        # Default aspect ratio
        if not aspect_ratio:
            aspect_ratio = DEFAULT_ASPECT

        # Check supported aspect ratio
        supported_aspects = MODEL_SUPPORTED_ASPECTS.get(base, [])
        if aspect_ratio not in supported_aspects and supported_aspects:
            debug_logger.log_warning(
                f"[MODEL_RESOLVER] Model {base} does not support aspectRatio={aspect_ratio},"
                f"falling back to {DEFAULT_ASPECT}"
            )
            aspect_ratio = DEFAULT_ASPECT

        # Build model name
        resolved = f"{base}-{aspect_ratio}"

        # Check supported imageSize
        if image_size and image_size != "1k":
            supported_sizes = MODEL_SUPPORTED_SIZES.get(base, [])
            if image_size in supported_sizes:
                resolved = f"{resolved}-{image_size}"
            else:
                debug_logger.log_warning(
                    f"[MODEL_RESOLVER] Model {base} does not support imageSize={image_size}, ignoring"
                )

        # Final validation
        if model_config and resolved not in model_config:
            debug_logger.log_warning(
                f"[MODEL_RESOLVER] Resolved model name {resolved} not in MODEL_CONFIG,"
                f"falling back to original model name {model}"
            )
            return model

        debug_logger.log_info(
            f"[MODEL_RESOLVER] Model name resolved: {model} → {resolved} "
            f"(aspectRatio={aspect_ratio}, imageSize={image_size or 'default'})"
        )
        return resolved

    # ────── Video model resolution ──────
    if model in VIDEO_BASE_MODELS:
        aspect_ratio, _ = (
            _extract_generation_params(request) if request else (None, None)
        )

        # Video defaults to landscape
        if not aspect_ratio or aspect_ratio not in ("landscape", "portrait"):
            aspect_ratio = "landscape"

        orientation_map = VIDEO_BASE_MODELS[model]
        resolved = orientation_map.get(aspect_ratio)

        if resolved and model_config and resolved in model_config:
            debug_logger.log_info(
                f"[MODEL_RESOLVER] videoModel name resolved: {model} → {resolved} "
                f"(aspectRatio={aspect_ratio})"
            )
            return resolved

        debug_logger.log_warning(
            f"[MODEL_RESOLVER] Video model {model} resolution failed (aspect={aspect_ratio}),"
            f"using original model name"
        )
        return model

    # If already a valid MODEL_CONFIG key, return as-is
    if model_config and model in model_config:
        return model

    # Unknown model name, return as-is (downstream MODEL_CONFIG validation will report error)
    return model


def get_base_model_aliases() -> Dict[str, str]:
    """Return all simplified model names (aliases) and descriptions for /v1/models endpoint."""
    aliases = {}

    for alias, base in IMAGE_BASE_MODELS.items():
        aspects = MODEL_SUPPORTED_ASPECTS.get(base, [])
        sizes = MODEL_SUPPORTED_SIZES.get(base, [])
        desc_parts = [f"aspects: {', '.join(aspects)}"]
        if sizes:
            desc_parts.append(f"sizes: {', '.join(sizes)}")
        aliases[alias] = f"image generation (alias) - {'; '.join(desc_parts)}"

    for alias in VIDEO_BASE_MODELS:
        aliases[alias] = (
            "video generation (alias) - supports landscape/portrait via generationConfig"
        )

    return aliases
