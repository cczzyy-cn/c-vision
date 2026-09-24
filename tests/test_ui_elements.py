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

    def test_out_of_order_words_on_same_line_are_sorted_left_to_right(self):
        """同一行里 OCR 词序可能是「先右后左」，合并前必须按 x 重排。

        回归的 bug（实测一份 248 词的窗口里出现 30+ 次）：只按 (竖直中心, 左边界) 排序时，
        两列竖直中心差 1~2px 就会让**右边**那列排在前面；而合并判据原本是 `gap <= join_gap`，
        对**负** gap 恒成立（-311 ≤ 5.4），于是「修改日期」列被硬并进「文件名」列，拼出
        `23 2 17 / / ： scripts` 这种跨列碎片——它的中心点正好落在两列之间的空隙上。
        """
        elements = ui.group_words(
            [
                word("2026", 300, 100, 40, 10),      # 右边那列，词框略矮 → 竖直中心更小，会排到前面
                word("README.md", 10, 100, 90, 12),  # 左边那列
            ]
        )
        self.assertEqual([e["text"] for e in elements], ["README.md", "2026"])
        self.assertEqual(len(elements), 2, "x 回退的词不能被合并")

    def test_negative_gap_never_merges(self):
        """新词落回上一个元素的左侧时，绝不允许合并（否则元素中心会落到两个控件之间）。"""
        elements = ui.group_words([word("右边", 500, 50, 40, 20), word("左边", 100, 50, 40, 20)])
        self.assertEqual([e["text"] for e in elements], ["左边", "右边"])

    def test_slight_overlap_still_merges(self):
        """词框轻微重叠是正常的（OCR 框比字略宽），不能因此被切成两个元素。"""
        elements = ui.group_words([word("保存", 100, 200, 44, 20), word("并", 140, 200, 16, 20)])
        self.assertEqual(len(elements), 1)

    def test_symbol_only_fragments_are_dropped(self):
        """纯分隔符碎片不可能被点击，不该占 max_elements 的名额。"""
        elements = ui.group_words(
            [
                word("确定", 10, 10, 40, 20),
                word("/", 200, 10, 8, 20),
                word(":", 240, 10, 6, 20),
                word("--", 280, 10, 14, 20),
            ]
        )
        self.assertEqual([e["text"] for e in elements], ["确定"])

    def test_short_but_clickable_symbols_are_kept(self):
        """`×`（关闭）、`下`、`OK` 这类短文本必须保留 —— 它们是真控件。"""
        elements = ui.group_words(
            [word("×", 10, 10, 12, 20), word("OK", 100, 10, 24, 20), word("下", 200, 10, 14, 20)]
        )
        self.assertEqual([e["text"] for e in elements], ["×", "OK", "下"])


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
