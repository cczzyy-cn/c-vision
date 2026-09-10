"""`cli_ocr` 的 stdout 契约测试（平台无关：stub 掉截屏与 OCR，不碰桌面/不需要 OCR 引擎）。

宿主半边 `src/index.ts` 的 `ocrJson()` CLI 回退分支按下面的形状读 stdout：

    {"ok": true, "text": str, "lines": [str], "words": [{text,x,y,w,h}]}

所以这份契约必须钉死。此前 `cli_ocr` 只输出 ``text``/``lines`` 两个键，导致常驻 server
不可用时（回退路径）``ocr`` 工具的词级边界框**恒为空**——文档承诺的「词框供精确定位
点击」静默失效。这两个用例就是防止它再退化。
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cvision import cli_ocr, ocr

WORDS = [
    {"text": "登录", "x": 10, "y": 20, "w": 30, "h": 12},
    {"text": "取消", "x": 60, "y": 20, "w": 30, "h": 12},
]


def run_cli(argv=None):
    """跑一次 CLI，返回 (exit_code, 解析后的 JSON)。"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli_ocr.main(argv or [])
    return code, json.loads(buffer.getvalue())


class TestCliOcrContract(unittest.TestCase):
    def _run_with_result(self, result):
        # 截屏不走真实桌面（CI 无桌面），OCR 也不装引擎。
        with mock.patch.object(ocr, "ocr_image", return_value=result):
            with mock.patch("cvision.capturer.capture_screen", return_value=object()):
                return run_cli([])

    def test_words_are_passed_through(self):
        """核心回归：OCR 识别出的词框必须原样出现在 stdout 里。"""
        payload_result = {"text": "登录 取消", "lines": ["登录 取消"], "words": WORDS}
        code, payload = self._run_with_result(payload_result)

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["text"], "登录 取消")
        self.assertEqual(payload["lines"], ["登录 取消"])
        self.assertEqual(payload["words"], WORDS)

    def test_word_shape_is_xywh(self):
        """词框字段必须是 {text,x,y,w,h}——宿主直接拿 x/y/w/h 算点击点。"""
        _, payload = self._run_with_result({"text": "", "lines": [], "words": WORDS})
        for word in payload["words"]:
            self.assertEqual(sorted(word.keys()), ["h", "text", "w", "x", "y"])
            for key in ("x", "y", "w", "h"):
                self.assertIsInstance(word[key], int)

    def test_all_three_keys_always_present(self):
        """OCR 结果缺字段时也要有这三个键（宿主按 key 取值，缺失会变成 undefined）。"""
        _, payload = self._run_with_result({})
        self.assertEqual(sorted(payload.keys()), ["lines", "ok", "text", "words"])
        self.assertEqual(payload["words"], [])
        self.assertEqual(payload["lines"], [])
        self.assertEqual(payload["text"], "")

    def test_region_is_forwarded_to_crop(self):
        """--region 必须真的被用上（回归：参数被忽略会静默 OCR 整屏）。"""
        with mock.patch("cvision.capturer.capture_screen", return_value="IMG"):
            with mock.patch("cvision.encoding.crop_region", return_value="CROPPED") as crop:
                with mock.patch.object(ocr, "ocr_image", return_value={"text": "", "lines": [], "words": []}) as run:
                    code, _ = run_cli(["--region", "1,2,3,4"])
        self.assertEqual(code, 0)
        crop.assert_called_once_with("IMG", "1,2,3,4")
        self.assertEqual(run.call_args[0][0], "CROPPED")


if __name__ == "__main__":
    unittest.main()
