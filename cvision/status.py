"""环境健康探针：检测插件运行所需依赖/后端是否可用，返回结构化信息。

供 ``cvision_status`` 工具调用，把「缺什么/装什么」变成可操作提示，避免裸
``execFile`` 的 ENOENT / import 报错。
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys

from cvision import capturer


def _module_ok(name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(name)
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - 探测失败即视为缺失
        return False, str(e)


def _backend() -> str:
    try:
        from cvision.capture import backend
        return backend.__name__.split(".")[-1] or "unknown"
    except Exception:
        return "unknown"


def _platform_support(backend: str, backend_implemented: bool) -> str:
    """把「本平台到底能不能用」变成**机器可读**的三态，而不是让人去读文档。

    - ``supported``：已在本平台实测（Windows）。
    - ``unverified``：代码写完了但**没在真机验证过**（macOS）——用起来可能踩边界，但确实有实现。
    - ``unsupported``：明确未实现（Linux Phase 2），调用会得到清晰报错。

    为什么要单列：README 只能写给人看，而模型在决定「要不要尝试 computer-use」时需要的是
    一个明确字段，不该靠「backend_implemented 是 true 还是 false」去反推可信度。
    """
    if not backend_implemented:
        return "unsupported"
    return "supported" if backend == "windows" else "unverified"


def _ocr_engine() -> str:
    # Windows.Media.Ocr(winsdk) 优先，其次 pytesseract。
    ok_win, _ = _module_ok("winsdk.windows.media.ocr")
    if ok_win:
        return "windows-media-ocr"
    ok_ts, _ = _module_ok("pytesseract")
    if ok_ts:
        return "pytesseract"
    return "none"


def _capture_backends() -> dict:
    """窗口捕获各后端的**真实**可用性（不是「包有没有装上」）。

    为什么值得单列：Windows 上 WGC 需要一个可用的 D3D11 设备，而拿设备这一步在虚拟机 / 受限
    驱动上会失败——实测 VMware SVGA 3D 虚拟显卡上 ``LearningModelDevice(DIRECT_X_*)`` 返回
    ``DXGI_ERROR_UNSUPPORTED``，于是 WGC 永远抓不到帧，**但 ``deps.winsdk`` 依然是 true**。
    只看依赖会把「抓不了被遮挡的窗口」说成能抓，所以这里做一次真实探测（结果有缓存，不慢）。
    """
    try:
        from cvision.capture import backend
    except Exception:
        return {}
    probe = getattr(backend, "wgc_probe", None)
    if probe is None:
        return {}  # 非 Windows 后端没有这个分层
    try:
        info = probe()
    except Exception as e:  # noqa: BLE001 - 探测本身失败也只是「不可用」
        return {"window_capture": ["printwindow", "grab_region"], "wgc": {"available": False, "reason": str(e)}}
    available = bool(info.get("available"))
    return {
        # WGC 不可用时，窗口捕获实际走这两条：PrintWindow 不需要前台，grab_region 需要。
        "window_capture": ["wgc", "printwindow", "grab_region"] if available else ["printwindow", "grab_region"],
        "wgc": {"available": available, "reason": str(info.get("reason") or ""), "cached": bool(info.get("cached"))},
    }


def status() -> dict:
    """返回插件运行环境的状态字典。"""
    try:
        from cvision import input as input_mod
        input_caps = list(getattr(input_mod, "CAPABILITIES", []))
    except Exception:
        input_caps = []

    deps = {
        "Pillow": "PIL",
        "pyautogui": "pyautogui",
        # pyperclip 看着只影响「非 Windows 的文本剪贴板」，实际是**导入期**硬依赖：
        # pyautogui -> mouseinfo -> `import pyperclip`（模块顶层，非惰性），
        # 所以它缺失会让 pyautogui 整个 import 失败 —— 也就是所有输入类工具都挂，
        # 但旧探针只查 pyautogui 会误报 ok，把「能跑」说成能跑。一并探掉。
        "pyperclip": "pyperclip",
    }
    if sys.platform.startswith("win"):
        deps["pywin32"] = "win32gui"
        deps["winsdk"] = "winsdk"
    elif sys.platform == "darwin":
        deps["pyobjc-Quartz"] = "Quartz"

    deps_status = {name: _module_ok(module)[0] for name, module in deps.items()}

    backend = _backend()
    backend_known = backend in ("windows", "macos", "linux")
    backend_implemented = {
        "windows": True,
        "macos": True,
        "linux": False,
    }.get(backend, False)

    # 包根目录。**不能用 os.getcwd()**：插件侧为了让 Windows 能替换安装目录，已把子进程 cwd 改成
    # 系统临时目录（见 src/index.ts 的 PY_CWD），那里的 cwd 不再代表 cvision 的位置。
    package_root = os.environ.get("CVISION_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        # ⚠️ `cvison_dir` 是历史拼写错误（少个 i），**必须保留**：既有消费方按它取值。
        # 同时提供拼写正确的 `cvision_dir`（新代码请用它），将来大版本再删旧键。
        "cvison_dir": package_root,
        "cvision_dir": package_root,
        "backend": backend,
        "backend_known": backend_known,
        "backend_implemented": backend_implemented,
        "platform_support": _platform_support(backend, backend_implemented),
        "ocr_engine": _ocr_engine(),
        "input_capabilities": input_caps,
        "capture_backends": _capture_backends(),
        "deps": deps_status,
        "ok": backend_implemented and deps_status.get("Pillow", False),
    }


if __name__ == "__main__":  # pragma: no cover - 用于快速自检
    import json

    print(json.dumps(status(), ensure_ascii=False, indent=2))
