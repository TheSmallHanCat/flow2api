import asyncio
import unittest

from src.core.config import config
from src.services.flow_client import FlowClient


class FlowClientLaunchGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(proxy_manager=None)
        flow_config = config.get_raw_config().setdefault("flow", {})
        self._original_values = {
            "image_launch_soft_limit": flow_config.get("image_launch_soft_limit"),
            "image_launch_wait_timeout": flow_config.get("image_launch_wait_timeout"),
            "image_launch_stagger_ms": flow_config.get("image_launch_stagger_ms"),
        }
        flow_config["image_launch_soft_limit"] = 1
        flow_config["image_launch_wait_timeout"] = 5
        flow_config["image_launch_stagger_ms"] = 0

    async def asyncTearDown(self):
        flow_config = config.get_raw_config().setdefault("flow", {})
        for key, value in self._original_values.items():
            if value is None:
                flow_config.pop(key, None)
            else:
                flow_config[key] = value
        await self.client.close()

    async def test_http_session_reused_within_same_loop(self):
        session_a = await self.client._get_http_session()
        session_b = await self.client._get_http_session()

        self.assertIs(session_a, session_b)

    async def test_image_launch_gate_blocks_until_release(self):
        ok_first, wait_first_ms, stagger_first_ms = await self.client._acquire_image_launch_gate(
            token_id=101,
            token_image_concurrency=8,
        )

        self.assertTrue(ok_first)
        self.assertGreaterEqual(wait_first_ms, 0)
        self.assertEqual(stagger_first_ms, 0)

        second_task = asyncio.create_task(
            self.client._acquire_image_launch_gate(
                token_id=202,
                token_image_concurrency=8,
            )
        )

        await asyncio.sleep(0.05)
        self.assertFalse(second_task.done())

        await self.client._release_image_launch_gate(101)

        ok_second, wait_second_ms, stagger_second_ms = await asyncio.wait_for(second_task, timeout=1.0)

        self.assertTrue(ok_second)
        self.assertGreaterEqual(wait_second_ms, 40)
        self.assertEqual(stagger_second_ms, 0)

        await self.client._release_image_launch_gate(202)


if __name__ == "__main__":
    unittest.main()
