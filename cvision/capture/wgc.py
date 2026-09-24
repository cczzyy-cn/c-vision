"""Windows Graphics Capture（WGC）：抓窗口的**真实合成内容**（含被遮挡窗口）。

支持两种 Python WinRT 绑定，优先用新的那套：

1. ``winrt``（PyWinRT 3.x）+ 官方 interop 包 —— 走**标准** D3D11 互操作路径；
2. ``winsdk``（旧绑定）—— 只能借 ``LearningModelDevice`` 变通拿设备。

**为什么优先级是这样（真机实测）**：借 WinML 那条路要求 GPU 满足 WinML 推理栈的设备接口，
在虚拟机上会失败 —— VMware SVGA 3D 实测返回 ``DXGI_ERROR_UNSUPPORTED``（0x887A0004），于是
WGC 永远抓不到帧；可同一台机器 ``GraphicsCaptureSession.is_supported()`` 是 True、
``D3D11CreateDevice(HARDWARE, 11_0)`` 也完全成功。也就是**机器没问题，是拿设备的路子不对**。
换到 PyWinRT 的标准 interop 后，同一台虚拟机立刻抓到了真实帧（方差 1889，非空白）。

依赖（全部可选，缺了只是回退到旧绑定）::

    pip install winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections \\
        winrt-Windows.Graphics winrt-Windows.Graphics.Capture \\
        winrt-Windows.Graphics.Capture.Interop winrt-Windows.Graphics.DirectX \\
        winrt-Windows.Graphics.DirectX.Direct3D11 \\
        winrt-Windows.Graphics.DirectX.Direct3D11.Interop winrt-Windows.Graphics.Imaging
"""

from __future__ import annotations

import asyncio
import ctypes
import threading
from typing import Any, Callable, NamedTuple

# ── D3D11 互操作用的常量与 IID ────────────────────────────────────────────────
_D3D_DRIVER_TYPE_HARDWARE = 1
_D3D_DRIVER_TYPE_WARP = 5
_D3D_FEATURE_LEVEL_11_0 = 0xB000
_D3D11_SDK_VERSION = 7
_DXGI_ERROR_UNSUPPORTED = 0x887A0004


class _GUID(ctypes.Structure):
    _fields_ = [
        ("d1", ctypes.c_ulong),
        ("d2", ctypes.c_ushort),
        ("d3", ctypes.c_ushort),
        ("d4", ctypes.c_ubyte * 8),
    ]


#: IDXGIDevice {54EC77FA-1377-44E6-8C32-88FD5F44C84C}
_IID_IDXGIDEVICE = _GUID(
    0x54EC77FA, 0x1377, 0x44E6,
    (ctypes.c_ubyte * 8)(0x8C, 0x32, 0x88, 0xFD, 0x5F, 0x44, 0xC8, 0x4C),
)


def _raw_d3d11_device() -> int:
    """用 ``d3d11.dll`` 建一个原始 ``ID3D11Device``，返回指针。

    先试硬件设备（特性级别 11_0），失败再退到 WARP（软件光栅器）——WGC 只要一个合法的
    ``IDirect3DDevice``，用 WARP 也能抓到帧，只是慢一些。
    """
    d3d11 = ctypes.WinDLL("d3d11")
    d3d11.D3D11CreateDevice.restype = ctypes.c_long
    last = ""
    for driver_type, label in ((_D3D_DRIVER_TYPE_HARDWARE, "HARDWARE"), (_D3D_DRIVER_TYPE_WARP, "WARP")):
        device = ctypes.c_void_p()
        context = ctypes.c_void_p()
        feature = ctypes.c_uint(_D3D_FEATURE_LEVEL_11_0)
        hr = d3d11.D3D11CreateDevice(
            None, driver_type, None, 0, ctypes.byref(feature), 1, _D3D11_SDK_VERSION,
            ctypes.byref(device), None, ctypes.byref(context),
        )
        if hr == 0 and device.value:
            return int(device.value)
        last = f"{label} hr=0x{hr & 0xFFFFFFFF:08X}"
    raise RuntimeError(f"D3D11CreateDevice 失败（{last}）")


def _dxgi_device(d3d11_device: int) -> int:
    """对 ``ID3D11Device`` 做 ``QueryInterface(IDXGIDevice)``，返回指针。

    互操作函数要的是 ``IDXGIDevice``，不是 ``ID3D11Device``——直接传后者会拿到
    "convert_to returned null" 一类含糊的失败。
    """
    vtable = ctypes.cast(
        ctypes.c_void_p(d3d11_device), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    )[0]
    query_interface = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
    )(vtable[0])  # IUnknown::QueryInterface 是 vtable[0]
    out = ctypes.c_void_p()
    hr = query_interface(ctypes.c_void_p(d3d11_device), ctypes.byref(_IID_IDXGIDEVICE), ctypes.byref(out))
    if hr != 0 or not out.value:
        raise RuntimeError(f"QueryInterface(IDXGIDevice) 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    return int(out.value)


class _Binding(NamedTuple):
    """一套 WinRT 绑定所需的全部入口（两种绑定的 API 形状一致，只是来源不同）。"""

    name: str
    create_for_window: Callable[[int], Any]
    frame_pool: Any
    pixel_format: Any
    software_bitmap: Any
    buffer_access_mode: Any
    init_apartment: Callable[[], None]
    make_device: Callable[[], Any]


def _load_winrt() -> _Binding | None:
    """PyWinRT 3.x 绑定（**标准路径**）。装不齐就返回 None，由调用方回退。"""
    try:
        import winrt.runtime as runtime
        import winrt.windows.graphics.capture as capture
        from winrt.windows.graphics.capture.interop import create_for_window
        from winrt.windows.graphics.directx import DirectXPixelFormat
        from winrt.windows.graphics.directx.direct3d11.interop import (
            create_direct3d11_device_from_dxgi_device,
        )
        from winrt.windows.graphics.imaging import BitmapBufferAccessMode, SoftwareBitmap
    except Exception:
        return None

    def make_device() -> Any:
        # 标准互操作：D3D11CreateDevice → QI(IDXGIDevice) → 官方 interop 转成 WinRT 设备。
        # 这条不经过 WinML，因此在虚拟机/受限驱动上同样可用。
        return create_direct3d11_device_from_dxgi_device(_dxgi_device(_raw_d3d11_device()))

    def init_apartment() -> None:
        runtime.init_apartment(runtime.ApartmentType.MULTI_THREADED)

    return _Binding(
        name="winrt",
        create_for_window=create_for_window,
        frame_pool=capture.Direct3D11CaptureFramePool,
        pixel_format=DirectXPixelFormat,
        software_bitmap=SoftwareBitmap,
        buffer_access_mode=BitmapBufferAccessMode,
        init_apartment=init_apartment,
        make_device=make_device,
    )


def _load_winsdk() -> _Binding | None:
    """``winsdk`` 绑定（旧路径：借 ``LearningModelDevice`` 拿设备）。

    保留它是为了兼容只装了 ``winsdk`` 的环境。注意这条路在 WinML GPU 不可用的机器上
    （虚拟机、受限驱动）必然失败——那正是 ``winrt`` 绑定存在的理由。
    """
    try:
        import winsdk._winrt as wr
        import winsdk.windows.graphics.capture as capture
        from winsdk.windows.ai.machinelearning import LearningModelDevice, LearningModelDeviceKind
        from winsdk.windows.graphics.capture.interop import create_for_window
        from winsdk.windows.graphics.directx import DirectXPixelFormat
        from winsdk.windows.graphics.imaging import BitmapBufferAccessMode, SoftwareBitmap
    except Exception:
        return None

    def make_device() -> Any:
        return LearningModelDevice(LearningModelDeviceKind.DIRECT_X_HIGH_PERFORMANCE).direct3_d11_device

    def init_apartment() -> None:
        wr.init_apartment(wr.MTA)

    return _Binding(
        name="winsdk",
        create_for_window=create_for_window,
        frame_pool=capture.Direct3D11CaptureFramePool,
        pixel_format=DirectXPixelFormat,
        software_bitmap=SoftwareBitmap,
        buffer_access_mode=BitmapBufferAccessMode,
        init_apartment=init_apartment,
        make_device=make_device,
    )


_BINDING: _Binding | None = None
_BINDING_LOADED = False
_DEVICE: Any = None


def binding() -> _Binding | None:
    """当前可用的绑定（winrt 优先），只探测一次。"""
    global _BINDING, _BINDING_LOADED
    if not _BINDING_LOADED:
        _BINDING = _load_winrt() or _load_winsdk()
        _BINDING_LOADED = True
    return _BINDING


def device() -> Any:
    """拿一个可复用的 WinRT D3D 设备（进程内缓存；失败不缓存，交给上层 probe 管）。"""
    global _DEVICE
    if _DEVICE is not None:
        return _DEVICE
    found = binding()
    if found is None:
        raise RuntimeError(
            "WGC 需要 Python WinRT 绑定：pip install winrt-runtime "
            "winrt-Windows.Graphics.Capture winrt-Windows.Graphics.Capture.Interop "
            "winrt-Windows.Graphics.DirectX winrt-Windows.Graphics.DirectX.Direct3D11 "
            "winrt-Windows.Graphics.DirectX.Direct3D11.Interop（或旧的 winsdk）"
        )
    _DEVICE = found.make_device()
    return _DEVICE


def capture_window(handle: int, timeout: float = 4.0):
    """用 WGC 抓一帧窗口画面；任何失败都返回 ``None``（由调用方回退到别的后端）。"""
    found = binding()
    if found is None:
        return None

    session = pool = None
    try:
        found.init_apartment()
        item = found.create_for_window(int(handle))
        pool = found.frame_pool.create_free_threaded(
            device(),
            found.pixel_format.B8_G8_R8_A8_UINT_NORMALIZED,
            1,
            item.size,
        )
        session = pool.create_capture_session(item)
        session.start_capture()

        arrived = threading.Event()
        frames: list[Any] = []

        def on_frame(_sender, _args) -> None:
            frame = pool.try_get_next_frame()
            if frame is not None:
                frames.append(frame)
                arrived.set()

        pool.add_frame_arrived(on_frame)
        if not arrived.wait(timeout):
            return None

        async def copy_surface():
            return await found.software_bitmap.create_copy_from_surface_async(frames[0].surface)

        bitmap = asyncio.run(copy_surface())
        buffer = bitmap.lock_buffer(found.buffer_access_mode.READ)
        try:
            raw = memoryview(buffer.create_reference()).tobytes()
        finally:
            buffer.close()

        from PIL import Image

        return Image.frombytes(
            "RGBA", (bitmap.pixel_width, bitmap.pixel_height), raw
        ).convert("RGB")
    except Exception:
        return None
    finally:
        for closeable in (session, pool):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception:
                pass
