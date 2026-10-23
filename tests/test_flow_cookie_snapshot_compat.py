"""Cookie 快照兼容性：缺失 get_updated_flow_cookies 时不能抛 AttributeError。

历史故障：自定义/旧版 flow_frontend 覆盖了上游新增的 Cookie 作用域方法，
导致添加 Google Cookie 时直接失败：

    添加失败: Google Cookie 验证失败：
    'FlowClient' object has no attribute 'get_updated_flow_cookies'

修复策略：调用点改为安全取值（getattr + 降级返回原始 Cookie 串），
既保留上游 Cookie 轮换快照能力，也能容忍不完整的 mixin 覆盖。
"""

import unittest

from src.services.flow_client import _safe_updated_flow_cookies as client_helper
from src.services.flow_current import _safe_updated_flow_cookies as current_helper

HELPERS = (client_helper, current_helper)


class _NoSuchMethod:
    """模拟缺少 get_updated_flow_cookies 的旧版 mixin。"""


class _WithMethod:
    def get_updated_flow_cookies(self, original):
        return f"{original};ROTATED=1"


class _RaisingMethod:
    def get_updated_flow_cookies(self, original):
        raise RuntimeError("boom")


class SafeUpdatedFlowCookiesTest(unittest.TestCase):
    def test_missing_method_falls_back_to_original(self):
        for helper in HELPERS:
            with self.subTest(helper=helper.__module__):
                self.assertEqual(helper(_NoSuchMethod(), "SID=abc"), "SID=abc")

    def test_missing_method_normalizes_none(self):
        for helper in HELPERS:
            with self.subTest(helper=helper.__module__):
                self.assertEqual(helper(_NoSuchMethod(), None), "")

    def test_uses_method_when_present(self):
        for helper in HELPERS:
            with self.subTest(helper=helper.__module__):
                self.assertEqual(
                    helper(_WithMethod(), "SID=abc"), "SID=abc;ROTATED=1"
                )

    def test_method_error_falls_back_to_original(self):
        for helper in HELPERS:
            with self.subTest(helper=helper.__module__):
                self.assertEqual(helper(_RaisingMethod(), " SID=abc "), "SID=abc")


if __name__ == "__main__":
    unittest.main()
