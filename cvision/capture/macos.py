"""macOS 屏幕/窗口捕获后端。

依赖 Quartz(pyobjc-framework-Quartz) + Pillow。
- 列窗口：``CGWindowListCopyWindowInfo``（CGWindowID + 标题 + 边界）。
- 抓窗口：``screencapture -l <windowID>``（系统级，能抓到 GPU 合成窗口内容）。
- 抓整屏：Pillow ``ImageGrab.grab()``（内部走 ``screencapture``）。

权限：macOS 10.15+ 需在「系统设置 → 隐私与安全 → 屏幕录制」授权，否则
``kCGWindowName`` 为空、``screencapture`` 只能抓到桌面/壁纸。
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from PIL import Image, ImageGrab

from cvision.capture.base import Window, pick_window
# 跨进程输入互斥：解除最小化会改前台/Z 序，而前台与输入设备是同一个资源（见 input_lock 的锁边界表）
from cvision.input_lock import ForegroundGuard

try:
    import Quartz
except ImportError:  # pragma: no cover - 仅当未装 pyobjc 时
    Quartz = None


def _require_quartz() -> None:
    if Quartz is None:
        raise RuntimeError(
            "macOS 后端需要 pyobjc-framework-Quartz：python -m pip install pyobjc-framework-Quartz"
        )


def list_windows() -> list[Window]:
    """枚举可见顶层窗口（仅普通窗口层），按左上角排序。"""
    _require_quartz()
    win_info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    result: list[Window] = []
    for info in win_info:
        layer = info.get(Quartz.kCGWindowLayer, 0)
        if layer != 0:  # 只取普通窗口层，过滤桌面/菜单栏/Dock 等
            continue
        wnum = info.get(Quartz.kCGWindowNumber)
        title = info.get(Quartz.kCGWindowName) or ""
        bounds = info.get(Quartz.kCGWindowBounds, {})
        x = int(bounds.get("X", 0))
        y = int(bounds.get("Y", 0))
        w = int(bounds.get("Width", 0))
        h = int(bounds.get("Height", 0))
        if w <= 0 or h <= 0:
            continue
        result.append(Window(int(wnum), title, x, y, w, h))
    result.sort(key=lambda w: (w.top, w.left, w.handle))
    return result


def find_window(title_substr: str) -> Window | None:
    """按标题定位窗口：精确标题优先，其次子串；无命中返回 None。"""
    return pick_window(list_windows(), title_substr)


def _unminimize(window_id: int) -> None:
    """尽力而为：通过 AppleScript 把最小化的窗口取消隐藏/置前。"""
    try:
        script = 'tell application "System Events" to set miniaturized of window id {} to false'.format(
            int(window_id)
        )
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    except Exception:
        pass


def _capture_window_by_id(window_id: int) -> Image.Image:
    _require_quartz()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        subprocess.run(
            ["screencapture", "-l", str(window_id), "-x", tmp],
            check=True,
            capture_output=True,
        )
        return Image.open(tmp).convert("RGB")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def capture_window(
    handle: int | None = None,
    title_substr: str | None = None,
    maximize: bool = False,
) -> Image.Image:
    """抓取窗口画面：``screencapture -l <CGWindowID>``。

    macOS 的 ``screencapture -l`` 直接抓窗口自身内容，与是否前台无关；默认不切前台。
    ``maximize`` 仅在窗口被最小化时尽力解除最小化（否则抓不到）——那一步**会**把窗口带到前面，
    所以要和输入共用同一把跨进程锁（与 Windows 后端的 ``ForegroundGuard`` 同一套边界）。
    """
    if handle is None:
        if not title_substr:
            raise ValueError("capture_window 需要 handle 或 title_substr 之一")
        win = find_window(title_substr)
        if win is None:
            raise LookupError(f"未找到标题含 {title_substr!r} 的窗口")
        handle = win.handle
    if not maximize:
        return _capture_window_by_id(int(handle))
    guard = ForegroundGuard(label=f"抓取窗口 {int(handle)}")
    try:
        guard.ensure("需要解除最小化才能抓到画面")
        _unminimize(int(handle))
        return _capture_window_by_id(int(handle))
    finally:
        guard.release()


def capture_screen() -> Image.Image:
    """抓取整个屏幕（Pillow 内部走 screencapture，需屏幕录制权限）。"""
    _require_quartz()
    return ImageGrab.grab()
