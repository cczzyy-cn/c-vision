"""图片编码：PIL Image -> base64 data URL，供 DeepSeek 视觉请求使用。"""

from __future__ import annotations

import base64
import io

from PIL import Image

# 支持的图片格式 -> data URL MIME 类型
_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

_DEFAULT_FORMAT = "PNG"


def image_to_base64(img: Image.Image, format: str = _DEFAULT_FORMAT) -> tuple[str, str]:
    """将 PIL 图片编码为 base64。

    返回 ``(mime_type, base64_str)``。默认 PNG（无损，适合截图/文字；所有调用方 CLI 的默认也是 PNG）。

    ⚠️ 多帧图（GIF/APNG/WEBP）走 :meth:`Image.Image.save` **只写第一帧**——本函数的 ``GIF`` 项只对
    单帧有效。宿主半边已把 GIF 从可回传类型里移除，别指望它做动图。
    """
    fmt = (format or _DEFAULT_FORMAT).upper()
    mime = _MIME.get(fmt)
    if mime is None:
        raise ValueError(f"不支持的图片格式 {fmt!r}，可选：{', '.join(_MIME)}")

    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return mime, b64


def image_to_data_url(img: Image.Image, format: str = _DEFAULT_FORMAT) -> str:
    """将 PIL 图片编码为 ``data:<mime>;base64,<data>`` 形式的 data URL。"""
    mime, b64 = image_to_base64(img, format=format)
    return f"data:{mime};base64,{b64}"


# Harness attachment 的图像限制（单边 <=8192px、单张 <=20MiB），超出会被拒。
IMAGE_MAX_SIDE = 8192
IMAGE_MAX_BYTES = 20 * 1024 * 1024


def _encoded_size(img: Image.Image, format: str) -> int:
    buf = io.BytesIO()
    img.save(buf, format=format.upper())
    return buf.tell()


def fit_for_attachment(
    img: Image.Image,
    format: str = "PNG",
    max_side: int = IMAGE_MAX_SIDE,
    max_bytes: int = IMAGE_MAX_BYTES,
) -> Image.Image:
    """把图缩放在附件限制内：单边不超过 ``max_side``，编码后不超过 ``max_bytes``。

    先等比例缩边，再若编码仍超限则逐步缩小，确保 ``saveImage`` 不会被拒。
    """
    if img is None:
        return None  # type: ignore[return-value]
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    for _ in range(8):
        if _encoded_size(img, format) <= max_bytes:
            break
        w, h = img.size
        if w <= 1 or h <= 1:
            break
        img = img.resize((max(1, int(w * 0.8)), max(1, int(h * 0.8))), Image.LANCZOS)
    return img


def crop_region(img: Image.Image, region: str) -> Image.Image:
    """按 ``x,y,w,h`` 裁剪图片区域（像素，相对该图）。"""
    try:
        x, y, w, h = (int(v.strip()) for v in region.split(","))
    except Exception:
        raise ValueError("region 需为 x,y,w,h 四个整数")
    if w <= 0 or h <= 0:
        raise ValueError("region 宽高需为正整数")
    return img.crop((x, y, x + w, y + h))
