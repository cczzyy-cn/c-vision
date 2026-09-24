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


class TestChangeClusters(unittest.TestCase):
    """变化区域聚类：**分开的**改动必须分别成框。

    这是 ``diff_bbox`` 单框缺陷的补丁——它包裹的是**全部**变化像素，两处同时变时那个框会横跨
    整屏（数值没错，信息量为零）。聚类之后，分开的改动才各得其所。
    """

    def _with_blocks(self, blocks, size=(160, 120)):
        before = flat(size, 0)
        after = before.copy()
        for ox, oy, side in blocks:
            for x in range(ox, ox + side):
                for y in range(oy, oy + side):
                    after.putpixel((x, y), 255)
        return before, after

    def test_two_separate_changes_become_two_boxes(self):
        before, after = self._with_blocks([(0, 0, 16), (144, 104, 16)])
        boxes = diff.change_clusters(before, after)
        self.assertEqual(len(boxes), 2, "两处相距很远的变化必须各成一框")
        # 一框在左上、一框在右下（顺序按变化量排，这里量相同，所以按位置判定）
        left = min(boxes, key=lambda b: b[0])
        right = max(boxes, key=lambda b: b[0])
        self.assertLess(left[0], 32)
        self.assertGreater(right[0], 128)

    def test_a_lone_change_is_covered_by_its_box(self):
        before, after = self._with_blocks([(40, 40, 16)])
        boxes = diff.change_clusters(before, after)
        self.assertEqual(len(boxes), 1)
        x, y, w, h = boxes[0]
        self.assertLessEqual(x, 40)
        self.assertLessEqual(y, 40)
        self.assertGreaterEqual(x + w, 56)
        self.assertGreaterEqual(y + h, 56)

    def test_no_change_gives_no_boxes(self):
        self.assertEqual(diff.change_clusters(flat(), flat()), [])

    def test_size_mismatch_is_not_clustered(self):
        """尺寸都变了（换分辨率/换窗口大小）时给不出有意义的框，返回空让调用方用整图。"""
        self.assertEqual(diff.change_clusters(flat((10, 10)), flat((20, 20))), [])

    def test_max_clusters_caps_the_result_but_keeps_the_biggest(self):
        blocks = [((i % 3) * 56, (i // 3) * 56, 8) for i in range(6)]
        before, after = self._with_blocks(blocks)
        self.assertEqual(len(diff.change_clusters(before, after, max_clusters=10)), 6, "放开上限应得到 6 个独立区域")
        self.assertEqual(len(diff.change_clusters(before, after, max_clusters=3)), 3, "上限要真的生效")
        # 默认上限就是 CLUSTER_MAX：碎块再多也不该无边界地塞给模型。
        self.assertEqual(len(diff.change_clusters(before, after)), diff.CLUSTER_MAX)


class TestScaleBoxes(unittest.TestCase):
    def test_scales_every_box(self):
        out = diff.scale_boxes([(8, 8, 8, 8), (16, 16, 8, 8)], (160, 120), (1600, 1200))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["x"], 78, "8 缩略图像素 × 10 倍，再减 2px 余量")

    def test_empty_input_stays_empty(self):
        self.assertEqual(diff.scale_boxes([], (160, 120), (1600, 1200)), [])


class TestHighlightBoxes(unittest.TestCase):
    def test_draws_onto_a_copy_and_leaves_the_original_alone(self):
        img = flat((100, 80), 0)
        out = diff.highlight_boxes(img, [{"x": 10, "y": 10, "w": 20, "h": 20}])
        self.assertIsNot(out, img, "必须返回新图")
        self.assertEqual(img.getpixel((10, 10)), 0, "原图不能被改动")
        self.assertNotEqual(out.getpixel((10, 10)), 0, "框线要真的画上去")

    def test_box_is_closed_on_all_four_sides(self):
        img = flat((100, 80), 0)
        out = diff.highlight_boxes(img, [{"x": 10, "y": 10, "w": 20, "h": 20}], width=1)
        for point in ((10, 10), (29, 10), (10, 29), (29, 29)):
            self.assertNotEqual(out.getpixel(point), 0, f"四角 {point} 都该有框线")

    def test_no_boxes_returns_the_original_untouched(self):
        img = flat()
        self.assertIs(diff.highlight_boxes(img, []), img)


if __name__ == "__main__":
    unittest.main()
