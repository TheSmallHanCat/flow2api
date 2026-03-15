import asyncio
import json

from src.api import routes
from src.core.auth import AuthManager, verify_api_key_flexible


def build_openai_completion(content: str) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "flow2api",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )


def test_openai_route_resolves_alias_and_returns_non_stream_result(client, fake_handler):
    fake_handler.non_stream_chunks = [build_openai_completion("![Generated Image](https://example.com/out.png)")]

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.0-pro-image",
            "messages": [{"role": "user", "content": "draw a sunset"}],
            "generationConfig": {
                "imageConfig": {
                    "aspectRatio": "16:9",
                    "imageSize": "2K",
                }
            },
        },
    )

    assert response.status_code == 200
    assert fake_handler.calls[0]["model"] == "gemini-3.0-pro-image-landscape-2k"
    assert response.json()["choices"][0]["message"]["content"].startswith("![Generated Image]")


def test_openai_route_returns_handler_error_status(client, fake_handler):
    fake_handler.non_stream_chunks = [
        json.dumps(
            {
                "error": {
                    "message": "没有可用的Token进行图片生成",
                    "status_code": 503,
                }
            }
        )
    ]

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.0-pro-image",
            "messages": [{"role": "user", "content": "draw a tree"}],
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "没有可用的Token进行图片生成"


def test_flexible_auth_accepts_x_goog_api_key(monkeypatch):
    monkeypatch.setattr(AuthManager, "verify_api_key", staticmethod(lambda api_key: api_key == "secret"))

    assert asyncio.run(
        verify_api_key_flexible(
            credentials=None,
            x_goog_api_key="secret",
            key=None,
        )
    ) == "secret"
