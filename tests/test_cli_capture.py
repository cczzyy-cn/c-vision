"""`cli_capture` 的 stdout 契约测试（平台无关：stub 掉捕获层，不碰真实桌面）。

宿主半边 `src/index.ts` 的 `captureDataUrl()` 按三种形状读这条 CLI 的 stdout：

    非 text（缺省）    data:image/png;base64,...      ← 裸 data URL，整串就是图（既有契约，不许破）
    非 text + --json   {ok,kind,data_url,width,height,image_screen_box,handle?,title?}
    --text             {ok,kind,data_url,width,height,elements,image_screen_box,handle?,title?}

为什么 `--json` 值得一组用例（v0.2.33）：CLI 回退路径原本只能给出裸 data URL，于是 `see(window=…)`
之后**宿主拿不到目标句柄**，后续 `type_text`/`press_key` 会沿用上一次的句柄——键盘输入被静默
送进错误的窗口，而整条链路报成功。这里钉住「句柄与屏幕矩形必须出现在 JSON 里」。

此前 `cli_capture` 这个入口**一行专测都没有**（`cli_ocr`/`cli_input`/`cli_server`/`cli_snip`/
`cli_clipboard` 都有）：它是 server 崩掉后唯一的兜底通道，形状漂移会直接让宿主解析失败。
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from PIL import Image

from cvision import cli_capture
from cvision.capture.base import Window

#: 仓库根（`cvision/` 的父目录）：子进程要靠它才能 import 到本仓库的包。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IMG = Image.new("RGB", (40, 20), "white")
BOX = {"x": 1, "y": 2, "width": 40, "height": 20}
WINDOW = Window(handle=1234, title="记事本", left=0, top=0, width=40, height=20)


def run_cli(argv=None):
    """跑一次 CLI，返回 (exit_code, stdout 原文)。"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli_capture.main(argv or [])
    return code, buffer.getvalue()


def make_pillow_blocker(tmp):
    """在 ``tmp`` 下造一个「一 import 就抛 ImportError」的 PIL 包，返回加了它前缀的环境变量。

    用它做**故障注入**：模拟「Pillow 没装」，比去卸载依赖安全、也比 mock 更接近真实导入路径。
    """
    pil_dir = os.path.join(tmp, "PIL")
    os.makedirs(pil_dir)
    with open(os.path.join(pil_dir, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write('raise ImportError("simulated: Pillow is not installed")\n')
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([tmp, REPO_ROOT])
    env["PYTHONUTF8"] = "1"
    return env


def run_python(args, env=None):
    """spawn 一个 Python 子进程（UTF-8 解码，超时 60s）。"""
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        cwd=REPO_ROOT,
        env=env,
    )


class TestCliCaptureContract(unittest.TestCase):
    def test_plain_capture_is_a_bare_data_url(self):
        """既有契约：非 text 且不带 --json 时 stdout 就是**裸 data URL**（不是 JSON）。"""
        with mock.patch("cvision.capturer.capture_screen", return_value=IMG):
            code, out = run_cli([])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("data:image/png;base64,"), out[:60])
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)

    def test_json_capture_carries_handle_and_frame(self):
        """--json：句柄与屏幕矩形必须带上——宿主靠它们记住「这次看的是哪个窗口/哪一块屏幕」。"""
        with mock.patch("cvision.capturer.capture_window", return_value=IMG) as cap:
            with mock.patch("cvision.capturer.resolve_window", return_value=WINDOW):
                with mock.patch("cvision.capturer.image_screen_frame", return_value=BOX):
                    code, out = run_cli(["--json", "--handle", "1234"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "capture")
        self.assertTrue(payload["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual((payload["width"], payload["height"]), (40, 20))
        self.assertEqual(payload["image_screen_box"], BOX)
        self.assertEqual(payload["handle"], 1234)
        self.assertEqual(payload["title"], "记事本")
        cap.assert_called_once_with(handle=1234, title_substr=None, maximize=False)

    def test_json_capture_screen_has_no_handle(self):
        """整屏抓取没有目标窗口：不许编一个句柄出来（宿主据此把旧目标清空）。"""
        with mock.patch("cvision.capturer.capture_screen", return_value=IMG):
            with mock.patch("cvision.capturer.image_screen_frame", return_value=BOX):
                code, out = run_cli(["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertNotIn("handle", payload)
        self.assertNotIn("title", payload)

    def test_args_are_forwarded_to_capture_window(self):
        """参数映射：--window/--maximize 必须真的传到捕获层（传丢了会静默抓错东西）。"""
        with mock.patch("cvision.capturer.capture_window", return_value=IMG) as cap:
            with mock.patch("cvision.capturer.resolve_window", return_value=None):
                with mock.patch("cvision.capturer.image_screen_frame", return_value={}):
                    code, out = run_cli(["--json", "--window", "记事本", "--maximize", "--delay", "1"])
        self.assertEqual(code, 0)
        cap.assert_called_once_with(handle=None, title_substr="记事本", maximize=True)
        self.assertFalse(json.loads(out)["image_screen_box"], "拿不到矩形时给空 dict，不给 null")

    def test_region_is_applied_before_encoding(self):
        """--region 要作用在图本身上（回归：参数被忽略会静默抓整屏，坐标全偏）。"""
        with mock.patch("cvision.capturer.capture_screen", return_value=IMG):
            with mock.patch("cvision.encoding.crop_region", return_value=IMG) as crop:
                with mock.patch("cvision.capturer.image_screen_frame", return_value=BOX):
                    code, _ = run_cli(["--json", "--region", "1,2,3,4"])
        self.assertEqual(code, 0)
        crop.assert_called_once_with(IMG, "1,2,3,4")

    def test_text_mode_shape(self):
        """--text 与常驻 server 的 capture_text 同形状（see(text=true) 的回退路径）。"""
        elements = [{"text": "确定", "screen_center": {"x": 10, "y": 20}}]
        with mock.patch("cvision.capturer.capture_with_text", return_value=(IMG, elements)) as cap:
            with mock.patch("cvision.capturer.resolve_window", return_value=WINDOW):
                code, out = run_cli(["--text"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "capture_text")
        self.assertEqual(payload["elements"], elements)
        self.assertEqual(payload["handle"], 1234)
        self.assertEqual((payload["width"], payload["height"]), (40, 20))
        self.assertEqual(cap.call_args.kwargs["title_substr"], None)

    def test_wait_changed_shape_carries_geometry_and_handle(self):
        """`wait_*` 也是抓取：JSON 里必须带 `image_screen_box`/`handle`，否则宿主无法把 `click_at`
        的比例基准换到这张新图上（那会让「等它变完 → 按比例点结果区」整体点偏，且不报错）。"""
        meta = {
            "changed": True,
            "samples": 3,
            "elapsed_ms": 120,
            "diff_ratio": 0.05,
            "mean_diff": 1.0,
            "diff_bbox": None,
            "diff_boxes": [],
        }
        with mock.patch("cvision.capturer.wait_until_changed", return_value=(IMG, meta)) as wait:
            with mock.patch("cvision.capturer.resolve_window", return_value=WINDOW):
                code, out = run_cli(["--wait-changed", "--handle", "1234"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "wait_changed")
        self.assertTrue(payload["changed"])
        self.assertIn("image_screen_box", payload, "宿主需要几何来更新 click_at 的基准")
        self.assertEqual(payload["handle"], 1234)
        self.assertIn("geometry_out", wait.call_args.kwargs, "geometry_out 必须真的传给捕获层")

    def test_missing_dependency_message_is_actionable(self):
        """捕获层导不进来时（缺依赖），给出的必须是可行动的一句话：装什么、怎么装、怎么复查。

        用**子进程 + 故障注入**测真实文案：`_capture_deps()` 的 except 分支只在真的导入失败时才走，
        而在同一进程里「把 sys.modules 里的 capturer 置 None」骗不过已经导入过的包属性
        （`from cvision import capturer` 会直接取 `cvision.capturer` 属性，不再走 import）。
        """
        code = "from cvision import cli_capture; cli_capture._capture_deps()"
        with tempfile.TemporaryDirectory() as tmp:
            out = run_python(["-c", code], env=make_pillow_blocker(tmp))
        self.assertNotEqual(out.returncode, 0, "抓图缺依赖时必须失败，不能假装成功")
        message = out.stderr + out.stdout
        self.assertIn("依赖不可用", message)
        self.assertIn("pip install", message, "报错里必须给出装依赖的办法")
        self.assertIn("--status", message, "要指向那条在缺依赖时仍能跑的排错命令")


if __name__ == "__main__":
    unittest.main()
