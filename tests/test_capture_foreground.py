"""抓取前的窗口准备逻辑：**是否会把目标窗口置前 / 改变状态**。

为什么单独钉这条：README 给 AI 的提示里写着「普通窗口抓取不抢前台，但**最小化窗口会被置前**」——
这是一条会影响用户体感的**行为声称**，却一直没有测试。

用假 win32 驱动 `_prepare_window_for_capture`，断言三种输入下**它到底碰了哪些 API**。
注意：`windows.py` 是在**模块顶层** `import win32gui`，所以必须 patch `windows.win32gui` 上的属性，
换 `sys.modules` 里的模块对象是无效的（本文件第一版就栽在这里，两条用例因为「假对象没被调用」而失败）。
"""

import unittest
from unittest import mock

from cvision.capture import windows as win_backend

SW_RESTORE = 9


class Recorder:
    """记录调用序列。"""

    def __init__(self, iconic: bool):
        self.iconic = iconic
        self.calls = []

    def IsIconic(self, hwnd):
        self.calls.append(("IsIconic", hwnd))
        return self.iconic

    def ShowWindow(self, hwnd, command):
        self.calls.append(("ShowWindow", hwnd, command))


def run_prepare(iconic: bool, maximize: bool):
    """跑一次 `_prepare_window_for_capture`，返回 (ShowWindow/IsIconic 调用, 置前调用, maximize mock)。"""
    rec = Recorder(iconic=iconic)
    fg_calls = []
    with mock.patch.object(win_backend.win32gui, "IsIconic", rec.IsIconic), \
         mock.patch.object(win_backend.win32gui, "ShowWindow", rec.ShowWindow), \
         mock.patch.object(win_backend.win32con, "SW_RESTORE", SW_RESTORE), \
         mock.patch.object(win_backend, "_ensure_foreground", lambda h: fg_calls.append(h)), \
         mock.patch.object(win_backend, "maximize_window") as maxi, \
         mock.patch.object(win_backend.time, "sleep", lambda *_: None):
        win_backend._prepare_window_for_capture(1234, maximize=maximize)
    return rec.calls, fg_calls, maxi


class TestPrepareWindowForCapture(unittest.TestCase):
    def test_normal_window_touches_nothing(self):
        """普通窗口：不 ShowWindow、不抢前台 —— 这是「不打扰用户」的核心保证。"""
        calls, fg, maxi = run_prepare(iconic=False, maximize=False)
        self.assertEqual([c for c in calls if c[0] == "ShowWindow"], [], "普通窗口不该被 ShowWindow")
        self.assertEqual(fg, [], "普通窗口抓取不该碰前台")
        maxi.assert_not_called()

    def test_minimized_window_is_restored_and_foregrounded(self):
        """最小化窗口：必须还原（否则抓不到内容），代价是**会置前**。"""
        calls, fg, maxi = run_prepare(iconic=True, maximize=False)
        self.assertIn(("ShowWindow", 1234, SW_RESTORE), calls, "最小化窗口必须被还原")
        self.assertEqual(fg, [1234], "还原后会被置前 —— README 已如实声明这条副作用")
        maxi.assert_not_called()

    def test_maximize_true_maximizes_and_foregrounds(self):
        """显式 maximize=true：先 maximize_window（keep_foreground=False），再 _ensure_foreground 置前。

        「最大化」这个动作本身要求用户看得见（他显式要求的），所以两条都走是**预期行为**。
        """
        calls, fg, maxi = run_prepare(iconic=False, maximize=True)
        maxi.assert_called_once()
        self.assertEqual(maxi.call_args[0][0], 1234)
        self.assertEqual(maxi.call_args[1].get("keep_foreground"), False)
        self.assertEqual(fg, [1234], "最大化后再置前，确保用户看得到")

    def test_maximize_true_does_not_also_restore(self):
        """既是最小化又要求最大化时走 maximize 分支，不应再单独 SW_RESTORE。"""
        calls, fg, maxi = run_prepare(iconic=True, maximize=True)
        maxi.assert_called_once()
        self.assertNotIn(("ShowWindow", 1234, SW_RESTORE), calls)

    def test_win32_failure_never_escapes(self):
        """win32 抛异常时必须被吞掉：抓图准备失败不该让整个 see 崩。"""
        with mock.patch.object(win_backend.win32gui, "IsIconic", side_effect=OSError("boom")), \
             mock.patch.object(win_backend.time, "sleep", lambda *_: None):
            win_backend._prepare_window_for_capture(1234, maximize=False)  # 不抛即为通过


# 说明：以上是**逻辑层**断言（假 win32，可跨平台跑）。「真机上最小化窗口确实会抢走前台」是**实测结论**，
# 写进了 README 的「给 AI 智能体的使用提示」——它需要真窗口与真桌面，且会改变跑测试者当前的前台窗口，
# 不适合放进 unittest 套件，所以这里刻意不留一个永远 skip 的占位用例冒充覆盖。


if __name__ == "__main__":
    unittest.main()
