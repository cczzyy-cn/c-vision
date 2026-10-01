"""cvision 命令行入口：截屏输出 Base64 data URL，或列出窗口。

供 DeepSeek Harness 插件等外部程序跨语言调用：:

    python -m cvision.cli_capture [--window 标题] [--maximize] [--region x,y,w,h] [--delay ms] [--format PNG]
    python -m cvision.cli_capture --list

- 不传 --window/--handle 时截整屏；输出 ``data:<mime>;base64,<data>`` 到 stdout。
- ``--list`` 则列出当前可见顶层窗口并输出 JSON 数组到 stdout（不截图）。
- ``--region`` 裁剪截图区域（相对该图的像素），``--delay`` 抓取前等待毫秒；
  输出前会缩放到附件限制（保证 saveImage 不被拒）。
- ``--text`` 额外做一次 OCR，输出 ``{ok,kind,data_url,width,height,elements}``（**一个 JSON 对象**，
  不是裸 data URL）。``elements`` 是「可点击元素 + 屏幕绝对坐标」，供宿主 ``see(text=true)``
  一次拿到图与点击目标。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

# 「会改前台」的抓取被跨进程输入锁挡住时抛的就是这个（见 cvision/input_lock.py 的锁边界表）
from cvision.input_lock import ForegroundBusy


def _capture_deps():
    """延迟导入捕获层，返回 ``(capturer, encoding)``。

    **为什么不能放在模块顶层**：这两个 import 会链式拉进 Pillow 与平台后端（Windows 上是
    ``win32gui``）。放顶层时，一个依赖都没装的环境连 ``--status`` 都跑不起来——而 ``--status``
    恰恰是那种环境下唯一还该能跑、也唯一能告诉用户「缺什么」的东西。

    v0.2.33 修的就是这个：``python -m cvision.cli_capture --status`` 曾在这条 import 链上
    （``cli_capture`` → ``capturer`` → ``encoding`` 的 ``from PIL import Image``）直接崩，
    宿主两条通道（常驻 server 与 CLI）因此**全断**，README 承诺的「缺什么 + 怎么装」拿不到。

    只有**真的要抓图**时才调用它，所以错误信息按「抓图失败」写给人看。
    """
    try:
        from cvision import capturer, encoding
    except Exception as e:  # noqa: BLE001 - 依赖缺失/后端导入失败都要变成一句可行动的话
        raise RuntimeError(
            f"cvision 的 Python 依赖不可用（{type(e).__name__}: {e}）。"
            "请先安装：python -m pip install -r requirements.txt；"
            "用 python -m cvision.cli_capture --status 可以看缺哪个模块。"
        ) from e
    return capturer, encoding


def _capture_with_text(args) -> int:
    """``--text``：一次返回「图片 data URL + 可点击元素 + 目标窗口句柄 + 图片屏幕矩形」。"""
    capturer, encoding = _capture_deps()
    box: dict = {}
    img, elements = capturer.capture_with_text(
        handle=args.handle,
        title_substr=args.window,
        maximize=args.maximize,
        region=args.region,
        delay=args.delay,
        format=args.format,
        geometry_out=box,
    )
    payload = {
        "ok": True,
        "kind": "capture_text",
        "data_url": encoding.image_to_data_url(img, format=args.format),
        "width": img.width,
        "height": img.height,
        "elements": elements,
        # 这张图覆盖的屏幕矩形：宿主用它把模型给的「比例坐标」换算成屏幕坐标（与图片被缩放到
        # 多少像素无关）。OCR 一个词都没认出来时它依然有效。
        "image_screen_box": box,
    }
    # 附带目标窗口句柄：宿主据此记住「这次看的是哪个窗口」，点击前用它置前并校验坐标归属。
    # 必须在抓取**之后**解析（maximize 会改窗口几何，抓之前的解析结果可能是错的）。
    win = capturer.resolve_window(args.handle, args.window)
    if win is not None:
        payload["handle"] = win.handle
        payload["title"] = win.title
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    return 0


def _wait_changed(args) -> int:
    """``--wait-changed``：轮询直到画面变化，返回变化后的那一帧 + 差异统计。"""
    capturer, encoding = _capture_deps()
    box: dict = {}
    img, metrics = capturer.wait_until_changed(
        handle=args.handle,
        title_substr=args.window,
        maximize=args.maximize,
        region=args.region,
        format=args.format,
        interval_ms=args.interval or 500,
        timeout_ms=args.timeout,
        threshold=args.threshold,
        pixel_delta=args.pixel_delta,
        geometry_out=box,
    )
    payload = {
        "ok": True,
        "kind": "wait_changed",
        "data_url": encoding.image_to_data_url(img, format=args.format),
        "width": img.width,
        "height": img.height,
        # 这张图覆盖的屏幕矩形 + 目标窗口（与常驻 server 同形状）：`wait_*` 返回的是一张**新图**，
        # 宿主据此把 `click_at` 的比例基准换到这一张上。
        "image_screen_box": box,
        **metrics,
    }
    win = capturer.resolve_window(args.handle, args.window)
    if win is not None:
        payload["handle"] = win.handle
        payload["title"] = win.title
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    return 0


def _wait_stable(args) -> int:
    """``--wait-stable``：轮询直到画面**连续若干次不再变化**，返回稳定后的那一帧 + 统计。"""
    capturer, encoding = _capture_deps()
    box: dict = {}
    img, metrics = capturer.wait_until_stable(
        handle=args.handle,
        title_substr=args.window,
        maximize=args.maximize,
        region=args.region,
        format=args.format,
        interval_ms=args.interval if args.interval else 300,
        stable_samples=args.stable_samples,
        timeout_ms=args.timeout,
        threshold=args.threshold,
        pixel_delta=args.pixel_delta,
        geometry_out=box,
    )
    payload = {
        "ok": True,
        "kind": "wait_stable",
        "data_url": encoding.image_to_data_url(img, format=args.format),
        "width": img.width,
        "height": img.height,
        "image_screen_box": box,
        **metrics,
    }
    win = capturer.resolve_window(args.handle, args.window)
    if win is not None:
        payload["handle"] = win.handle
        payload["title"] = win.title
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    return 0


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="截屏输出 base64 data URL，或列出窗口")
    parser.add_argument("--list", action="store_true", help="列出可见窗口(JSON)，不截图")
    parser.add_argument("--screen-info", action="store_true", help="输出显示器/DPI 布局(JSON)，不截图")
    parser.add_argument("--status", action="store_true", help="输出运行环境健康状态(JSON)，不截图")
    parser.add_argument("--text", action="store_true", help="截图后同时 OCR，返回可点击元素（含屏幕坐标）")
    parser.add_argument("--wait-changed", action="store_true", dest="wait_changed",
                        help="轮询直到画面变化才返回（等进度条/等弹窗）")
    parser.add_argument("--wait-stable", action="store_true", dest="wait_stable",
                        help="轮询直到画面**连续若干次不再变化**才返回（等加载完成/等动画结束）")
    parser.add_argument("--stable-samples", type=int, default=3, dest="stable_samples",
                        help="--wait-stable 连续多少次「没变」才算稳定（默认 3）")
    parser.add_argument("--interval", type=float, default=0, help="采样间隔毫秒（--wait-changed 默认 500；--wait-stable 默认 300）")
    parser.add_argument("--timeout", type=float, default=10000, help="总超时毫秒（默认 10000）")
    parser.add_argument("--threshold", type=float, default=0.01,
                        help="判定「变了」的像素占比阈值（默认 0.01）")
    parser.add_argument("--pixel-delta", type=float, default=13, dest="pixel_delta",
                        help="单个像素算变化所需的灰度差（默认 13）")
    parser.add_argument("--window", default=None, help="窗口标题子串；留空则截全屏")
    parser.add_argument("--handle", type=int, default=None, help="窗口句柄")
    parser.add_argument("--maximize", action="store_true", help="先最大化目标窗口再截")
    parser.add_argument("--region", default=None, help="裁剪区域 x,y,w,h（像素，相对截图）")
    parser.add_argument("--delay", type=float, default=0, help="抓取前等待毫秒")
    parser.add_argument("--format", default="PNG", help="PNG/JPEG/WEBP，默认 PNG（与插件 see 工具一致）")
    parser.add_argument("--json", action="store_true",
                        help="非 text 截图也输出 JSON（含 handle 与 image_screen_box），而不是裸 data URL")
    args = parser.parse_args(argv)

    if args.list:
        # ensure_ascii=True：中文窗口标题转成 \uXXXX，输出纯 ASCII，避免 GBK 控制台
        # 编码问题；插件端 JSON.parse 会还原成正确的中文。
        capturer, _ = _capture_deps()
        sys.stdout.write(
            json.dumps([w.to_dict() for w in capturer.list_windows()], ensure_ascii=True)
        )
        return 0

    if args.screen_info:
        from cvision import screen

        sys.stdout.write(json.dumps(screen.screen_info(), ensure_ascii=True))
        return 0

    if args.status:
        from cvision import status

        sys.stdout.write(json.dumps(status.status(), ensure_ascii=True))
        return 0

    if args.text:
        return _capture_with_text(args)

    if args.wait_changed:
        return _wait_changed(args)

    if args.wait_stable:
        return _wait_stable(args)

    capturer, encoding = _capture_deps()

    if args.delay:
        time.sleep(args.delay / 1000.0)

    window_info = None
    if args.handle is not None or args.window:
        img = capturer.capture_window(handle=args.handle, title_substr=args.window, maximize=args.maximize)
        # maximize 会改窗口矩形，必须在抓完之后重新解析，否则算出来的矩形整体偏移
        # （与 cli_server._capture_image 同一处理）。只有 `--json` 需要它：缺省路径保持原样，
        # 不为一个用不到的字段多枚举一次窗口。
        if args.json:
            window_info = capturer.resolve_window(args.handle, args.window)
    else:
        img = capturer.capture_screen()

    if args.region:
        img = encoding.crop_region(img, args.region)

    # 几何要在 fit_for_attachment **之前**算：那一步会改像素尺寸，而矩形是屏幕坐标、与像素无关。
    box: dict = {}
    if args.json:
        try:
            box = capturer.image_screen_frame(img, args.region, window_info)
        except Exception:  # noqa: BLE001 - 拿不到矩形不该让整次截图失败（元素/图片仍然有效）
            box = {}

    img = encoding.fit_for_attachment(img, format=args.format)

    if args.json:
        # 与 `cli_server` 的非 text 抓取**同形状**（并附带 handle/title）：宿主靠它记住
        # 「这次看的是哪个窗口」「这张图覆盖屏幕哪一块」——后续 click/type_text 才不会
        # 沿用上一次的窗口（那会把键盘输入送进错误的窗口，v0.2.33 修的真实缺陷）。
        payload = {
            "ok": True,
            "kind": "capture",
            "data_url": encoding.image_to_data_url(img, format=args.format),
            "width": img.width,
            "height": img.height,
            "image_screen_box": box,
        }
        if window_info is not None:
            payload["handle"] = window_info.handle
            payload["title"] = window_info.title
        sys.stdout.write(json.dumps(payload, ensure_ascii=True))
        return 0

    sys.stdout.write(encoding.image_to_data_url(img, format=args.format))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：把**预期内**的拒绝打成一行说明，而不是一屏 traceback。

    抓图要改前台、但另一个进程正在操作电脑时，``capture_window`` 会抛 :class:`ForegroundBusy`
    （消息本身就是可行动的人话）。常驻 server 路径上它已经是干净的 JSON 错误；CLI 回退路径若让它
    冒成 traceback，那句提示就会被埋在一堆栈帧里——而它恰恰是要给用户看的那一句。
    """
    try:
        return _run(argv)
    except ForegroundBusy as e:
        sys.stderr.write(str(e) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
