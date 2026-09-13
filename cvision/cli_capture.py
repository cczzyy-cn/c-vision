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

from cvision import capturer, encoding


def _capture_with_text(args) -> int:
    """``--text``：一次返回「图片 data URL + 可点击元素」。"""
    img, elements = capturer.capture_with_text(
        handle=args.handle,
        title_substr=args.window,
        maximize=args.maximize,
        region=args.region,
        delay=args.delay,
        format=args.format,
    )
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "kind": "capture_text",
                "data_url": encoding.image_to_data_url(img, format=args.format),
                "width": img.width,
                "height": img.height,
                "elements": elements,
            },
            ensure_ascii=True,
        )
    )
    return 0


def _wait_changed(args) -> int:
    """``--wait-changed``：轮询直到画面变化，返回变化后的那一帧 + 差异统计。"""
    img, metrics = capturer.wait_until_changed(
        handle=args.handle,
        title_substr=args.window,
        maximize=args.maximize,
        region=args.region,
        format=args.format,
        interval_ms=args.interval,
        timeout_ms=args.timeout,
        threshold=args.threshold,
        pixel_delta=args.pixel_delta,
    )
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "kind": "wait_changed",
                "data_url": encoding.image_to_data_url(img, format=args.format),
                "width": img.width,
                "height": img.height,
                **metrics,
            },
            ensure_ascii=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="截屏输出 base64 data URL，或列出窗口")
    parser.add_argument("--list", action="store_true", help="列出可见窗口(JSON)，不截图")
    parser.add_argument("--screen-info", action="store_true", help="输出显示器/DPI 布局(JSON)，不截图")
    parser.add_argument("--status", action="store_true", help="输出运行环境健康状态(JSON)，不截图")
    parser.add_argument("--text", action="store_true", help="截图后同时 OCR，返回可点击元素（含屏幕坐标）")
    parser.add_argument("--wait-changed", action="store_true", dest="wait_changed",
                        help="轮询直到画面变化才返回（等进度条/等弹窗）")
    parser.add_argument("--interval", type=float, default=500, help="--wait-changed 的采样间隔毫秒（默认 500）")
    parser.add_argument("--timeout", type=float, default=10000, help="--wait-changed 的总超时毫秒（默认 10000）")
    parser.add_argument("--threshold", type=float, default=0.01,
                        help="--wait-changed 判定「变了」的像素占比阈值（默认 0.01）")
    parser.add_argument("--pixel-delta", type=float, default=13, dest="pixel_delta",
                        help="--wait-changed 单个像素算变化所需的灰度差（默认 13）")
    parser.add_argument("--window", default=None, help="窗口标题子串；留空则截全屏")
    parser.add_argument("--handle", type=int, default=None, help="窗口句柄")
    parser.add_argument("--maximize", action="store_true", help="先最大化目标窗口再截")
    parser.add_argument("--region", default=None, help="裁剪区域 x,y,w,h（像素，相对截图）")
    parser.add_argument("--delay", type=float, default=0, help="抓取前等待毫秒")
    parser.add_argument("--format", default="PNG", help="PNG/JPEG/WEBP，默认 PNG（与插件 see 工具一致）")
    args = parser.parse_args(argv)

    if args.list:
        # ensure_ascii=True：中文窗口标题转成 \uXXXX，输出纯 ASCII，避免 GBK 控制台
        # 编码问题；插件端 JSON.parse 会还原成正确的中文。
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

    if args.delay:
        time.sleep(args.delay / 1000.0)

    if args.handle is not None or args.window:
        img = capturer.capture_window(handle=args.handle, title_substr=args.window, maximize=args.maximize)
    else:
        img = capturer.capture_screen()

    if args.region:
        img = encoding.crop_region(img, args.region)
    img = encoding.fit_for_attachment(img, format=args.format)

    sys.stdout.write(encoding.image_to_data_url(img, format=args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
