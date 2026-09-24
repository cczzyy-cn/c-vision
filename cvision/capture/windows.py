"""Windows 屏幕/窗口捕获后端。

依赖 pywin32 + Pillow（WGC 后端可选依赖 winsdk）。窗口捕获优先级：
    Windows Graphics Capture(真实合成内容) > PrintWindow(PW_RENDERFULLCONTENT) >
    ImageGrab 读合成桌面区域。
WGC 能抓 GPU/Chromium 合成窗口与「被遮挡窗口」的真实内容；PrintWindow 只对普通
GDI 窗口可靠；读合成桌面区域作为最后兜底。
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

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

    ⚠️ 这条路径的置前**必须真的成功**：``_grab_region`` 读的是屏幕该矩形处的合成画面，
    目标窗口不在最前时抓到的就是**遮挡它的那个窗口**的内容，而元素坐标仍按目标窗口的矩形换算
    ——画面与坐标会一起错。

    实现已与 ``cvision.input``（``focus_window`` 用的那份）统一。旧版本在这里自己写了一份
    ``AttachThreadInput``，但把当前线程挂到了**目标窗口线程**上；前台规则要求的是挂到
    **前台窗口线程**。实测前者 ``AttachThreadInput`` 返回 0、置前 100% 失败，后者返回 1、
    一击即中。这里刻意不再保留第二份实现，避免两处再次漂移。
    """
    try:
        from cvision.input import _force_foreground_native

        _force_foreground_native(int(hwnd))
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
# 抓帧实现与 D3D 设备缓存都在 `cvision.capture.wgc`（那里按 PyWinRT → winsdk 的顺序挑绑定）。
_WGC_DEVICE = None

#: WGC 可用性的**真实**探测结果（进程内缓存 + 磁盘缓存）。
#:
#: 为什么不能只看 `import winsdk`：包装上了 ≠ WGC 能用。WGC 需要一个可用的 D3D11 设备，而拿
#: 设备这一步在虚拟机 / 受限驱动上会失败 —— 实测 VMware SVGA 3D 虚拟显卡上
#: ``LearningModelDevice(DIRECT_X_*)`` 返回 ``DXGI_ERROR_UNSUPPORTED``，此时 WGC 永远抓不到帧，
#: 但 ``import winsdk`` 依旧成功。只看 import 会让 ``cvision_status`` 报出「能抓被遮挡窗口」的
#: 错误期待。
#:
#: 为什么还要**磁盘**缓存：首次探测要 ~280ms（初始化 WinML/DirectX 栈），而抓一张图才 ~170ms；
#: CLI 每次调用都是新进程，进程内缓存救不了它，于是每次 ``see`` 都白付这 280ms。
_WGC_PROBE_MEMORY: dict | None = None
_WGC_PROBE_FILE = Path(tempfile.gettempdir()) / "cvision-wgc-probe.json"
#: 失败结论的保鲜期：过了就重新探测，免得环境修好后（装驱动、换显卡、重装绑定包）一直被误判。
_WGC_PROBE_TTL_SECONDS = 12 * 3600


def _wgc_import_ok() -> bool:
    """有没有可用的 Python WinRT 绑定（PyWinRT 优先，回退 winsdk）。

    只说明「包在」，**不代表能抓帧** —— 能不能抓要看 :func:`wgc_probe` 的真实探测。
    """
    from cvision.capture import wgc

    return wgc.binding() is not None


def _environment_fingerprint() -> str:
    """环境指纹：解释器 / 系统版本 / **Python WinRT 绑定**任何一项变了，缓存立刻作废。

    绑定这一项是关键：装了 PyWinRT（``winrt``）之后，WGC 可能从「不可用」直接变成「可用」
    （它走标准 D3D11 互操作，不依赖 WinML）。指纹里不含它的话，旧的失败结论会一直被复用。
    """
    parts = [sys.version.split()[0], platform.version(), platform.machine()]
    for module_name in ("winsdk._winrt", "winrt._winrt"):
        try:
            module = __import__(module_name, fromlist=["_winrt"])
            stat = os.stat(module.__file__)
            parts.append(f"{stat.st_size}:{int(stat.st_mtime)}")
        except Exception:
            parts.append("absent")
    return "|".join(parts)


def _read_probe_cache() -> dict | None:
    """读磁盘上的**失败**结论（可用时本来就该走 WGC，没什么好跳过的）。"""
    try:
        raw = json.loads(_WGC_PROBE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("available"):
        return None
    if raw.get("fingerprint") != _environment_fingerprint():
        return None
    try:
        age = time.time() - float(raw.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > _WGC_PROBE_TTL_SECONDS:
        return None
    raw["cached"] = True
    return raw


def _write_probe_cache(result: dict) -> None:
    try:
        _WGC_PROBE_FILE.write_text(
            json.dumps(
                {"fingerprint": _environment_fingerprint(), "ts": time.time(), **result},
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def wgc_probe(force: bool = False) -> dict:
    """**真实**探测 WGC 能不能用——而不是看包在不在。

    :param force: 忽略缓存重新探测。
    :returns: ``{"available": bool, "reason": str, "binding": str, "cached": bool}``；
        ``binding`` 是实际拿到设备的那套绑定（``winrt`` = 标准互操作，``winsdk`` = WinML 变通）。
    """
    global _WGC_PROBE_MEMORY
    if not force:
        if _WGC_PROBE_MEMORY is not None:
            return dict(_WGC_PROBE_MEMORY)
        cached = _read_probe_cache()
        if cached is not None:
            _WGC_PROBE_MEMORY = cached
            return dict(cached)

    from cvision.capture import wgc

    found = wgc.binding()
    if found is None:
        result = {
            "available": False,
            "reason": (
                "未安装 Python WinRT 绑定：pip install winrt-runtime winrt-Windows.Graphics.Capture "
                "winrt-Windows.Graphics.Capture.Interop winrt-Windows.Graphics.DirectX "
                "winrt-Windows.Graphics.DirectX.Direct3D11 "
                "winrt-Windows.Graphics.DirectX.Direct3D11.Interop（或旧的 winsdk）"
            ),
            "binding": "",
            "cached": False,
        }
    else:
        try:
            wgc.device()
            result = {"available": True, "reason": "ok", "binding": found.name, "cached": False}
        except Exception as exc:  # noqa: BLE001 - 探测失败即不可用，原因如实带回
            result = {
                "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "binding": found.name,
                "cached": False,
            }

    if not result["available"]:
        _write_probe_cache(result)
    _WGC_PROBE_MEMORY = result
    return dict(result)


def _wgc_backend_available() -> bool:
    """WGC 是否**真的**可用（走 :func:`wgc_probe`：真实探测 + 缓存，不再只看 import）。"""
    return bool(wgc_probe().get("available"))


def _new_wgc_device():
    """构造一个用于 Windows Graphics Capture 的 Direct3D 设备。

    实现在 :mod:`cvision.capture.wgc`：那里**优先走 PyWinRT 的标准 D3D11 互操作**
    （``D3D11CreateDevice`` → ``QI(IDXGIDevice)`` → 官方 interop），失败才回退 ``winsdk`` 的
    ``LearningModelDevice`` 变通路径。保留这个薄封装只为让既有调用方与测试继续可用。
    """
    from cvision.capture import wgc

    return wgc.device()


def _get_wgc_device():
    """获取（并缓存）Direct3D 设备；缓存由 :func:`cvision.capture.wgc.device` 负责。"""
    return _new_wgc_device()


def capture_window_wgc(hwnd: int, timeout: float = 4.0) -> Image.Image | None:
    """用 Windows Graphics Capture 抓取指定窗口的真实合成内容。

    实现在 :mod:`cvision.capture.wgc`（优先 PyWinRT 标准互操作，回退 ``winsdk``/WinML）。
    返回 PIL 图；任何失败、超时或缺依赖都返回 ``None``，由调用方回退到别的后端。
    """
    from cvision.capture import wgc

    return wgc.capture_window(int(hwnd), timeout=timeout)


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
