"""剪贴板：读取**图片**与「内容是否变了」的廉价判定（跨平台）。

为什么要它：浏览器**无法**在后台读剪贴板（`navigator.clipboard.read()` 需要用户手势与授权，也没有
变更事件），所以「每秒检查剪贴板里有没有新图片」只能由宿主原生侧完成，页面再通过同源 HTTP 轮询
宿主的状态。这样也天然覆盖「其它软件截图」（微信 / QQ / Win+Shift+S 都是把图放进系统剪贴板）。

分工：
- :func:`state` —— **廉价**状态：只查「有没有图片格式」+ 一个 token（Windows 剪贴板序列号 /
  macOS pasteboard changeCount），**不解码图片**，所以能被每秒轮询。
- :func:`read_image` —— 真正解码图片（只在用户长按要插图时调用一次）。
- 文本剪贴板见 ``input.get_clipboard/set_clipboard``（Windows 原生 / 其它平台 pyperclip）。

平台：Windows 完整（ctypes + Pillow）；macOS 用 pyobjc AppKit（``requirements.txt`` 已声明
``pyobjc-framework-Cocoa``，**未在真机验证**）；Linux 为 Phase 2（明确返回不支持，交由调用方降级）。
"""

from __future__ import annotations

import io
import sys

#: Windows 剪贴板里代表位图的那几个格式。
_CF_BITMAP = 2
_CF_DIB = 8
_CF_DIBV5 = 17
_IMAGE_FORMATS = (_CF_BITMAP, _CF_DIB, _CF_DIBV5)


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def is_supported() -> bool:
    """当前平台能否读剪贴板图片。"""
    return _is_windows() or sys.platform == "darwin"


def unsupported_reason() -> str:
    if is_supported():
        return ""
    return f"剪贴板图片读取尚未在 {sys.platform} 上实现（Phase 2：X11 可走 xclip/Wayland 走 wl-paste）"


def token() -> str | None:
    """剪贴板内容的廉价身份：内容一变就变；平台不支持时返回 None。"""
    if _is_windows():
        import ctypes

        return str(ctypes.windll.user32.GetClipboardSequenceNumber())  # type: ignore[attr-defined]
    if sys.platform == "darwin":
        try:
            from AppKit import NSPasteboard

            return str(NSPasteboard.generalPasteboard().changeCount())
        except Exception:  # noqa: BLE001 - 没有 pyobjc 就当不支持
            return None
    return None


def has_image() -> bool:
    """剪贴板当前是否有图片格式——不解码，供高频状态查询用。"""
    if _is_windows():
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        return any(bool(user32.IsClipboardFormatAvailable(fmt)) for fmt in _IMAGE_FORMATS)
    if sys.platform == "darwin":
        try:
            from AppKit import NSPasteboard

            return NSPasteboard.generalPasteboard().dataForType_("public.png") is not None
        except Exception:  # noqa: BLE001
            return False
    return False


def state() -> dict:
    """给宿主路由用的状态：``{supported, image, token, reason}``（廉价、可高频调用）。"""
    if not is_supported():
        return {"supported": False, "image": False, "token": None, "reason": unsupported_reason()}
    try:
        return {"supported": True, "image": bool(has_image()), "token": token(), "reason": ""}
    except Exception as error:  # noqa: BLE001 - 剪贴板被别的进程占用时不该炸掉轮询
        return {"supported": True, "image": False, "token": None, "reason": f"{type(error).__name__}: {error}"}


def read_image():
    """解码剪贴板里的图片。

    :returns: ``PIL.Image``；剪贴板不是图片或平台不支持时返回 ``None``。
    """
    if _is_windows():
        from PIL import ImageGrab

        try:
            content = ImageGrab.grabclipboard()
        except Exception:  # noqa: BLE001 - 被占用按「没有图」处理
            return None
        if content is None or isinstance(content, list):
            return None  # 文件拖放列表不算图片
        return _normalize(content)
    if sys.platform == "darwin":
        return _read_image_macos()
    return None


def _normalize(image):
    """统一成 RGB/RGBA，避免后续编码遇到调色板/16 位等模式。"""
    if image.mode in ("RGB", "RGBA"):
        return image
    return image.convert("RGBA" if "A" in image.getbands() else "RGB")


def _read_image_macos():
    """macOS：优先 PNG，其次 TIFF（有些 App 只放 TIFF）。"""
    try:
        from AppKit import NSPasteboard
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        board = NSPasteboard.generalPasteboard()
        data = board.dataForType_("public.png")
        if data is not None:
            return _normalize(Image.open(io.BytesIO(bytes(data))))
        data = board.dataForType_("public.tiff")
        if data is not None:
            return _normalize(Image.open(io.BytesIO(bytes(data))))
    except Exception:  # noqa: BLE001
        return None
    return None
