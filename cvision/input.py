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


def attach_thread_for(current_tid: int, foreground_tid: int, target_tid: int) -> int | None:
    """决定 ``AttachThreadInput`` 该把当前线程挂到**哪个**线程；``None`` 表示不用挂。

    **只能挂前台窗口的线程**（``foreground_tid``）——Windows 的前台规则是「调用线程必须拥有
    前台窗口的输入队列」。把当前线程挂到**目标窗口**的线程（这正是本插件旧实现的做法）满足
    不了这条规则：真机实测 ``AttachThreadInput`` 直接返回 0，紧接着的 ``SetForegroundWindow``
    照旧被前台锁定拒绝，于是 ``focus_window`` 100% 无效。

    这个判断被单独抽成纯函数，就是为了让上面这条规则能被跨平台单测钉死，避免哪天又被改回去。
    """
    if not foreground_tid:
        return None
    if foreground_tid == target_tid:
        return None  # 目标线程本身就是前台线程，无需附加
    if foreground_tid == current_tid:
        return None  # 当前线程已是前台线程，直接调用即可
    return int(foreground_tid)


def _force_foreground_native(hwnd: int) -> bool:
    """尽力把 ``hwnd`` 变成**前台窗口**，返回是否真的成功（仅 Windows）。

    为什么必须这么写（真机实测出来的缺陷）：``SetForegroundWindow`` 会被 Windows 的**前台锁定**
    直接拒绝——非前台进程调用它几乎一定失败。而旧实现的两条路径都不成立：

    * ``bring_to_front`` 只调 ``SetForegroundWindow`` + ``BringWindowToTop``，没有任何兜底；
    * ``capture.windows._ensure_foreground`` 虽有 ``AttachThreadInput`` 兜底，却把当前线程挂到
      **目标窗口的线程**上；前台规则要求的其实是「调用线程拥有**前台窗口**的输入队列」。
      实测：挂目标线程时 ``AttachThreadInput`` 返回 0（附加失败），置前依旧失败；
      挂前台线程返回 1，置前立刻成功。

    因此顺序是：先给当前线程补一个消息队列（CLI 子进程的线程默认没有消息队列，附加会失败），
    再挂**前台窗口的线程**并置前；仍失败就轻敲一次 ALT，让本进程成为「最后收到输入事件的进程」
    以绕过前台锁定。最后以 ``GetForegroundWindow`` 的**实测结果**为准，不靠返回值猜。
    """
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    def foreground() -> int:
        return int(user32.GetForegroundWindow() or 0)

    target = int(hwnd)
    if foreground() == target:
        return True

    # 当前线程可能还没有消息队列；AttachThreadInput 要求调用线程有队列（CLI 子进程尤其如此）。
    msg = wt.MSG()
    user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 0)

    cur_tid = int(kernel32.GetCurrentThreadId())
    pid = wt.DWORD()
    fg = foreground()
    fg_tid = int(user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))) if fg else 0
    target_tid = int(user32.GetWindowThreadProcessId(target, ctypes.byref(pid)))

    attached = False
    attach_tid = attach_thread_for(cur_tid, fg_tid, target_tid)
    if attach_tid is not None:
        attached = bool(user32.AttachThreadInput(cur_tid, attach_tid, True))
    try:
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
        user32.SetFocus(target)
    except Exception:
        pass
    finally:
        if attached:
            try:
                user32.AttachThreadInput(cur_tid, attach_tid, False)
            except Exception:
                pass

    if foreground() == target:
        return True

    # 兜底：轻敲 ALT 把「最后输入事件」交给本进程，从而绕过前台锁定。
    vk_menu, keyeventf_keyup = 0x12, 0x0002
    try:
        user32.keybd_event(vk_menu, 0, 0, 0)
        user32.keybd_event(vk_menu, 0, keyeventf_keyup, 0)
        user32.SetForegroundWindow(target)
    except Exception:
        pass
    return foreground() == target


def window_at(x: int, y: int) -> int:
    """返回屏幕物理坐标 ``(x, y)`` 处**最顶层**窗口的句柄（无窗口时返回 0）。

    用于点击前校验「这个坐标现在到底属于哪个窗口」——屏幕坐标点击只会命中该点最顶层的窗口，
    这正是「坐标没错却点错窗口」的判据。
    """
    if not _is_windows():
        raise RuntimeError("window_at 仅在 Windows 上支持（依赖 ctypes/win32）")
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = wt.HWND
    user32.WindowFromPoint.argtypes = [wt.POINT]
    hwnd = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
    if not hwnd:
        return 0
    root = user32.GetAncestor(hwnd, 2)  # GA_ROOT：从子控件上溯到顶层窗口
    return int(root or hwnd)


def window_title(handle: int) -> str:
    """窗口标题（拿不到就返回空串）。报错时用它点明「究竟是谁挡住了目标」。"""
    try:
        import win32gui

        return str(win32gui.GetWindowText(int(handle)) or "")
    except Exception:
        return ""


def window_rect(handle: int) -> tuple[int, int, int, int] | None:
    """窗口的屏幕矩形 ``(left, top, right, bottom)``；拿不到返回 None。"""
    try:
        import win32gui

        left, top, right, bottom = win32gui.GetWindowRect(int(handle))
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return (int(left), int(top), int(right), int(bottom))


def _caption_height() -> int:
    """标题栏高度（像素）；取不到系统度量时按 30 估。"""
    try:
        import win32api
        import win32con

        return int(win32api.GetSystemMetrics(win32con.SM_CYCAPTION)) or 30
    except Exception:
        return 30


def activation_probes(rect: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    """可用于「点击激活」的候选点，按**安全程度**从高到低排列（纯函数，便于单测）。

    首选**标题栏中央**：那里单击只激活窗口，不会碰到最小化/最大化/关闭按钮，也不会触发标签页
    切换。其余候选依次向左右两侧、以及客户区上沿退让，供没有标准标题栏的窗口兜底。
    """
    left, top, right, bottom = rect
    width = max(1, right - left)
    caption = _caption_height()
    y = top + max(4, caption // 2)
    return [
        (left + width // 2, y),
        (left + width // 4, y),
        (left + width * 3 // 4, y),
        (left + width // 2, top + caption + 6),
    ]


def _visible_probe(target: int, rect: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """在窗口矩形里找一个**当前确实属于它**的候选点；一个都没有 = 它被完全盖住了。"""
    for point in activation_probes(rect):
        if window_at(point[0], point[1]) == target:
            return point
    return None


def ensure_front(
    handle: int,
    at: tuple[int, int] | None = None,
    *,
    unblock: bool = False,
) -> dict:
    """确保 ``handle`` 在前台（点击类动作的前置条件），可选校验坐标归属。

    屏幕坐标点击命中的是该点**最顶层**的窗口——不只是「前台」那么简单。所以这里做两件事：
    先激活目标，再用 :func:`window_at` 复核 ``at=(x, y)`` 确实属于它。

    :param unblock: 目标被**别的窗口盖住**时，是否用「点击激活」把它带到最前。
        **为什么需要它**：程序化提升 z 序在 Windows 上并不成立——实测 ``SetWindowPos(HWND_TOP)``、
        ``BringWindowToTop``、``SwitchToThisWindow`` 都**返回成功却不改变层叠顺序**（后台进程的权限
        所限），只有 ``SetForegroundWindow`` 能改「前台」但改不了「谁盖在谁上面」。而**真实的鼠标
        输入**会触发系统重排层叠——所以模拟人类「先点一下窗口标题栏，再点目标」才是真正的解法。
        代价是**会真的移动并点击一次鼠标**（只点标题栏中央，不碰任何按钮）。

    :returns: ``{"handle","stale","focused","match","inside","front_before","front_after",
        "point_before","point_after","blocker","blocker_title","unblock_point"}``。
        ``stale=True``：窗口已不存在（调用方应丢弃记录）；
        ``inside=False``：给的坐标压根不在目标窗口矩形内（窗口可能被移动过，应重新 ``see``）；
        ``blocker``/``blocker_title``：挡住了该点的那个窗口是谁。
    """
    if not _is_windows():
        raise RuntimeError("ensure_front 仅在 Windows 上支持")
    import win32gui

    target = int(handle)
    if not win32gui.IsWindow(target):
        return {"handle": target, "stale": True, "focused": False, "match": False, "inside": False}

    rect = window_rect(target)
    inside = True
    if at and rect:
        inside = rect[0] <= int(at[0]) < rect[2] and rect[1] <= int(at[1]) < rect[3]

    front_before = int(win32gui.GetForegroundWindow() or 0)
    point_before = window_at(*at) if at else 0
    focused = front_before == target
    if not focused:
        focused = _force_foreground_native(target)
    front_after = int(win32gui.GetForegroundWindow() or 0)
    point_after = window_at(*at) if at else 0

    blocker = 0
    unblock_point: tuple[int, int] | None = None
    if at and point_after != target:
        blocker = point_after
        if unblock and rect is not None:
            probe = _visible_probe(target, rect)
            if probe is not None:
                # 真的点一下：只有真实用户输入才会让系统重排层叠顺序。
                move(probe[0], probe[1])
                time.sleep(0.05)
                click(probe[0], probe[1])
                time.sleep(0.25)
                point_after = window_at(*at)
                unblock_point = probe
                front_after = int(win32gui.GetForegroundWindow() or 0)

    match = (point_after == target) if at else bool(focused)
    return {
        "handle": target,
        "stale": False,
        "focused": bool(focused),
        "match": bool(match),
        "inside": bool(inside),
        "front_before": front_before,
        "front_after": front_after,
        "point_before": point_before,
        "point_after": point_after,
        "blocker": blocker,
        "blocker_title": window_title(blocker) if blocker else "",
        "unblock_point": list(unblock_point) if unblock_point else None,
    }


def focus_window(title_substr: str | None = None, handle: int | None = None) -> int:
    """把窗口置前：优先按 ``handle`` 精确定位，否则按标题（精确标题优先，其次子串）。

    **只置前，不改窗口状态**：最小化的窗口会被还原，最大化/普通窗口保持原样。仅 Windows 支持。

    与旧实现的区别：置前后用 ``GetForegroundWindow`` **实测校验**，失败就抛
    :class:`RuntimeError`——不再出现「调用返回成功、其实根本没置前」的假成功（那会让调用方
    在一个并非前台的窗口上继续点击，点中的是压在上面的别的东西）。
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
    if not _force_foreground_native(hwnd):
        raise RuntimeError(
            f"窗口 0x{hwnd:x} 未能置前（仍停留在前台窗口 0x{int(win32gui.GetForegroundWindow() or 0):x}）。"
            "Windows 前台锁定拒绝了这次激活；请重试，或先用鼠标点一下该窗口。"
        )
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
