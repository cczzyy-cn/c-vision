"""``focus_window`` 的置前语义测试（平台无关：用假 win32 驱动，不碰真窗口）。

回归的 bug：``focus_window`` 曾无条件调用 ``ShowWindow(SW_RESTORE)``，于是把一个**最大化**的
窗口（例如全屏的浏览器）降级成普通尺寸——用户看到的就是「窗口被改成半屏」。这里钉死三条语义：
最小化才还原、最大化必须保持最大化、普通窗口保持原样；并且这三种情况下都要置前。
"""

import sys
import unittest

from cvision import input as input_module
from cvision.input import attach_thread_for, bring_to_front, show_command_for

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


if __name__ == "__main__":
    unittest.main()
