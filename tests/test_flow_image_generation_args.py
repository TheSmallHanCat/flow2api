import unittest

from src.services.flow_client import FlowClient


PROJECT_ID = "01234567-89ab-cdef-0123-456789abcdef"
MEDIA_ID = "11234567-89ab-cdef-0123-456789abcdef"


class FlowClientImageGenerationArgumentTests(unittest.TestCase):
    def setUp(self):
        self.client = FlowClient(proxy_manager=None)

    def test_reference_images_use_media_envelope(self):
        argument = self.client._build_frontend_image_generation_argument(
            project_id=PROJECT_ID,
            prompt="换成针织开衫",
            model_name="BELUGA",
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            recaptcha_token="recaptcha-token",
            session_id="session-id",
            image_inputs=[{"name": MEDIA_ID}],
        )

        request = argument[1][0]
        # the reference slot is a 5-field envelope, not [1, mediaId]
        self.assertEqual(request[2], [[MEDIA_ID, None, None, None, 1]])
        # slot 9 stays null; a serialized settings blob here is rejected
        self.assertIsNone(request[9])
        self.assertEqual(request[8], [[["换成针织开衫"]]])

    def test_text_only_generation_omits_reference_slot(self):
        argument = self.client._build_frontend_image_generation_argument(
            project_id=PROJECT_ID,
            prompt="一只红苹果",
            model_name="BELUGA",
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            recaptcha_token="recaptcha-token",
            session_id="session-id",
        )

        request = argument[1][0]
        self.assertIsNone(request[2])
        self.assertIsNone(request[9])

    def test_image_requests_follow_frontend_slot_layout(self):
        for model_name in ("BELUGA", "HARBOR_SEAL", "GEM_PIX_2"):
            for image_inputs in (
                None,
                [],
                [{"name": MEDIA_ID}],
                [{"mediaId": MEDIA_ID}],
                [{"name": MEDIA_ID}, {"mediaId": "second-reference"}],
            ):
                with self.subTest(model=model_name, references=bool(image_inputs)):
                    argument = self.client._build_frontend_image_generation_argument(
                        project_id=PROJECT_ID,
                        prompt="an apple",
                        model_name=model_name,
                        aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                        recaptcha_token="recaptcha-token",
                        session_id="session-id",
                        image_inputs=image_inputs,
                    )
                    request = argument[1][0]
                    self.assertEqual(len(request), 15 if image_inputs else 14)
                    self.assertEqual(request[14:], [4] if image_inputs else [])
                    self.assertEqual(request[5], model_name)
                    self.assertEqual(request[9:12], [None, None, None])
                    self.assertEqual(request[12], "session-id")
                    self.assertRegex(request[13], r"^[0-9A-F-]{36}$")
                    self.assertEqual(argument[4], ["session-id"])

    def test_empty_reference_ids_keep_text_only_layout(self):
        for image_inputs in (
            [{}],
            [{"name": " "}],
            [{"mediaId": None}, None],
        ):
            with self.subTest(image_inputs=image_inputs):
                argument = self.client._build_frontend_image_generation_argument(
                    project_id=PROJECT_ID,
                    prompt="an apple",
                    model_name="BELUGA",
                    aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                    recaptcha_token="recaptcha-token",
                    session_id="session-id",
                    image_inputs=image_inputs,
                )
                request = argument[1][0]
                self.assertIsNone(request[2])
                self.assertEqual(len(request), 14)


if __name__ == "__main__":
    unittest.main()
