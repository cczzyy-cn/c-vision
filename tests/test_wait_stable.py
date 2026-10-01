"""``wait_until_stable`` 的判定逻辑（纯逻辑：注入假取帧函数，不碰真实桌面）。

为什么要单独钉：``wait_until_changed`` 的语义是「等它**开始**变」，而这里要的是「等它**变完**」——
两者的差别全在**连续**二字上。加载中的画面会一直动，动一次就必须重新计数；一旦把「总共安静过几次」
当成「连续安静」，一个中途卡顿的画面就会被误判成「已经加载完成」。
"""

import itertools
import unittest
from unittest import mock

from PIL import Image

from cvision import capturer


def frame(color: str) -> Image.Image:
    return Image.new("RGB", (60, 40), color)


class TestPollUntilStable(unittest.TestCase):
    def _run(self, frames, **overrides):
        iterator = iter(frames)
        kwargs = dict(interval=0.0, stable_samples=3, timeout=2.0, threshold=0.01, pixel_delta=8)
        kwargs.update(overrides)
        return capturer._poll_until_stable(lambda: next(iterator), **kwargs)

    def test_already_stable_returns_after_n_quiet_samples(self):
        """一直不动：采 1 + N 次就该下结论（不能白等到超时）。"""
        _, meta = self._run([frame("black")] * 10)
        self.assertTrue(meta["stable"])
        self.assertEqual(meta["stable_for"], 3)
        self.assertEqual(meta["samples"], 4, "初始帧 + 3 次比较")
        self.assertEqual(meta["diff_ratio"], 0.0)
        self.assertEqual(meta["max_diff_ratio"], 0.0)

    def test_a_change_resets_the_quiet_counter(self):
        """中途变一次 → 计数归零重来，这正是「连续」的意义。"""
        frames = [frame("black")] * 3 + [frame("white")] + [frame("black")] * 4
        _, meta = self._run(frames)
        self.assertTrue(meta["stable"])
        self.assertEqual(meta["stable_for"], 3)
        self.assertEqual(meta["samples"], 8, "变化之后必须重新攒够 3 次安静")
        self.assertEqual(meta["max_diff_ratio"], 1.0, "中间那次全屏变化要被记下来")

    def test_never_settling_times_out_and_says_so(self):
        """画面一直在动 → 超时返回 stable=False，并如实给出最大差异。"""
        frames = itertools.cycle([frame("black"), frame("white")])
        _, meta = self._run(frames, interval=0.001, timeout=0.05)
        self.assertFalse(meta["stable"])
        self.assertLess(meta["stable_for"], 3)
        self.assertEqual(meta["max_diff_ratio"], 1.0)
        self.assertGreater(meta["samples"], 3)

    def test_stable_samples_one_accepts_the_first_quiet_pair(self):
        _, meta = self._run([frame("black")] * 5, stable_samples=1)
        self.assertTrue(meta["stable"])
        self.assertEqual(meta["samples"], 2)

    def test_metrics_are_json_safe_numbers(self):
        """返回值要能被 JSON 序列化（CLI 直接 json.dumps 它）。"""
        _, meta = self._run([frame("black")] * 5)
        for key in ("stable", "samples", "elapsed_ms", "diff_ratio", "max_diff_ratio", "stable_for"):
            self.assertIn(key, meta, f"{key} 必须在返回值里（schema 与 CLI 都依赖它）")


BOX = {"x": 11, "y": 22, "width": 60, "height": 40}


class TestWaitGeometry(unittest.TestCase):
    """两个 ``wait_until_*`` 也要回报「这张图覆盖的屏幕矩形」（v0.2.33）。

    为什么值得单独钉：它们返回的是一张**新图**，而 ``click_at`` 是**按比例**点「最近一次抓取那张图」。
    不更新基准的话，模型「等它变完 → 按比例点结果区」这条最自然的链路会用**上一次 see** 的矩形换算，
    位置整体偏——而且不报错。

    另一个必须先钉住的时序：几何要在 ``fit_for_attachment`` **之前**用**原始帧**算。那一步会把图片
    缩小（附件限额），而矩形是屏幕坐标；用缩过的图去算，矩形会整体算小。
    """

    def test_stable_geometry_uses_raw_frame_not_fitted(self):
        raw = Image.new("RGB", (60, 40), "black")
        fitted = Image.new("RGB", (15, 10), "black")  # 假装附件限额把图缩小了
        box = {}
        with mock.patch.object(capturer, "_make_shooter", return_value=lambda: raw):
            with mock.patch.object(capturer.encoding, "fit_for_attachment", return_value=fitted):
                with mock.patch.object(capturer, "image_screen_frame", return_value=BOX) as geo:
                    img, _ = capturer.wait_until_stable(
                        region="0,0,60,40",
                        stable_samples=1,
                        interval_ms=0,
                        timeout_ms=50,
                        geometry_out=box,
                    )
        self.assertIs(img, fitted, "返回给模型的仍是缩放后的图")
        self.assertEqual(geo.call_args[0][0].size, raw.size, "几何必须基于**原始帧**（缩放前）")
        self.assertEqual(box, BOX, "geometry_out 要被填上")

    def test_changed_geometry_is_filled_when_it_changes(self):
        frames = iter([frame("black"), frame("white")])
        box = {}
        with mock.patch.object(capturer, "_make_shooter", return_value=lambda: next(frames)):
            with mock.patch.object(capturer, "image_screen_frame", return_value=BOX):
                _, meta = capturer.wait_until_changed(
                    interval_ms=0, timeout_ms=500, geometry_out=box
                )
        self.assertTrue(meta["changed"])
        self.assertEqual(box, BOX)

    def test_geometry_failure_does_not_break_the_capture(self):
        """几何算不出来（screen_info 挂了之类）不该让整次等待失败——调用方会据此清掉旧基准。"""
        box = {}
        with mock.patch.object(capturer, "_make_shooter", return_value=lambda: frame("black")):
            with mock.patch.object(capturer, "image_screen_frame", side_effect=RuntimeError("boom")):
                img, meta = capturer.wait_until_stable(
                    stable_samples=1, interval_ms=0, timeout_ms=50, geometry_out=box
                )
        self.assertTrue(meta["stable"], "抓图本身仍然成功")
        self.assertEqual(box, {}, "拿不到几何就给空 dict")


if __name__ == "__main__":
    unittest.main()
