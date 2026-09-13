"""把 OCR 词框合并成**可点击的 UI 元素**（纯逻辑，不依赖平台与第三方库）。

为什么需要它：``ocr`` 给的是**词**级边界框（``"登录"`` 和 ``"取消"`` 各一个框），而模型要的是
「屏幕上哪些东西可以点、每个的点击中心在哪」。逐词给框有两个问题：

1. 点击目标通常是**一个控件**（按钮/标签/菜单项），不是一个词——按词点容易点偏；
2. 词框往往**紧贴文字**，比真实可点区域小，中心点可能落在控件的边缘或外部。

本模块做两件事：把同一行、水平相邻的词**合并成一个元素**；再给元素外扩一点 padding，
让中心点更稳地落在控件内部。合并结果带上**屏幕绝对坐标**（由 ``coordinates`` 换算），
可直接传给 ``click``。

纯函数、无 I/O，因此可跨平台单测。
"""

from __future__ import annotations

from typing import Callable, Iterable

#: 同一行的判定阈值：两词竖直中心的距离小于「行高 × 该系数」即视为同一行。
_SAME_LINE_RATIO = 0.5
#: 相邻词的合并阈值：水平间距小于「行高 × 该系数」即视为同一个控件。
_JOIN_GAP_RATIO = 0.6
#: 元素四周外扩量占行高的比例（让中心点落进控件内部而不是文字边缘）。
_PAD_RATIO = 0.2

Box = dict


def _bbox(word: Box) -> tuple[int, int, int, int] | None:
    """取词框 ``(x, y, w, h)``；缺字段或宽高非正时返回 None（该词被跳过）。"""
    try:
        x, y = int(word["x"]), int(word["y"])
        w, h = int(word["w"]), int(word["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def group_words(words: Iterable[Box]) -> list[dict]:
    """把词框按「同一行 + 水平相邻」合并成元素。

    :param words: ``[{"text","x","y","w","h"}, ...]``（``ocr`` 的 ``words``）。
    :returns: ``[{"text", "box", "center", "word_count"}, ...]``，按从上到下、从左到右排序。
        拿不到有效词框的词会被静默跳过（OCR 偶发脏数据不该让整个工具失败）。
    """
    parsed: list[tuple[str, int, int, int, int]] = []
    for word in words or []:
        box = _bbox(word)
        if box is None:
            continue
        text = str(word.get("text", "") or "").strip()
        if not text:
            continue
        parsed.append((text, *box))
    if not parsed:
        return []

    heights = [h for _, _, _, _, h in parsed]
    line_h = _median(heights) or 1.0
    same_line = line_h * _SAME_LINE_RATIO
    join_gap = line_h * _JOIN_GAP_RATIO
    pad = line_h * _PAD_RATIO

    # 先按 (竖直中心, 左边界) 排序，让同一行的词在序列里相邻。
    parsed.sort(key=lambda item: (item[2] + item[4] / 2.0, item[1]))

    groups: list[dict] = []
    for text, x, y, w, h in parsed:
        cy = y + h / 2.0
        if groups:
            last = groups[-1]
            last_cy = last["y"] + last["h"] / 2.0
            gap = x - (last["x"] + last["w"])
            if abs(cy - last_cy) <= same_line and gap <= join_gap:
                # 并入上一个元素：扩框、拼文本。
                left = min(last["x"], x)
                top = min(last["y"], y)
                right = max(last["x"] + last["w"], x + w)
                bottom = max(last["y"] + last["h"], y + h)
                last.update(
                    x=left, y=top, w=right - left, h=bottom - top,
                    text=(last["text"] + " " + text).strip(),
                    word_count=last["word_count"] + 1,
                )
                continue
        groups.append({"text": text, "x": x, "y": y, "w": w, "h": h, "word_count": 1})

    results: list[dict] = []
    for group in groups:
        x, y, w, h = group["x"], group["y"], group["w"], group["h"]
        cx = x + w / 2.0
        cy = y + h / 2.0
        results.append(
            {
                "text": group["text"],
                "box": {"x": x, "y": y, "w": w, "h": h},
                "center": {"x": int(round(cx)), "y": int(round(cy))},
                "word_count": group["word_count"],
            }
        )
    results.sort(key=lambda item: (item["box"]["y"], item["box"]["x"]))
    return results


def to_screen_elements(
    elements: list[dict],
    to_screen: Callable[[float, float], tuple[int, int]],
    screen_size: tuple[int, int] | None = None,
) -> list[dict]:
    """把元素的图片坐标换算成屏幕绝对坐标，并外扩一点 padding。

    :param elements: :func:`group_words` 的结果。
    :param to_screen: ``coordinates.make_mapper()`` 产出的映射函数。
    :param screen_size: 图片尺寸 ``(w, h)``，用于把外扩后的框夹回图内（避免算出图外的点）。
    :returns: 每项在图片坐标字段之外，追加 ``screen_center``（**可直接 click**）、``screen_box``。
    """
    out: list[dict] = []
    for element in elements:
        box = element["box"]
        x, y, w, h = box["x"], box["y"], box["w"], box["h"]
        pad = max(1.0, h * _PAD_RATIO)
        left, top = x - pad, y - pad
        right, bottom = x + w + pad, y + h + pad
        if screen_size:
            left = max(0.0, left)
            top = max(0.0, top)
            right = min(float(screen_size[0]), right)
            bottom = min(float(screen_size[1]), bottom)

        sx1, sy1 = to_screen(left, top)
        sx2, sy2 = to_screen(right, bottom)
        screen_cx, screen_cy = to_screen((left + right) / 2.0, (top + bottom) / 2.0)
        enriched = dict(element)
        enriched["screen_box"] = {"x": sx1, "y": sy1, "w": max(1, sx2 - sx1), "h": max(1, sy2 - sy1)}
        enriched["screen_center"] = {"x": screen_cx, "y": screen_cy}
        out.append(enriched)
    return out


def nearest(elements: list[dict], x: int, y: int, key: str = "screen_center") -> dict | None:
    """找出离 ``(x, y)`` 最近的元素（用于「这个坐标附近有什么可点」）。"""
    best = None
    best_d = None
    for element in elements:
        point = element.get(key) or element.get("center")
        if not point:
            continue
        dx = float(point["x"]) - x
        dy = float(point["y"]) - y
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = element, d
    return best
