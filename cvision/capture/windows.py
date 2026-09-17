"""Windows 屏幕/窗口捕获后端。

依赖 pywin32 + Pillow（WGC 后端可选依赖 winsdk）。窗口捕获优先级：
    Windows Graphics Capture(真实合成内容) > PrintWindow(PW_RENDERFULLCONTENT) >
    ImageGrab 读合成桌面区域。
WGC 能抓 GPU/Chromium 合成窗口与「被遮挡窗口」的真实内容；PrintWindow 只对普通
GDI 窗口可靠；读合成桌面区域作为最后兜底。
"""

from __future__ import annotations

import ctypes
import time

import win32con
import win32gui
import win32ui
from PIL import Image, ImageGrab

from cvision.capture.base import Window, pick_window
from cvision.detect import (
    GPU_WINDOW_CLASS_PREFIXES as _GPU_WINDOW_CLASS_PREFIXES,
    is_blank_image as _is_blank_image,
    looks_like_gpu_class as _looks_like_gpu_class,
)
# 窗口跟踪诊断（默认关闭、零开销；开启后能确定「哪一步动了窗口」，见 cvision/diagnose.py）
from cvision.diagnose import snapshot as _trace_snapshot
from cvision.diagnose import trace_capture as _trace_capture
from cvision.diagnose import trace_step as _trace_step

# PrintWindow 的 PW_RENDERFULLCONTENT（Win 8.1+），可捕获前台之外窗口内容
_PW_RENDERFULLCONTENT = 2

_WS_EX_NOREDIRECTIONBITMAP = 0x00200000  # 窗口不走重定向位图，PrintWindow 无效

_DPI_SET = False


def _gpu_window_ext_style(hwnd: int) -> bool:
    """用 WS_EX_NOREDIRECTIONBITMAP 扩展样式识别 GPU 合成窗口。"""
    try:
        return bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & _WS_EX_NOREDIRECTIONBITMAP)
    except Exception:
        return False


def _set_dpi_aware() -> None:
    """让进程 DPI 感知，使窗口坐标为物理像素（否则高 DPI 下截图尺寸偏差）。"""
    global _DPI_SET
    if _DPI_SET:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _DPI_SET = True


def _safe_get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _enum_all_windows() -> list[Window]:
    """枚举所有可见顶层窗口（含最小化/工具窗口），按左上角排序。"""
    _set_dpi_aware()
    result: list[Window] = []

    def _enum(hwnd: int, _: int) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        rect = _safe_get_window_rect(hwnd)
        if rect is None:
            return
        title = win32gui.GetWindowText(hwnd)
        left, top, right, bottom = rect
        result.append(Window(hwnd, title, left, top, right - left, bottom - top))

    win32gui.EnumWindows(_enum, 0)
    result.sort(key=lambda w: (w.top, w.left, w.handle))
    return result


def list_windows() -> list[Window]:
    """枚举可见顶层窗口（供模型列出）。保留最小化/工具窗口：它们可能是用户要精确抓取/恢复的对象
    （如最小化的微信主窗，可先 ``maximize`` 再抓）；最小化窗口的矩形是 Windows 的负占位值，仅作参考。"""
    return _enum_all_windows()


def find_window(title_substr: str) -> Window | None:
    """按标题定位窗口：精确标题优先，其次子串；无命中返回 None。

    在**所有**可见窗口（含最小化）里找，以便对最小化窗口也能按标题恢复后再抓。
    """
    return pick_window(_enum_all_windows(), title_substr)


def _grab_rect(left: int, top: int, right: int, bottom: int) -> Image.Image:
    return ImageGrab.grab(bbox=(left, top, right, bottom))


def should_restore_placement(maximize: bool, was_iconic: bool) -> bool:
    """**只有我们真的改过窗口状态时**才需要写回 placement —— 纯函数，便于单测。

    为什么必须加这道判断（真实缺陷，v0.2.23 修）：

    Windows **吸附（Snap）** 态下，``GetWindowPlacement`` 返回的 ``rcNormalPosition`` 是
    **吸附之前**的位置，而 ``rect`` 是吸附后的位置。于是：

        rect      = (-7, 0, 1287, 1399)      ← 吸附后（贴左半屏）
        rcNormal  = (-12, 63, 1282, 1462)    ← 吸附【之前】的位置

    ``SetWindowPlacement(那份 placement)`` 会让 Windows 把窗口放回 ``rcNormalPosition``
    ——**等于把吸附撤销、分屏被破坏**。而旧代码在每次抓取后都无条件调用它，即使我们
    从头到尾没碰过窗口状态（普通窗口抓取是纯只读的）。

    判据：
      - ``maximize=True``：我们调过 ``ShowWindow(SW_MAXIMIZE)`` → 必须还原；
      - ``was_iconic``（抓之前最小化）：我们调过 ``SW_RESTORE`` → 必须还原回最小化；
      - 其余情况（普通窗口）：**什么都没碰，不要写回 placement**，否则会撤销用户的吸附。
    """
    return bool(maximize or was_iconic)


def _safe_get_placement(hwnd: int):
    """安全获取窗口的 WINDOWPLACEMENT（仅在需要还原时使用）；失败返回 None。"""
    try:
        return win32gui.GetWindowPlacement(hwnd)
    except Exception:
        return None


def _restore_placement(hwnd: int, placement) -> None:
    """把窗口恢复为截图前的放置状态（位置/尺寸/showCmd）。

    ⚠️ 调用方必须先经 :func:`should_restore_placement` 判断——对**没被我们改过状态**的窗口
    写回 placement，会把 Windows 的吸附态一并撤销（见该函数文档）。
    """
    if placement is None:
        return
    try:
        win32gui.SetWindowPlacement(hwnd, placement)
    except Exception:
        pass


def _ensure_foreground(hwnd: int) -> None:
    """把窗口置前并置顶，避免被其他窗口遮挡（读合成桌面区域时必需）。

    Windows 会限制后台进程直接 ``SetForegroundWindow``，因此先用常规方式，失败后
    再用 ``AttachThreadInput`` 临时把自己的输入线程挂到目标线程上置前，最后分离。
    """
    try:
        win32gui.SetForegroundWindow(hwnd)
        win32gui.BringWindowToTop(hwnd)
    except Exception:
        pass

    try:
        if win32gui.GetForegroundWindow() == hwnd:
            return
        cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        pid = ctypes.c_ulong()
        fg_tid = ctypes.windll.user32.GetWindowThreadProcessId(
            win32gui.GetForegroundWindow(), ctypes.byref(pid)
        )
        pid2 = ctypes.c_ulong()
        target_tid = ctypes.windll.user32.GetWindowThreadProcessId(
            hwnd, ctypes.byref(pid2)
        )
        if fg_tid and target_tid and fg_tid != target_tid:
            ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, True)
            try:
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            finally:
                ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, False)
    except Exception:
        pass


def _prepare_window_for_capture(hwnd: int, maximize: bool = False) -> None:
    """确保目标窗口可见且在前台，为抓图做准备。"""
    try:
        if maximize:
            maximize_window(hwnd, keep_foreground=False)
            _ensure_foreground(hwnd)
        elif win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            _ensure_foreground(hwnd)
    except Exception:
        pass
    time.sleep(0.15)


def _is_gpu_composited_window(hwnd: int) -> bool:
    """判断窗口是否大概率走 GPU/合成渲染（PrintWindow 不可靠）。"""
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        cls = ""
    return _looks_like_gpu_class(cls) or _gpu_window_ext_style(hwnd)


def _print_window(hwnd: int) -> Image.Image | None:
    """用 PrintWindow 抓取指定窗口画面（可含被遮挡内容）。失败返回 None。"""
    _set_dpi_aware()
    rect = _safe_get_window_rect(hwnd)
    if rect is None:
        return None
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), _PW_RENDERFULLCONTENT)
        if not ok:
            return None

        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    except Exception:
        return None
    finally:
        try:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            pass
        try:
            if save_dc is not None:
                save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def maximize_window(handle: int, keep_foreground: bool = True) -> None:
    """最大化指定窗口，默认不切走用户当前的前台焦点。"""
    if not win32gui.IsWindow(handle):
        raise LookupError(f"无效窗口句柄 {handle}")
    _set_dpi_aware()
    prev = win32gui.GetForegroundWindow()
    win32gui.ShowWindow(handle, win32con.SW_MAXIMIZE)
    if keep_foreground and prev and prev != handle:
        try:
            win32gui.SetForegroundWindow(prev)
        except Exception:
            pass
    time.sleep(0.6)


# ── Windows Graphics Capture (WGC) 后端：抓取窗口「真实合成内容」 ──────────────
_WGC_CACHE: bool | None = None
# D3D 设备跨调用复用（单进程内多次抓屏提速）。CLI 每次独立进程用不到，MCP/循环采集可用。
_WGC_DEVICE = None


def _wgc_backend_available() -> bool:
    """懒加载判断 winsdk 是否可用（仅探测一次）。"""
    global _WGC_CACHE
    if _WGC_CACHE is None:
        try:
            import winsdk._winrt  # noqa: F401
            _WGC_CACHE = True
        except Exception:
            _WGC_CACHE = False
    return _WGC_CACHE


def _new_wgc_device():
    """构造一个用于 Windows Graphics Capture 的 Direct3D 设备。

    说明：winsdk 未直接暴露 D3D11CreateDevice，社区通行做法是借
    ``LearningModelDevice(DIRECT_X_HIGH_PERFORMANCE)`` 的 ``direct3_d11_device``
    拿一个 D3D11 设备（WGC 的 ``Direct3D11CaptureFramePool`` 需要）。这是文档化的
    变通手段，对 D3D11 设备是否来自 ML 命名空间不敏感。
    """
    from winsdk.windows.ai.machinelearning import LearningModelDevice, LearningModelDeviceKind

    return LearningModelDevice(LearningModelDeviceKind.DIRECT_X_HIGH_PERFORMANCE).direct3_d11_device


def _get_wgc_device():
    """获取（并缓存，仅成功时缓存）Direct3D 设备。多次抓屏复用同一设备更快。"""
    global _WGC_DEVICE
    if _WGC_DEVICE is not None:
        return _WGC_DEVICE
    _WGC_DEVICE = _new_wgc_device()
    return _WGC_DEVICE


def capture_window_wgc(hwnd: int, timeout: float = 4.0) -> Image.Image | None:
    """用 Windows Graphics Capture 抓取指定窗口的真实合成内容。

    返回 PIL 图；任何失败/超时/未安装均返回 None（不抛错，由调用方回退）。
    """
    import asyncio
    import threading

    try:
        import winsdk._winrt as wr
        import winsdk.windows.graphics.capture as gc
        from winsdk.windows.graphics.capture.interop import create_for_window
        from winsdk.windows.ai.machinelearning import LearningModelDevice, LearningModelDeviceKind
        from winsdk.windows.graphics.directx import DirectXPixelFormat
        from winsdk.windows.graphics.imaging import SoftwareBitmap, BitmapBufferAccessMode
    except Exception:
        return None

    session = pool = None
    try:
        wr.init_apartment(wr.MTA)
        item = create_for_window(hwnd)
        try:
            device = _get_wgc_device()
        except Exception:
            device = _new_wgc_device()
        pool = gc.Direct3D11CaptureFramePool.create_free_threaded(
            device, DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED, 1, item.size
        )
        session = pool.create_capture_session(item)
        session.start_capture()

        ev = threading.Event()
        frames = []

        def on_frame(_sender, _args) -> None:
            f = pool.try_get_next_frame()
            if f is not None:
                frames.append(f)
                ev.set()

        pool.add_frame_arrived(on_frame)
        if not ev.wait(timeout):
            return None
        frame = frames[0]

        async def _copy_surface():
            op = SoftwareBitmap.create_copy_from_surface_async(frame.surface)
            return await op

        sb = asyncio.run(_copy_surface())
        buf = sb.lock_buffer(BitmapBufferAccessMode.READ)
        raw = bytes(buf.create_reference())
        return Image.frombytes("RGBA", (sb.pixel_width, sb.pixel_height), raw).convert("RGB")
    except Exception:
        return None
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass
        try:
            if pool is not None:
                pool.close()
        except Exception:
            pass


def capture_window(
    handle: int | None = None,
    title_substr: str | None = None,
    maximize: bool = False,
) -> Image.Image:
    """抓取窗口画面（自愈式：WGC 优先，失败回退 PrintWindow/读合成桌面区域）。"""
    _set_dpi_aware()
    if handle is None:
        if not title_substr:
            raise ValueError("capture_window 需要 handle 或 title_substr 之一")
        win = find_window(title_substr)
        if win is None:
            raise LookupError(f"未找到标题含 {title_substr!r} 的窗口")
        handle = win.handle

    # 先记录「我们是否将要改动窗口状态」：只有这两种情况才需要事后还原。
    # ⚠️ 普通窗口抓取是纯只读的，**绝不能**无条件写回 placement —— 那会撤销用户的吸附/分屏
    # （Windows 吸附态下 rcNormalPosition 是吸附前的位置，写回它等于取消吸附）。
    try:
        was_iconic = bool(win32gui.IsIconic(handle))
    except Exception:
        was_iconic = False
    saved_placement = _safe_get_placement(handle) if should_restore_placement(maximize, was_iconic) else None

    # 跟踪：在任何可能改动窗口的内部动作**之前**先取一次快照，随后按阶段对比。
    # 未开启跟踪时 snapshot() 直接返回 None，这里退化为零开销。
    _tr = _trace_snapshot(handle)
    _tr_backend = "none"
    _tr_outcome = "exception"

    def _grab_region() -> Image.Image:
        _ensure_foreground(handle)
        rect = _safe_get_window_rect(handle)
        if rect is None:
            raise RuntimeError(f"窗口 {handle} 无法确定矩形")
        return _grab_rect(*rect)

    try:
        _prepare_window_for_capture(handle, maximize=maximize)
        if _tr is not None:
            _tr = _trace_step(handle, "prepare(maximize=%s)" % maximize, _tr) or _tr

        if _wgc_backend_available():
            wgc_img = capture_window_wgc(handle)
            if _tr is not None:
                _tr = _trace_step(handle, "wgc", _tr) or _tr
            # WGC 对某些合成/工具窗口会返回"纯黑空帧"（如微信的 Qt 工具窗），
            # 不能仅凭非 None 就信任；空白帧要回退到 PrintWindow/桌面区域抓取。
            if wgc_img is not None and not _is_blank_image(wgc_img):
                _tr_backend, _tr_outcome = "wgc", "ok"
                return wgc_img
            _tr_backend = "wgc-blank"

        if _is_gpu_composited_window(handle):
            _tr_backend, _tr_outcome = "grab_region(gpu)", "ok"
            return _grab_region()

        img = _print_window(handle)
        if _tr is not None:
            _tr = _trace_step(handle, "printwindow", _tr) or _tr
        if img is not None and not _is_blank_image(img):
            _tr_backend, _tr_outcome = "printwindow", "ok"
            return img

        _tr_backend, _tr_outcome = "grab_region(fallback)", "ok"
        return _grab_region()
    except BaseException:
        _tr_outcome = "exception"
        raise
    finally:
        # saved_placement 只在「我们确实改过窗口状态」时才非 None（见 should_restore_placement）：
        # 普通窗口抓取时它是 None，_restore_placement 直接返回 —— 不会去撤销用户的吸附/分屏。
        _restore_placement(handle, saved_placement)
        if _tr is not None:
            _trace_capture(handle, _tr_backend, _tr_outcome, _tr, _trace_snapshot(handle))


def capture_screen() -> Image.Image:
    """抓取整屏（多显示器时覆盖全部屏幕）。"""
    _set_dpi_aware()
    return ImageGrab.grab(all_screens=True)
