"""窗口抓取诊断：记录**抓取期间窗口几何是否被改动**，用于定位「抓图会把窗口挪走」这类问题。

为什么需要它：`capture_window` 内部会按需调用 `ShowWindow(SW_RESTORE)` /
`_ensure_foreground()` / `_grab_region()` 等**可能改变窗口状态**的动作，而外部观测者
（我自己用探针脚本）**无法区分**「窗口是在抓取过程中被移动的」还是「抓取前后被别的因素移动的」。
唯一可靠的办法是在**插件内部**、在这些动作的紧邻位置记录几何快照。

设计约束：
- **默认关闭**，零开销：不设 `CVISION_TRACE_WINDOWS=1` 时 `enabled()` 为假，所有快照函数直接返回。
  热路径上每次抓图都会调用它们，所以必须便宜。
- **绝不写 stdout**：stdout 是宿主读的 JSON-line 协议（`cli_server`），污染它就等于弄坏工具。
  日志一律写文件。
- **诊断失败不能影响抓图**：写文件、采样几何全部包在 try/except 里，任何异常都吞掉。
- 快照只取与「窗口是否被动手脚」直接相关的字段：rect / showCmd / 是否最大化 / 是否最小化 / 前台窗口。

用法（定位问题时）：
    PowerShell:  $env:CVISION_TRACE_WINDOWS=1; $env:CVISION_TRACE_FILE="C:\\Temp\\cv-trace.jsonl"
    然后复现一次「抓图把窗口挪走」，再看该 jsonl：每行是一次采样，`stage` 标明发生在哪一步。
"""

from __future__ import annotations

import json
import os
import sys
import time

#: 采样字段顺序固定，便于 jsonl 阅读与 diff。
_FIELDS = ("rect", "showCmd", "zoomed", "iconic", "foreground")


def enabled() -> bool:
    """是否开启了窗口跟踪（默认关）。"""
    return os.environ.get("CVISION_TRACE_WINDOWS", "").strip() not in ("", "0", "false", "False")


def trace_path() -> str:
    """跟踪日志文件路径。默认写到系统临时目录，避免污染包目录。"""
    explicit = os.environ.get("CVISION_TRACE_FILE", "").strip()
    if explicit:
        return explicit
    import tempfile

    return os.path.join(tempfile.gettempdir(), "cvision-window-trace.jsonl")


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def snapshot(hwnd: int) -> dict | None:
    """取一次窗口几何快照；失败返回 None（诊断不该抛）。"""
    try:
        import ctypes

        import win32gui

        u = ctypes.windll.user32
        p = win32gui.GetWindowPlacement(hwnd)
        return {
            "rect": list(win32gui.GetWindowRect(hwnd)),
            "showCmd": p[1],
            "zoomed": bool(u.IsZoomed(hwnd)),
            "iconic": bool(u.IsIconic(hwnd)),
            "foreground": u.GetForegroundWindow(),
        }
    except Exception:  # noqa: BLE001
        return None


def changed_fields(before: dict | None, after: dict | None) -> list[str]:
    """哪些字段变了 —— 纯函数，可单测。"""
    if before is None or after is None:
        return []
    return [name for name in _FIELDS if before.get(name) != after.get(name)]


def write_event(event: str, hwnd: int, **extra) -> None:
    """追加一行 JSON 事件；未开启跟踪或写入失败时静默返回。"""
    if not enabled():
        return
    try:
        record = {"t": _now_ms(), "pid": os.getpid(), "event": event, "hwnd": hwnd}
        record.update(extra)
        with open(trace_path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 诊断失败绝不影响抓图
        pass


def trace_step(hwnd: int, stage: str, before: dict | None) -> dict | None:
    """在某个内部动作**之后**调用：对比快照并记录差异，返回新快照供链式传递。

    :param stage: 动作名（如 ``prepare`` / ``wgc`` / ``printwindow`` / ``grab_region``）。
    :returns: 本次采样（未开启跟踪时返回 None，调用方原样传下去即可）。
    """
    if not enabled():
        return None
    after = snapshot(hwnd)
    diff = changed_fields(before, after)
    write_event(
        "step",
        hwnd,
        stage=stage,
        changed=diff,
        before=before,
        after=after,
        **({"note": "几何被改动"} if diff else {}),
    )
    return after


def trace_capture(hwnd: int, backend: str, outcome: str, before: dict | None, after: dict | None) -> None:
    """一次抓取结束时的总结事件（含最终判定，便于一眼看出结论）。"""
    if not enabled():
        return
    diff = changed_fields(before, after)
    write_event(
        "capture",
        hwnd,
        backend=backend,
        outcome=outcome,
        moved=bool(diff),
        changed=diff,
        before=before,
        after=after,
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m cvision.diagnose``：打印跟踪状态与已记录的事件摘要。"""
    print(f"跟踪开关 CVISION_TRACE_WINDOWS = {os.environ.get('CVISION_TRACE_WINDOWS', '(未设置)')}")
    print(f"当前是否开启 = {enabled()}")
    path = trace_path()
    print(f"日志路径 = {path}")
    if not os.path.exists(path):
        print("日志文件尚不存在（还没抓到需要跟踪的时刻）。")
        print()
        print("开启方式：")
        print("  $env:CVISION_TRACE_WINDOWS = '1'")
        print("  $env:CVISION_TRACE_FILE = 'C:\\Temp\\cv-trace.jsonl'")
        return 0

    moved = 0
    total = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if record.get("event") != "capture":
                continue
            total += 1
            if record.get("moved"):
                moved += 1
                print(f"  ❌ hwnd={record['hwnd']} backend={record.get('backend')} "
                      f"outcome={record.get('outcome')} 变化字段={record.get('changed')}")
                print(f"       before={record.get('before')}")
                print(f"       after ={record.get('after')}")
    print()
    print(f"共 {total} 次抓取记录，其中 {moved} 次检测到窗口几何被改动。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
