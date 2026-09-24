"""坐标换算测试（纯逻辑，跨平台、不碰桌面）。

这是 computer-use 里最容易算错的一环：``ocr`` 给的是**图片像素**，``click`` 要的是**屏幕绝对坐标**。
错一点点就会点偏，所以把每种偏移与缩放场景都钉死。
"""

import unittest

from cvision import coordinates as co


class TestImageScreenBox(unittest.TestCase):
    """图片覆盖的屏幕矩形 —— 「按比例点击」的换算基准。

    为什么要它：模型读不准图片像素（它看到的预览被 DSH 按图片 token 规则缩过：1920×1080 的整屏
    截图到模型手里只剩 1708×961），但读得准**比例**。只要矩形对，``(rx, ry) ∈ [0,1]`` 换算出的
    屏幕坐标就与图片被缩放到多少像素无关，也就不必去复现 provider 那套会随版本漂移的规则。
    """

    def test_identity_geometry(self):
        self.assertEqual(
            co.image_screen_box(1000, 800, (300, 150), 1.0),
            {"x": 300, "y": 150, "width": 1000, "height": 800},
        )

    def test_applies_scale(self):
        """图片是逻辑像素时，矩形要按 scale 还原成屏幕物理像素。"""
        self.assertEqual(
            co.image_screen_box(1280, 720, (0, 0), 1.5),
            {"x": 0, "y": 0, "width": 1920, "height": 1080},
        )

    def test_matches_screen_center_of_a_known_element(self):
        """比例换算与 elements 的 screen_center 必须落在同一套坐标里。

        用 see 的真实几何（窗口 left=304/top=159，截图 1353×782，scale=1）：
        图片中心 (0.5, 0.5) 应等于该图中心元素的 screen_center —— 元素中心是
        ``origin + box.center``，而 box.center 正好是图的一半。
        """
        box = co.image_screen_box(1353, 782, (304, 159), 1.0)
        to_screen = co.make_mapper(1353, 782, None, (304, 159))
        element_center = to_screen(1353 / 2, 782 / 2)
        # 两边都要先取整：make_mapper 内部是 int(round(...))，而图片宽是奇数时 0.5*width 会落在 .5 上。
        ratio_point = (
            round(box["x"] + 0.5 * box["width"]),
            round(box["y"] + 0.5 * box["height"]),
        )
        self.assertEqual(ratio_point, element_center)

    def test_negative_origin_for_multi_screen(self):
        """副屏挂在主屏左侧时原点是负数，矩形必须如实反映（否则会点到主屏上）。"""
        self.assertEqual(co.image_screen_box(1920, 1080, (-1920, 0), 1.0)["x"], -1920)

    def test_never_collapses_to_zero_size(self):
        """极端缩放下宽高也不能变成 0（否则换算会除零/退化成一个点）。"""
        box = co.image_screen_box(1, 1, (0, 0), 0.1)
        self.assertGreaterEqual(box["width"], 1)
        self.assertGreaterEqual(box["height"], 1)


class TestResolveScale(unittest.TestCase):
    def test_identity_when_process_is_dpi_aware(self):
        """进程 DPI 感知时图片尺寸 == 显示器物理尺寸，即使系统缩放 150% 也不该再乘 scale。"""
        screen = {"width": 1920, "height": 1080, "scale": 1.5}
        self.assertEqual(co.resolve_scale(1920, 1080, screen), 1.0)

    def test_uses_scale_when_image_is_logical_pixels(self):
        """图片是逻辑像素（1920/1.5=1280）时才用 scale 校正。"""
        screen = {"width": 1920, "height": 1080, "scale": 1.5}
        self.assertEqual(co.resolve_scale(1280, 720, screen), 1.5)

    def test_plain_100_percent(self):
        screen = {"width": 2560, "height": 1440, "scale": 1}
        self.assertEqual(co.resolve_scale(2560, 1440, screen), 1.0)

    def test_no_screen_info(self):
        self.assertEqual(co.resolve_scale(800, 600, None), 1.0)

    def test_absurd_scale_is_rejected(self):
        """探测到的 scale 离谱时退回 1.0，宁可 1:1 也不要按荒谬比例缩放。"""
        for bad in (0, -1, 0.01, 99, "abc", None):
            self.assertEqual(co.resolve_scale(1000, 800, {"width": 1000, "height": 800, "scale": bad}), 1.0)

    def test_missing_size_falls_back_to_reported_scale(self):
        self.assertEqual(co.resolve_scale(0, 0, {"scale": 2.0}), 2.0)


class TestMakeMapper(unittest.TestCase):
    def test_origin_only(self):
        to_screen = co.make_mapper(1000, 800, None, (100, 50))
        self.assertEqual(to_screen(0, 0), (100, 50))
        self.assertEqual(to_screen(10, 20), (110, 70))

    def test_negative_origin(self):
        """副屏挂在主屏左侧时坐标可以是负数。"""
        to_screen = co.make_mapper(1000, 800, None, (-1920, 0))
        self.assertEqual(to_screen(0, 0), (-1920, 0))
        self.assertEqual(to_screen(100, 100), (-1820, 100))

    def test_scaled_image(self):
        screen = {"width": 1920, "height": 1080, "scale": 1.5}
        to_screen = co.make_mapper(1280, 720, screen, (0, 0))
        # 图片是逻辑像素：乘 1.5 后回到物理坐标。
        self.assertEqual(to_screen(100, 100), (150, 150))

    def test_rounding_is_stable(self):
        to_screen = co.make_mapper(100, 100, None, (0, 0))
        self.assertEqual(to_screen(0.4, 0.6), (0, 1))
        self.assertEqual(to_screen(1.5, 2.5), (2, 2))  # Python round 的 banker's rounding


class TestCaptureOrigin(unittest.TestCase):
    def test_full_screen_primary(self):
        screens = [{"x": 0, "y": 0, "width": 2560, "height": 1440, "primary": True}]
        self.assertEqual(co.capture_origin(None, None, screens), (0, 0))

    def test_multi_screen_uses_virtual_desktop_origin(self):
        """整屏捕获覆盖整个虚拟桌面，原点是所有屏的最小 (x, y)。"""
        screens = [
            {"x": 0, "y": 0, "width": 1920, "height": 1080, "primary": True},
            {"x": -1280, "y": -200, "width": 1280, "height": 1024},
        ]
        self.assertEqual(co.capture_origin(None, None, screens), (-1280, -200))

    def test_window_uses_window_position(self):
        window = {"left": 300, "top": 150}
        self.assertEqual(co.capture_origin(None, window, None), (300, 150))

    def test_region_offsets_within_image(self):
        screens = [{"x": 0, "y": 0, "width": 1000, "height": 800, "primary": True}]
        self.assertEqual(co.capture_origin("100,50,200,100", None, screens), (100, 50))

    def test_window_plus_region_accumulates(self):
        """窗口 + region：region 是相对窗口图的，所以两个偏移要相加。"""
        window = {"left": 300, "top": 150}
        self.assertEqual(co.capture_origin("10,20,30,40", window, None), (310, 170))

    def test_malformed_region_ignored(self):
        self.assertEqual(co.capture_origin("abc", None, [{"x": 5, "y": 6}]), (5, 6))


class TestScreenForImage(unittest.TestCase):
    def test_picks_monitor_containing_origin(self):
        screens = [
            {"x": 0, "y": 0, "width": 1920, "height": 1080, "primary": True, "scale": 1.0},
            {"x": -1280, "y": 0, "width": 1280, "height": 1024, "scale": 2.0},
        ]
        self.assertEqual(co.screen_for_image(100, 100, (-1280, 0), screens)["scale"], 2.0)
        self.assertEqual(co.screen_for_image(100, 100, (0, 0), screens)["scale"], 1.0)

    def test_falls_back_to_primary(self):
        screens = [{"x": 0, "y": 0, "width": 10, "height": 10, "primary": True}]
        self.assertEqual(co.screen_for_image(5000, 5000, (9999, 9999), screens)["primary"], True)

    def test_no_screens(self):
        self.assertIsNone(co.screen_for_image(10, 10, (0, 0), None))


if __name__ == "__main__":
    unittest.main()
