"""``wait_until_stable`` 的判定逻辑（纯逻辑：注入假取帧函数，不碰真实桌面）。

为什么要单独钉：``wait_until_changed`` 的语义是「等它**开始**变」，而这里要的是「等它**变完**」——
两者的差别全在**连续**二字上。加载中的画面会一直动，动一次就必须重新计数；一旦把「总共安静过几次」
当成「连续安静」，一个中途卡顿的画面就会被误判成「已经加载完成」。
"""

import itertools
import unittest

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


if __name__ == "__main__":
    unittest.main()
