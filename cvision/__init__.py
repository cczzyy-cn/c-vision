"""C-Vision 的 Python 半边（截屏 / OCR / 用户级输入 / 系统截图 / 剪贴板）。

由 DSH 视觉插件（npm 包 ``vision``）捆绑分发：宿主半边通过 ``python -m cvision.cli_*``
与常驻的 ``cvision.cli_server`` 调用它。
"""

# ⚠️ 必须与 package.json 的 version 一致（由 `npm run check:docs` 机械校验）：
# 这个数曾经长期停在 "0.1.0"（包已经到 0.2.x）——手写的版本号多一处，就多一处漂移。
__version__ = "0.2.33"
