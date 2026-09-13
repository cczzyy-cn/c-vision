"""``cli_input`` 参数层的契约测试（平台无关：stub 掉 ``cvision.input``，不碰鼠标键盘）。

该模块把 13 个子命令映射成 ``cvision.input`` 的调用，此前**引用数为 0**——也就是说
「``--scroll-h`` 少传一个参数」「``--drag`` 把 x2/y2 传反」这类错误只能等用户实际点击时暴露。
这里只用假对象记录调用，断言「命令行参数 → 函数参数」的映射逐条正确。
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cvision import cli_input
from cvision import input as inp


class Recorder:
    """记录每次调用的假 ``cvision.input`` 实现。"""

    def __init__(self):
        self.calls = []

    def _make(self, name):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return _fn

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._make(name)


def run_cli(argv, recorder, *, get_clipboard_returns="剪贴板文本"):
    """跑一次 ``cli_input.main``，返回 (exit_code, stdout, recorder)。"""
    patches = [
        mock.patch.object(inp, "focus_window", recorder.focus_window),
        mock.patch.object(inp, "click", recorder.click),
        mock.patch.object(inp, "move", recorder.move),
        mock.patch.object(inp, "scroll", recorder.scroll),
        mock.patch.object(inp, "drag", recorder.drag),
        mock.patch.object(inp, "type_text", recorder.type_text),
        mock.patch.object(inp, "press_keys", recorder.press_keys),
        mock.patch.object(inp, "set_clipboard", recorder.set_clipboard),
        mock.patch.object(inp, "get_clipboard", lambda: get_clipboard_returns),
    ]
    for patch in patches:
        patch.start()
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            code = cli_input.main(argv)
    finally:
        for patch in patches:
            patch.stop()
    return code, buffer.getvalue()


class TestArgumentMapping(unittest.TestCase):
    def setUp(self):
        self.recorder = Recorder()

    def _one(self, argv, **kwargs):
        code, out = run_cli(argv, self.recorder, **kwargs)
        self.assertEqual(len(self.recorder.calls), 1, f"应当只触发一个动作：{self.recorder.calls}")
        return code, out, self.recorder.calls[0]

    def test_click_with_default_button(self):
        code, out, (name, args, kwargs) = self._one(["--click", "10", "20"])
        self.assertEqual((name, args, kwargs), ("click", (10, 20), {"button": "left"}))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True})

    def test_click_with_right_button(self):
        _, _, (_, args, kwargs) = self._one(["--click", "10", "20", "--button", "right"])
        self.assertEqual(kwargs, {"button": "right"})

    def test_double_sets_double_flag(self):
        """双击必须是 double=True；漏掉这个 flag 会静默变成单击。"""
        _, _, (name, args, kwargs) = self._one(["--double", "30", "40"])
        self.assertEqual(name, "click")
        self.assertEqual(args, (30, 40))
        self.assertEqual(kwargs, {"button": "left", "double": True})

    def test_move(self):
        _, _, (name, args, _) = self._one(["--move", "7", "8"])
        self.assertEqual((name, args), ("move", (7, 8)))

    def test_scroll_vertical_passes_dx_zero(self):
        _, _, (name, args, kwargs) = self._one(["--scroll", "100", "200", "-3"])
        self.assertEqual(name, "scroll")
        self.assertEqual((args, kwargs), ((100, 200), {"dx": 0, "dy": -3}))

    def test_scroll_horizontal_passes_dy_zero(self):
        """--scroll-h 必须走 dx 通道且 dy=0；参数传反是最容易犯的错。"""
        _, _, (name, args, kwargs) = self._one(["--scroll-h", "100", "200", "3"])
        self.assertEqual(name, "scroll")
        self.assertEqual((args, kwargs), ((100, 200), {"dx": 3, "dy": 0}))

    def test_drag_four_coordinates_in_order(self):
        _, _, (name, args, kwargs) = self._one(["--drag", "1", "2", "3", "4"])
        self.assertEqual(name, "drag")
        self.assertEqual(args, (1, 2, 3, 4), "四个坐标必须按 x1,y1,x2,y2 顺序传")
        self.assertEqual(kwargs, {"button": "left"})

    def test_type_text(self):
        _, _, (name, args, _) = self._one(["--type", "你好 world"])
        self.assertEqual((name, args), ("type_text", ("你好 world",)))

    def test_press_keys(self):
        _, _, (name, args, _) = self._one(["--keys", "ctrl+shift+t"])
        self.assertEqual((name, args), ("press_keys", ("ctrl+shift+t",)))

    def test_focus_by_title(self):
        _, _, (name, args, kwargs) = self._one(["--focus", "记事本"])
        self.assertEqual(name, "focus_window")
        self.assertEqual((args, kwargs), ((), {"title_substr": "记事本"}))

    def test_focus_by_handle(self):
        _, _, (name, args, kwargs) = self._one(["--focus-handle", "12345"])
        self.assertEqual(name, "focus_window")
        self.assertEqual((args, kwargs), ((), {"handle": 12345}))

    def test_set_clipboard(self):
        _, _, (name, args, _) = self._one(["--set-clipboard", "写入的文本"])
        self.assertEqual((name, args), ("set_clipboard", ("写入的文本",)))

    def test_set_clipboard_accepts_empty_string(self):
        """空串是合法输入（清空剪贴板），不能被判成「未指定动作」。"""
        _, _, (name, args, _) = self._one(["--set-clipboard", ""])
        self.assertEqual((name, args), ("set_clipboard", ("",)))

    def test_get_clipboard_emits_json_and_skips_ok_line(self):
        """get-clipboard 的契约是 ``{"text": ...}``，**不是** ``{"ok": true}``。"""
        code, out = run_cli(["--get-clipboard"], self.recorder, get_clipboard_returns="读到的文本")
        self.assertEqual(json.loads(out), {"text": "读到的文本"})
        self.assertEqual(code, 0)
        # 它直接写 stdout，不经过任何输入动作（所以 recorder 应当没有记录）。
        self.assertEqual(self.recorder.calls, [])


class TestDispatchPriority(unittest.TestCase):
    def setUp(self):
        self.recorder = Recorder()

    def test_focus_handle_wins_over_focus(self):
        run_cli(["--focus", "标题", "--focus-handle", "9"], self.recorder)
        self.assertEqual(self.recorder.calls[0][2], {"handle": 9})

    def test_click_wins_over_move(self):
        """同时给了多个动作时只有一个生效——按 dispatch 顺序，click 优先。"""
        run_cli(["--click", "1", "2", "--move", "3", "4"], self.recorder)
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0][0], "click")


class TestErrors(unittest.TestCase):
    def test_no_action_exits_with_message(self):
        rec = Recorder()
        with self.assertRaises(SystemExit) as ctx:
            run_cli([], rec)
        self.assertIn("未指定动作", str(ctx.exception))
        self.assertEqual(rec.calls, [])

    def test_click_needs_two_coordinates(self):
        with self.assertRaises(SystemExit):
            run_cli(["--click", "10"], Recorder())

    def test_drag_needs_four_coordinates(self):
        with self.assertRaises(SystemExit):
            run_cli(["--drag", "1", "2", "3"], Recorder())

    def test_bad_button_is_rejected(self):
        with self.assertRaises(SystemExit):
            run_cli(["--click", "1", "2", "--button", "sideways"], Recorder())

    def test_non_integer_coordinate_is_rejected(self):
        with self.assertRaises(SystemExit):
            run_cli(["--click", "abc", "2"], Recorder())


if __name__ == "__main__":
    unittest.main()
