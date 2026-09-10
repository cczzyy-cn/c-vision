"""命令行入口：截屏 -> OCR -> 输出 JSON 文本。

用法::

    python -m cvision.cli_ocr [--window 标题] [--maximize] [--region x,y,w,h] [--delay ms]

不传 --window/--handle 时对整屏做 OCR；输出
``{"ok": true, "text": ..., "lines": [...], "words": [{text,x,y,w,h}]}`` 到 stdout。

``words`` 必须一并输出：宿主半边 `ocrJson` 的 CLI 回退分支正是从这里读词级边界框
（常驻 server 不可用时走这条路）。此前只输出 ``text``/``lines``，导致回退路径下
``ocr`` 工具的 ``words`` 恒为空——文档承诺过的「词框供精确定位点击」在回退时静默失效。
输出与 `cli_server` 的 ``{"op":"ocr"}`` 响应保持同一形状，两条路径行为一致。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from cvision import capturer, encoding, ocr


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="截屏并 OCR，输出 JSON 文本")
    p.add_argument("--window", default=None, help="窗口标题子串；留空则对整屏 OCR")
    p.add_argument("--handle", type=int, default=None, help="窗口句柄")
    p.add_argument("--maximize", action="store_true", help="先最大化目标窗口再截")
    p.add_argument("--region", default=None, help="裁剪区域 x,y,w,h（像素，相对截图）")
    p.add_argument("--delay", type=float, default=0, help="抓取前等待毫秒")
    args = p.parse_args(argv)

    if args.delay:
        time.sleep(args.delay / 1000.0)

    if args.handle is not None or args.window:
        img = capturer.capture_window(handle=args.handle, title_substr=args.window, maximize=args.maximize)
    else:
        img = capturer.capture_screen()
    if args.region:
        img = encoding.crop_region(img, args.region)

    result = ocr.ocr_image(img)
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "text": result.get("text", ""),
                "lines": result.get("lines", []),
                "words": result.get("words", []),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
