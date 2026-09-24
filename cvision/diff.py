"""帧间差异度量（纯逻辑，只依赖 Pillow）。

用途：``wait_until_changed`` 要反复截图直到「画面真的变了」。判断必须廉价且稳：
- **廉价**：比较在灰度缩略图上做，不逐像素比 2560×1440 原图；
- **稳**：单个光标移动或抗锯齿抖动不该被当成「变化」，所以用「差异明显的像素占比」而不是
  「有任何像素不同」。

缩略图还会顺手抹掉「只有几个像素变了」的噪声（缩图后那几个像素被平均掉了）。
"""

from __future__ import annotations

from PIL import Image, ImageDraw

#: 判定「这个像素变了」的单通道差值阈值（0-255）。低于它算噪声。
PIXEL_DELTA = 8

#: 缩略图长边上限：比较在这个尺寸上进行。
THUMB_MAX_SIDE = 160

#: 变化聚类的网格边长（缩略图像素）。格子越小框越精确，但碎框也越多。
CLUSTER_GRID = 8

#: 变化聚类最多返回几个区域（按变化像素数从多到少）。
CLUSTER_MAX = 5

#: 高亮框的颜色与线宽。
HIGHLIGHT_COLOR = (255, 64, 64)
HIGHLIGHT_WIDTH = 3


def thumbnail(img: Image.Image, max_side: int = THUMB_MAX_SIDE) -> Image.Image:
    """把图缩成灰度缩略图，供廉价比较（不等比缩放带来的轻微模糊本身就是抗噪）。"""
    grey = img.convert("L")
    w, h = grey.size
    longest = max(w, h)
    if longest <= max_side:
        return grey
    scale = max_side / float(longest)
    return grey.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)


def diff_metrics(before: Image.Image, after: Image.Image, pixel_delta: int = PIXEL_DELTA) -> dict:
    """比较两张**缩略图**，返回 ``{diff_ratio, mean_diff, bbox}``。

    :param before: 上一帧的缩略图（:func:`thumbnail` 的产物）。
    :param after: 当前帧的缩略图。
    :param pixel_delta: 单像素算「变了」的差值阈值。
    :returns: ``diff_ratio`` = 差异明显像素的占比（0~1）；``mean_diff`` = 平均绝对差（0~255）；
        ``bbox`` = 变化区域在原图**原尺寸**下的 ``(x, y, w, h)``，无变化时为 None。
    """
    if before.size != after.size:
        # 尺寸都变了（换分辨率/换窗口大小）——必然是变化，且无法给出有意义的 bbox。
        return {"diff_ratio": 1.0, "mean_diff": 255.0, "bbox": None}

    w, h = before.size
    if w == 0 or h == 0:
        return {"diff_ratio": 0.0, "mean_diff": 0.0, "bbox": None}

    b_px = list(before.getdata())
    a_px = list(after.getdata())
    total = len(b_px)
    changed = 0
    diff_sum = 0
    min_x, min_y, max_x, max_y = w, h, -1, -1
    for index, (old, new) in enumerate(zip(b_px, a_px)):
        delta = abs(new - old)
        diff_sum += delta
        if delta > pixel_delta:
            changed += 1
            x = index % w
            y = index // w
            if x < min_x:
                min_x = x
            if y < min_y:
                min_y = y
            if x > max_x:
                max_x = x
            if y > max_y:
                max_y = y

    if changed == 0:
        return {"diff_ratio": 0.0, "mean_diff": diff_sum / float(total), "bbox": None}
    return {
        "diff_ratio": changed / float(total),
        "mean_diff": diff_sum / float(total),
        "bbox": (min_x, min_y, max_x - min_x + 1, max_y - min_y + 1),
    }


def change_clusters(
    before: Image.Image,
    after: Image.Image,
    pixel_delta: int = PIXEL_DELTA,
    grid: int = CLUSTER_GRID,
    max_clusters: int = CLUSTER_MAX,
) -> list[tuple[int, int, int, int]]:
    """把变化像素**聚类成若干矩形**（缩略图坐标），按变化量从多到少返回。

    为什么需要它：:func:`diff_metrics` 给的 ``bbox`` 是包裹**全部**变化像素的最小矩形。两处同时变
    （左上弹提示、右下刷列表）时，那个框会横跨整屏——数值上没错，信息上等于零。这里改成按 8 邻域
    连通性聚类，分开的改动就分别成框。

    做法是**在网格上**聚类而不是逐像素：先把「有变化的格子」标出来（格子边长 ``grid``），再对格子做
    连通分量。这样既快又天然抗噪（零散的单像素变化落不满格子边缘，容易并进邻域）。

    :param grid: 网格边长（缩略图像素）。
    :param max_clusters: 最多返回几个区域；超出的丢弃（它们的变化量更小）。
    :returns: ``[(x, y, w, h), ...]``，缩略图坐标；无变化时返回空列表。
    """
    if before.size != after.size:
        return []
    width, height = before.size
    if width == 0 or height == 0:
        return []

    cells: dict[tuple[int, int], int] = {}
    before_px = list(before.getdata())
    after_px = list(after.getdata())
    for index, (old, new) in enumerate(zip(before_px, after_px)):
        if abs(new - old) > pixel_delta:
            cell = ((index % width) // grid, (index // width) // grid)
            cells[cell] = cells.get(cell, 0) + 1
    if not cells:
        return []

    seen: set[tuple[int, int]] = set()
    groups: list[list[tuple[int, int]]] = []
    for start in cells:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        members: list[tuple[int, int]] = []
        while stack:
            current = stack.pop()
            members.append(current)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (current[0] + dx, current[1] + dy)
                    if neighbour in cells and neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
        groups.append(members)

    scored = []
    for members in groups:
        xs = [member[0] for member in members]
        ys = [member[1] for member in members]
        x0, y0 = min(xs) * grid, min(ys) * grid
        x1 = min(width, (max(xs) + 1) * grid)
        y1 = min(height, (max(ys) + 1) * grid)
        weight = sum(cells[member] for member in members)
        scored.append((weight, (x0, y0, x1 - x0, y1 - y0)))
    scored.sort(key=lambda item: -item[0])
    return [box for _, box in scored[: max(1, int(max_clusters))]]


def scale_boxes(
    boxes: list[tuple[int, int, int, int]],
    thumb_size: tuple[int, int],
    full_size: tuple[int, int],
) -> list[dict]:
    """把一批缩略图坐标的 bbox 一起换算回原图尺寸（逐个走 :func:`scale_bbox`）。"""
    scaled = []
    for box in boxes:
        converted = scale_bbox(box, thumb_size, full_size)
        if converted:
            scaled.append(converted)
    return scaled


def highlight_boxes(
    image: Image.Image,
    boxes: list[dict],
    color: tuple[int, int, int] = HIGHLIGHT_COLOR,
    width: int = HIGHLIGHT_WIDTH,
) -> Image.Image:
    """在图上把变化区域**框红**，返回新图（不改原图）。

    给模型看「哪里变了」比给一串 ``(x, y, w, h)`` 可靠得多——它能直接看图，却读不准像素。
    框线只有几像素宽，不会挡住内容。
    """
    if not boxes:
        return image
    out = image.copy()
    if out.mode != "RGB":
        # 灰度/调色板图不能直接画 RGB 颜色（PIL 会抛 TypeError）。实际抓屏是 RGB，
        # 但这个函数不该因此就只能在某一种模式上工作。
        out = out.convert("RGB")
    draw = ImageDraw.Draw(out)
    for box in boxes:
        x, y = int(box["x"]), int(box["y"])
        w, h = max(1, int(box["w"])), max(1, int(box["h"]))
        draw.rectangle([x, y, x + w - 1, y + h - 1], outline=color, width=max(1, int(width)))
    return out


def scale_bbox(bbox: tuple[int, int, int, int] | None, thumb_size: tuple[int, int], full_size: tuple[int, int]):
    """把缩略图坐标的 bbox 换算回原图尺寸（再加一点余量，避免刚好框在边缘）。"""
    if bbox is None:
        return None
    tw, th = thumb_size
    fw, fh = full_size
    if tw == 0 or th == 0:
        return None
    sx = fw / float(tw)
    sy = fh / float(th)
    x, y, w, h = bbox
    nx = max(0, int(x * sx) - 2)
    ny = max(0, int(y * sy) - 2)
    nw = min(fw - nx, int(w * sx) + 4)
    nh = min(fh - ny, int(h * sy) + 4)
    return {"x": nx, "y": ny, "w": max(1, nw), "h": max(1, nh)}
