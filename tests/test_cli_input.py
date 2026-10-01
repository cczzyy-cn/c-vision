"""``cli_input`` 参数层的契约测试（平台无关：stub 掉 ``cvision.input``，不碰鼠标键盘）。

该模块把 13 个子命令映射成 ``cvision.input`` 的调用，此前**引用数为 0**——也就是说
「``--scroll-h`` 少传一个参数」「``--drag`` 把 x2/y2 传反」这类错误只能等用户实际点击时暴露。
这里只用假对象记录调用，断言「命令行参数 → 函数参数」的映射逐条正确。
"""

import io
import json
import unittest
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from cvision import cli_input
from cvision import input as inp
from cvision import input_lock


class Recorder:
    """记录每次调用的假 ``cvision.input`` 实现。

    ``returns`` 可指定某个函数返回什么（给 ``ensure_front`` / ``window_at`` 这类**有返回值**的
    接口用）；值若是异常实例则改为抛出，用来验证失败路径。
    ``log`` 传入一个列表时，还会把 ``("call", 函数名)`` 追加进去——与假锁的 ``lock-enter/exit``
    共用一个列表，就能断言「动作有没有落在持锁区间里」这件事的**顺序**。
    """

    def __init__(self, returns=None, log=None):
        self.calls = []
        self.returns = returns or {}
        self.log = log

    def _make(self, name):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.log is not None:
                self.log.append(("call", name))
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


def fake_lock(log, *, state=None, error=None):
    """替身 ``input_lock.input_lock``：只记**顺序**，可按需伪造状态或异常。

    真锁的语义（跨进程互斥、超时、降级）由 ``tests/test_input_lock.py`` 真刀真枪地测；这里要钉的是
    **接线**——动作必须落在持锁区间**里面**，`--no-lock` / `--lock-timeout` 必须真的传下去。
    """

    @contextmanager
    def _cm(**kwargs):
        log.append(("lock-enter", kwargs))
        if error is not None:
            raise error
        try:
            yield SimpleNamespace(note=lambda: dict(state or {}))
        finally:
            log.append(("lock-exit", {}))

    return _cm


def run_cli(argv, recorder, *, get_clipboard_returns="剪贴板文本", lock=None):
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
    if lock is not None:
        patches.append(mock.patch.object(input_lock, "input_lock", lock))
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

    def test_type_text_direct_flag(self):
        """``--type-direct`` 必须把 ``paste=False`` 传下去：输入法环境里强制逐键的逃生口。"""
        _, _, (name, args, kwargs) = self._one(["--type", "abc", "--type-direct"])
        self.assertEqual((name, args, kwargs), ("type_text", ("abc",), {"paste": False}))

    def test_type_text_paste_flag(self):
        """``--type-paste`` 相反，强制走剪贴板（默认是「按文本与输入法自动选」）。"""
        _, _, (name, args, kwargs) = self._one(["--type", "abc", "--type-paste"])
        self.assertEqual((name, args, kwargs), ("type_text", ("abc",), {"paste": True}))

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
        self.assertEqual(rec.calls[0], ("ensure_front", (4242, (10, 20)), {"unblock": False}))
        self.assertTrue(json.loads(out)["ok"])
        self.assertEqual(code, 0)

    def test_ensure_front_can_request_unblock(self):
        """``--unblock`` 要能一路传到 input 层——它是「目标被盖住时点标题栏把它带到最前」的开关。"""
        rec = Recorder(returns={"ensure_front": dict(self.MATCHING)})
        code, out = run_cli(["--ensure-front", "4242", "--at", "10", "20", "--unblock"], rec)
        self.assertEqual(rec.calls[0], ("ensure_front", (4242, (10, 20)), {"unblock": True}))
        self.assertEqual(code, 0)

    def test_ensure_front_without_at_checks_front_only(self):
        rec = Recorder(returns={"ensure_front": {"handle": 7, "stale": False, "focused": True, "match": False}})
        code, out = run_cli(["--ensure-front", "7"], rec)
        self.assertEqual(rec.calls[0], ("ensure_front", (7, None), {"unblock": False}))
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


class TestInputLockWiring(unittest.TestCase):
    """「动作落在持锁区间内」的接线契约（跨进程互斥的真行为见 ``tests/test_input_lock.py``）。

    为什么单列：跨进程互斥只在**一次 CLI 调用内部**有效，所以「先置前、再动作」如果拆成两次调用，
    锁就等于没加——另一个进程正好能在两次调用之间插队把它的窗口置前。这个缝在真实使用中几乎看不出来
    （只会偶发点错窗口），只能靠断言调用顺序钉住。
    """

    MATCHING = TestFrontGuard.MATCHING

    def test_action_runs_inside_the_lock(self):
        log = []
        rec = Recorder(log=log)
        code, out = run_cli(["--click", "10", "20"], rec, lock=fake_lock(log))
        self.assertEqual([entry[0] for entry in log], ["lock-enter", "call", "lock-exit"])
        self.assertEqual(json.loads(out), {"ok": True}, "锁正常时 JSON 形状不许变")
        self.assertEqual(code, 0)

    def test_preflight_and_action_share_one_lock(self):
        """置前与动作必须在**同一个**持锁区间里：两次调用各锁一次 = 中间有缝 = 等于没锁。"""
        log = []
        rec = Recorder(returns={"ensure_front": dict(self.MATCHING)}, log=log)
        code, _ = run_cli(
            ["--ensure-front", "4242", "--unblock", "--at", "10", "20", "--click", "10", "20"],
            rec,
            lock=fake_lock(log),
        )
        self.assertEqual([entry[0] for entry in log], ["lock-enter", "call", "call", "lock-exit"])
        self.assertEqual([call[0] for call in rec.calls], ["ensure_front", "click"], "顺序必须是先置前后动作")
        self.assertEqual(rec.calls[0][2], {"unblock": True})
        self.assertEqual(code, 0)

    def test_front_check_failure_skips_the_action(self):
        """校验没过时**不许**执行动作——「宁可报错，绝不点偏」在合并调用里同样成立。"""
        log = []
        rec = Recorder(returns={"ensure_front": dict(self.MATCHING, match=False, point_after=7)}, log=log)
        code, out = run_cli(
            ["--ensure-front", "4242", "--at", "10", "20", "--click", "10", "20"],
            rec,
            lock=fake_lock(log),
        )
        self.assertNotEqual(code, 0)
        self.assertEqual([call[0] for call in rec.calls], ["ensure_front"])
        self.assertIn("已阻止这次点击", json.loads(out)["error"])

    def test_lock_timeout_fails_without_touching_input(self):
        log = []
        rec = Recorder(log=log)
        code, out = run_cli(
            ["--click", "1", "2"],
            rec,
            lock=fake_lock(log, error=input_lock.InputLockTimeout(0.5, {"pid": 4242, "label": "holder"})),
        )
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertNotEqual(code, 0)
        self.assertIn("pid=4242", payload["error"], "超时要如实说清是谁占着")
        self.assertEqual(rec.calls, [], "超时绝不能还是把点击发出去了")
        self.assertEqual([entry[0] for entry in log], ["lock-enter"], "根本没进临界区")

    def test_no_lock_flag_reaches_the_lock(self):
        """``--no-lock`` 必须真的传下去（enabled=False），而不是被静默忽略。"""
        log = []
        rec = Recorder(log=log)
        run_cli(["--click", "1", "2", "--no-lock"], rec, lock=fake_lock(log))
        self.assertEqual(log[0][1]["enabled"], False)
        self.assertEqual(log[0][1]["label"], "click(1,2)", "持锁记录要写清在干什么")

    def test_no_lock_is_reported_in_json(self):
        """走**真锁**：跳过互斥这件事必须写进返回 JSON（静默失去保证比报错更危险）。

        用真锁而不是替身：``enabled=False`` 根本不会碰操作系统对象，所以在任何平台上都安全。
        """
        rec = Recorder()
        _, out = run_cli(["--click", "1", "2", "--no-lock"], rec)
        self.assertIn("--no-lock", json.loads(out)["lock"]["skipped"])

    def test_lock_timeout_flag_reaches_the_lock(self):
        log = []
        rec = Recorder(log=log)
        run_cli(["--click", "1", "2", "--lock-timeout", "0.25"], rec, lock=fake_lock(log))
        self.assertEqual(log[0][1]["timeout_s"], 0.25)

    def test_lock_label_carries_the_session_identity(self):
        """宿主传来的会话身份要出现在持有者记录最前面——等锁的对方才知道该去看哪个会话。"""
        log = []
        rec = Recorder(log=log)
        run_cli(
            ["--click", "1", "2", "--lock-label", "session=abc123"],
            rec,
            lock=fake_lock(log),
        )
        self.assertEqual(log[0][1]["label"], "session=abc123 click(1,2)")

    def test_lock_label_also_covers_a_preflight_only_call(self):
        """只有 --ensure-front（没有动作）时同样要有标签，否则超时的人只看到一句空白。"""
        log = []
        rec = Recorder(returns={"ensure_front": dict(TestFrontGuard.MATCHING)}, log=log)
        run_cli(
            ["--ensure-front", "4242", "--lock-label", "session=abc123"],
            rec,
            lock=fake_lock(log),
        )
        self.assertEqual(log[0][1]["label"], "session=abc123 ensure-front(0x1092)")

    def test_degraded_lock_is_surfaced_in_json(self):
        """锁机制不可用（降级放行）时必须让调用方看见——静默失去保证比报错更危险。"""
        log = []
        rec = Recorder(log=log)
        _, out = run_cli(
            ["--click", "1", "2"], rec, lock=fake_lock(log, state={"degraded": "CreateMutexW 失败"})
        )
        self.assertEqual(json.loads(out)["lock"], {"degraded": "CreateMutexW 失败"})
        self.assertEqual([call[0] for call in rec.calls], ["click"], "降级仍然要执行动作（不然等于把工具打死）")


class TestFrontError(unittest.TestCase):
    """失败原因的**分类**：说清到底是「没激活」「坐标不在窗口内」还是「被谁挡住了」。

    为什么值得单独钉：旧实现把所有情况都写成「置前未生效」，而实测目标被盖住时**置前其实成功了**
    （前台确实切换了），做不到的是提升层叠顺序。那句措辞会把调用方引向重试 ``focus_window``——
    一条同样无效的路。分类正确才谈得上「给出可执行的下一步」。
    """

    def _msg(self, info, at=(10, 20)):
        return cli_input._front_error(info, list(at) if at else None)

    def test_stale_window(self):
        self.assertIn("已不存在", self._msg({"handle": 9, "stale": True}))

    def test_coordinate_outside_target_points_at_resee(self):
        """坐标压根不在窗口矩形内 = 窗口被移动过，该让调用方重新 see，而不是去折腾前台。"""
        msg = self._msg({"handle": 0x100, "inside": False, "focused": True, "blocker": 1})
        self.assertIn("不在目标窗口", msg)
        self.assertIn("重新 see", msg)

    def test_blocked_window_names_the_blocker(self):
        """被挡住时必须点名**是谁挡的**（带上标题），否则调用方无从判断该挪开哪个窗口。"""
        msg = self._msg({
            "handle": 0x100, "inside": True, "focused": True, "match": False,
            "blocker": 0x200, "blocker_title": "记事本", "unblock_point": None, "point_after": 0x200,
        })
        self.assertIn("0x200", msg)
        self.assertIn("记事本", msg)
        self.assertIn("已阻止这次点击", msg)
        self.assertNotIn("置前未生效", msg, "旧措辞与实测不符，不得再出现")

    def test_unblock_attempt_is_reported(self):
        """尝试过「点标题栏激活」就该如实说，否则调用方不知道插件已经努力过。"""
        msg = self._msg({
            "handle": 0x100, "inside": True, "focused": True,
            "blocker": 0x200, "blocker_title": "", "unblock_point": [5, 6],
        })
        self.assertIn("已尝试点一下", msg)
        self.assertIn("已阻止这次点击", msg)

    def test_focus_failure_is_distinguished_from_being_covered(self):
        """「没激活成功」和「激活了但被盖住」是两回事，不能混为一谈。"""
        msg = self._msg({
            "handle": 0x100, "inside": True, "focused": False, "front_after": 0x300,
            "blocker": 0x200, "blocker_title": "", "unblock_point": None,
        })
        self.assertIn("未能激活", msg)
        self.assertIn(f"0x{0x300:x}", msg)

    def test_front_only_failure_does_not_mention_coordinates(self):
        msg = self._msg({"handle": 0x100, "focused": False, "front_after": 0x300}, at=None)
        self.assertIn("未能激活", msg)


if __name__ == "__main__":
    unittest.main()
