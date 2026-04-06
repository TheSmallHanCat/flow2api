import unittest
from unittest.mock import AsyncMock

from src.services.flow_client import FlowClient


JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16


class FlowClientUploadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_scoped_upload_uses_new_endpoint_with_project_id(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            return {
                "media": {
                    "name": "new-media-id",
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(request_calls), 1)
        self.assertTrue(request_calls[0]["url"].endswith("/flow/uploadImage"))
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )

    async def test_project_scoped_upload_does_not_fallback_to_legacy_endpoint(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            self.fail("带 project_id 的上传不应回退到 legacy 接口")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        with self.assertRaisesRegex(RuntimeError, "legacy :uploadUserImage fallback is disabled"):
            await client.upload_image(
                at="test-at",
                image_bytes=JPEG_BYTES,
                aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                project_id="project-123",
            )

        self.assertEqual(len(request_calls), 1)
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )

    async def test_upload_without_project_id_keeps_legacy_fallback(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            if kwargs["url"].endswith(":uploadUserImage"):
                return {
                    "mediaGenerationId": {
                        "mediaGenerationId": "legacy-media-id",
                    }
                }
            self.fail(f"Unexpected url: {kwargs['url']}")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=None,
        )

        self.assertEqual(media_id, "legacy-media-id")
        self.assertEqual(len(request_calls), 2)
        self.assertNotIn(
            "projectId",
            request_calls[1]["json_data"]["clientContext"],
        )


if __name__ == "__main__":
    unittest.main()
