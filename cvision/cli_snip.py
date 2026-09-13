"""cvision 命令行入口：系统级区域截图（人工框选）。

供 DeepSeek Harness 插件的浏览器半边跨语言调用：::

    python -m cvision.cli_snip [--timeout 60] [--format PNG]

stdout 始终是**一行 JSON**（``ensure_ascii=True``，避免非 ASCII 控制台编码问题）：

- ``{"ok": true, "data_url": "data:image/png;base64,..."}`` —— 用户框选成功
- ``{"ok": false, "reason": "cancelled"}`` —— 用户 Esc 或超时
- ``{"ok": false, "reason": "unsupported", "message": "..."}`` —— 平台未实现（插件会回退浏览器抓屏）
- ``{"ok": false, "reason": "error", "message": "..."}`` —— 其它异常

输出前与 ``cli_capture`` 一致地缩放到附件限制（``encoding.fit_for_attachment``）。
"""

from __future__ import annotations

import argparse
import json
import sys

from cvision import encoding, snip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="系统级区域截图：拉起系统截图 UI 并取回框选结果")
    parser.add_argument("--timeout", type=float, default=60.0, help="等待用户框选的最长秒数")
    parser.add_argument("--format", default="PNG", help="PNG/JPEG/WEBP，默认 PNG（与 see 工具一致）")
    args = parser.parse_args(argv)

    def emit(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True))

    try:
        image = snip.snip_selection(args.timeout)
    except snip.SnipUnsupported as error:
        emit({"ok": False, "reason": "unsupported", "message": str(error)})
        return 3
    except Exception as error:  # noqa: BLE001 - CLI 边界：把异常翻译成结构化结果
        emit({"ok": False, "reason": "error", "message": f"{type(error).__name__}: {error}"})
        return 1

    if image is None:
        emit({"ok": False, "reason": "cancelled"})
        return 2

    image = encoding.fit_for_attachment(image, format=args.format)
    emit({"ok": True, "data_url": encoding.image_to_data_url(image, format=args.format)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
