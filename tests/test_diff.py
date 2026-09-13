"""帧间差异度量测试（纯逻辑，只依赖 Pillow）。

``wait_until_changed`` 的正确性全靠这里：阈值定错了，工具要么在第一次轮询就返回「变了」
（等于没用），要么永远等不到变化。所以把「相同 / 微变 / 实变 / 尺寸变化」四种情形钉死。
"""

import unittest

from PIL import Image

from cvision import diff


def flat(size=(64, 48), value=100):
    return Image.new("L", size, value)


class TestThumbnail(unittest.TestCase):
    def test_downsizes_long_side_only(self):
        thumb = diff.thumbnail(flat((640, 480)))
        self.assertEqual(max(thumb.size), diff.THUMB_MAX_SIDE)
        # 等比：640x480 -> 160x120
        self.assertEqual(thumb.size, (160, 120))

    def test_leaves_small_images_alone(self):
        self.assertEqual(diff.thumbnail(flat((80, 40))).size, (80, 40))

    def test_converts_to_grayscale(self):
        self.assertEqual(diff.thumbnail(Image.new("RGB", (10, 10))).mode, "L")

    def test_never_zero_sized(self):
        thumb = diff.thumbnail(flat((400, 3)))
        self.assertGreaterEqual(min(thumb.size), 1)


class TestDiffMetrics(unittest.TestCase):
    def test_identical_images_report_no_change(self):
        m = diff.diff_metrics(flat(), flat())
        self.assertEqual(m["diff_ratio"], 0.0)
        self.assertEqual(m["mean_diff"], 0.0)
        self.assertIsNone(m["bbox"])

    def test_small_change_is_measurable(self):
        """一个角上改一块：diff_ratio 应明显大于 0，且 bbox 落在改动区域。"""
        before = flat((100, 100))
        after = flat((100, 100))
        for x in range(0, 10):
            for y in range(0, 10):
                after.putpixel((x, y), 255)
        m = diff.diff_metrics(before, after)
        self.assertAlmostEqual(m["diff_ratio"], 100 / 10000, places=4)
        self.assertIsNotNone(m["bbox"])
        x, y, w, h = m["bbox"]
        self.assertLessEqual(x + w, 12)
        self.assertLessEqual(y + h, 12)

    def test_subpixel_noise_below_pixel_delta_is_ignored(self):
        """差值在 pixel_delta 以内的抖动（抗锯齿/压缩噪声）不该算变化。"""
        before = flat(value=100)
        after = flat(value=105)  # 差 5 < 13
        m = diff.diff_metrics(before, after, pixel_delta=13)
        self.assertEqual(m["diff_ratio"], 0.0)
        self.assertIsNone(m["bbox"])
        # 但平均差仍如实反映（调用方可以据此做更细的判断）
        self.assertAlmostEqual(m["mean_diff"], 5.0, places=3)

    def test_pixel_delta_boundary_is_exclusive(self):
        """差值恰好等于阈值不算变化（严格大于才算）。"""
        m = diff.diff_metrics(flat(value=100), flat(value=113), pixel_delta=13)
        self.assertEqual(m["diff_ratio"], 0.0)
        m2 = diff.diff_metrics(flat(value=100), flat(value=114), pixel_delta=13)
        self.assertEqual(m2["diff_ratio"], 1.0)

    def test_full_change(self):
        m = diff.diff_metrics(flat(value=0), flat(value=255))
        self.assertEqual(m["diff_ratio"], 1.0)
        self.assertEqual(m["bbox"], (0, 0, 64, 48))

    def test_size_mismatch_counts_as_changed(self):
        m = diff.diff_metrics(flat((10, 10)), flat((20, 10)))
        self.assertEqual(m["diff_ratio"], 1.0)
        self.assertIsNone(m["bbox"])

    def test_zero_sized_image_is_safe(self):
        m = diff.diff_metrics(Image.new("L", (0, 0)), Image.new("L", (0, 0)))
        self.assertEqual(m["diff_ratio"], 0.0)


class TestScaleBbox(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(diff.scale_bbox(None, (10, 10), (100, 100)))

    def test_scales_back_to_full_size(self):
        # 缩略图 10x10 里的 (1,1,2,2) → 原图 100x100 里约 (10,10,20,20)（含 ±2 余量）
        box = diff.scale_bbox((1, 1, 2, 2), (10, 10), (100, 100))
        self.assertEqual(box["x"], 8)
        self.assertEqual(box["y"], 8)
        self.assertEqual(box["w"], 24)
        self.assertEqual(box["h"], 24)

    def test_clamped_inside_full_size(self):
        box = diff.scale_bbox((0, 0, 10, 10), (10, 10), (50, 50))
        self.assertGreaterEqual(box["x"], 0)
        self.assertGreaterEqual(box["y"], 0)
        self.assertLessEqual(box["x"] + box["w"], 50)
        self.assertLessEqual(box["y"] + box["h"], 50)

    def test_zero_thumb_size_is_safe(self):
        self.assertIsNone(diff.scale_bbox((0, 0, 1, 1), (0, 0), (10, 10)))


if __name__ == "__main__":
    unittest.main()
