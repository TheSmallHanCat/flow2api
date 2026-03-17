import base64
import json

from src.api import routes


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


def test_generate_content_returns_gemini_response(client, fake_handler, monkeypatch):
    fake_handler.non_stream_chunks = [
        build_openai_completion("![Generated Image](https://example.com/generated.png)")
    ]

    async def fake_retrieve_image_data(url: str):
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(routes, "retrieve_image_data", fake_retrieve_image_data)

    response = client.post(
        "/v1beta/models/gemini-3.0-pro-image:generateContent",
        json={
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "draw a mountain"}],
                }
            ],
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
    body = response.json()
    assert body["modelVersion"] == "gemini-3.0-pro-image"
    part = body["candidates"][0]["content"]["parts"][0]["inlineData"]
    assert part["mimeType"] == "image/png"
    assert base64.b64decode(part["data"]).startswith(b"\x89PNG")


def test_stream_generate_content_returns_sse_chunks(client, fake_handler, monkeypatch):
    fake_handler.stream_chunks = [
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"flow2api","choices":[{"index":0,"delta":{"reasoning_content":"starting generation"},"finish_reason":null}]}\n\n',
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"flow2api","choices":[{"index":0,"delta":{"content":"![Generated Image](https://example.com/final.png)"},"finish_reason":"stop"}]}\n\n',
    ]

    async def fake_retrieve_image_data(url: str):
        return b"\x89PNG\r\n\x1a\nstream"

    monkeypatch.setattr(routes, "retrieve_image_data", fake_retrieve_image_data)

    response = client.post(
        "/v1beta/models/gemini-3.0-pro-image:streamGenerateContent?alt=sse",
        json={
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "draw a city"}],
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    data_lines = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert len(data_lines) == 2

    first_chunk = json.loads(data_lines[0])
    assert first_chunk["modelVersion"] == "gemini-3.0-pro-image"
    assert first_chunk["candidates"][0]["content"]["parts"][0]["text"] == "starting generation"

    second_chunk = json.loads(data_lines[1])
    assert second_chunk["modelVersion"] == "gemini-3.0-pro-image"
    image_part = second_chunk["candidates"][0]["content"]["parts"][0]["inlineData"]
    assert image_part["mimeType"] == "image/png"
    assert second_chunk["candidates"][0]["finishReason"] == "STOP"


def test_models_generate_content_supports_system_instruction_and_file_data(client, fake_handler):
    fake_handler.non_stream_chunks = [
        build_openai_completion("![Generated Image](https://example.com/generated-square.png)")
    ]

    reference_image = base64.b64encode(b"\x89PNG\r\n\x1a\nref").decode()

    response = client.post(
        "/models/gemini-3.1-flash-image:generateContent",
        json={
            "systemInstruction": {
                "parts": [{"text": "answer in English"}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "draw a cat"},
                        {
                            "fileData": {
                                "fileUri": f"data:image/png;base64,{reference_image}",
                                "mimeType": "image/png",
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "imageConfig": {
                    "aspectRatio": "1:1",
                    "imageSize": "1K",
                }
            },
        },
    )

    assert response.status_code == 200
    assert fake_handler.calls[0]["model"] == "gemini-3.1-flash-image-square"
    assert response.json()["modelVersion"] == "gemini-3.1-flash-image"
    assert fake_handler.calls[0]["prompt"] == "answer in English\n\ndraw a cat"
    assert len(fake_handler.calls[0]["images"]) == 1


def test_models_root_lists_gemini_models_for_newapi_compatibility(client):
    response = client.get("/models")

    assert response.status_code == 200
    body = response.json()
    names = {item["name"] for item in body["models"]}

    assert "models/gemini-3.0-pro-image" in names
    assert "models/gemini-3.1-flash-image" in names
    assert any(
        item["name"] == "models/gemini-3.1-flash-image"
        and item["supportedGenerationMethods"] == ["generateContent", "streamGenerateContent"]
        for item in body["models"]
    )


def test_v1beta_models_root_lists_gemini_models(client):
    response = client.get("/v1beta/models")

    assert response.status_code == 200
    body = response.json()

    assert any(item["name"] == "models/gemini-3.0-pro-image" for item in body["models"])
