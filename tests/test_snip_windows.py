"""Windows 系统截图的「取消识别」测试（平台无关：注入假时钟、假覆盖层、假剪贴板）。

线上 bug：用户点截图 → 在系统截图界面按取消 → 之后用微信截图 → **那张微信截图被自动插进附件栏**。
根因是取消没被识别：老实现只看剪贴板变化，于是循环一直等到超时（默认 60s），期间任何新图片都被当成本次
截图。这里用覆盖层窗口（`SnipOverlayRootWindow`）作判据，把两种行为都钉死：

- 覆盖层消失且剪贴板始终没新图 → **立即**判定取消（不再等到超时）；
- 覆盖层消失**之后**才出现的剪贴板图片 → **绝不算**本次截图（这正是微信截图那次的场景）。
"""

import unittest
from unittest import mock

from PIL import Image

from cvision import snip


class FakeClock:
    """可推进的假时钟：让 sleep 推进时间，从而不真的等待。"""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def run_snip(*, overlay_until: float, token_at: float | None = None, timeout: float = 10.0):
    """在假环境里跑一次 ``_snip_windows``。

    :param overlay_until: 覆盖层可见到这一刻（负数=始终观测不到，模拟老系统/类名不同）。
    :param token_at: 剪贴板在这一刻之后出现新图（``None``=始终不变）。
    :returns: ``(结果, 消耗的假时间)``。
    """
    clock = FakeClock()

    def overlay_visible() -> bool:
        return clock.t < overlay_until

    def token() -> str:
        return "8" if token_at is not None and clock.t >= token_at else "7"

    def read_image():
        if token_at is not None and clock.t >= token_at:
            return Image.new("RGB", (4, 4), (1, 2, 3))
        return None

    with (
        mock.patch.object(snip, "_launch_windows_snip", lambda: True),
        mock.patch.object(snip, "_snip_overlay_visible", overlay_visible),
        mock.patch.object(snip.time, "monotonic", clock.monotonic),
        mock.patch.object(snip.time, "sleep", clock.sleep),
        mock.patch.object(snip.clipboard, "token", token),
        mock.patch.object(snip.clipboard, "read_image", read_image),
    ):
        result = snip._snip_windows(timeout)
    return result, clock.t


class TestWindowsSnipCancel(unittest.TestCase):
    def test_cancel_is_detected_immediately(self):
        """覆盖层 0.4s 后消失、剪贴板始终没图 → 很快返回 None，而不是等到 10s 超时。"""
        result, elapsed = run_snip(overlay_until=0.4, timeout=10.0)
        self.assertIsNone(result)
        self.assertLess(elapsed, 2.0, "取消必须立刻判定，不能一直等到超时")

    def test_late_clipboard_image_is_not_claimed(self):
        """取消之后才出现的剪贴板图片（微信截图那次）绝不能算成本次截图。"""
        result, elapsed = run_snip(overlay_until=0.4, token_at=1.5, timeout=10.0)
        self.assertIsNone(result, "取消后的新剪贴板图片不该被当成框选结果")
        self.assertLess(elapsed, 2.0)

    def test_image_while_overlay_is_up_is_returned(self):
        """正常框选：覆盖层还在时剪贴板出图 → 返回图片（并走静置逻辑）。"""
        result, _elapsed = run_snip(overlay_until=5.0, token_at=1.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.size, (4, 4))

    def test_overlay_never_detected_falls_back_to_waiting(self):
        """观测不到覆盖层（老系统/类名不同）→ 退回旧行为：只等剪贴板，仍能拿到图。"""
        result, _elapsed = run_snip(overlay_until=-1.0, token_at=1.0)
        self.assertIsNotNone(result)

    def test_timeout_without_image_returns_none(self):
        result, elapsed = run_snip(overlay_until=-1.0, timeout=2.0)
        self.assertIsNone(result)
        self.assertGreaterEqual(elapsed, 2.0)

    def test_launch_failure_raises_unsupported(self):
        with (
            mock.patch.object(snip, "_launch_windows_snip", lambda: False),
            self.assertRaises(snip.SnipUnsupported),
        ):
            snip._snip_windows(1.0)


if __name__ == "__main__":
    unittest.main()
