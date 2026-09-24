"""图片像素坐标 → 屏幕绝对坐标（供 computer-use 精确定位点击点）。

为什么需要它：``ocr`` 返回的词框是**相对被识别图片**的像素坐标，而 ``click`` 吃的是**屏幕绝对
坐标**。两者之间差了三件事，模型很容易算错：

1. **裁剪偏移**：``region="x,y,w,h"`` 只截了一块，词框是相对那一块的；
2. **窗口/多屏偏移**：``see(window=...)`` 截的是窗口，图片原点不等于屏幕原点；多屏时图片原点
   可能是虚拟桌面的左上角（负数）；
3. **DPI 缩放**：Windows 显示缩放不是 100% 时，捕获到的图片尺寸与显示器逻辑尺寸不一致。

本模块把这层换算固定成代码——**返回可直接传给 ``click`` 的坐标**，而不是每次都指望模型自己折算。

约定：坐标一律是**物理像素**（Windows 下进程已 ``SetProcessDpiAwareness``，API 返回的就是物理像素），
与 ``screen_info()`` 的 ``x/y/width/height`` 同一坐标系。
"""

from __future__ import annotations

#: 缩放比例的合理区间。超出即认为探测值不可信，退回 1.0（宁可 1:1，也不要按荒谬比例缩放）。
_MIN_SCALE = 0.1
_MAX_SCALE = 10.0


def resolve_scale(image_width: int, image_height: int, screen: dict | None) -> float:
    """推断「图片像素 / 屏幕物理像素」的缩放比。

    正常情况（进程 DPI 感知且与显示器缩放一致）图片尺寸**等于**显示器物理尺寸，返回 1.0。
    若探测到的 ``scale`` 与尺寸比一致（图片是逻辑像素），返回该 ``scale``。

    :param image_width: 被识别图片的宽（像素）。
    :param image_height: 被识别图片的高（像素）。
    :param screen: 该图对应的 ``screen_info()`` 条目（``{"width","height","scale",...}``），无则 None。
    """
    if not screen:
        return 1.0
    reported = screen.get("scale")
    try:
        reported = float(reported)
    except (TypeError, ValueError):
        return 1.0
    if not (_MIN_SCALE <= reported <= _MAX_SCALE):
        return 1.0
    if reported == 1.0:
        return 1.0

    screen_w = screen.get("width")
    screen_h = screen.get("height")
    if not screen_w or not screen_h or not image_width or not image_height:
        return reported

    # 图片是逻辑像素时，image * scale ≈ 显示器物理尺寸 —— 这才用 scale 校正。
    ratio = (screen_w / image_width + screen_h / image_height) / 2.0
    return reported if abs(ratio - reported) < 0.02 else 1.0


def make_mapper(image_width: int, image_height: int, screen: dict | None, origin: tuple[int, int]):
    """构造「图片像素 → 屏幕绝对坐标」的映射函数。

    :param image_width: 被识别图片的宽。
    :param image_height: 被识别图片的高。
    :param screen: 该图对应的显示器信息（``screen_info()`` 条目），用于推断缩放。
    :param origin: 图片左上角在屏幕上的**物理像素**位置 ``(x, y)``。
    :returns: ``(x, y) -> (screen_x, screen_y)`` 的函数。
    """
    scale = resolve_scale(image_width, image_height, screen)
    origin_x, origin_y = int(origin[0]), int(origin[1])

    def to_screen(x: float, y: float) -> tuple[int, int]:
        return (int(round(origin_x + float(x) * scale)), int(round(origin_y + float(y) * scale)))

    return to_screen


def capture_origin(region: str | None, window: dict | None, screens: list[dict] | None) -> tuple[int, int]:
    """算出「图片左上角」的屏幕坐标。

    :param region: ``"x,y,w,h"`` 或 None；有裁剪时偏移即裁剪原点。
    :param window: 窗口捕获时的窗口信息（含 ``left``/``top``），整屏捕获时为 None。
    :param screens: ``screen_info()`` 结果；用于整屏捕获时定位图片原点。
    """
    base_x, base_y = 0, 0
    if window:
        base_x = int(window.get("left", 0) or 0)
        base_y = int(window.get("top", 0) or 0)
    elif screens:
        # 整屏捕获：多屏时图片覆盖整个虚拟桌面，其左上角是所有显示器的最小 (x,y)
        # （Windows 主屏在 (0,0)，副屏挂在左侧/上方时坐标可为负）。
        base_x = min(int(s.get("x", 0) or 0) for s in screens)
        base_y = min(int(s.get("y", 0) or 0) for s in screens)

    if region:
        try:
            rx, ry = (int(v.strip()) for v in str(region).split(",")[:2])
        except Exception:
            rx, ry = 0, 0
        base_x += rx
        base_y += ry
    return base_x, base_y


def image_screen_box(
    image_width: int,
    image_height: int,
    origin: tuple[int, int],
    scale: float,
) -> dict:
    """图片覆盖的**屏幕矩形**（物理像素）``{"x","y","width","height"}``。

    为什么需要它：模型没法可靠地读「图片像素坐标」——它看到的预览是被**缩过**的（DSH 按图片
    token 规则把它缩到预算内：1920×1080 的整屏截图，模型实际收到的只有 1708×961）。但**比例位置
    在缩放前后不变**，所以只要给出这张图覆盖的屏幕矩形，模型按 ``(rx, ry) ∈ [0,1]`` 表达位置就能精确落点::

        screen_x = box.x + rx * box.width
        screen_y = box.y + ry * box.height

    这条换算与图片被缩放到多少像素完全无关，因此不需要知道 provider 的缩放规则（那些规则会随
    版本漂移）。纯函数，便于跨平台单测。
    """
    return {
        "x": int(origin[0]),
        "y": int(origin[1]),
        "width": max(1, int(round(image_width * scale))),
        "height": max(1, int(round(image_height * scale))),
    }


def screen_for_image(image_width: int, image_height: int, origin: tuple[int, int], screens: list[dict] | None) -> dict | None:
    """挑出这张图主要落在哪个显示器上（用于取该屏的 scale）。"""
    if not screens:
        return None
    ox, oy = origin
    hits = [
        s
        for s in screens
        if int(s.get("x", 0) or 0) <= ox < int(s.get("x", 0) or 0) + int(s.get("width", 0) or 0)
        and int(s.get("y", 0) or 0) <= oy < int(s.get("y", 0) or 0) + int(s.get("height", 0) or 0)
    ]
    if hits:
        # 命中多个（图跨屏）时取面积最大的那块，它的 scale 最有代表性。
        return max(hits, key=lambda s: int(s.get("width", 0) or 0) * int(s.get("height", 0) or 0))
    # 没命中就用主屏（或第一块）。
    for s in screens:
        if s.get("primary"):
            return s
    return screens[0]
