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
#: 0.8 是实测调的：资源管理器里同一格里 ``2026/9/23`` 与 ``2:17`` 之间隔着一个空格（约 0.67×行高），
#: 取 0.6 会把一行日期切成两半；而**列与列**之间的间距是它的 20 倍以上（实测 186px vs 7px），
#: 放到 0.8 完全够不到跨列，安全。
_JOIN_GAP_RATIO = 0.8
#: 允许的**负**间距上限（词框互相轻微重叠）占行高的比例。超过它就是 x 回退——
#: 两个词在水平方向上是反的，绝不属于同一个控件（见 group_words 的说明）。
_MAX_OVERLAP_RATIO = 0.2
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


#: 会被当作「OCR 符号碎片」丢掉的字符：分隔符与标点。
#: 刻意**不用**「非字母数字」这种通用判据——那会把 ``×``（关闭按钮）、``✓`` 这类真实可点的符号
#: 一起丢掉。这里只列那些几乎不可能独立成为控件的分隔符。
_SYMBOL_NOISE_CHARS = frozenset("\\/:;,.|-_=+*^~<>()[]{}\"'`!?·…、。，：；！？")


def is_noise_text(text: str) -> bool:
    """判断元素文本是否只是 OCR 碎片（纯分隔符、且很短）——这种不可能是点击目标。

    为什么值得滤掉：``max_elements`` 按「从上到下、从左到右」截取，一堆 ``/``、``:``、``|``
    碎片会把名额（以及模型的注意力）占满，真正能点的控件反而被挤出截断之外。
    判据刻意保守：**长度 > 2 一律保留**，且只认 :data:`_SYMBOL_NOISE_CHARS` 里那些字符。
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 2:
        return False
    return all(ch in _SYMBOL_NOISE_CHARS for ch in stripped)


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
    max_overlap = line_h * _MAX_OVERLAP_RATIO
    pad = line_h * _PAD_RATIO

    # 分两步排序：先聚成「行」，再在**行内**按左边界排序。这一步不能省——
    # 只按 (竖直中心, 左边界) 整体排序时，同一物理行里不同列的竖直中心常差 1~2px，排序结果
    # 就会出现 **x 回退**（先给右边那列、再给左边那列）。而合并判据原本写成 `gap <= join_gap`，
    # 对**负** gap 恒成立（-311 <= 5.4），于是「修改日期」列被硬并进「文件名」列，拼出
    # `23 2 17 / / ： scripts` 这种跨列碎片，其中心点正好落在两列之间的空隙上——点它等于点在空气里。
    # 实测一份 248 词的窗口里，这类错误合并有 30+ 次。
    parsed.sort(key=lambda item: (item[2] + item[4] / 2.0, item[1]))
    rows: list[list[tuple[str, int, int, int, int]]] = []
    for item in parsed:
        cy = item[2] + item[4] / 2.0
        if rows:
            head = rows[-1][0]
            head_cy = head[2] + head[4] / 2.0
            if abs(cy - head_cy) <= same_line:
                rows[-1].append(item)
                continue
        rows.append([item])
    ordered: list[tuple[str, int, int, int, int]] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda item: item[1]))

    groups: list[dict] = []
    for text, x, y, w, h in ordered:
        cy = y + h / 2.0
        if groups:
            last = groups[-1]
            last_cy = last["y"] + last["h"] / 2.0
            gap = x - (last["x"] + last["w"])
            # 右边界必须真的在右侧（允许轻微重叠）：`gap` 下限拦住 x 回退，
            # 上限拦住跨控件（两列/两个按钮之间）的误并。
            if abs(cy - last_cy) <= same_line and -max_overlap <= gap <= join_gap:
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
        if is_noise_text(group["text"]):
            continue  # 纯符号碎片：不可能是点击目标，别占 max_elements 的名额
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
