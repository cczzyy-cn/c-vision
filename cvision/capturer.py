"""兼容层：``cvision.capturer`` 转发到平台捕获后端（``cvision.capture``）。

保留旧导入路径（``from cvision import capturer``）可用；实际实现按平台分发到
``cvision.capture.windows / macos / linux``。

另外提供 :func:`capture_with_text`——「抓图 + OCR + 换算成屏幕坐标」的一次性组合，供
``cli_capture --text`` 与常驻 ``cli_server`` 的 ``{"op":"capture","text":true}`` 共用。
两条路径必须只有一份实现，否则会漂移（宿主对两者的响应形状有同样的期待）。
"""

from __future__ import annotations

import time

from cvision import coordinates, encoding, ocr, screen, ui_elements
from cvision.capture import Window, capture_screen, capture_window, list_windows
from cvision.capture.base import CaptureBackend, pick_window

__all__ = [
    "Window",
    "CaptureBackend",
    "list_windows",
    "capture_window",
    "capture_screen",
    "pick_window",
    "capture_with_text",
    "resolve_window",
]


def resolve_window(handle: int | None, title_substr: str | None) -> Window | None:
    """把 ``handle`` / ``title_substr`` 解析成**当前**的 :class:`Window`（含句柄）。

    宿主用它把「这次 see 看的是哪个窗口」记下来：点击类操作前要靠这个句柄把窗口置前并校验
    坐标归属——屏幕坐标点击只命中前台窗口，不知道目标就无从校验。``maximize`` 之后窗口几何会变，
    必须**抓完之后**再解析（见 :func:`capture_with_text`）。
    """
    return _resolve_window(handle, title_substr)


def _resolve_window(handle: int | None, title_substr: str | None) -> Window | None:
    """把 ``handle`` / ``title_substr`` 解析成 ``Window``（用于拿它的屏幕位置）。"""
    if handle is not None:
        for win in list_windows():
            if win.handle == int(handle):
                return win
        return None
    if title_substr:
        return pick_window(list_windows(), title_substr)
    return None


def image_screen_frame(img, region: str | None, window_info: Window | None) -> dict:
    """算出这张图覆盖的**屏幕矩形**（纯换算，不抓图）。

    供 ``capture_with_text``、``cli_server`` 的非 text 抓取等多条路径共用——两处各写一份
    迟早会漂移，而这里是「按比例点击」的唯一基准。

    :param img: **尚未** ``fit_for_attachment`` 缩放的原图（缩放会改变像素尺寸，但矩形是屏幕
        坐标、与它无关；这里要的是图片的原始像素尺寸与 DPI 缩放比）。
    :param region: 抓取时用的裁剪区域 ``"x,y,w,h"``（原点要加上它）。
    :param window_info: 窗口抓取时的窗口信息；整屏抓取传 None。
    """
    screens = screen.screen_info()
    origin = coordinates.capture_origin(
        region,
        window_info.to_dict() if window_info else None,
        screens,
    )
    matched = coordinates.screen_for_image(img.width, img.height, origin, screens)
    return coordinates.image_screen_box(
        img.width,
        img.height,
        origin,
        coordinates.resolve_scale(img.width, img.height, matched),
    )


def capture_with_text(
    *,
    handle: int | None = None,
    title_substr: str | None = None,
    maximize: bool = False,
    region: str | None = None,
    delay: float = 0,
    format: str = "PNG",
    geometry_out: dict | None = None,
) -> tuple[object, list[dict]]:
    """抓一张图，并附带**可直接点击**的可点击元素列表。

    一次性完成「截图 → OCR → 词框合并 → 屏幕坐标换算」，宿主只要一次调用就能同时拿到
    图片与点击目标（不必先 ``ocr`` 再由模型自己折算坐标——那正是最容易算错的地方）。

    :param geometry_out: 传入一个 dict 时，会被填入这张图覆盖的**屏幕矩形**
        ``{"x","y","width","height"}``（见 :func:`coordinates.image_screen_box`）。宿主用它支持
        「按比例点击」：模型读不准图片像素（它看到的预览被缩过），但读得准**比例**。用出参
        而不是加返回值，是为了不破坏 ``(img, elements)`` 这个既有契约。
    :returns: ``(PIL.Image, elements)``。``elements`` 每项含 ``text`` / ``box`` / ``center`` /
        ``screen_box`` / ``screen_center`` / ``word_count``；没识别到文字或 OCR 不可用时返回
        空列表——**OCR 的问题不该让整个 ``see`` 失败**（``geometry_out`` 仍然有效）。
    """
    if delay:
        time.sleep(float(delay) / 1000.0)

    window_info: Window | None = None
    if handle is not None or title_substr:
        img = capture_window(handle=handle, title_substr=title_substr, maximize=maximize)
        # maximize 会改变窗口矩形，必须在抓完之后重新解析，否则算出来的坐标整体偏移。
        window_info = _resolve_window(handle, title_substr)
        if window_info is not None and handle is None:
            handle = window_info.handle
    else:
        img = capture_screen()
    if region:
        img = encoding.crop_region(img, region)

    elements: list[dict] = []
    try:
        words = ocr.ocr_image(img).get("words", [])
        grouped = ui_elements.group_words(words)
    except Exception:  # noqa: BLE001 - OCR 不可用不该让截图整体失败
        grouped = []

    # 图片覆盖的屏幕矩形：它与 OCR 成败**无关**——纯图标界面（一个词都认不出来）同样需要它。
    # 归一化坐标点击（模型按 0~1 的比例表达位置）就靠这个矩形换算成屏幕坐标，而比例在图片被
    # 缩放到多少像素前后都不变，所以这条路径不依赖任何 provider 的缩放规则。
    try:
        box = image_screen_frame(img, region, window_info)
        if geometry_out is not None:
            geometry_out.update(box)
        if grouped:
            screens = screen.screen_info()
            origin = coordinates.capture_origin(
                region,
                window_info.to_dict() if window_info else None,
                screens,
            )
            matched = coordinates.screen_for_image(img.width, img.height, origin, screens)
            to_screen = coordinates.make_mapper(img.width, img.height, matched, origin)
            elements = ui_elements.to_screen_elements(grouped, to_screen, (img.width, img.height))
    except Exception:  # noqa: BLE001 - 坐标换算失败不该让截图整体失败
        elements = []

    # 缩放到附件限额可能改变图片尺寸。`screen_center`/`screen_box` 是**屏幕坐标**，与图片缩放
    # 无关，必须原样保留；而 `box`/`center` 是**图片坐标**，缩了图就得同比例换算——否则模型
    # 拿 box 去对应图上的位置会错位。
    before_w, before_h = img.width, img.height
    img = encoding.fit_for_attachment(img, format=format)
    if img.width != before_w or img.height != before_h:
        scale_x = img.width / float(before_w or 1)
        scale_y = img.height / float(before_h or 1)
        for element in elements:
            box = element.get("box")
            center = element.get("center")
            if isinstance(box, dict):
                element["box"] = {
                    "x": int(round(box["x"] * scale_x)),
                    "y": int(round(box["y"] * scale_y)),
                    "w": max(1, int(round(box["w"] * scale_x))),
                    "h": max(1, int(round(box["h"] * scale_y))),
                }
            if isinstance(center, dict):
                element["center"] = {
                    "x": int(round(center["x"] * scale_x)),
                    "y": int(round(center["y"] * scale_y)),
                }
    return img, elements


def wait_until_changed(
    *,
    handle: int | None = None,
    title_substr: str | None = None,
    maximize: bool = False,
    region: str | None = None,
    format: str = "PNG",
    interval_ms: float = 500,
    timeout_ms: float = 10000,
    threshold: float = 0.01,
    pixel_delta: int = 13,
) -> tuple[object, dict]:
    """反复截图直到画面变化（或超时），返回**变化后的那一帧**与差异统计。

    比「连拍 N 张图都塞给模型」省得多：模型不必看 N 张相似图，只需要知道「变没变、变在哪」。
    典型用法是「等进度条跑完」「等弹窗出现」——先 ``see`` 看基线，再 ``wait_until_changed``。

    :param interval_ms: 两次采样之间的间隔（毫秒）。
    :param timeout_ms: 总超时（毫秒），到这里即使没变化也返回。
    :param threshold: ``diff_ratio`` 超过它才算「变了」。**默认 0.01（缩略图 1% 像素）**：
        实测光标/文本插入符闪烁约占 0.5%，所以阈值必须高于它，否则工具会在第一次轮询就返回
        「变了」（那等于毫无用处）。窗口出现这类真实变化通常 ≥5%，留了足够余量。
        要更灵敏就调低，但**同时用 ``region`` 把闪烁区域排除掉**才稳。
    :param pixel_delta: 单像素算「变了」的灰度差阈值。
    :returns: ``(PIL.Image, metrics)``，``metrics`` 含 ``changed`` / ``samples`` / ``elapsed_ms`` /
        ``diff_ratio`` / ``mean_diff`` / ``diff_bbox``（变化区域，原图坐标）。
    """
    from cvision import diff as diff_mod

    interval = max(0.0, float(interval_ms) / 1000.0)
    deadline = time.monotonic() + max(0.0, float(timeout_ms) / 1000.0)

    def shoot():
        if handle is not None or title_substr:
            frame = capture_window(handle=handle, title_substr=title_substr, maximize=maximize)
        else:
            frame = capture_screen()
        if region:
            frame = encoding.crop_region(frame, region)
        return frame

    started = time.monotonic()
    baseline = shoot()
    base_thumb = diff_mod.thumbnail(baseline)
    samples = 1
    metrics = {"changed": False, "diff_ratio": 0.0, "mean_diff": 0.0, "bbox": None}

    while time.monotonic() < deadline:
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        current = shoot()
        samples += 1
        metrics = diff_mod.diff_metrics(base_thumb, diff_mod.thumbnail(current), pixel_delta)
        if metrics["diff_ratio"] >= threshold:
            full = encoding.fit_for_attachment(current, format=format)
            return full, {
                "changed": True,
                "samples": samples,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "diff_ratio": round(metrics["diff_ratio"], 6),
                "mean_diff": round(metrics["mean_diff"], 3),
                "diff_bbox": diff_mod.scale_bbox(metrics["bbox"], base_thumb.size, current.size),
            }
        baseline, base_thumb = current, diff_mod.thumbnail(current)

    full = encoding.fit_for_attachment(baseline, format=format)
    return full, {
        "changed": False,
        "samples": samples,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "diff_ratio": round(metrics["diff_ratio"], 6),
        "mean_diff": round(metrics["mean_diff"], 3),
        "diff_bbox": None,
    }
