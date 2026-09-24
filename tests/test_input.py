"""``focus_window`` 的置前语义测试（平台无关：用假 win32 驱动，不碰真窗口）。

回归的 bug：``focus_window`` 曾无条件调用 ``ShowWindow(SW_RESTORE)``，于是把一个**最大化**的
窗口（例如全屏的浏览器）降级成普通尺寸——用户看到的就是「窗口被改成半屏」。这里钉死三条语义：
最小化才还原、最大化必须保持最大化、普通窗口保持原样；并且这三种情况下都要置前。
"""

import sys
import unittest
from unittest import mock

from cvision import input as input_module
from cvision.input import activation_probes, attach_thread_for, bring_to_front, show_command_for

SW_RESTORE = 9
SW_MAXIMIZE = 3


class FakeWin32:
    """记录调用序列的假 win32gui / win32con。"""

    def __init__(self, *, iconic=False, zoomed=False):
        self.iconic = iconic
        self.zoomed = zoomed
        self.calls = []

    def IsIconic(self, hwnd):
        self.calls.append(("IsIconic", hwnd))
        return self.iconic

    def IsZoomed(self, hwnd):
        self.calls.append(("IsZoomed", hwnd))
        return self.zoomed

    def ShowWindow(self, hwnd, command):
        self.calls.append(("ShowWindow", hwnd, command))

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))

    def BringWindowToTop(self, hwnd):
        self.calls.append(("BringWindowToTop", hwnd))

    @property
    def show_window_calls(self):
        return [call for call in self.calls if call[0] == "ShowWindow"]


class FakeCon:
    SW_RESTORE = SW_RESTORE
    SW_MAXIMIZE = SW_MAXIMIZE


class TestShowCommandFor(unittest.TestCase):
    def test_minimized_needs_restore(self):
        self.assertEqual(show_command_for(is_minimized=True, is_maximized=False), "restore")

    def test_maximized_must_be_kept(self):
        self.assertEqual(show_command_for(is_minimized=False, is_maximized=True), "keep")

    def test_normal_window_is_left_alone(self):
        self.assertIsNone(show_command_for(is_minimized=False, is_maximized=False))


class TestCapabilities(unittest.TestCase):
    def test_capabilities_match_platform(self):
        """能力清单必须按平台如实报告：focus_window 只在 Windows 上存在（别处调用会抛错）。"""
        if sys.platform.startswith("win"):
            self.assertIn("focus_window", input_module.CAPABILITIES)
        else:
            self.assertNotIn("focus_window", input_module.CAPABILITIES)
        # 这些是跨平台的（pyautogui / pyperclip）
        for name in ("click", "type_text", "press_keys", "get_clipboard", "set_clipboard"):
            self.assertIn(name, input_module.CAPABILITIES)


class TestBringToFront(unittest.TestCase):
    def test_maximized_window_is_never_restored(self):
        gui = FakeWin32(zoomed=True)
        bring_to_front(4242, gui, FakeCon())
        self.assertEqual(gui.show_window_calls, [], "最大化窗口绝不能被 ShowWindow 降级")
        self.assertIn(("SetForegroundWindow", 4242), gui.calls)
        self.assertIn(("BringWindowToTop", 4242), gui.calls)

    def test_minimized_window_is_restored_then_focused(self):
        gui = FakeWin32(iconic=True)
        bring_to_front(7, gui, FakeCon())
        self.assertEqual(gui.show_window_calls, [("ShowWindow", 7, SW_RESTORE)])
        self.assertIn(("SetForegroundWindow", 7), gui.calls)

    def test_normal_window_only_focuses(self):
        gui = FakeWin32()
        bring_to_front(9, gui, FakeCon())
        self.assertEqual(gui.show_window_calls, [])
        self.assertIn(("SetForegroundWindow", 9), gui.calls)

    def test_win32_failures_never_escape(self):
        class Broken(FakeWin32):
            def IsIconic(self, hwnd):
                raise OSError("窗口已销毁")

            def SetForegroundWindow(self, hwnd):
                raise OSError("前台锁")

        bring_to_front(1, Broken(), FakeCon())  # 不抛异常即为通过


class TestAttachThreadFor(unittest.TestCase):
    """``AttachThreadInput`` 必须挂到**前台窗口的线程**——真机实测出来的关键规则。

    回归的 bug：旧实现挂的是**目标窗口**的线程。Windows 的前台规则是「调用线程必须拥有
    **前台窗口**的输入队列」，所以那条附加满足不了规则：实测 ``AttachThreadInput`` 直接返回 0，
    随后的 ``SetForegroundWindow`` 继续被前台锁定拒绝，``focus_window`` 在真机上 100% 无效。
    后果不是「置前没生效」这么轻——click 只命中**前台**窗口，于是用户看到的是
    「坐标明明没错，点了却没反应 / 点到了别的窗口上」。
    """

    def test_attaches_to_foreground_thread_not_target(self):
        self.assertEqual(
            attach_thread_for(100, 200, 300), 200,
            "必须挂前台线程(200)，绝不能挂目标线程(300)",
        )

    def test_no_attach_when_target_is_already_foreground(self):
        self.assertIsNone(attach_thread_for(100, 300, 300))

    def test_no_attach_when_current_thread_is_foreground(self):
        self.assertIsNone(attach_thread_for(200, 200, 300))

    def test_no_attach_without_a_foreground_window(self):
        self.assertIsNone(attach_thread_for(100, 0, 300))


class TestActivationProbes(unittest.TestCase):
    """「点击激活」的候选点 —— 这是「目标被盖住时怎么把它带到最前」的操作策略。

    背景：程序化提升层叠顺序在 Windows 上不成立（实测 ``SetWindowPos(HWND_TOP)``、
    ``BringWindowToTop``、``SwitchToThisWindow`` 都返回成功却**不改变层叠**）。只有**真实的鼠标
    输入**才会让系统重排，于是策略改成模拟人的做法：先点一下目标窗口的**标题栏中央**把它带到最前，
    再点真正想点的位置。点错标题栏代价很大（可能误触关闭/最小化），所以顺序必须钉死。
    """

    def test_first_probe_is_the_caption_center(self):
        """首选必须是标题栏**中央**：那里单击只激活窗口，碰不到任何按钮，也不会切标签页。"""
        left, top, right, bottom = 100, 200, 900, 700
        probes = activation_probes((left, top, right, bottom))
        self.assertTrue(probes, "至少要给出一个候选点")
        x, y = probes[0]
        self.assertEqual(x, (left + right) // 2, "第一个候选点必须是水平中央")
        self.assertGreater(y, top, "必须落在窗口内（标题栏里）")
        self.assertLess(y, top + 60, "必须落在标题栏那一带，而不是客户区深处")

    def test_every_probe_stays_inside_the_window(self):
        rect = (100, 200, 900, 700)
        for x, y in activation_probes(rect):
            self.assertGreaterEqual(x, rect[0])
            self.assertLessEqual(x, rect[2])
            self.assertGreaterEqual(y, rect[1])
            self.assertLessEqual(y, rect[3])

    def test_probes_are_ordered_center_first_then_sides(self):
        """两侧候选点是「中央恰好被挡住」时的退路，按安全程度排序。"""
        probes = activation_probes((0, 0, 800, 600))
        self.assertEqual(probes[0][0], 400)
        self.assertEqual(probes[1][0], 200)
        self.assertEqual(probes[2][0], 600)

    def test_degenerate_window_does_not_crash(self):
        """极窄窗口会让候选点重合——不能崩，也不能给出窗口外的点。"""
        probes = activation_probes((0, 0, 4, 100))
        self.assertTrue(probes)
        for x, y in probes:
            self.assertTrue(0 <= x <= 4)
            self.assertTrue(0 <= y <= 100)


@unittest.skipUnless(sys.platform.startswith("win"), "需要 pywin32 的窗口 API")
class TestEnsureFrontUnblock(unittest.TestCase):
    """目标被盖住时的兜底策略：**先点它自己的标题栏**把它带到最前，再复核目标点。

    为什么必须是「真点一下」：程序化提升层叠顺序在 Windows 上不可靠（实测
    ``SetWindowPos(HWND_TOP)`` / ``BringWindowToTop`` / ``SwitchToThisWindow`` 都可能返回成功却
    不改变层叠）。真实鼠标输入才会让系统重排——这正是人遇到这种情况的做法。
    这里把三种分支都钉死：能点就点、不该点就不点、完全被盖住时不许乱点。
    """

    RECT = (100, 200, 900, 700)
    TARGET = 0x100
    BLOCKER = 0x200

    def _run(self, *, unblock, probe_visible=True, focus_ok=True):
        """跑一次 ensure_front，返回 (info, 实际发生的点击列表)。"""
        import win32gui

        caption = activation_probes(self.RECT)[0]
        state = {"unblocked": False}
        clicks: list[tuple[int, int]] = []

        def fake_window_at(x, y):
            if probe_visible and (x, y) == caption:
                return self.TARGET  # 标题栏露着 → 可点
            return self.TARGET if state["unblocked"] else self.BLOCKER

        def fake_click(x, y, button="left", double=False):
            clicks.append((x, y))
            if (x, y) == caption:
                state["unblocked"] = True  # 真实点击会让系统把窗口带到最前

        with mock.patch.object(win32gui, "IsWindow", return_value=True), \
             mock.patch.object(win32gui, "GetWindowRect", return_value=self.RECT), \
             mock.patch.object(win32gui, "GetWindowText", return_value="记事本"), \
             mock.patch.object(win32gui, "GetForegroundWindow",
                               return_value=self.TARGET if focus_ok else self.BLOCKER), \
             mock.patch.object(input_module, "_force_foreground_native", return_value=focus_ok), \
             mock.patch.object(input_module, "window_at", side_effect=fake_window_at), \
             mock.patch.object(input_module, "move", lambda x, y: None), \
             mock.patch.object(input_module, "click", side_effect=fake_click), \
             mock.patch.object(input_module.time, "sleep", lambda *_: None):
            info = input_module.ensure_front(self.TARGET, (400, 600), unblock=unblock)
        return info, clicks, caption

    def test_unblock_clicks_the_caption_and_then_matches(self):
        info, clicks, caption = self._run(unblock=True)
        self.assertTrue(info["match"], "点过标题栏之后，目标点应归还目标窗口")
        self.assertEqual(info["unblock_point"], list(caption))
        self.assertEqual(clicks, [caption], "只允许点标题栏那一个候选点")
        self.assertEqual(info["blocker"], self.BLOCKER, "要记下当初是谁挡着的")

    def test_without_unblock_nothing_is_clicked(self):
        """没开开关时不许自作主张动鼠标——点击是真实副作用。"""
        info, clicks, _ = self._run(unblock=False)
        self.assertFalse(info["match"])
        self.assertEqual(clicks, [])
        self.assertIsNone(info["unblock_point"])

    def test_fully_covered_target_is_never_clicked(self):
        """连标题栏都露不出来时**不能乱点**（点下去只会命中遮挡者），应如实失败。"""
        info, clicks, _ = self._run(unblock=True, probe_visible=False)
        self.assertFalse(info["match"])
        self.assertEqual(clicks, [], "没有可见的候选点就一个都不许点")
        self.assertIsNone(info["unblock_point"])
        self.assertEqual(info["blocker"], self.BLOCKER)

    def test_reports_coordinate_outside_target_rect(self):
        """坐标压根不在窗口里（窗口被移动过）时要说清楚，别让调用方去折腾前台。"""
        import win32gui

        with mock.patch.object(win32gui, "IsWindow", return_value=True), \
             mock.patch.object(win32gui, "GetWindowRect", return_value=self.RECT), \
             mock.patch.object(win32gui, "GetWindowText", return_value=""), \
             mock.patch.object(win32gui, "GetForegroundWindow", return_value=self.TARGET), \
             mock.patch.object(input_module, "window_at", return_value=self.BLOCKER):
            info = input_module.ensure_front(self.TARGET, (5, 5), unblock=True)
        self.assertFalse(info["inside"])


if __name__ == "__main__":
    unittest.main()
