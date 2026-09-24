"""持久化 cvision 服务：长驻子进程 + JSON-line 协议，复用捕获/OCR 资源。

用法（由 DSH 插件 TS 层管理生命周期）::

    python -m cvision.cli_server

- 从 stdin 逐行读取 JSON 请求，每行输出一个 JSON 响应。
- 复用同一进程内的资源（如 Windows Graphics Capture 的 D3D 设备、临时图编码），
  避免每次工具调用冷启动一个 Python 进程。
- 遇到错误返回 ``{"ok": false, "error": "..."}`` 并继续服务；收到 ``{"op":"quit"}`` 退出。

请求::

    {"op":"ping"}
    {"op":"capture","window":str?,"handle":int?,"maximize":bool,"region":"x,y,w,h"?,"delay":ms,"format":"PNG","text":bool?}
    {"op":"wait_changed","window":str?,"handle":int?,"region":"x,y,w,h"?,"interval":ms,"timeout":ms,"threshold":float}
    {"op":"wait_stable","window":str?,"handle":int?,"region":"x,y,w,h"?,"interval":ms,"stable_samples":int,"timeout":ms,"threshold":float}
    {"op":"ocr","window":str?,"handle":int?,"maximize":bool,"region":"x,y,w,h"?,"delay":ms}
    {"op":"list"}
    {"op":"screen_info"}
    {"op":"status"}
    {"op":"clipboard_state"}   # 廉价剪贴板状态（页面每秒轮询用）

响应::

    {"ok":true,"kind":"capture","data_url":"data:...","width":int,"height":int}
    {"ok":true,"kind":"capture_text","data_url":"data:...","width":int,"height":int,"elements":[{...}]}
    {"ok":true,"kind":"ocr","text":str,"lines":[str],"words":[{...}]}
    {"ok":true,"kind":"wait_changed","data_url":"data:...","width":int,"height":int,"changed":bool,"diff_ratio":float,...}
    {"ok":true,"kind":"wait_stable","data_url":"data:...","width":int,"height":int,"stable":bool,"max_diff_ratio":float,...}
    {"ok":true,"kind":"list","windows":[{...}]}
    {"ok":true,"kind":"screen_info","displays":[{...}]}
    {"ok":true,"kind":"status","status":{...}}
    {"ok":true,"kind":"clipboard_state","supported":bool,"image":bool,"token":str?,"reason":str}
    {"ok":true,"kind":"pong"}
    {"ok":false,"error":"..."}
"""

from __future__ import annotations

import json
import sys
import time


def _capture_image(args: dict, geometry_out: dict | None = None):
    from cvision import capturer, encoding

    if args.get("delay"):
        time.sleep(float(args.get("delay")) / 1000.0)
    window_info = None
    if args.get("handle") is not None or args.get("window"):
        img = capturer.capture_window(
            handle=args.get("handle"),
            title_substr=args.get("window"),
            maximize=bool(args.get("maximize")),
        )
        # maximize 会改窗口矩形，必须在抓完之后重新解析，否则算出来的矩形整体偏移。
        window_info = capturer.resolve_window(args.get("handle"), args.get("window"))
    else:
        img = capturer.capture_screen()
    if args.get("region"):
        img = encoding.crop_region(img, str(args.get("region")))
    # 几何要在 `fit_for_attachment` **之前**算（那一步会改像素尺寸）。它描述的是屏幕矩形，
    # 与图片被缩放到多少像素无关，所以时序上只要用原始尺寸即可。
    if geometry_out is not None:
        try:
            geometry_out.update(capturer.image_screen_frame(img, args.get("region"), window_info))
        except Exception:
            pass
    img = encoding.fit_for_attachment(img, format=args.get("format", "PNG"))
    return img


def _capture(args: dict):
    from cvision import capturer, encoding

    if args.get("text"):
        # 与 `cli_capture --text` **同形状**：一次返回图 + 可点击元素。
        # 宿主 see(text=true) 两条路径（常驻 server / CLI 回退）读的是同一套字段。
        box: dict = {}
        img, elements = capturer.capture_with_text(
            handle=args.get("handle"),
            title_substr=args.get("window"),
            maximize=bool(args.get("maximize")),
            region=args.get("region"),
            delay=args.get("delay") or 0,
            format=args.get("format", "PNG"),
            geometry_out=box,
        )
        resp = {
            "ok": True,
            "kind": "capture_text",
            "data_url": encoding.image_to_data_url(img, format=args.get("format", "PNG")),
            "width": img.width,
            "height": img.height,
            "elements": elements,
            # 与 `cli_capture --text` 同形状：图片覆盖的屏幕矩形（供「按比例点击」换算）。
            "image_screen_box": box,
        }
        # 与 `cli_capture --text` 同形状地附带目标窗口句柄（宿主靠它做点击前置前与坐标归属校验）。
        win = capturer.resolve_window(args.get("handle"), args.get("window"))
        if win is not None:
            resp["handle"] = win.handle
            resp["title"] = win.title
        return resp

    box: dict = {}
    img = _capture_image(args, geometry_out=box)
    data_url = encoding.image_to_data_url(img, format=args.get("format", "PNG"))
    return {
        "ok": True,
        "kind": "capture",
        "data_url": data_url,
        "width": img.width,
        "height": img.height,
        # 非 text 抓取（整屏/窗口）同样回报屏幕矩形：模型看完图直接按比例点击是最常见的用法。
        "image_screen_box": box,
    }


def _ocr(args: dict):
    from cvision import ocr

    img = _capture_image(args)
    result = ocr.ocr_image(img)
    return {"ok": True, "kind": "ocr", **result}


def _list():
    from cvision import capturer

    return {
        "ok": True,
        "kind": "list",
        "windows": [w.to_dict() for w in capturer.list_windows()],
    }


def _screen_info():
    from cvision import screen

    return {"ok": True, "kind": "screen_info", "displays": screen.screen_info()}


def _status():
    from cvision import status

    return {"ok": True, "kind": "status", "status": status.status()}


def _clipboard_state():
    """廉价剪贴板状态：只查格式 + token，不解码图片——供页面每秒轮询（走这个常驻进程最省）。"""
    from cvision import clipboard

    return {"ok": True, "kind": "clipboard_state", **clipboard.state()}


def _wait_changed(args: dict):
    """轮询直到画面变化（供 wait_until_changed 工具）。

    ⚠️ 这个 op 会**阻塞** server 直到有变化或超时，期间不再处理其它请求（宿主侧是单请求队列，
    本来也不会并发发）。所以：**内部超时必须小于宿主的请求超时**，由调用方保证（宿主的
    request timeout 已从 30s 提到 45s，见 src/index.ts）。
    """
    from cvision import capturer, encoding

    img, metrics = capturer.wait_until_changed(
        handle=args.get("handle"),
        title_substr=args.get("window"),
        maximize=bool(args.get("maximize")),
        region=args.get("region"),
        format=args.get("format", "PNG"),
        interval_ms=args.get("interval") or 500,
        timeout_ms=args.get("timeout") or 10000,
        threshold=args.get("threshold") if args.get("threshold") is not None else 0.01,
    )
    return {
        "ok": True,
        "kind": "wait_changed",
        "data_url": encoding.image_to_data_url(img, format=args.get("format", "PNG")),
        "width": img.width,
        "height": img.height,
        **metrics,
    }


def _wait_stable(args: dict):
    """轮询直到画面**连续若干次不再变化**（供 wait_until_stable 工具）。

    ⚠️ 与 ``wait_changed`` 一样会**阻塞** server 直到稳定或超时，所以内部超时必须小于宿主的
    请求超时（宿主 request timeout 45s）。
    """
    from cvision import capturer, encoding

    img, metrics = capturer.wait_until_stable(
        handle=args.get("handle"),
        title_substr=args.get("window"),
        maximize=bool(args.get("maximize")),
        region=args.get("region"),
        format=args.get("format", "PNG"),
        interval_ms=args.get("interval") or 300,
        stable_samples=args.get("stable_samples") or 3,
        timeout_ms=args.get("timeout") or 15000,
        threshold=args.get("threshold") if args.get("threshold") is not None else 0.01,
    )
    return {
        "ok": True,
        "kind": "wait_stable",
        "data_url": encoding.image_to_data_url(img, format=args.get("format", "PNG")),
        "width": img.width,
        "height": img.height,
        **metrics,
    }


def handle(req: dict) -> dict:
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "kind": "pong"}
    if op == "capture":
        return _capture(req)
    if op == "wait_changed":
        return _wait_changed(req)
    if op == "wait_stable":
        return _wait_stable(req)
    if op == "ocr":
        return _ocr(req)
    if op == "list":
        return _list()
    if op == "screen_info":
        return _screen_info()
    if op == "status":
        return _status()
    if op == "clipboard_state":
        return _clipboard_state()
    return {"ok": False, "error": f"unknown op: {op!r}"}


def main(argv: list[str] | None = None) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:  # noqa: BLE001
            sys.stdout.write(json.dumps({"ok": False, "error": f"bad request: {e}"}) + "\n")
            sys.stdout.flush()
            continue
        if req.get("op") == "quit":
            return 0
        try:
            resp = handle(req)
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": str(e)}
        sys.stdout.write(json.dumps(resp, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
