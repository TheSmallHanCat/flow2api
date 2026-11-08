import types
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.api.routes import _get_openai_model_catalog, _normalize_openai_request
from src.core.models import ChatCompletionRequest, ChatMessage
from src.core.config import config
from src.core.model_resolver import resolve_model_name
from src.services.flow_client import FlowClient
from src.services.generation_handler import MODEL_CONFIG, GenerationHandler


def _make_image_bytes(size: tuple[int, int], color: str = "white") -> bytes:
    image = Image.new("RGB", size, color=color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class VeoLiteModelResolverTests(unittest.TestCase):
    def test_public_catalog_exposes_current_flow_families(self):
        listed = {item["id"] for item in _get_openai_model_catalog()}

        self.assertIn("veo-3.1-lite-4s-landscape", listed)
        self.assertIn("veo-3.1-fast-4s-landscape", listed)
        self.assertIn("veo-3.1-quality-4s-landscape", listed)
        self.assertIn("omni-1.1-flash-4s-landscape", listed)
        self.assertIn("veo-3.1-fast", listed)
        self.assertIn("omni-1.1-flash", listed)
        self.assertNotIn("veo_3_1_t2v_fast_landscape_4s", listed)
        self.assertIn("veo_3_1_t2v_fast_landscape_4s", MODEL_CONFIG)

    def test_current_aliases_use_live_upstream_keys(self):
        self.assertEqual(
            MODEL_CONFIG["veo-3.1-fast-4s-landscape"]["model_key"],
            "veo_3_1_t2v_fast_4s_relaxed",
        )
        self.assertEqual(
            MODEL_CONFIG["omni-1.1-flash-4s-landscape"]["model_key"],
            "abra_t2v_4s",
        )
        self.assertEqual(
            MODEL_CONFIG["veo-3.1-fast-r2v-8s-landscape"]["model_key"],
            "veo_3_1_r2v_fast_landscape",
        )
        self.assertEqual(
            MODEL_CONFIG["veo-3.1-fast-r2v-8s-portrait"]["model_key"],
            "veo_3_1_r2v_fast_portrait",
        )

    def test_omni_1_2_flash_registered_with_live_upstream_keys(self):
        # 2026-11-08 实测：Omni 1.2 Flash 上游 family key 为 macaron_cookie，
        # 纯文本 YhhmEf 用 macaron_cookie_t2v_8s，参考图 MZZa6b 用 macaron_cookie_r2v_*。
        listed = {item["id"] for item in _get_openai_model_catalog()}

        self.assertIn("omni-1.2-flash", listed)
        for seconds in (3, 4, 5, 6, 7, 8, 9, 10):
            self.assertIn(f"omni-1.2-flash-{seconds}s-landscape", listed)
            self.assertIn(f"omni-1.2-flash-{seconds}s-portrait", listed)
        for seconds in (11, 12, 13, 14, 15):
            self.assertIn(f"omni-1.2-flash-{seconds}s-portrait", listed)
            self.assertNotIn(f"omni-1.2-flash-{seconds}s-landscape", listed)

        landscape_8s = MODEL_CONFIG["omni-1.2-flash-8s-landscape"]
        self.assertEqual(landscape_8s["model_key"], "macaron_cookie_t2v_8s")
        self.assertEqual(landscape_8s["reference_model_key"], "macaron_cookie_r2v_8s")
        self.assertEqual(landscape_8s["reference_duration"], 8)
        self.assertEqual(landscape_8s["aspect_ratio"], "VIDEO_ASPECT_RATIO_LANDSCAPE")

        portrait_15s = MODEL_CONFIG["omni-1.2-flash-15s-portrait"]
        self.assertEqual(portrait_15s["model_key"], "macaron_cookie_t2v_15s")
        self.assertEqual(portrait_15s["aspect_ratio"], "VIDEO_ASPECT_RATIO_PORTRAIT")

        alias = MODEL_CONFIG["omni-1.2-flash"]
        self.assertEqual(alias["model_key"], "macaron_cookie_t2v_8s")
        self.assertEqual(alias["aspect_ratio"], "VIDEO_ASPECT_RATIO_LANDSCAPE")

        legacy = MODEL_CONFIG["omni"]
        self.assertEqual(legacy["model_key"], "macaron_cookie_t2v_8s")
        legacy_portrait = MODEL_CONFIG["omni_portrait"]
        self.assertEqual(legacy_portrait["aspect_ratio"], "VIDEO_ASPECT_RATIO_PORTRAIT")

    def test_resolve_t2v_lite_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_lite",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_lite_portrait")

    def test_resolve_quality_4s_upsample_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_4s_4k",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_portrait_4s_4k")

    def test_resolve_video_image_size_to_upsample_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(
                aspectRatio="landscape", imageSize="1080p"
            )
        )

        resolved = resolve_model_name(
            "veo_3_1_i2v_s_6s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_i2v_s_6s_1080p")

    def test_resolve_quality_8s_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_8s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_portrait_8s")

    def test_resolve_quality_8s_upsample_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(
                aspectRatio="portrait", imageSize="4k"
            )
        )

        resolved = resolve_model_name(
            "veo_3_1_i2v_s_8s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_i2v_s_portrait_8s_4k")

    def test_image_model_follows_reference_image_aspect_ratio(self):
        request = types.SimpleNamespace(generationConfig=None)
        portrait_image = _make_image_bytes((900, 1600))

        resolved = resolve_model_name(
            "gemini-3.0-pro-image",
            request=request,
            model_config=MODEL_CONFIG,
            images=[portrait_image],
        )

        self.assertEqual(resolved, "gemini-3.0-pro-image-portrait")


class VeoLiteGenerationHandlerTests(unittest.TestCase):
    def test_tier_two_does_not_upgrade_lite_model_to_fake_ultra(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v_lite",
                "allow_tier_upgrade": False,
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v_lite")
        self.assertIsNone(message)

    def test_tier_two_still_upgrades_regular_model(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v_fast",
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v_fast_ultra")
        self.assertIn("ultra", message)

    def test_quality_model_does_not_upgrade_to_fake_ultra(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v",
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v")
        self.assertIsNone(message)

    def test_quality_4s_upsample_model_generates_then_upsamples(self):
        cfg = MODEL_CONFIG["veo_3_1_t2v_4s_4k"]

        self.assertEqual(cfg["model_key"], "veo_3_1_t2v_quality_4s")
        self.assertEqual(cfg["video_type"], "t2v")
        self.assertEqual(cfg["upsample"]["model_key"], "veo_3_1_upsampler_4k")
        self.assertEqual(cfg["upsample"]["resolution"], "VIDEO_RESOLUTION_4K")

    def test_quality_6s_i2v_1080p_model_generates_then_upsamples(self):
        cfg = MODEL_CONFIG["veo_3_1_i2v_s_6s_1080p"]

        self.assertEqual(cfg["model_key"], "veo_3_1_i2v_s_quality_6s_fl")
        self.assertEqual(cfg["video_type"], "i2v")
        self.assertEqual(cfg["upsample"]["model_key"], "veo_3_1_upsampler_1080p")
        self.assertEqual(cfg["upsample"]["resolution"], "VIDEO_RESOLUTION_1080P")

    def test_explicit_8s_aliases_reuse_default_upstream_keys(self):
        self.assertEqual(MODEL_CONFIG["veo_3_1_t2v_8s"]["model_key"], "veo_3_1_t2v")
        self.assertEqual(
            MODEL_CONFIG["veo_3_1_i2v_s_8s"]["model_key"], "veo_3_1_i2v_s_fl"
        )
        self.assertEqual(
            MODEL_CONFIG["veo_3_1_r2v_fast_ultra_8s"]["model_key"],
            "veo_3_1_r2v_fast_landscape_ultra",
        )

    def test_short_duration_models_include_explicit_landscape_aliases(self):
        expected_aliases = {
            "veo_3_1_t2v_landscape_4s": "veo_3_1_t2v_4s",
            "veo_3_1_t2v_landscape_6s": "veo_3_1_t2v_6s",
            "veo_3_1_i2v_s_landscape_4s": "veo_3_1_i2v_s_4s",
            "veo_3_1_i2v_s_landscape_6s": "veo_3_1_i2v_s_6s",
            "veo_3_1_t2v_landscape_4s_4k": "veo_3_1_t2v_4s_4k",
            "veo_3_1_i2v_s_landscape_6s_1080p": "veo_3_1_i2v_s_6s_1080p",
        }

        for alias, target in expected_aliases.items():
            self.assertIn(alias, MODEL_CONFIG)
            self.assertEqual(MODEL_CONFIG[alias], MODEL_CONFIG[target])

    def test_default_duration_models_include_explicit_8s_aliases(self):
        expected_aliases = {
            "veo_3_1_t2v_landscape_8s": "veo_3_1_t2v_8s",
            "veo_3_1_t2v_landscape_8s_4k": "veo_3_1_t2v_8s_4k",
            "veo_3_1_t2v_lite_landscape_8s": "veo_3_1_t2v_lite_8s_landscape",
            "veo_3_1_i2v_s_landscape_8s": "veo_3_1_i2v_s_8s",
            "veo_3_1_i2v_s_landscape_8s_1080p": "veo_3_1_i2v_s_8s_1080p",
            "veo_3_1_i2v_lite_landscape_8s": "veo_3_1_i2v_lite_8s_landscape",
            "veo_3_1_interpolation_lite_landscape_8s": "veo_3_1_interpolation_lite_8s_landscape",
            "veo_3_1_r2v_fast_landscape_8s": "veo_3_1_r2v_fast_8s",
            "veo_3_1_r2v_fast_landscape_ultra_8s": "veo_3_1_r2v_fast_ultra_8s",
            "veo_3_1_r2v_fast_landscape_ultra_relaxed_8s": "veo_3_1_r2v_fast_ultra_relaxed_8s",
        }

        for alias, target in expected_aliases.items():
            self.assertIn(alias, MODEL_CONFIG)
            self.assertEqual(MODEL_CONFIG[alias], MODEL_CONFIG[target])

    def test_r2v_models_include_explicit_landscape_aliases(self):
        expected_aliases = {
            "veo_3_1_r2v_fast_landscape": "veo_3_1_r2v_fast",
            "veo_3_1_r2v_fast_landscape_ultra": "veo_3_1_r2v_fast_ultra",
            "veo_3_1_r2v_fast_landscape_ultra_relaxed": "veo_3_1_r2v_fast_ultra_relaxed",
            "veo_3_1_r2v_fast_landscape_ultra_4k": "veo_3_1_r2v_fast_ultra_4k",
            "veo_3_1_r2v_fast_landscape_ultra_1080p": "veo_3_1_r2v_fast_ultra_1080p",
        }

        for alias, target in expected_aliases.items():
            self.assertIn(alias, MODEL_CONFIG)
            self.assertEqual(MODEL_CONFIG[alias], MODEL_CONFIG[target])

    def test_direct_upsampler_keys_are_not_public_models(self):
        self.assertNotIn("veo_3_1_upsampler_4k", MODEL_CONFIG)
        self.assertNotIn("veo_3_1_upsampler_1080p", MODEL_CONFIG)


class VeoLiteFlowClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FlowClient(proxy_manager=None)
        self.client._acquire_video_launch_gate = AsyncMock(
            return_value=(True, None, None)
        )
        self.client._release_video_launch_gate = AsyncMock()
        self.client._get_recaptcha_token = AsyncMock(
            return_value=("recaptcha-token", "browser-1")
        )
        self.client._notify_browser_captcha_request_finished = AsyncMock()

    async def test_generate_video_text_uses_v2_payload_for_lite(self):
        operation_id = "11111111-1111-1111-1111-111111111111"
        project_id = "22222222-2222-2222-2222-222222222222"
        media_id = "33333333-3333-3333-3333-333333333333"
        self.client._call_flow_frontend_rpc = AsyncMock(
            return_value=[[operation_id, project_id, media_id]]
        )

        await self.client.generate_video_text(
            at="at-token",
            project_id=project_id,
            prompt="猫猫",
            model_key="veo_3_1_t2v_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
            google_cookies="SID=session-cookie",
        )

        rpc = self.client._call_flow_frontend_rpc.await_args.kwargs
        self.assertEqual(rpc["rpc_id"], "YhhmEf")
        request_data = rpc["argument"][0][0]
        self.assertEqual(request_data[0], [None, None, [[["猫猫"]]]])
        self.assertEqual(request_data[1], "veo_3_1_t2v_lite")
        self.assertEqual(request_data[2], 2)
        self.assertTrue(rpc["argument"][2][0])

    async def test_short_fast_relaxed_key_falls_back_to_same_duration_standard_key(self):
        operation_id = "11111111-1111-1111-1111-111111111111"
        project_id = "22222222-2222-2222-2222-222222222222"
        media_id = "33333333-3333-3333-3333-333333333333"
        self.client._call_flow_frontend_rpc = AsyncMock(
            side_effect=[
                RuntimeError("Flow frontend RPC rejected: rpc=YhhmEf, code=[5]"),
                [[operation_id, project_id, media_id]],
            ]
        )

        await self.client.generate_video_text(
            at="at-token",
            project_id=project_id,
            prompt="猫猫",
            model_key="veo_3_1_t2v_fast_6s_relaxed",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
            google_cookies="SID=session-cookie",
        )

        calls = self.client._call_flow_frontend_rpc.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["argument"][0][0][1], "veo_3_1_t2v_fast_6s_relaxed")
        self.assertEqual(calls[1].kwargs["argument"][0][0][1], "veo_3_1_t2v_fast_6s")

    async def test_model_fallback_runs_even_when_general_retry_budget_is_one(self):
        operation_id = "11111111-1111-1111-1111-111111111111"
        project_id = "22222222-2222-2222-2222-222222222222"
        media_id = "33333333-3333-3333-3333-333333333333"
        self.client._call_flow_frontend_rpc = AsyncMock(
            side_effect=[
                RuntimeError("Flow frontend RPC rejected: rpc=YhhmEf, code=[5]"),
                [[operation_id, project_id, media_id]],
            ]
        )
        previous_retries = config._config.setdefault("flow", {}).get("max_retries")
        config._config["flow"]["max_retries"] = 1
        try:
            await self.client.generate_video_text(
                at="at-token",
                project_id=project_id,
                prompt="猫猫",
                model_key="veo_3_1_t2v_fast_6s_relaxed",
                aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
                use_v2_model_config=True,
                google_cookies="SID=session-cookie",
            )
        finally:
            if previous_retries is None:
                config._config["flow"].pop("max_retries", None)
            else:
                config._config["flow"]["max_retries"] = previous_retries
        self.assertEqual(self.client._call_flow_frontend_rpc.await_count, 2)

    async def test_access_denied_does_not_fallback_to_standard_key(self):
        self.client._call_flow_frontend_rpc = AsyncMock(
            side_effect=RuntimeError("PUBLIC_ERROR_MODEL_ACCESS_DENIED")
        )

        with self.assertRaisesRegex(RuntimeError, "ACCESS_DENIED"):
            await self.client.generate_video_text(
                at="at-token",
                project_id="22222222-2222-2222-2222-222222222222",
                prompt="猫猫",
                model_key="veo_3_1_t2v_fast_6s_relaxed",
                aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
                use_v2_model_config=True,
                google_cookies="SID=session-cookie",
            )
        self.client._call_flow_frontend_rpc.assert_awaited_once()

    async def test_quota_reached_is_not_retried_on_same_account(self):
        self.client._call_flow_frontend_rpc = AsyncMock(
            side_effect=RuntimeError(
                "Flow frontend RPC rejected: rpc=eb1hJf, error=PUBLIC_ERROR_USER_QUOTA_REACHED"
            )
        )
        self.client._notify_browser_captcha_error = AsyncMock()

        with patch("src.services.flow_client.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "QUOTA_REACHED"):
                await self.client.generate_video_start_image(
                    at="at-token",
                    project_id="22222222-2222-2222-2222-222222222222",
                    prompt="猫猫",
                    model_key="veo_3_1_i2v_s_fast",
                    aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
                    start_media_id="33333333-3333-3333-3333-333333333333",
                    google_cookies="SID=session-cookie",
                )

        self.client._call_flow_frontend_rpc.assert_awaited_once()

    async def test_unusual_activity_retries_once_then_raises_and_reports(self):
        unusual = RuntimeError(
            "Flow frontend RPC rejected: rpc=eb1hJf, error=PUBLIC_ERROR_UNUSUAL_ACTIVITY"
        )
        self.client._call_flow_frontend_rpc = AsyncMock(side_effect=unusual)
        self.client._notify_browser_captcha_error = AsyncMock()

        with patch("src.services.flow_client.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "UNUSUAL_ACTIVITY"):
                await self.client.generate_video_start_image(
                    at="at-token",
                    project_id="22222222-2222-2222-2222-222222222222",
                    prompt="猫猫",
                    model_key="veo_3_1_i2v_s_fast",
                    aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
                    start_media_id="33333333-3333-3333-3333-333333333333",
                    google_cookies="SID=session-cookie",
                )

        # 同账号只重试一次，剩下交给上层换号
        self.assertEqual(self.client._call_flow_frontend_rpc.await_count, 2)
        # 每次被拒都通知打码服务自愈
        self.assertEqual(self.client._notify_browser_captcha_error.await_count, 2)

    async def test_transient_retry_keeps_original_model_key(self):
        operation_id = "11111111-1111-1111-1111-111111111111"
        project_id = "22222222-2222-2222-2222-222222222222"
        media_id = "33333333-3333-3333-3333-333333333333"
        self.client._call_flow_frontend_rpc = AsyncMock(
            side_effect=[
                RuntimeError("connection timed out"),
                [[operation_id, project_id, media_id]],
            ]
        )
        await self.client.generate_video_text(
            at="at-token",
            project_id=project_id,
            prompt="猫猫",
            model_key="veo_3_1_t2v_fast_6s_relaxed",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
            google_cookies="SID=session-cookie",
        )
        calls = self.client._call_flow_frontend_rpc.await_args_list
        self.assertEqual(calls[0].kwargs["argument"][0][0][1], "veo_3_1_t2v_fast_6s_relaxed")
        self.assertEqual(calls[1].kwargs["argument"][0][0][1], "veo_3_1_t2v_fast_6s_relaxed")

    def test_reference_video_uses_current_prompt_and_media_envelopes(self):
        rpc_id, argument = self.client._build_frontend_video_generation_argument(
            project_id="22222222-2222-2222-2222-222222222222",
            prompt="参考图视频",
            model_key="abra_r2v_10s",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            recaptcha_token="recaptcha-token",
            session_id="session-id",
            mode="references",
            reference_media_ids=["reference-media"],
        )

        self.assertEqual(rpc_id, "MZZa6b")
        request_data = argument[0][0]
        self.assertEqual(request_data[0], [None, None, [[["参考图视频"]]]])
        self.assertEqual(request_data[1], [[None, "reference-media"]])

    async def test_generate_video_text_normalizes_media_only_create_response(self):
        operation_id = "11111111-1111-1111-1111-111111111111"
        project_id = "22222222-2222-2222-2222-222222222222"
        media_id = "33333333-3333-3333-3333-333333333333"
        self.client._call_flow_frontend_rpc = AsyncMock(
            return_value=[[operation_id, project_id, media_id]]
        )

        result = await self.client.generate_video_text(
            at="at-token",
            project_id=project_id,
            prompt="猫猫",
            model_key="veo_3_1_t2v_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
            google_cookies="SID=session-cookie",
        )

        self.assertEqual(result["operations"][0]["operation"]["name"], operation_id)
        self.assertEqual(result["operations"][0]["projectId"], project_id)
        self.assertEqual(
            result["operations"][0]["status"],
            "MEDIA_GENERATION_STATUS_ACTIVE",
        )


class RouteNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FlowClient(proxy_manager=None)
        self.client._acquire_video_launch_gate = AsyncMock(
            return_value=(True, None, None)
        )
        self.client._release_video_launch_gate = AsyncMock()
        self.client._get_recaptcha_token = AsyncMock(
            return_value=("recaptcha-token", "browser-1")
        )
        self.client._notify_browser_captcha_request_finished = AsyncMock()

    async def test_openai_history_reference_image_can_drive_aspect_ratio(self):
        portrait_image = _make_image_bytes((900, 1600))
        request = ChatCompletionRequest(
            model="gemini-3.0-pro-image",
            messages=[
                ChatMessage(role="user", content="先生成一张图"),
                ChatMessage(
                    role="assistant",
                    content="![cat](https://example.com/cat.png)",
                ),
                ChatMessage(role="user", content="基于上一张图继续编辑"),
            ],
        )

        with patch(
            "src.api.routes.retrieve_image_data",
            new=AsyncMock(return_value=portrait_image),
        ):
            normalized = await _normalize_openai_request(request)

        self.assertEqual(normalized.model, "gemini-3.0-pro-image-portrait")
        self.assertEqual(len(normalized.images), 1)

    async def test_check_video_status_uses_media_payload_and_normalizes_response(self):
        operation_id = "11111111-1111-1111-1111-111111111111"
        project_id = "22222222-2222-2222-2222-222222222222"
        media_id = "33333333-3333-3333-3333-333333333333"
        video_url = f"https://flow-content.google/video/{media_id}?token=abc"
        self.client._call_flow_frontend_rpc = AsyncMock(
            return_value=[operation_id, project_id, media_id, video_url]
        )

        result = await self.client.check_video_status(
            at="at-token",
            operations=[
                {
                    "operation": {"name": operation_id},
                    "name": operation_id,
                    "mediaName": media_id,
                    "projectId": project_id,
                }
            ],
            google_cookies="SID=session-cookie",
        )

        rpc = self.client._call_flow_frontend_rpc.await_args.kwargs
        self.assertEqual(rpc["rpc_id"], "jwpduf")
        self.assertEqual(rpc["argument"][2], [[operation_id]])
        operation = result["operations"][0]
        self.assertEqual(operation["operation"]["name"], operation_id)
        self.assertEqual(operation["status"], "MEDIA_GENERATION_STATUS_SUCCESSFUL")
        self.assertEqual(
            operation["operation"]["metadata"]["video"]["fifeUrl"],
            video_url,
        )

    async def test_check_video_status_surfaces_terminal_frontend_error(self):
        operation_id = "11111111-1111-1111-1111-111111111111"
        project_id = "22222222-2222-2222-2222-222222222222"
        media_id = "33333333-3333-3333-3333-333333333333"
        self.client._call_flow_frontend_rpc = AsyncMock(
            return_value=[
                operation_id,
                project_id,
                media_id,
                None,
                [4, [3, "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED"]],
            ]
        )

        result = await self.client.check_video_status(
            at="at-token",
            operations=[
                {
                    "operation": {"name": operation_id},
                    "name": operation_id,
                    "mediaName": media_id,
                    "projectId": project_id,
                }
            ],
            google_cookies="SID=session-cookie",
        )

        operation = result["operations"][0]
        self.assertEqual(operation["status"], "MEDIA_GENERATION_STATUS_FAILED")
        self.assertEqual(
            operation["operation"]["error"]["code"],
            "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED",
        )
        self.assertIn("人物安全过滤", operation["operation"]["error"]["message"])

    async def test_generate_video_start_end_uses_v2_payload_for_interpolation_lite(
        self,
    ):
        operation_id = "11111111-1111-1111-1111-111111111111"
        project_id = "22222222-2222-2222-2222-222222222222"
        media_id = "33333333-3333-3333-3333-333333333333"
        self.client._call_flow_frontend_rpc = AsyncMock(
            return_value=[[operation_id, project_id, media_id]]
        )

        await self.client.generate_video_start_end(
            at="at-token",
            project_id=project_id,
            prompt="变身猫猫",
            model_key="veo_3_1_interpolation_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            start_media_id="start-media",
            end_media_id="end-media",
            use_v2_model_config=True,
            google_cookies="SID=session-cookie",
        )

        rpc = self.client._call_flow_frontend_rpc.await_args.kwargs
        self.assertEqual(rpc["rpc_id"], "nprQif")
        request_data = rpc["argument"][0][0]
        self.assertEqual(request_data[0], [None, None, [[["变身猫猫"]]]])
        self.assertEqual(request_data[1], "veo_3_1_interpolation_lite")
        self.assertEqual(request_data[2], 1)
        self.assertEqual(request_data[4][1], "start-media")
        self.assertEqual(request_data[5][1], "end-media")


if __name__ == "__main__":
    unittest.main()
