"""可点击元素合并测试（纯逻辑 + 坐标换算，跨平台）。

``ocr`` 给词框，模型要的是「哪些东西能点、点哪」。这里钉死合并规则与坐标换算，
避免「按半个词点」或「点到控件外面」。
"""

import unittest

from cvision import coordinates as co
from cvision import ui_elements as ui


def word(text, x, y, w, h):
    return {"text": text, "x": x, "y": y, "w": w, "h": h}


class TestGroupWords(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(ui.group_words([]), [])
        self.assertEqual(ui.group_words(None), [])

    def test_merges_adjacent_words_on_same_line(self):
        """「取消」按钮的两个词应合成一个元素。"""
        elements = ui.group_words([word("取消", 100, 200, 40, 20), word("操作", 145, 200, 40, 20)])
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["text"], "取消 操作")
        self.assertEqual(elements[0]["word_count"], 2)
        self.assertEqual(elements[0]["box"], {"x": 100, "y": 200, "w": 85, "h": 20})

    def test_splits_on_large_horizontal_gap(self):
        """同一行但离得远（不同控件）不能被合并。"""
        elements = ui.group_words([word("确定", 100, 200, 40, 20), word("取消", 900, 200, 40, 20)])
        self.assertEqual(len(elements), 2)
        self.assertEqual([e["text"] for e in elements], ["确定", "取消"])

    def test_splits_on_different_lines(self):
        elements = ui.group_words([word("第一行", 10, 10, 60, 20), word("第二行", 10, 80, 60, 20)])
        self.assertEqual(len(elements), 2)

    def test_center_is_box_center(self):
        elements = ui.group_words([word("按钮", 100, 200, 60, 20)])
        self.assertEqual(elements[0]["center"], {"x": 130, "y": 210})

    def test_skips_malformed_and_blank_words(self):
        elements = ui.group_words(
            [
                word("好的", 10, 10, 20, 10),
                {"text": "缺字段", "x": 50},
                word("零宽", 80, 10, 0, 10),
                word("   ", 90, 10, 10, 10),
                {"x": 5, "y": 5, "w": 5, "h": 5},  # 无 text
            ]
        )
        self.assertEqual([e["text"] for e in elements], ["好的"])

    def test_sorted_top_to_bottom_then_left_to_right(self):
        elements = ui.group_words(
            [
                word("下", 10, 100, 20, 20),
                word("右上", 200, 10, 40, 20),
                word("左上", 10, 10, 40, 20),
            ]
        )
        self.assertEqual([e["text"] for e in elements], ["左上", "右上", "下"])

    def test_cjk_without_spaces_still_merges(self):
        """连续中文（OCR 按词切）也要合成一句。"""
        elements = ui.group_words(
            [word("保存", 0, 0, 30, 16), word("并", 32, 0, 16, 16), word("关闭", 50, 0, 30, 16)]
        )
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["text"], "保存 并 关闭")


class TestToScreenElements(unittest.TestCase):
    def test_adds_screen_center_with_origin(self):
        elements = ui.group_words([word("确定", 100, 200, 60, 20)])
        to_screen = co.make_mapper(1000, 800, None, (300, 150))
        out = ui.to_screen_elements(elements, to_screen, (1000, 800))
        # 元素中心 (130, 210) → 屏幕 (430, 360)
        self.assertEqual(out[0]["screen_center"], {"x": 430, "y": 360})
        # 图片坐标字段保留，便于对照
        self.assertEqual(out[0]["center"], {"x": 130, "y": 210})

    def test_padding_expands_box_but_keeps_center(self):
        """外扩是为了让中心更稳地落在控件内部，中心点不应因此偏移。"""
        elements = ui.group_words([word("确定", 100, 200, 60, 20)])
        to_screen = co.make_mapper(1000, 800, None, (0, 0))
        out = ui.to_screen_elements(elements, to_screen, (1000, 800))
        box = out[0]["screen_box"]
        self.assertGreater(box["w"], 60)
        self.assertGreater(box["h"], 20)

    def test_padding_is_clamped_inside_image(self):
        """贴边元素的 padding 不能算到图外（否则可能点到别的显示器/窗口上）。"""
        elements = ui.group_words([word("角", 0, 0, 20, 20)])
        to_screen = co.make_mapper(100, 100, None, (0, 0))
        out = ui.to_screen_elements(elements, to_screen, (100, 100))
        screen_box = out[0]["screen_box"]
        self.assertGreaterEqual(screen_box["x"], 0)
        self.assertGreaterEqual(screen_box["y"], 0)
        self.assertLessEqual(screen_box["x"] + screen_box["w"], 100)
        self.assertLessEqual(screen_box["y"] + screen_box["h"], 100)

    def test_scaled_screen(self):
        screen = {"width": 1920, "height": 1080, "scale": 1.5}
        elements = ui.group_words([word("按钮", 100, 100, 60, 20)])
        to_screen = co.make_mapper(1280, 720, screen, (0, 0))
        out = ui.to_screen_elements(elements, to_screen, (1280, 720))
        self.assertEqual(out[0]["screen_center"], {"x": 195, "y": 165})


class TestNearest(unittest.TestCase):
    def test_picks_closest_by_screen_center(self):
        elements = [
            {"text": "a", "screen_center": {"x": 100, "y": 100}},
            {"text": "b", "screen_center": {"x": 500, "y": 500}},
        ]
        self.assertEqual(ui.nearest(elements, 480, 490)["text"], "b")
        self.assertEqual(ui.nearest(elements, 10, 10)["text"], "a")

    def test_none_when_empty(self):
        self.assertIsNone(ui.nearest([], 0, 0))


if __name__ == "__main__":
    unittest.main()
