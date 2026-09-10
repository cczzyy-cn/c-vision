"""系统截图 CLI 的边界契约测试（平台无关：只 stub 掉真正的截图，不碰桌面）。

宿主半边按 ``cli_snip`` 的 stdout JSON 决定回退还是报错，所以这份契约要钉死：
- ``ok: true`` + data URL（成功）
- ``reason: "cancelled"``（用户 Esc / 超时）
- ``reason: "unsupported"``（平台未实现 → 浏览器半边回退抓屏）
- ``reason: "error"``（其它异常）
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from PIL import Image

from cvision import cli_snip, snip


def run_cli(argv=None):
    """跑一次 CLI，返回 (exit_code, 解析后的 JSON)。"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli_snip.main(argv or [])
    return code, json.loads(buffer.getvalue())


class TestToRgb(unittest.TestCase):
    def test_normalizes_palette_mode(self):
        image = Image.new("P", (4, 3))
        self.assertEqual(snip._to_rgb(image).mode, "RGB")

    def test_keeps_rgb_and_rgba(self):
        self.assertEqual(snip._to_rgb(Image.new("RGB", (2, 2))).mode, "RGB")
        self.assertEqual(snip._to_rgb(Image.new("RGBA", (2, 2))).mode, "RGBA")


class TestCliContract(unittest.TestCase):
    def test_success_returns_png_data_url(self):
        image = Image.new("RGB", (12, 8), (10, 20, 30))
        with mock.patch.object(snip, "snip_selection", return_value=image):
            code, payload = run_cli(["--timeout", "5"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data_url"].startswith("data:image/png;base64,"))

    def test_cancelled_reports_reason(self):
        with mock.patch.object(snip, "snip_selection", return_value=None):
            code, payload = run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(payload, {"ok": False, "reason": "cancelled"})

    def test_unsupported_reports_reason_and_message(self):
        error = snip.SnipUnsupported("系统截图尚未在 linux 上实现")
        with mock.patch.object(snip, "snip_selection", side_effect=error):
            code, payload = run_cli()
        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "unsupported")
        self.assertIn("linux", payload["message"])

    def test_unexpected_error_is_wrapped_not_raised(self):
        with mock.patch.object(snip, "snip_selection", side_effect=ValueError("boom")):
            code, payload = run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "error")
        self.assertIn("ValueError", payload["message"])

    def test_selection_is_fitted_before_returning(self):
        """输出必须过 fit_for_attachment：与 see 工具同一套附件限额。"""
        image = Image.new("RGB", (9000, 4000), (1, 2, 3))
        with mock.patch.object(snip, "snip_selection", return_value=image) as selection:
            code, payload = run_cli([])
        self.assertEqual(code, 0)
        selection.assert_called_once()
        self.assertLessEqual(len(payload["data_url"]), 30 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
