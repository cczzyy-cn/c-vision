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
    """记录每次调用的假 ``cvision.input`` 实现。

    ``returns`` 可指定某个函数返回什么（给 ``ensure_front`` / ``window_at`` 这类**有返回值**的
    接口用）；值若是异常实例则改为抛出，用来验证失败路径。
    """

    def __init__(self, returns=None):
        self.calls = []
        self.returns = returns or {}

    def _make(self, name):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.returns:
                value = self.returns[name]
                if isinstance(value, BaseException):
                    raise value
                return value
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
        mock.patch.object(inp, "window_at", recorder.window_at),
        mock.patch.object(inp, "ensure_front", recorder.ensure_front),
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


class TestFrontGuard(unittest.TestCase):
    """点击前置前与坐标归属校验的契约（``--window-at`` / ``--ensure-front`` / ``--focus-*``）。

    这三条契约是「坐标没错却点错窗口」的防线：宿主在每次点击前都会用它们把「最近 see 的那个
    窗口」置前，并复核点击坐标确实属于它。**失败必须非零退出**，否则调用方会把「没置前」
    当成成功，接着点到压在上面的别的东西上。
    """

    MATCHING = {
        "handle": 4242, "stale": False, "focused": True, "match": True,
        "front_before": 4242, "front_after": 4242, "point_before": 4242, "point_after": 4242,
    }

    def test_window_at_reports_handle(self):
        rec = Recorder(returns={"window_at": 4242})
        code, out = run_cli(["--window-at", "10", "20"], rec)
        self.assertEqual(rec.calls[0], ("window_at", (10, 20), {}))
        self.assertEqual(json.loads(out), {"ok": True, "handle": 4242})
        self.assertEqual(code, 0)

    def test_ensure_front_passes_coordinates_through(self):
        rec = Recorder(returns={"ensure_front": dict(self.MATCHING)})
        code, out = run_cli(["--ensure-front", "4242", "--at", "10", "20"], rec)
        self.assertEqual(rec.calls[0], ("ensure_front", (4242, (10, 20)), {}))
        self.assertTrue(json.loads(out)["ok"])
        self.assertEqual(code, 0)

    def test_ensure_front_without_at_checks_front_only(self):
        rec = Recorder(returns={"ensure_front": {"handle": 7, "stale": False, "focused": True, "match": False}})
        code, out = run_cli(["--ensure-front", "7"], rec)
        self.assertEqual(rec.calls[0], ("ensure_front", (7, None), {}))
        self.assertTrue(json.loads(out)["ok"], "没给坐标时只看是否置前成功")
        self.assertEqual(code, 0)

    def test_ensure_front_blocks_when_point_belongs_to_another_window(self):
        """校验不过时必须报 ok:false **且非零退出** —— 这是「宁可报错，绝不点偏」的落点。"""
        rec = Recorder(returns={"ensure_front": dict(self.MATCHING, match=False, point_after=7)})
        code, out = run_cli(["--ensure-front", "4242", "--at", "10", "20"], rec)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertNotEqual(code, 0)
        self.assertIn("已阻止这次点击", payload["error"], "失败原因要说清「为什么没点」")
        self.assertIn(f"0x{4242:x}", payload["error"], "原因里要带上目标窗口，便于排查")

    def test_ensure_front_reports_stale_window(self):
        rec = Recorder(returns={"ensure_front": {"handle": 9, "stale": True, "focused": False, "match": False}})
        code, out = run_cli(["--ensure-front", "9", "--at", "1", "1"], rec)
        payload = json.loads(out)
        self.assertTrue(payload["stale"], "宿主靠 stale 丢弃过期的窗口记录")
        self.assertNotEqual(code, 0)

    def test_focus_reports_real_handle(self):
        rec = Recorder(returns={"focus_window": 4242})
        code, out = run_cli(["--focus-handle", "4242"], rec)
        self.assertEqual(json.loads(out), {"ok": True, "handle": 4242, "focused": True})
        self.assertEqual(code, 0)

    def test_focus_failure_is_reported_not_swallowed(self):
        """置前失败**绝不能**回 {"ok": true}：旧实现的假成功正是点击点偏的根源。"""
        rec = Recorder(returns={"focus_window": RuntimeError("前台锁定拒绝了这次激活")})
        code, out = run_cli(["--focus-handle", "5"], rec)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertNotEqual(code, 0)
        self.assertIn("前台锁定", payload["error"])


if __name__ == "__main__":
    unittest.main()
