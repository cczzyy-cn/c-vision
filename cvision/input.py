"""用户级输入：模拟真人操作（鼠标点击/移动/滚动、键盘输入/快捷键、窗口聚焦）。

跨平台用 ``pyautogui``（需本机有显示）；窗口聚焦在 Windows 用 pywin32 置前。
所有函数都直接作用于**当前桌面**，被调用前请先 ``see`` 确认目标。
"""

from __future__ import annotations

import sys
import time

# 常见按键名 -> pyautogui 键名
_KEY_ALIASES = {
    "enter": "enter", "return": "enter", "tab": "tab", "esc": "esc", "escape": "esc",
    "space": "space", "backspace": "backspace", "delete": "delete", "del": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
    "ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "win": "win",
    "cmd": "win", "meta": "win",
}
for _i in range(1, 13):
    _KEY_ALIASES[f"f{_i}"] = f"f{_i}"


def _require_pyautogui():
    try:
        import pyautogui  # noqa: F401
        return pyautogui
    except ImportError:
        raise RuntimeError("需要 pyautogui：python -m pip install pyautogui（并确保有桌面环境/显示）")


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _parse_keys(keys: str) -> tuple[list[str], str]:
    """解析 ``ctrl+shift+t`` 之类的组合，返回 (修饰键列表, 主键)。"""
    parts = [p.strip().lower() for p in keys.replace(" ", "").split("+") if p.strip()]
    mods, main = parts[:-1], parts[-1]
    main = _KEY_ALIASES.get(main, main)
    mods = [_KEY_ALIASES.get(m, m) for m in mods]
    return mods, main


def show_command_for(*, is_minimized: bool, is_maximized: bool) -> str | None:
    """置前前是否需要 ``ShowWindow``，以及用哪个语义。

    **只有最小化的窗口**才需要 ``SW_RESTORE``；**最大化的窗口绝不能调用它**——``SW_RESTORE``
    会把最大化窗口降级成普通尺寸（用户看到的就是「窗口被改成半屏」）。最大化和普通窗口都保持原状，
    只做置前。这条守卫与 ``capture/windows.py:_prepare_window_for_capture`` 保持一致。

    :returns: ``"restore"`` → 调用 ``ShowWindow(SW_RESTORE)``；``"keep"`` / ``None`` → 不碰它的
        尺寸与位置。
    """
    if is_minimized:
        return "restore"
    if is_maximized:
        return "keep"
    return None


def bring_to_front(hwnd: int, gui, con) -> None:
    """把一个窗口置前，且**不改变**它的最大化/普通状态。

    ``gui`` / ``con`` 是 ``win32gui`` / ``win32con``，由调用方传入——这样这条逻辑可以在非 Windows
    环境里用假对象单测（本仓库的 CI 在 Linux/macOS 上跑）。
    """
    try:
        command = show_command_for(
            is_minimized=bool(gui.IsIconic(hwnd)),
            is_maximized=bool(gui.IsZoomed(hwnd)),
        )
        if command == "restore":
            gui.ShowWindow(hwnd, con.SW_RESTORE)  # 仅最小化时还原，最大化不能被降级
    except Exception:
        pass
    try:
        gui.SetForegroundWindow(hwnd)
        gui.BringWindowToTop(hwnd)
    except Exception:
        pass


def focus_window(title_substr: str | None = None, handle: int | None = None) -> int | None:
    """把窗口置前：优先按 ``handle`` 精确定位，否则按标题（精确标题优先，其次子串）。

    **只置前，不改窗口状态**：最小化的窗口会被还原，最大化/普通窗口保持原样（同理，不抢前台时也不
    改尺寸）。Windows 用 pywin32；仅 Windows 支持。返回最终置前的窗口句柄。
    """
    if not _is_windows():
        raise RuntimeError("focus_window 仅在 Windows 上支持（依赖 pywin32）")
    import win32con
    import win32gui

    if handle is None:
        if not title_substr:
            raise ValueError("focus_window 需要 handle 或 title_substr 之一")
        # 统一走 capture 层挑选：精确标题优先，避免“微信”误中标题含它的浏览器标签
        from cvision.capture import list_windows, pick_window

        win = pick_window(list_windows(), title_substr)
        if win is None:
            raise LookupError(f"未找到标题含 {title_substr!r} 的窗口")
        handle = win.handle

    hwnd = int(handle)
    if not win32gui.IsWindow(hwnd):
        raise LookupError(f"无效窗口句柄 {handle}")
    bring_to_front(hwnd, win32gui, win32con)
    time.sleep(0.2)
    return hwnd


def move(x: int, y: int) -> None:
    _require_pyautogui().moveTo(x, y, duration=0.1)


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    pg = _require_pyautogui()
    if double:
        pg.doubleClick(x, y, button=button)
    else:
        pg.click(x, y, button=button)


def scroll(x: int, y: int, dx: int = 0, dy: int = 0) -> None:
    pg = _require_pyautogui()
    pg.moveTo(x, y, duration=0.05)
    # pyautogui.scroll(正数)=向上滚，负数=向下滚。dy>0 约定为“向上滚”，故直接传 dy。
    if dy:
        pg.scroll(dy, x, y)
    if dx and hasattr(pg, "hscroll"):
        pg.hscroll(dx, x, y)


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
    """从 (x1,y1) 拖拽到 (x2,y2)（模拟按住左键拖动）。"""
    pg = _require_pyautogui()
    pg.moveTo(x1, y1, duration=0.1)
    pg.dragTo(x2, y2, duration=0.3, button=button)


def _win_clipboard_text() -> str | None:
    try:
        import win32clipboard
        import win32con
    except ImportError as e:  # pragma: no cover - 仅当未装 pywin32
        raise RuntimeError("Windows 剪贴板需要 pywin32：pip install pywin32") from e
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return ""
    finally:
        win32clipboard.CloseClipboard()


def _win_set_clipboard(text: str) -> None:
    try:
        import win32clipboard
        import win32con
    except ImportError as e:  # pragma: no cover - 仅当未装 pywin32
        raise RuntimeError("Windows 剪贴板需要 pywin32：pip install pywin32") from e
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def get_clipboard() -> str:
    """读取剪贴板文本（Windows 原生；其他平台尝试 pyperclip，缺则报错）。"""
    if _is_windows():
        return _win_clipboard_text() or ""
    try:
        import pyperclip  # type: ignore[import-not-found]
        return pyperclip.paste()
    except Exception:
        raise RuntimeError(
            "剪贴板读取仅支持 Windows（pywin32）；其他平台请先 pip install pyperclip"
        )


def set_clipboard(text: str) -> None:
    """写入剪贴板文本。"""
    if _is_windows():
        _win_set_clipboard(text)
        return
    try:
        import pyperclip  # type: ignore[import-not-found]
        pyperclip.copy(text)
    except Exception:
        raise RuntimeError(
            "剪贴板写入仅支持 Windows（pywin32）；其他平台请先 pip install pyperclip"
        )


def _read_clipboard_text(win32clipboard, win32con) -> str | None:
    """读剪贴板文本；没有文本格式或读不到（被别的进程占用）时返回 None。"""
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    except Exception:
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def _write_clipboard_text(win32clipboard, win32con, text: str) -> None:
    """把文本写进剪贴板（清空后写入）；被占用时抛异常，由调用方决定怎么处理。"""
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _paste_clipboard(text: str) -> None:
    """用剪贴板 + Ctrl+V 输入非 ASCII 文本（pyautogui.write 打不进中文等）。

    ⚠️ **不能盲目恢复剪贴板**。这里有个真实的竞态：本函数为了打字临时占用剪贴板，打完再把
    旧内容写回去——但如果**用户在我们占用期间复制了别的东西**，那个「恢复」会把用户刚复制的
    内容直接覆盖掉（这是本插件里唯一会破坏用户数据的路径）。所以恢复前要比对：只有当剪贴板
    **仍是我们写进去的那份**时才恢复；一旦发现被改动，就尊重用户的新内容，不动它。
    """
    try:
        import win32clipboard
        import win32con
    except ImportError as e:  # pragma: no cover - 仅当未装 pywin32
        raise RuntimeError("输入非 ASCII 文本需要 pywin32（Windows）：pip install pywin32") from e
    old = _read_clipboard_text(win32clipboard, win32con)
    _write_clipboard_text(win32clipboard, win32con, text)
    time.sleep(0.05)
    _require_pyautogui().hotkey("ctrl", "v")
    # 只有剪贴板仍是我们写进去的 `text` 才恢复旧内容；否则说明用户在这期间复制了东西，
    # 那份新内容远比「恢复我们的旧备份」重要，必须原样留着。
    if old is not None and _read_clipboard_text(win32clipboard, win32con) == text:
        try:
            _write_clipboard_text(win32clipboard, win32con, old)
        except Exception:
            pass


def type_text(text: str) -> None:
    pg = _require_pyautogui()
    if text.isascii():
        pg.write(text, interval=0.03)
        return
    _paste_clipboard(text)


def press_keys(keys: str) -> None:
    """发送快捷键/按键，如 ``ctrl+l``、``enter``、``ctrl+shift+t``。"""
    pg = _require_pyautogui()
    mods, main = _parse_keys(keys)
    if mods:
        pg.hotkey(*(mods + [main]))
    else:
        pg.press(main)


# 供 CLI/测试判断能力。**按平台如实报告**：focus_window 只在 Windows 实现（别的地方调用会抛错），
# 所以非 Windows 上不能把它算作能力——`cvision_status` 就是这么读的。
_BASE_CAPABILITIES = [
    "click",
    "double_click",
    "move",
    "scroll",
    "drag",
    "type_text",
    "press_keys",
    "focus_window",
    "get_clipboard",
    "set_clipboard",
]
CAPABILITIES = [cap for cap in _BASE_CAPABILITIES if cap != "focus_window" or _is_windows()]
