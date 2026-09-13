"""帧间差异度量（纯逻辑，只依赖 Pillow）。

用途：``wait_until_changed`` 要反复截图直到「画面真的变了」。判断必须廉价且稳：
- **廉价**：比较在灰度缩略图上做，不逐像素比 2560×1440 原图；
- **稳**：单个光标移动或抗锯齿抖动不该被当成「变化」，所以用「差异明显的像素占比」而不是
  「有任何像素不同」。

缩略图还会顺手抹掉「只有几个像素变了」的噪声（缩图后那几个像素被平均掉了）。
"""

from __future__ import annotations

from PIL import Image

#: 判定「这个像素变了」的单通道差值阈值（0-255）。低于它算噪声。
PIXEL_DELTA = 8

#: 缩略图长边上限：比较在这个尺寸上进行。
THUMB_MAX_SIDE = 160


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
