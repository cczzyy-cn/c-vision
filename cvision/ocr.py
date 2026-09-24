"""OCR 文本识别：对 PIL 图返回文字（含词级边界框）。

优先用 **Windows.Media.Ocr**（winsdk，免额外二进制、跟随系统语言包）；失败/未装时
回退 **pytesseract**（需额外安装 Tesseract）。两者都不可用时给出明确报错。

返回结构（统一）::

    {
      "text": str,          # 全文
      "lines": [str],       # 按行
      "words": [            # 词级边界框（供 computer-use 精确定位点击点）
        {"text": str, "x": int, "y": int, "w": int, "h": int},
        ...
      ],
    }

``x/y/w/h`` 为相对被识别图片的像素坐标。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from PIL import Image

#: OCR 前的放大倍数，以及放大后长边的上限。
#:
#: 为什么必须放大（真机实测）：``Windows.Media.Ocr`` 对**小字**识别得非常差。同一个
#: 1353×782 的资源管理器窗口，1x 时把 ``scripts`` 认成 ``scrlpts``、``2026/9/23`` 认成
#: ``2025/g/23``、``文件夹`` 认成 ``文 仁 夹``、``修改日期`` 被拆成 ``修 改`` + ``期``；
#: 拿该目录里真实存在的 15 个文件名当真值统计，1x 命中 **3/15**，3x 命中 **9/15**。
#: 而这些文本正是「可点击元素」能否被模型认出来的关键——识别错字，模型就找不到目标。
#: 代价是 OCR 耗时从 ~146ms 涨到 ~331ms（同一次 see 里的一次性开销）。
_OCR_UPSCALE = 3.0
#: 放大后长边上限：4K 截图再乘 3 会变成上亿像素，OCR 会明显变慢甚至失败。
_OCR_MAX_SIDE = 4200


def _upscale_for_ocr(img: Image.Image) -> tuple[Image.Image, float]:
    """按经验放大待识别图；返回 ``(图, 倍数)``，倍数 1.0 表示没放大。"""
    longest = max(int(img.width), int(img.height))
    if longest <= 0:
        return img, 1.0
    factor = max(1.0, min(_OCR_UPSCALE, _OCR_MAX_SIDE / float(longest)))
    if factor <= 1.01:
        return img, 1.0
    size = (max(1, int(round(img.width * factor))), max(1, int(round(img.height * factor))))
    return img.resize(size), factor


def _scale_words_back(words: list[dict], factor: float) -> list[dict]:
    """把放大图上识别到的词框按倍数还原成**原图坐标**。

    调用方拿到的 ``words`` 永远是原图坐标系——放大只是识别手段，不该泄漏到外面去
    （否则 ``screen_center`` 会整体放大 3 倍，点哪儿都偏）。
    """
    out: list[dict] = []
    for word in words:
        try:
            x, y = int(word.get("x", 0)), int(word.get("y", 0))
            w, h = int(word.get("w", 0)), int(word.get("h", 0))
        except (TypeError, ValueError):
            out.append(word)
            continue
        out.append(
            {
                "text": word.get("text", ""),
                "x": int(round(x / factor)),
                "y": int(round(y / factor)),
                "w": max(1, int(round(w / factor))),
                "h": max(1, int(round(h / factor))),
            }
        )
    return out


def _rect_to_xywh(rect) -> dict:
    """把平台 Rect 对象转成 ``{x,y,w,h}``（Windows/Microsoft.UI 矩形）。"""
    try:
        return {
            "x": int(rect.x),
            "y": int(rect.y),
            "w": int(rect.width),
            "h": int(rect.height),
        }
    except Exception:
        return {"x": 0, "y": 0, "w": 0, "h": 0}


def _ocr_via_windows(img: Image.Image) -> dict:
    import winsdk._winrt as wr
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import StorageFile

    wr.init_apartment(wr.MTA)
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR 没有可用的识别语言")

    # 用安全临时文件（NamedTemporaryFile，避免 mktemp 的可预测路径 / TOCTOU 风险）
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as _tmp:
        tmp = _tmp.name
    img.save(tmp)
    try:
        async def _go():
            f = await StorageFile.get_file_from_path_async(tmp)
            stream = await f.open_read_async()
            dec = await BitmapDecoder.create_async(stream)
            sb = await dec.get_software_bitmap_async()
            return await engine.recognize_async(sb)

        res = asyncio.run(_go())
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    lines: list[str] = []
    words: list[dict] = []
    for line in res.lines:
        lines.append(line.text)
        for word in line.words:
            words.append(
                {"text": word.text, **_rect_to_xywh(word.bounding_rect)}
            )
    return {"text": res.text, "lines": lines, "words": words}


def _ocr_via_pytesseract(img: Image.Image) -> dict:
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(img, output_type=Output.DICT)
    lines: list[str] = []
    words: list[dict] = []
    cur_line = -1
    cur_text: list[str] = []
    for i in range(len(data["text"] or [])):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        word = {
            "text": txt,
            "x": int(data.get("left", [])[i] if data.get("left") else 0),
            "y": int(data.get("top", [])[i] if data.get("top") else 0),
            "w": int(data.get("width", [])[i] if data.get("width") else 0),
            "h": int(data.get("height", [])[i] if data.get("height") else 0),
        }
        words.append(word)
        ln = int(data.get("line_num", [])[i] if data.get("line_num") else 0) + (
            int(data.get("par_num", [])[i] if data.get("par_num") else 0) * 1000
        )
        if ln != cur_line and cur_text:
            lines.append(" ".join(cur_text))
            cur_text = []
        cur_line = ln
        cur_text.append(txt)
    if cur_text:
        lines.append(" ".join(cur_text))

    # 顺序：pytesseract 的行/词序可能与 y 排序不同，稳一版——按 (y,x) 排序。
    words.sort(key=lambda w: (w["y"], w["x"]))
    return {"text": "\n".join(lines), "lines": lines, "words": words}


def ocr_image(img: Image.Image) -> dict:
    """识别图片文字，返回 ``{"text","lines","words"}``。

    识别前会按 :data:`_OCR_UPSCALE` 把图放大（小字识别率显著更高），
    但 ``words`` 的坐标**已还原成原图坐标**，调用方不必关心放大这件事。

    优先 Windows.Media.Ocr，失败回退 pytesseract；两者皆不可用则报错。
    """
    scaled, factor = _upscale_for_ocr(img)
    result = _recognize(scaled)
    if factor > 1.0:
        result = {**result, "words": _scale_words_back(result.get("words", []), factor)}
    return result


def _recognize(img: Image.Image) -> dict:
    """对（可能已放大的）图做一次识别：Windows OCR 优先，pytesseract 兜底。"""
    try:
        return _ocr_via_windows(img)
    except Exception:
        pass
    try:
        return _ocr_via_pytesseract(img)
    except Exception:
        raise RuntimeError(
            "OCR 不可用：Windows.Media.Ocr(winsdk) 失败，且未安装 pytesseract/Tesseract。"
            "Windows 请 pip install winsdk（自带系统 OCR）；或 pip install pytesseract 并安装 Tesseract。"
        )
