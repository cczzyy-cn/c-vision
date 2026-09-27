"""命令行入口：模拟一次用户级输入（鼠标/键盘/窗口聚焦/剪贴板）。

用法::

    python -m cvision.cli_input --focus "Google Chrome"
    python -m cvision.cli_input --focus-handle 12345
    python -m cvision.cli_input --window-at 400 300      # 该屏幕点最顶层的窗口句柄
    python -m cvision.cli_input --ensure-front 12345 --at 400 300   # 点击前置前 + 校验归属
    python -m cvision.cli_input --click 400 300 --button left
    python -m cvision.cli_input --double 400 300
    python -m cvision.cli_input --move 200 200
    python -m cvision.cli_input --scroll 500 400 -3      # 在(500,400)向下滚3格
    python -m cvision.cli_input --scroll-h 500 400 3     # 在(500,400)向右滚3格
    python -m cvision.cli_input --drag 100 100 400 300   # 从(100,100)拖到(400,300)
    python -m cvision.cli_input --type "Hello"
    python -m cvision.cli_input --keys "ctrl+l"
    python -m cvision.cli_input --get-clipboard          # 输出 {"text": "..."}
    python -m cvision.cli_input --set-clipboard "文本"    # 写入剪贴板
    python -m cvision.cli_input --ensure-front 12345 --unblock --at 400 300 --click 400 300
                                                         # 置前+校验与点击**合成一次调用**

每次只执行一个动作；``--ensure-front`` 可以与任一动作用同时给出，此时**两者在同一个持锁区间内**
执行（见下）。调用前请先 ``see`` 确认目标。

``--focus`` / ``--ensure-front`` 会**如实**在 JSON 里报告是否真的置前成功（``focused`` /
``match``）——屏幕坐标点击只命中前台窗口，静默失败会让调用方点到压在上面的别的东西上。

**跨进程输入互斥**：每次执行动作前都会取一把跨进程锁（Windows 命名互斥体 / POSIX flock，
见 ``cvision.input_lock``），保证同一时刻只有一个进程在驱动这套鼠标键盘。两个 DSH 实例、
独立进程的子代理、用户自己的脚本都因此不会互相插队——尤其是「A 置前之后、A 点击之前」这段
缝。拿不到锁**不无限等**：超时即 ``{"ok": false, "error": ...}`` + 非零退出（``--lock-timeout``
调等待上限，环境变量 ``CVISION_INPUT_LOCK_TIMEOUT`` 同义）。互斥被 ``--no-lock`` 关掉、或锁机制
本身不可用时，返回 JSON 里会多一个 ``lock`` 字段如实说明（正常路径的形状不变）。``--lock-label``
写在持有者记录最前面（宿主传会话身份），让**等锁的对方**一眼知道是哪个会话占着。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable

from cvision import input as inp
from cvision import input_lock


def _emit(payload: dict) -> None:
    """输出一行 JSON（ensure_ascii：中文转义，避免 GBK 控制台编码问题）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    sys.stdout.flush()


def _front_error(info: dict, at: list[int] | None) -> str:
    """把 ensure_front 的结果转成一句人话——**按真实原因分类**，而不是一律说「置前未生效」。

    这个区分是实测逼出来的：目标被别的窗口盖住时，「置前」其实**成功了**（前台确实切换了），
    做不到的是**提升层叠顺序**（Windows 不给后台进程这个权限）。若继续告诉调用方「置前未生效」，
    它会去重试 ``focus_window``——而那条路同样无效，白费一轮。
    """
    handle = int(info.get("handle") or 0)
    if info.get("stale"):
        return f"窗口 0x{handle:x} 已不存在"

    if not at:
        return f"窗口 0x{handle:x} 未能激活（前台仍是 0x{int(info.get('front_after') or 0):x}）"

    x, y = at
    if info.get("inside") is False:
        return f"坐标 ({x}, {y}) 不在目标窗口 0x{handle:x} 的矩形内——窗口可能被移动过，请重新 see"

    blocker = int(info.get("blocker") or info.get("point_after") or 0)
    title = str(info.get("blocker_title") or "").strip()
    covered = f"「{title}」" if title else ""

    if info.get("unblock_point") is not None:
        return (
            f"已尝试点一下目标窗口 0x{handle:x} 把它带到最前，但坐标 ({x}, {y}) 仍属于 "
            f"0x{blocker:x}{covered}；已阻止这次点击以免点错窗口"
            "（请先手动把目标窗口切到前面，或改点它露出来的部分）"
        )
    if not info.get("focused"):
        return (
            f"窗口 0x{handle:x} 未能激活（前台仍是 0x{int(info.get('front_after') or 0):x}），"
            f"且坐标 ({x}, {y}) 处最顶层的是 0x{blocker:x}{covered}；已阻止这次点击以免点错窗口"
        )
    return (
        f"坐标 ({x}, {y}) 处最顶层的窗口是 0x{blocker:x}{covered}，而不是目标窗口 0x{handle:x}："
        "目标被完全挡住、连标题栏都露不出来，无法把它带到最前；已阻止这次点击以免点错窗口"
        "（请先手动把目标窗口切到前面）"
    )


def _act(call) -> tuple[dict, int]:
    """跑一个「只做事、不返回信息」的输入动作，给出本模块的契约输出。"""
    call()
    return {"ok": True}, 0


def _focus_action(*, handle: int | None = None, title: str | None = None) -> tuple[dict, int]:
    """置前一个窗口。失败**如实回报**（结构化 ``ok:false`` + 非零退出），绝不假装成功。"""
    try:
        hwnd = (
            inp.focus_window(handle=handle)
            if handle is not None
            else inp.focus_window(title_substr=title)
        )
    except Exception as e:  # noqa: BLE001
        # 置前失败却回 {"ok":true} 会让调用方在一个不是前台的窗口上继续点击。
        sys.stderr.write(str(e) + "\n")
        return {"ok": False, "error": str(e)}, 1
    return {"ok": True, "handle": int(hwnd) if hwnd is not None else None, "focused": True}, 0


def _resolve_action(args) -> tuple[str, Callable[[], tuple[dict, int]]] | None:
    """把参数解析成 ``(给持锁人看的标签, 执行函数)``；没有任何动作时返回 None。

    **只解析、不执行**——动作必须在取到锁**之后**才跑，好让 ``--ensure-front`` 的置前与坐标校验
    和真正的动作落在**同一个**持锁区间里。否则两个进程仍能在「置前」与「点击」之间插队：A 置前
    之后 B 把它的窗口置前，A 这一击就落到 B 的窗口上，而坐标全对。优先级与重构前**逐条一致**：
    focus > click > double > move > scroll > scroll-h > drag > set-clipboard > get-clipboard >
    type > keys。
    """
    if args.focus_handle is not None:
        return f"focus(0x{args.focus_handle:x})", lambda: _focus_action(handle=args.focus_handle)
    if args.focus:
        return f"focus({args.focus!r})", lambda: _focus_action(title=args.focus)
    if args.click:
        x, y = args.click
        return f"click({x},{y})", lambda: _act(lambda: inp.click(x, y, button=args.button))
    if args.double:
        x, y = args.double
        return f"double_click({x},{y})", lambda: _act(
            lambda: inp.click(x, y, button=args.button, double=True)
        )
    if args.move:
        x, y = args.move
        return f"mouse_move({x},{y})", lambda: _act(lambda: inp.move(x, y))
    if args.scroll:
        x, y, dy = args.scroll
        return f"scroll({x},{y},{dy})", lambda: _act(lambda: inp.scroll(x, y, dx=0, dy=dy))
    if args.scroll_h:
        x, y, dx = args.scroll_h
        return f"scroll_h({x},{y},{dx})", lambda: _act(lambda: inp.scroll(x, y, dx=dx, dy=0))
    if args.drag:
        x1, y1, x2, y2 = args.drag
        return f"drag({x1},{y1}->{x2},{y2})", lambda: _act(
            lambda: inp.drag(x1, y1, x2, y2, button=args.button)
        )
    if args.set_clipboard is not None:
        return "set_clipboard", lambda: _act(lambda: inp.set_clipboard(args.set_clipboard))
    if args.get_clipboard:
        return "get_clipboard", lambda: ({"text": inp.get_clipboard()}, 0)
    if args.text is not None:
        return "type_text", lambda: _act(lambda: inp.type_text(args.text))
    if args.keys:
        return f"press_keys({args.keys})", lambda: _act(lambda: inp.press_keys(args.keys))
    return None


def _emit_payload(payload: dict, code: int, note: dict) -> int:
    """统一收口：只有**互斥没有正常生效**（被关掉 / 锁机制降级）时才多一个 ``lock`` 字段。

    正常路径的 JSON 形状**一字不改**——既有调用方与契约测试都按原样读它；而「这一次其实没有跨进程
    保证」必须让调用方看得见，所以那两种情况如实写出来。
    """
    if note:
        payload = {**payload, "lock": note}
    _emit(payload)
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="模拟用户级输入（鼠标/键盘/聚焦/剪贴板）")
    p.add_argument("--focus", default=None, help="按标题子串把窗口置前（精确标题优先；不改尺寸/最大化状态）")
    p.add_argument("--focus-handle", type=int, default=None, help="按窗口句柄把窗口置前（不改尺寸/最大化状态）")
    p.add_argument("--window-at", nargs=2, type=int, metavar=("X", "Y"),
                   help="输出屏幕坐标 (X,Y) 处最顶层窗口的句柄（用于点击前校验归属）")
    p.add_argument("--ensure-front", type=int, default=None, metavar="HANDLE",
                   help="确保该窗口在前台（点击类动作的前置条件）；配 --at 时并校验坐标归属")
    p.add_argument("--unblock", action="store_true",
                   help="配合 --ensure-front：目标被别的窗口盖住时，点一下它的标题栏把它带到最前再复核")
    p.add_argument("--at", nargs=2, type=int, metavar=("X", "Y"), help="配合 --ensure-front：要校验归属的屏幕坐标")
    p.add_argument("--click", nargs=2, type=int, metavar=("X", "Y"), help="鼠标单击屏幕坐标(绝对像素)")
    p.add_argument("--button", default="left", choices=["left", "right", "middle"])
    p.add_argument("--double", nargs=2, type=int, metavar=("X", "Y"), help="鼠标双击")
    p.add_argument("--move", nargs=2, type=int, metavar=("X", "Y"), help="移动鼠标到屏幕坐标")
    p.add_argument("--scroll", nargs=3, type=int, metavar=("X", "Y", "DY"), help="在 (X,Y) 竖直滚动 DY 格")
    p.add_argument("--scroll-h", nargs=3, type=int, metavar=("X", "Y", "DX"), help="在 (X,Y) 水平滚动 DX 格")
    p.add_argument("--drag", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"), help="从 (X1,Y1) 拖到 (X2,Y2)")
    p.add_argument("--type", dest="text", default=None, help="输入/键入文本")
    p.add_argument("--keys", default=None, help="发送快捷键，如 ctrl+l / enter / ctrl+shift+t")
    p.add_argument("--get-clipboard", action="store_true", help="读取剪贴板文本并输出 JSON")
    p.add_argument("--set-clipboard", default=None, help="把文本写入剪贴板")
    p.add_argument("--lock-timeout", type=float, default=None, metavar="SECONDS",
                   help=f"跨进程输入锁的等待上限（默认 {input_lock.DEFAULT_TIMEOUT_S:g}s，可用环境变量 "
                        f"{input_lock.TIMEOUT_ENV} 覆盖）；超时即报错，不无限排队")
    p.add_argument("--no-lock", action="store_true",
                   help="**跳过跨进程输入互斥**（只给排错用：会与另一个 DSH 实例同时驱动鼠标键盘）")
    p.add_argument("--lock-label", default=None, metavar="TEXT",
                   help="写在持有者记录最前面的身份（宿主传会话/子代理 id）：超时的对方能看见**是哪个会话**占着")
    args = p.parse_args(argv)

    if args.window_at:
        try:
            hwnd = inp.window_at(args.window_at[0], args.window_at[1])
        except Exception as e:  # noqa: BLE001
            _emit({"ok": False, "error": str(e)})
            return 1
        _emit({"ok": True, "handle": int(hwnd) if hwnd else 0})
        return 0

    resolved = _resolve_action(args)
    if resolved is None and args.ensure_front is None:
        raise SystemExit(
            "未指定动作：--focus/--focus-handle/--window-at/--ensure-front/--click/--double/"
            "--move/--scroll/--scroll-h/--drag/--type/--keys/--get-clipboard/--set-clipboard 之一"
        )
    # 没有动作、只有 --ensure-front 时也要有个标签（超时的人要能看见是谁占着锁）。
    label, action = (
        resolved if resolved is not None else (f"ensure-front(0x{args.ensure_front:x})", None)
    )
    if args.lock_label:
        # 身份放最前：对方读到的第一眼就是「哪个会话」，而不是一串坐标。
        label = f"{args.lock_label.strip()} {label}".strip()

    try:
        with input_lock.input_lock(
            timeout_s=args.lock_timeout, enabled=not args.no_lock, label=label
        ) as lock:
            note = lock.note()

            if args.ensure_front is not None:
                at = list(args.at) if args.at else None
                try:
                    info = inp.ensure_front(
                        args.ensure_front, tuple(at) if at else None, unblock=bool(args.unblock)
                    ) or {}
                except Exception as e:  # noqa: BLE001
                    return _emit_payload({"ok": False, "error": str(e)}, 1, note)
                ok = bool(info.get("match")) if at else bool(info.get("focused"))
                if not ok:
                    # 前置校验没过 → **动作根本不执行**：宁可报错，也不点到压在上面的别的东西上。
                    return _emit_payload(
                        {"ok": False, **info, "error": _front_error(info, at)}, 1, note
                    )
                if action is None:
                    return _emit_payload({"ok": True, **info}, 0, note)

            if action is None:  # 上面已覆盖；留个明确失败，不给自己留「静默成功」的口子
                return _emit_payload({"ok": False, "error": "未指定动作"}, 1, note)

            payload, code = action()
            return _emit_payload(payload, code, note)
    except input_lock.InputLockTimeout as e:
        # 如实报错：不排队等到天荒地老，也不假装点过了。
        _emit({"ok": False, "error": str(e)})
        sys.stderr.write(str(e) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
