"""剪贴板模块与 ``cli_clipboard`` 的契约测试（平台无关：stub 掉真实剪贴板）。

宿主半边按 ``cli_clipboard`` 的 stdout JSON 决定「点亮按钮 / 取图 / 降级」，所以这份契约要钉死：
``--state`` 透传 ``{supported,image,token,reason}``；``--image`` 成功给 data URL，空剪贴板给
``reason="empty"``，平台不支持给 ``reason="unsupported"``；异常被翻译成 ``reason="error"``。
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from PIL import Image

from cvision import cli_clipboard, clipboard


def run_cli(argv):
    """跑一次 CLI，返回 (exit_code, 解析后的 JSON)。"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli_clipboard.main(argv)
    return code, json.loads(buffer.getvalue())


class TestPlatformDispatch(unittest.TestCase):
    def test_unsupported_platform_reports_reason(self):
        with mock.patch.object(clipboard.sys, "platform", "linux"):
            self.assertFalse(clipboard.is_supported())
            state = clipboard.state()
            self.assertFalse(state["supported"])
            self.assertFalse(state["image"])
            self.assertIsNone(state["token"])
            self.assertIn("Phase 2", state["reason"])
            self.assertIsNone(clipboard.read_image())

    def test_state_swallows_clipboard_errors(self):
        """剪贴板被别的进程占用时不该炸掉每秒轮询，而是如实报 false + 原因。"""
        with (
            mock.patch.object(clipboard, "is_supported", return_value=True),
            mock.patch.object(clipboard, "has_image", side_effect=OSError("clipboard busy")),
        ):
            state = clipboard.state()
            self.assertTrue(state["supported"])
            self.assertFalse(state["image"])
            self.assertIn("OSError", state["reason"])

    def test_state_reports_image_and_token(self):
        with (
            mock.patch.object(clipboard, "is_supported", return_value=True),
            mock.patch.object(clipboard, "has_image", return_value=True),
            mock.patch.object(clipboard, "token", return_value="42"),
        ):
            self.assertEqual(clipboard.state()["token"], "42")
            self.assertTrue(clipboard.state()["image"])


class TestCliContract(unittest.TestCase):
    def test_state_passes_module_state_through(self):
        fake = {"supported": True, "image": True, "token": "7", "reason": ""}
        with mock.patch.object(clipboard, "state", return_value=fake):
            code, payload = run_cli(["--state"])
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"ok": True, **fake})

    def test_image_success_returns_data_url(self):
        with (
            mock.patch.object(clipboard, "read_image", return_value=Image.new("RGB", (8, 6), (1, 2, 3))),
        ):
            code, payload = run_cli(["--image"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data_url"].startswith("data:image/png;base64,"))

    def test_empty_clipboard_reports_empty(self):
        with (
            mock.patch.object(clipboard, "read_image", return_value=None),
            mock.patch.object(clipboard, "is_supported", return_value=True),
        ):
            code, payload = run_cli(["--image"])
        self.assertEqual(code, 2)
        self.assertEqual(payload, {"ok": False, "reason": "empty"})

    def test_unsupported_platform_reports_message(self):
        with (
            mock.patch.object(clipboard, "read_image", return_value=None),
            mock.patch.object(clipboard, "is_supported", return_value=False),
        ):
            code, payload = run_cli(["--image"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason"], "unsupported")
        self.assertIn("Phase 2", payload["message"])

    def test_unexpected_error_is_wrapped_not_raised(self):
        with mock.patch.object(clipboard, "read_image", side_effect=ValueError("boom")):
            code, payload = run_cli(["--image"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "error")
        self.assertIn("ValueError", payload["message"])

    def test_mode_is_required(self):
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                cli_clipboard.main([])


if __name__ == "__main__":
    unittest.main()
