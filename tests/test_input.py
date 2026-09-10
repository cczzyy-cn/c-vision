"""``focus_window`` 的置前语义测试（平台无关：用假 win32 驱动，不碰真窗口）。

回归的 bug：``focus_window`` 曾无条件调用 ``ShowWindow(SW_RESTORE)``，于是把一个**最大化**的
窗口（例如全屏的浏览器）降级成普通尺寸——用户看到的就是「窗口被改成半屏」。这里钉死三条语义：
最小化才还原、最大化必须保持最大化、普通窗口保持原样；并且这三种情况下都要置前。
"""

import unittest

from cvision.input import bring_to_front, show_command_for

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


if __name__ == "__main__":
    unittest.main()
