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


def _ocr_engine() -> str:
    # Windows.Media.Ocr(winsdk) 优先，其次 pytesseract。
    ok_win, _ = _module_ok("winsdk.windows.media.ocr")
    if ok_win:
        return "windows-media-ocr"
    ok_ts, _ = _module_ok("pytesseract")
    if ok_ts:
        return "pytesseract"
    return "none"


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

    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        # 键名是历史拼写（`cvison_dir`，少个 i），保留以兼容既有消费方；值 = **包根目录**。
        # 不能用 os.getcwd()：插件侧为了让 Windows 能替换安装目录，已把子进程 cwd 改成系统临时目录
        # （见 src/index.ts 的 PY_CWD），那里的 cwd 不再代表 cvision 的位置。
        "cvison_dir": os.environ.get("CVISION_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backend": backend,
        "backend_known": backend_known,
        "backend_implemented": backend_implemented,
        "ocr_engine": _ocr_engine(),
        "input_capabilities": input_caps,
        "deps": deps_status,
        "ok": backend_implemented and deps_status.get("Pillow", False),
    }


if __name__ == "__main__":  # pragma: no cover - 用于快速自检
    import json

    print(json.dumps(status(), ensure_ascii=False, indent=2))
