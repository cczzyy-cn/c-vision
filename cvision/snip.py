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

from cvision import clipboard

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


#: Windows 11 截图覆盖层的窗口类名（语言无关；标题会随语言变，类名不会）。
#: 实测：覆盖层打开时 SnippingTool.exe 有 `SnipOverlayRootWindow`，取消后该窗口消失。
_SNIP_OVERLAY_CLASS = "SnipOverlayRootWindow"
#: 等覆盖层出现的上限（拉起来通常 0.3~1s；超时说明这次观测不到 UI，退回「只等剪贴板」的旧行为）。
_UI_APPEAR_TIMEOUT_SECONDS = 6.0
#: 覆盖层消失后再确认一眼剪贴板，避开「先关窗后写入」的极小竞态。
_CANCEL_RECHECK_SECONDS = 0.3


def _snip_overlay_visible() -> bool:
    """系统截图覆盖层此刻是否在屏幕上（纯 ctypes，不依赖 pywin32）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    visible = []

    def visit(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        if buffer.value == _SNIP_OVERLAY_CLASS:
            visible.append(hwnd)
            return False  # 找到一个就够
        return True

    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    user32.EnumWindows(callback, 0)
    return bool(visible)


def _settle_clipboard_image(image, grace: float):
    """第一张图到手后短暂静置：期间剪贴板又更新（用户去标注了）就取更新的那张。"""
    started = time.monotonic()
    deadline = started + grace
    hard_deadline = started + _SETTLE_HARD_CAP_SECONDS
    sequence = clipboard.token()
    while time.monotonic() < min(deadline, hard_deadline):
        time.sleep(0.15)
        current = clipboard.token()
        if current == sequence:
            continue
        sequence = current
        newer = clipboard.read_image()
        if newer is not None:
            image = newer
            deadline = min(time.monotonic() + grace, hard_deadline)
    return image


def _snip_windows(timeout: float):
    """Windows：拉起截图覆盖层，等剪贴板出现「用户刚框选的那张」。

    **必须识别用户取消**：早期版本只看剪贴板变化，用户按 Esc 取消后循环会一直等到超时（默认 60s），
    这段时间里任何新出现的剪贴板图片（例如之后用微信截的图）都会被当成本次截图返回——线上实测就是
    「点了截图→取消→再微信截图，附件栏自动多出那张微信截图」。现在用覆盖层窗口作判据：

    - 覆盖层**在**：继续等剪贴板；
    - 覆盖层**消失且剪贴板始终没有新图** → 用户取消，立即返回 ``None``（宿主据此回 204，客户端静默）；
    - 覆盖层观测不到（老版本 Windows / 类名不同）→ 退回旧行为，只等剪贴板与超时。
    """
    if not _launch_windows_snip():
        raise SnipUnsupported("无法拉起 Windows 截图（pyautogui 与 ms-screenclip: 均不可用）")

    before = clipboard.token()
    # 等覆盖层出现：等到了才启用「消失即取消」的判据（避免把「没观测到 UI」误判成取消）。
    seen_overlay = False
    appear_deadline = time.monotonic() + _UI_APPEAR_TIMEOUT_SECONDS
    while time.monotonic() < appear_deadline:
        if _snip_overlay_visible():
            seen_overlay = True
            break
        time.sleep(_POLL_SECONDS)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        current = clipboard.token()
        if current == before:
            if seen_overlay and not _snip_overlay_visible():
                # 覆盖层关了却始终没等到图 → 用户取消。先确认一眼剪贴板（避开「先关窗后写入」的竞态）。
                time.sleep(_CANCEL_RECHECK_SECONDS)
                late = clipboard.token()
                if late != before:
                    image = clipboard.read_image()
                    if image is not None:
                        return _settle_clipboard_image(image, _SETTLE_SECONDS)
                    before = late
                    continue
                return None
            continue
        image = clipboard.read_image()
        if image is None:
            # 变了但不是图片（用户复制了别的东西）：记下新序列号继续等真正的截图。
            before = current
            continue
        return _settle_clipboard_image(image, _SETTLE_SECONDS)
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
