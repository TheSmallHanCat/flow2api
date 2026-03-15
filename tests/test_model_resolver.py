import pytest

from src.core.model_resolver import resolve_model_name
from src.core.models import ChatCompletionRequest, ChatMessage
from src.services.generation_handler import MODEL_CONFIG


def build_request(model: str, **kwargs) -> ChatCompletionRequest:
    payload = {
        "model": model,
        "messages": [ChatMessage(role="user", content="draw a cat")],
    }
    payload.update(kwargs)
    return ChatCompletionRequest(**payload)


def test_image_alias_resolves_with_official_generation_config():
    request = build_request(
        "gemini-3.0-pro-image",
        generationConfig={
            "imageConfig": {
                "aspectRatio": "16:9",
                "imageSize": "2K",
            }
        },
    )

    assert (
        resolve_model_name(request.model, request, MODEL_CONFIG)
        == "gemini-3.0-pro-image-landscape-2k"
    )


def test_image_alias_treats_1k_as_default_size():
    request = build_request(
        "gemini-3.1-flash-image",
        generationConfig={
            "imageConfig": {
                "aspectRatio": "1:1",
                "imageSize": "1K",
            }
        },
    )

    assert (
        resolve_model_name(request.model, request, MODEL_CONFIG)
        == "gemini-3.1-flash-image-square"
    )


def test_generation_config_can_come_from_extra_body():
    request = build_request(
        "gemini-3.1-flash-image",
        extra_body={
            "generationConfig": {
                "imageConfig": {
                    "aspectRatio": "4:3",
                    "imageSize": "4K",
                }
            }
        },
    )

    assert (
        resolve_model_name(request.model, request, MODEL_CONFIG)
        == "gemini-3.1-flash-image-four-three-4k"
    )


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("veo_3_1_t2v_fast_ultra", "veo_3_1_t2v_fast_portrait_ultra"),
        (
            "veo_3_1_t2v_fast_ultra_relaxed",
            "veo_3_1_t2v_fast_portrait_ultra_relaxed",
        ),
        ("veo_3_1_i2v_s_fast_fl", "veo_3_1_i2v_s_fast_portrait_fl"),
        ("veo_3_1_i2v_s_fast_ultra_fl", "veo_3_1_i2v_s_fast_portrait_ultra_fl"),
        (
            "veo_3_1_i2v_s_fast_ultra_relaxed",
            "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
        ),
        ("veo_3_1_r2v_fast", "veo_3_1_r2v_fast_portrait"),
        ("veo_3_1_r2v_fast_ultra", "veo_3_1_r2v_fast_portrait_ultra"),
        (
            "veo_3_1_r2v_fast_ultra_relaxed",
            "veo_3_1_r2v_fast_portrait_ultra_relaxed",
        ),
    ],
)
def test_conflicting_video_aliases_resolve_to_portrait(alias, expected):
    request = build_request(
        alias,
        generationConfig={"imageConfig": {"aspectRatio": "9:16"}},
    )

    assert resolve_model_name(request.model, request, MODEL_CONFIG) == expected
