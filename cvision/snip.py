"""系统级区域截图：拉起各平台自带的截图 UI，把用户框选的结果取回来。

与 ``capture/`` 那套「程序化抓屏」不同，这里是**人工**通道：用户在系统截图工具里画框
（还能顺手标注），结果落到剪贴板（Windows）或临时文件（macOS），本模块负责把它接回来。

- Windows：``Win+Shift+S``（``ms-screenclip:`` 兜底）→ 轮询剪贴板。
- macOS：``screencapture -i -x``（用户 Esc 则不留文件）。
- Linux：Phase 2（与 ``capture/linux.py`` 一致，抛 :class:`SnipUnsupported`；插件的浏览器
  半边会自动回退到浏览器抓屏）。

**只认「调用之后新出现」的结果**：Windows 用剪贴板序列号（``GetClipboardSequenceNumber``，
精确且零拷贝）做前后对比，绝不去读用户上一次复制的旧图；序列号变了但内容不是图片（比如用户
复制了文本）就继续等。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time

#: 轮询间隔（秒）；序列号比较是常数开销，可以问得勤一点。
_POLL_SECONDS = 0.2
#: 拿到第一张图后再静置多久，等用户可能继续去标注（Windows 截图工具是先入剪贴板再开编辑器）。
_SETTLE_SECONDS = 1.2
#: 静置总时长上限，避免剪贴板被反复改写时无限延长。
_SETTLE_HARD_CAP_SECONDS = 6.0


class SnipUnsupported(RuntimeError):
    """当前平台没有可用的系统截图通道。"""


def snip_selection(timeout: float = 60.0):
    """拉起系统截图 UI 并等待用户框选。

    :param timeout: 等待用户完成框选的最长秒数。
    :returns: ``PIL.Image``；用户取消或超时返回 ``None``。
    :raises SnipUnsupported: 平台未实现，或截图 UI 拉不起来。
    """
    if sys.platform == "win32":
        return _snip_windows(timeout)
    if sys.platform == "darwin":
        return _snip_macos(timeout)
    raise SnipUnsupported(f"系统截图尚未在 {sys.platform} 上实现（Phase 2）；请改用浏览器抓屏")


def _to_rgb(image):
    """统一成 RGB/RGBA，避免后续编码遇到调色板/16 位等模式。"""
    if image.mode in ("RGB", "RGBA"):
        return image
    return image.convert("RGBA" if "A" in image.getbands() else "RGB")


# ── Windows ────────────────────────────────────────────────────────────────


def _launch_windows_snip() -> bool:
    """拉起 Windows 截图覆盖层。首选 Win+Shift+S（成败可判定），再退回 ms-screenclip:。"""
    try:
        import pyautogui

        try:
            pyautogui.hotkey("win", "shift", "s")
        except Exception:  # noqa: BLE001 - 某些版本只认 winleft
            pyautogui.hotkey("winleft", "shift", "s")
        return True
    except Exception:  # noqa: BLE001 - 没有 pyautogui 时退回协议
        pass
    try:
        subprocess.Popen(["explorer.exe", "ms-screenclip:"], close_fds=True)  # noqa: S603
        return True
    except OSError:
        return False


def _read_clipboard_image():
    """读剪贴板里的图片；不是图片（空 / 文件列表 / 文本）返回 None。"""
    from PIL import ImageGrab

    try:
        content = ImageGrab.grabclipboard()
    except Exception:  # noqa: BLE001 - 剪贴板被别的进程占用时按「没有图」处理
        return None
    if content is None or isinstance(content, list):
        return None
    try:
        return _to_rgb(content)
    except Exception:  # noqa: BLE001 - 非图像句柄
        return None


def _settle_clipboard_image(user32, image, grace: float):
    """第一张图到手后短暂静置：期间剪贴板又更新（用户去标注了）就取更新的那张。"""
    started = time.monotonic()
    deadline = started + grace
    hard_deadline = started + _SETTLE_HARD_CAP_SECONDS
    sequence = user32.GetClipboardSequenceNumber()
    while time.monotonic() < min(deadline, hard_deadline):
        time.sleep(0.15)
        current = user32.GetClipboardSequenceNumber()
        if current == sequence:
            continue
        sequence = current
        newer = _read_clipboard_image()
        if newer is not None:
            image = newer
            deadline = min(time.monotonic() + grace, hard_deadline)
    return image


def _snip_windows(timeout: float):
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    if not _launch_windows_snip():
        raise SnipUnsupported("无法拉起 Windows 截图（pyautogui 与 ms-screenclip: 均不可用）")

    before = user32.GetClipboardSequenceNumber()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        current = user32.GetClipboardSequenceNumber()
        if current == before:
            continue
        image = _read_clipboard_image()
        if image is None:
            # 变了但不是图片（用户复制了别的东西）：记下新序列号继续等真正的截图。
            before = current
            continue
        return _settle_clipboard_image(user32, image, _SETTLE_SECONDS)
    return None


# ── macOS ──────────────────────────────────────────────────────────────────


def _snip_macos(timeout: float):
    """``screencapture -i`` 交互框选：直接写文件，用户 Esc 则文件不存在（比读剪贴板干净）。"""
    handle, path = tempfile.mkstemp(prefix="cvision-snip-", suffix=".png")
    os.close(handle)
    with contextlib.suppress(OSError):
        os.unlink(path)  # 让 screencapture 自己创建：文件存在与否就是「用户是否框选」的判据
    try:
        subprocess.run(  # noqa: S603
            ["screencapture", "-i", "-x", "-t", "png", path],
            check=False,
            timeout=timeout + 5,
        )
        if not os.path.exists(path):
            return None
        from PIL import Image

        with Image.open(path) as image:
            return _to_rgb(image.copy())
    except FileNotFoundError as error:
        raise SnipUnsupported("未找到 screencapture（macOS 自带；请确认 PATH）") from error
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
