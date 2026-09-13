"""``cli_server`` 的 JSON-line 协议契约测试（平台无关：stub 掉截屏，不碰桌面）。

为什么必须有：``cli_server`` 是宿主与 Python 之间**唯一的常驻通道**，`see` / `ocr` /
`list_windows` / `screen_info` / 剪贴板轮询全走它。宿主按「一行请求 → 一行响应」严格配对
（`src/index.ts` 的 `CvisionServer`），所以这份契约一旦漂移，只能在用户实际使用时暴露。
此前该模块**一行测试都没有**。

两条线都测：
1. :func:`cvision.cli_server.handle` 的响应形状（含未知 op 与异常必须变成 ``ok:false`` 而不是崩掉）；
2. **真的 spawn 一个进程**跑 stdin/stdout 往返——这才是宿主实际用的方式。
"""

import json
import subprocess
import sys
import unittest
from unittest import mock

from PIL import Image

from cvision import capturer, cli_server, encoding
from cvision.capture.base import Window

#: 响应里必须出现的键（宿主读这几个字段）。
_CAPTURE_KEYS = {"ok", "kind", "data_url", "width", "height"}


def fake_image(width=8, height=6):
    """真实的 PIL 图（很小，编码很快）。

    刻意**不**做手写替身：``fit_for_attachment`` 要用 ``.size`` / ``.resize`` / ``s.resize``，
    手写替身只会把测试变成「对着自己写的假接口编程」。
    """
    return Image.new("RGB", (width, height), (10, 20, 30))


class TestHandleContract(unittest.TestCase):
    def test_unknown_op_is_an_error_not_a_crash(self):
        resp = cli_server.handle({"op": "no-such-op"})
        self.assertFalse(resp["ok"])
        self.assertIn("unknown op", resp["error"])

    def test_missing_op_is_an_error(self):
        resp = cli_server.handle({})
        self.assertFalse(resp["ok"])

    def test_ping(self):
        self.assertEqual(cli_server.handle({"op": "ping"}), {"ok": True, "kind": "pong"})

    def test_capture_shape(self):
        with mock.patch.object(capturer, "capture_screen", return_value=fake_image()):
            resp = cli_server.handle({"op": "capture", "format": "PNG"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["kind"], "capture")
        self.assertTrue(set(resp) >= _CAPTURE_KEYS)

    def test_capture_text_shape(self):
        """see(text=true) 走这条：必须带 elements，且与 cli_capture --text 同形状。"""
        with mock.patch.object(capturer, "capture_screen", return_value=fake_image()):
            with mock.patch.object(capturer, "capture_with_text", return_value=(fake_image(), [{"text": "确定"}])):
                resp = cli_server.handle({"op": "capture", "text": True, "format": "PNG"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["kind"], "capture_text")
        self.assertEqual(resp["elements"], [{"text": "确定"}])
        self.assertTrue(set(resp) >= _CAPTURE_KEYS | {"elements"})

    def test_wait_changed_shape(self):
        meta = {"changed": True, "samples": 3, "elapsed_ms": 120, "diff_ratio": 0.05, "mean_diff": 1.0, "diff_bbox": None}
        with mock.patch.object(capturer, "wait_until_changed", return_value=(fake_image(), meta)):
            resp = cli_server.handle({"op": "wait_changed"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["kind"], "wait_changed")
        for key in ("changed", "samples", "elapsed_ms", "diff_ratio"):
            self.assertIn(key, resp, key)

    def test_ocr_shape_includes_words(self):
        """词框是 ocr 工具的核心产物，协议里不能少（v0.2.17 修过一次这个问题）。"""
        result = {"text": "登录", "lines": ["登录"], "words": [{"text": "登录", "x": 1, "y": 2, "w": 3, "h": 4}]}
        with mock.patch.object(capturer, "capture_screen", return_value=fake_image()):
            with mock.patch("cvision.ocr.ocr_image", return_value=result):
                resp = cli_server.handle({"op": "ocr"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["words"], result["words"])

    def test_list_shape(self):
        window = Window(handle=1, title="记事本", left=0, top=0, width=10, height=10)
        with mock.patch.object(capturer, "list_windows", return_value=[window]):
            resp = cli_server.handle({"op": "list"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["windows"][0]["title"], "记事本")

    def test_exception_becomes_ok_false(self):
        """任何 op 抛错都必须被 main 的 try 包成 ``ok:false``，不能让整个进程死掉。"""
        with mock.patch.object(capturer, "capture_screen", side_effect=RuntimeError("截图炸了")):
            with self.assertRaises(RuntimeError):
                # handle() 本身不吞异常（由 main 兜），这里确认边界在哪
                cli_server.handle({"op": "capture"})


class TestMainJsonLineProtocol(unittest.TestCase):
    """真的 spawn 一个 ``python -m cvision.cli_server``，验证 stdin/stdout 的逐行协议。"""

    def _roundtrip(self, requests: list[dict], timeout=30):
        """发一串请求，返回逐行解析后的响应列表。"""
        payload = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run(
            [sys.executable, "-m", "cvision.cli_server"],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def test_ping_quit_roundtrip(self):
        responses = self._roundtrip([{"op": "ping"}, {"op": "quit"}])
        self.assertEqual(len(responses), 1, "quit 之后不该再有输出")
        self.assertEqual(responses[0], {"ok": True, "kind": "pong"})

    def test_bad_json_does_not_kill_the_server(self):
        """坏请求要回一行错误并**继续服务**，后面的请求仍要被应答。"""
        payload = 'not json at all\n{"op":"ping"}\n{"op":"quit"}\n'
        proc = subprocess.run(
            [sys.executable, "-m", "cvision.cli_server"],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(responses), 2, "坏请求一行错误 + ping 一行响应")
        self.assertFalse(responses[0]["ok"])
        self.assertIn("bad request", responses[0]["error"])
        self.assertTrue(responses[1]["ok"], "坏请求之后服务必须还活着")

    def test_unknown_op_roundtrip_stays_alive(self):
        responses = self._roundtrip([{"op": "nope"}, {"op": "ping"}, {"op": "quit"}])
        self.assertEqual(len(responses), 2)
        self.assertFalse(responses[0]["ok"])
        self.assertTrue(responses[1]["ok"])

    def test_blank_lines_are_ignored(self):
        payload = "\n\n   \n{\"op\":\"ping\"}\n{\"op\":\"quit\"}\n"
        proc = subprocess.run(
            [sys.executable, "-m", "cvision.cli_server"],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(responses), 1, "空行不该产生任何响应")

    def test_eof_exits_cleanly(self):
        """stdin 关闭（宿主退出）应当干净结束，而不是抛异常。"""
        proc = subprocess.run(
            [sys.executable, "-m", "cvision.cli_server"],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestDataUrlContract(unittest.TestCase):
    def test_data_url_is_a_valid_data_url(self):
        """宿主用 parseDataUrl 解它，格式必须是 ``data:<mime>;base64,<payload>``。"""
        with mock.patch.object(capturer, "capture_screen", return_value=fake_image()):
            resp = cli_server.handle({"op": "capture", "format": "PNG"})
        self.assertTrue(resp["data_url"].startswith("data:image/png;base64,"))

    def test_encoding_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            encoding.image_to_data_url(fake_image(), format="BMP")


if __name__ == "__main__":
    unittest.main()
