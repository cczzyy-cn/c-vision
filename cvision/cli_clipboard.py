"""cvision 命令行入口：剪贴板图片的**状态**查询与**取图**。

供 DeepSeek Harness 插件的宿主半边调用（浏览器无法后台读剪贴板，只能由宿主代查）：

    python -m cvision.cli_clipboard --state     # 廉价：有没有图 + token，不解码
    python -m cvision.cli_clipboard --image     # 取图（长按插入时才调用）

stdout 始终是一行 JSON（``ensure_ascii=True``）：

- ``--state`` → ``{"ok": true, "supported": true, "image": true, "token": "12345", "reason": ""}``
- ``--image`` 成功 → ``{"ok": true, "data_url": "data:image/png;base64,..."}``
- ``--image`` 剪贴板没有图 → ``{"ok": false, "reason": "empty"}``
- 平台未实现 → ``{"ok": false, "reason": "unsupported", "message": "..."}``
- 其它异常 → ``{"ok": false, "reason": "error", "message": "..."}``
"""

from __future__ import annotations

import argparse
import json
import sys

from cvision import clipboard, encoding


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="剪贴板图片状态查询 / 取图")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--state", action="store_true", help="只查状态（有没有图 + token），不解码")
    group.add_argument("--image", action="store_true", help="取剪贴板图片（输出 data URL）")
    parser.add_argument("--format", default="PNG", help="JPEG/PNG/WEBP/GIF，默认 PNG")
    args = parser.parse_args(argv)

    def emit(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True))

    if args.state:
        emit({"ok": True, **clipboard.state()})
        return 0

    try:
        image = clipboard.read_image()
    except Exception as error:  # noqa: BLE001 - CLI 边界：把异常翻译成结构化结果
        emit({"ok": False, "reason": "error", "message": f"{type(error).__name__}: {error}"})
        return 1

    if image is None:
        reason = "empty" if clipboard.is_supported() else "unsupported"
        payload = {"ok": False, "reason": reason}
        if reason == "unsupported":
            payload["message"] = clipboard.unsupported_reason()
        emit(payload)
        return 2

    image = encoding.fit_for_attachment(image, format=args.format)
    emit({"ok": True, "data_url": encoding.image_to_data_url(image, format=args.format)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
