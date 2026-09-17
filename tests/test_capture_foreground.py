"""抓取前的窗口准备逻辑：**是否会把目标窗口置前 / 改变状态**。

为什么单独钉这条：README 给 AI 的提示里写着「普通窗口抓取不抢前台，但**最小化窗口会被置前**」——
这是一条会影响用户体感的**行为声称**，却一直没有测试。

⚠️ 两条本文件必须遵守的约束（都是踩过的坑）：
1. **不能在模块顶层 import `cvision.capture.windows`**：它在模块顶层就 `import win32gui/win32con/win32ui`，
   而这些包只存在于 Windows。直接导入会让 **macOS/Linux 的整个 unittest 套件在 collection 阶段崩掉**
   （本文件第一版就是这样，CI 上 ubuntu 与 macOS 两个 job 全红）。所以这里把导入**推迟**并加 `skipUnless`。
2. **不能换 `sys.modules["win32gui"]` 来注入假对象**：`windows.py` 持有的是模块顶层导入的直接引用，
   换 `sys.modules` 完全无效（第一版因此有两条用例「假对象从未被调用」而失败）。
   正确做法是 patch `windows.win32gui` 上的属性。
"""

import sys
import unittest

#: 这些测试只能跑在 Windows 上：`cvision.capture.windows` 在**模块顶层**就 `import win32gui/win32con/win32ui`，
#: 而这些包只存在于 Windows。所以这里必须 try/except 兜住导入 —— 否则 macOS/Linux 的整个 unittest 套件会在
#: **collection 阶段**崩掉（本文件第一版就是这样，CI 上 ubuntu 与 macOS 两个 job 全红，而不只是这一条失败）。
#:
#: 对比：`cvision/input.py` 把 win32 导入放在**函数内部**，所以 `test_input.py` 天然跨平台；
#: `capture/windows.py` 没这么做，于是平台限定性必须由测试自己承担。
try:
    from cvision.capture import windows as win_backend

    _IMPORT_ERROR = None
except ImportError as exc:  # 非 Windows（缺 pywin32）
    win_backend = None
    _IMPORT_ERROR = exc

SW_RESTORE = 9


@unittest.skipIf(win_backend is None, f"仅 Windows 可跑：{_IMPORT_ERROR}")
class TestPrepareWindowForCapture(unittest.TestCase):
    """用假 win32 驱动 `_prepare_window_for_capture`，断言它到底碰了哪些 API。"""

    class Recorder:
        def __init__(self, iconic: bool):
            self.iconic = iconic
            self.calls = []

        def IsIconic(self, hwnd):
            self.calls.append(("IsIconic", hwnd))
            return self.iconic

        def ShowWindow(self, hwnd, command):
            self.calls.append(("ShowWindow", hwnd, command))

    def run_prepare(self, iconic: bool, maximize: bool):
        """返回 (ShowWindow/IsIconic 调用, 置前调用, maximize mock)。"""
        from unittest import mock

        rec = self.Recorder(iconic=iconic)
        fg_calls = []
        with mock.patch.object(win_backend.win32gui, "IsIconic", rec.IsIconic), \
             mock.patch.object(win_backend.win32gui, "ShowWindow", rec.ShowWindow), \
             mock.patch.object(win_backend.win32con, "SW_RESTORE", SW_RESTORE), \
             mock.patch.object(win_backend, "_ensure_foreground", lambda h: fg_calls.append(h)), \
             mock.patch.object(win_backend, "maximize_window") as maxi, \
             mock.patch.object(win_backend.time, "sleep", lambda *_: None):
            win_backend._prepare_window_for_capture(1234, maximize=maximize)
        return rec.calls, fg_calls, maxi

    def test_normal_window_touches_nothing(self):
        """普通窗口：不 ShowWindow、不抢前台 —— 这是「不打扰用户」的核心保证。"""
        calls, fg, maxi = self.run_prepare(iconic=False, maximize=False)
        self.assertEqual([c for c in calls if c[0] == "ShowWindow"], [], "普通窗口不该被 ShowWindow")
        self.assertEqual(fg, [], "普通窗口抓取不该碰前台")
        maxi.assert_not_called()

    def test_minimized_window_is_restored_and_foregrounded(self):
        """最小化窗口：必须还原（否则抓不到内容），代价是**会置前**。"""
        calls, fg, maxi = self.run_prepare(iconic=True, maximize=False)
        self.assertIn(("ShowWindow", 1234, SW_RESTORE), calls, "最小化窗口必须被还原")
        self.assertEqual(fg, [1234], "还原后会被置前 —— README 已如实声明这条副作用")
        maxi.assert_not_called()

    def test_maximize_true_maximizes_then_foregrounds(self):
        """显式 maximize=true：先 maximize_window(keep_foreground=False)，再 _ensure_foreground 置前。"""
        calls, fg, maxi = self.run_prepare(iconic=False, maximize=True)
        maxi.assert_called_once()
        self.assertEqual(maxi.call_args[0][0], 1234)
        self.assertEqual(maxi.call_args[1].get("keep_foreground"), False)
        self.assertEqual(fg, [1234], "最大化后再置前，确保用户看得到")

    def test_maximize_true_does_not_also_restore(self):
        """既是最小化又要求最大化时走 maximize 分支，不应再单独 SW_RESTORE。"""
        calls, fg, maxi = self.run_prepare(iconic=True, maximize=True)
        maxi.assert_called_once()
        self.assertNotIn(("ShowWindow", 1234, SW_RESTORE), calls)

    def test_win32_failure_never_escapes(self):
        """win32 抛异常时必须被吞掉：抓图准备失败不该让整个 see 崩。"""
        from unittest import mock

        with mock.patch.object(win_backend.win32gui, "IsIconic", side_effect=OSError("boom")), \
             mock.patch.object(win_backend.time, "sleep", lambda *_: None):
            win_backend._prepare_window_for_capture(1234, maximize=False)  # 不抛即为通过


# 说明：以上是**逻辑层**断言（假 win32）。「真机上最小化窗口确实会抢走前台」是**实测结论**，写进了
# README 的「给 AI 智能体的使用提示」——它需要真窗口与真桌面，且会改变跑测试者当前的前台窗口，
# 不适合放进 unittest 套件，所以这里刻意不留一个永远 skip 的占位用例冒充覆盖。


if __name__ == "__main__":
    unittest.main()
