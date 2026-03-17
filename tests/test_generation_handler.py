import asyncio
from types import SimpleNamespace

from src.services.generation_handler import GenerationHandler


class FakeFlowClient:
    async def upload_image(self, at, image_bytes, aspect_ratio, project_id=None):
        return "media-uploaded"

    async def generate_image(
        self,
        at,
        project_id,
        prompt,
        model_name,
        aspect_ratio,
        image_inputs=None,
        token_id=None,
        token_image_concurrency=None,
        progress_callback=None,
    ):
        if progress_callback is not None:
            await progress_callback("solving_image_captcha", 38)
            await progress_callback("submitting_image", 48)
        return (
            {
                "media": [
                    {
                        "name": "media-generated",
                        "image": {
                            "generatedImage": {
                                "fifeUrl": "https://example.com/generated.png"
                            }
                        },
                    }
                ]
            },
            "session-1",
            {"generation_attempts": [{"launch_queue_ms": 0, "launch_stagger_ms": 0}]},
        )


class FakeDB:
    def __init__(self):
        self.status_updates = []

    async def update_request_log(self, log_id, **kwargs):
        self.status_updates.append(
            {
                "log_id": log_id,
                "status_text": kwargs.get("status_text"),
                "progress": kwargs.get("progress"),
            }
        )


async def _collect(async_gen):
    items = []
    async for item in async_gen:
        items.append(item)
    return items


def test_image_generation_progress_switches_from_upload_to_captcha():
    db = FakeDB()
    handler = GenerationHandler(
        flow_client=FakeFlowClient(),
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )
    token = SimpleNamespace(
        id=1,
        at="at-token",
        image_concurrency=-1,
        user_paygate_tier="PAYGATE_TIER_NOT_PAID",
    )
    generation_result = handler._create_generation_result()
    request_log_state = {"id": 123}

    asyncio.run(
        _collect(
            handler._handle_image_generation(
                token=token,
                project_id="project-1",
                model_config={
                    "model_name": "NARWHAL",
                    "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
                },
                prompt="draw a cat",
                images=[b"fake-image"],
                stream=False,
                perf_trace={},
                generation_result=generation_result,
                request_log_state=request_log_state,
                pending_token_state={"active": False},
            )
        )
    )

    status_texts = [item["status_text"] for item in db.status_updates]

    assert status_texts[:4] == [
        "uploading_images",
        "solving_image_captcha",
        "submitting_image",
        "image_generated",
    ]
