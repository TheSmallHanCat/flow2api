import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from src.services.generation_handler import GenerationHandler

IMAGE_MODEL_CONFIG = {
    "model_name": "NARWHAL",
    "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
}

I2V_MODEL_CONFIG = {
    "video_type": "i2v",
    "model_key": "veo_3_1_i2v_s_fast_fl",
    "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
    "min_images": 1,
    "max_images": 2,
}

R2V_MODEL_CONFIG = {
    "video_type": "r2v",
    "model_key": "veo_3_1_r2v_fast_landscape",
    "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
    "min_images": 0,
    "max_images": 3,
}


class FakeFlowClient:
    def __init__(self):
        self.upload_calls = []
        self.upload_delay = 0
        self.generate_image_errors = []
        self.generate_image_call_count = 0
        self.video_calls = []

    async def upload_image(self, at, image_bytes, aspect_ratio, project_id=None):
        if self.upload_delay > 0:
            await asyncio.sleep(self.upload_delay)
        self.upload_calls.append(
            {
                "at": at,
                "image_bytes": image_bytes,
                "aspect_ratio": aspect_ratio,
                "project_id": project_id,
            }
        )
        return f"media-uploaded-{len(self.upload_calls)}"

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
        self.generate_image_call_count += 1
        if progress_callback is not None:
            await progress_callback("solving_image_captcha", 38)
            await progress_callback("submitting_image", 48)

        if self.generate_image_errors:
            error = self.generate_image_errors.pop(0)
            if error is not None:
                raise error

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

    async def generate_video_start_image(self, **kwargs):
        self.video_calls.append(("i2v-single", kwargs))
        return {
            "operations": [
                {
                    "operation": {"name": "task-i2v-single"},
                    "sceneId": "scene-i2v-single",
                    "status": "MEDIA_GENERATION_STATUS_PENDING",
                }
            ]
        }

    async def generate_video_start_end(self, **kwargs):
        self.video_calls.append(("i2v-pair", kwargs))
        return {
            "operations": [
                {
                    "operation": {"name": "task-i2v-pair"},
                    "sceneId": "scene-i2v-pair",
                    "status": "MEDIA_GENERATION_STATUS_PENDING",
                }
            ]
        }

    async def generate_video_reference_images(self, **kwargs):
        self.video_calls.append(("r2v", kwargs))
        return {
            "operations": [
                {
                    "operation": {"name": "task-r2v"},
                    "sceneId": "scene-r2v",
                    "status": "MEDIA_GENERATION_STATUS_PENDING",
                }
            ]
        }

    async def generate_video_text(self, **kwargs):
        self.video_calls.append(("t2v", kwargs))
        return {
            "operations": [
                {
                    "operation": {"name": "task-t2v"},
                    "sceneId": "scene-t2v",
                    "status": "MEDIA_GENERATION_STATUS_PENDING",
                }
            ]
        }


class FakeDB:
    def __init__(self):
        self.status_updates = []
        self.uploaded_image_cache = {}
        self.deleted_keys = []
        self.created_tasks = []

    async def update_request_log(self, log_id, **kwargs):
        self.status_updates.append(
            {
                "log_id": log_id,
                "status_text": kwargs.get("status_text"),
                "progress": kwargs.get("progress"),
            }
        )

    async def get_uploaded_image_cache(self, email, project_id, image_hash, aspect_ratio):
        return self.uploaded_image_cache.get((email, project_id, image_hash, aspect_ratio))

    async def upsert_uploaded_image_cache(self, email, project_id, image_hash, aspect_ratio, media_id):
        self.uploaded_image_cache[(email, project_id, image_hash, aspect_ratio)] = {
            "email": email,
            "project_id": project_id,
            "image_hash": image_hash,
            "aspect_ratio": aspect_ratio,
            "media_id": media_id,
        }

    async def touch_uploaded_image_cache(self, email, project_id, image_hash, aspect_ratio):
        return None

    async def delete_uploaded_image_cache(self, email, project_id, image_hash, aspect_ratio):
        self.deleted_keys.append((email, project_id, image_hash, aspect_ratio))
        self.uploaded_image_cache.pop((email, project_id, image_hash, aspect_ratio), None)

    async def create_task(self, task):
        self.created_tasks.append(task)
        return len(self.created_tasks)

    async def update_task(self, task_id, **kwargs):
        return None


class NoPollGenerationHandler(GenerationHandler):
    async def _poll_video_result(self, *args, **kwargs):
        if False:
            yield None


def _make_token(email="user@example.com", token_id=1):
    return SimpleNamespace(
        id=token_id,
        at="at-token",
        email=email,
        image_concurrency=-1,
        video_concurrency=-1,
        user_paygate_tier="PAYGATE_TIER_NOT_PAID",
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
    token = _make_token()
    generation_result = handler._create_generation_result()
    request_log_state = {"id": 123}

    asyncio.run(
        _collect(
            handler._handle_image_generation(
                token=token,
                project_id="project-1",
                model_config=IMAGE_MODEL_CONFIG,
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


def test_image_generation_reuses_cached_media_ids_across_requests():
    db = FakeDB()
    flow_client = FakeFlowClient()
    handler = GenerationHandler(
        flow_client=flow_client,
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )
    token = _make_token()

    for _ in range(2):
        asyncio.run(
            _collect(
                handler._handle_image_generation(
                    token=token,
                    project_id="project-1",
                    model_config=IMAGE_MODEL_CONFIG,
                    prompt="draw a cat",
                    images=[b"fake-image"],
                    stream=False,
                    perf_trace={},
                    generation_result=handler._create_generation_result(),
                    request_log_state={"id": 123},
                    pending_token_state={"active": False},
                )
            )
        )

    assert len(flow_client.upload_calls) == 1


def test_uploaded_media_cache_is_scoped_by_project():
    db = FakeDB()
    flow_client = FakeFlowClient()
    handler = GenerationHandler(
        flow_client=flow_client,
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )
    token = _make_token()

    asyncio.run(
        handler._resolve_uploaded_media_records(
            token=token,
            project_id="project-a",
            images=[b"same-image"],
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )
    asyncio.run(
        handler._resolve_uploaded_media_records(
            token=token,
            project_id="project-b",
            images=[b"same-image"],
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )

    assert len(flow_client.upload_calls) == 2


def test_uploaded_media_cache_is_scoped_by_email():
    db = FakeDB()
    flow_client = FakeFlowClient()
    handler = GenerationHandler(
        flow_client=flow_client,
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )

    asyncio.run(
        handler._resolve_uploaded_media_records(
            token=_make_token(email="first@example.com", token_id=1),
            project_id="project-1",
            images=[b"same-image"],
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )
    asyncio.run(
        handler._resolve_uploaded_media_records(
            token=_make_token(email="second@example.com", token_id=2),
            project_id="project-1",
            images=[b"same-image"],
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )

    assert len(flow_client.upload_calls) == 2


def test_uploaded_media_cache_dedupes_concurrent_identical_uploads():
    db = FakeDB()
    flow_client = FakeFlowClient()
    flow_client.upload_delay = 0.05
    handler = GenerationHandler(
        flow_client=flow_client,
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )
    token = _make_token()

    async def _run():
        return await asyncio.gather(
            handler._resolve_uploaded_media_records(
                token=token,
                project_id="project-1",
                images=[b"same-image"],
                aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
            ),
            handler._resolve_uploaded_media_records(
                token=token,
                project_id="project-1",
                images=[b"same-image"],
                aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
            ),
        )

    results = asyncio.run(_run())

    assert len(flow_client.upload_calls) == 1
    assert results[0][0]["media_id"] == results[1][0]["media_id"]


def test_image_generation_evicts_stale_cached_media_and_reuploads_once():
    db = FakeDB()
    flow_client = FakeFlowClient()
    flow_client.generate_image_errors = [
        Exception("INVALID_ARGUMENT: referenceImages mediaId not found"),
        None,
    ]
    handler = GenerationHandler(
        flow_client=flow_client,
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )
    token = _make_token()
    cache_key = handler._build_uploaded_image_cache_key(
        email=token.email,
        project_id="project-1",
        image_hash=hashlib.sha256(b"fake-image").hexdigest(),
        aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
    )
    asyncio.run(db.upsert_uploaded_image_cache(media_id="stale-media", **cache_key))

    asyncio.run(
        _collect(
            handler._handle_image_generation(
                token=token,
                project_id="project-1",
                model_config=IMAGE_MODEL_CONFIG,
                prompt="draw a cat",
                images=[b"fake-image"],
                stream=False,
                perf_trace={},
                generation_result=handler._create_generation_result(),
                request_log_state={"id": 123},
                pending_token_state={"active": False},
            )
        )
    )

    assert flow_client.generate_image_call_count == 2
    assert len(flow_client.upload_calls) == 1
    assert db.deleted_keys == [
        (
            token.email,
            "project-1",
            hashlib.sha256(b"fake-image").hexdigest(),
            "IMAGE_ASPECT_RATIO_SQUARE",
        )
    ]


def test_image_generation_does_not_evict_cache_for_non_asset_errors():
    db = FakeDB()
    flow_client = FakeFlowClient()
    flow_client.generate_image_errors = [Exception("PUBLIC_ERROR: internal error")]
    handler = GenerationHandler(
        flow_client=flow_client,
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )
    token = _make_token()
    cache_key = handler._build_uploaded_image_cache_key(
        email=token.email,
        project_id="project-1",
        image_hash=hashlib.sha256(b"fake-image").hexdigest(),
        aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
    )
    asyncio.run(db.upsert_uploaded_image_cache(media_id="cached-media", **cache_key))

    with pytest.raises(Exception, match="PUBLIC_ERROR: internal error"):
        asyncio.run(
            _collect(
                handler._handle_image_generation(
                    token=token,
                    project_id="project-1",
                    model_config=IMAGE_MODEL_CONFIG,
                    prompt="draw a cat",
                    images=[b"fake-image"],
                    stream=False,
                    perf_trace={},
                    generation_result=handler._create_generation_result(),
                    request_log_state={"id": 123},
                    pending_token_state={"active": False},
                )
            )
        )

    assert len(flow_client.upload_calls) == 0
    assert db.deleted_keys == []


def test_i2v_reuses_cached_uploads_for_start_and_end_images():
    db = FakeDB()
    flow_client = FakeFlowClient()
    handler = NoPollGenerationHandler(
        flow_client=flow_client,
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )
    token = _make_token()

    for _ in range(2):
        asyncio.run(
            _collect(
                handler._handle_video_generation(
                    token=token,
                    project_id="project-1",
                    model_config=I2V_MODEL_CONFIG,
                    prompt="animate",
                    images=[b"start", b"end"],
                    stream=False,
                    perf_trace={},
                    generation_result=handler._create_generation_result(),
                    request_log_state={"id": 123},
                    pending_token_state={"active": False},
                )
            )
        )

    assert len(flow_client.upload_calls) == 2
    assert [call[0] for call in flow_client.video_calls] == ["i2v-pair", "i2v-pair"]


def test_r2v_reuses_cached_uploads_for_reference_images():
    db = FakeDB()
    flow_client = FakeFlowClient()
    handler = NoPollGenerationHandler(
        flow_client=flow_client,
        token_manager=None,
        load_balancer=None,
        db=db,
        concurrency_manager=None,
        proxy_manager=None,
    )
    token = _make_token()
    images = [b"one", b"two", b"three"]

    for _ in range(2):
        asyncio.run(
            _collect(
                handler._handle_video_generation(
                    token=token,
                    project_id="project-1",
                    model_config=R2V_MODEL_CONFIG,
                    prompt="animate",
                    images=images,
                    stream=False,
                    perf_trace={},
                    generation_result=handler._create_generation_result(),
                    request_log_state={"id": 123},
                    pending_token_state={"active": False},
                )
            )
        )

    assert len(flow_client.upload_calls) == 3
    assert [call[0] for call in flow_client.video_calls] == ["r2v", "r2v"]
