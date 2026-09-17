# 变更记录（Changelog）

> **维护约定**
>
> - 凡是**改行为**，就升 `package.json` 版本并在本文件追加一条（附提交号）；**文档改动**同样升版，便于追溯。
> - 条目按**当时的真实行为**书写，不在旧条目里改数字；**当前行为以 [README](./README.md) 为准**。
> - 发布：`git tag vX.Y.Z` → `git push origin vX.Y.Z` 触发 CI 出 Release。
>   ⚠️ **一次只推一个 tag**：实测一条 `git push` 连推多个 tag 时，GitHub 可能**不为这些 tag 创建任何
>   workflow run**（Release 也就不会生成）；逐个推（或分批、间隔几秒）才可靠。
> - `v0.1.0` ~ `v0.1.9` 的说明只在 [GitHub Releases](https://github.com/cczzyy-cn/c-vision/releases) 里
>   （那时还没有本文件）。

## v0.2.20

**补文档缺口：把「抓取会抢前台」与「被遮挡窗口点不到」两条实测事实写进给 AI 的提示。**

- 缺口一：`see(window=…)` 的帮助与 README 一直说「不抢焦点、不切走你正在用的窗口」，但这条**只对普通窗口
  成立**。本机实测（Windows）：

  | 目标状态 | 抢前台 | 几何保持 |
  |---|---|---|
  | 普通窗口（**前台或背景**） | ✅ 否 | ✅ 是 |
  | **最小化**窗口 | ❌ **是**（抓完仍是最小化态） | ✅ 是 |
  | 最小化 + `maximize=true` | ❌ 是（用户显式要求） | ✅ 是 |

  即：目标最小化时会走「先 `ShowWindow(SW_RESTORE)` 再 `_ensure_foreground`」那条路径，**代价就是抢前台**；
  兜底路径（WGC 与 PrintWindow 都失败、改读合成桌面区域）同样会置前。现在 README 明确写出「不要假设抓完
  前台没变」，并加进 STORE 契约的「失败边界」。
- 缺口二：README 的「computer-use 推荐流程」第 2 步仍写着「从截图（或 `ocr` 的词框）读出目标的屏幕绝对
  坐标」——**这正是 v0.2.19 要消灭的那种让模型自己折算坐标的旧做法**，没跟着更新。现改为「用
  `see(text=true)` 拿 `screen_center`」，并注明只有需要**词级**粒度时才用 `ocr`。
- 同时补第三条此前没写的事实：**被遮挡/重叠窗口的控件点不到**——`screen_center` 坐标本身正确，但点击按
  屏幕坐标下发、只会命中前台窗口。多窗口重叠时先 `focus_window` 置前再点。
- 新增 `tests/test_capture_foreground.py`（5 条，假 win32、跨平台）：把「普通窗口不碰前台 / 最小化窗口会被
  置前 / `maximize=true` 走 maximize 分支 / win32 抛错被吞掉」钉成断言。此前
  `_prepare_window_for_capture` 的行为**一条测试都没有**，而它正是那条副作用声明的实现。
  写这个测试时我自己踩了两个坑并修正：一开始换 `sys.modules` 无效（`windows.py` 是**模块顶层**
  `import win32gui`，必须 patch 模块上的属性）；以及我误以为 maximize 分支不会再调 `_ensure_foreground`
  ——实际会，是我的断言错了、代码没错。
- Python 163 → **168**。无运行时代码改动（纯文档 + 测试）。

## v0.2.19

**给模型「可直接点击的坐标」+ 等到画面变化才截图 + 补上两处零覆盖的协议测试 + 修剪贴板破坏性竞态。**

> 🐞 **修掉本版之前引入的一个真实回归**（`v0.2.17` 起，Windows 上装不上依赖）：
> 那时我把 `winsdk` 从 `>=1.0.0b10` 收敛成 `>=1.0.0b10,<1.0.0`，写下「它是 beta 通道，上游已转向 winrt
> 分包」——**上界本身是错的**。PEP 440 下 `<1.0.0` 这类区间会**排除预发布版**，于是上界把唯一可用的
> `1.0.0b10` 也排除了，pip 直接报 `No matching distribution found for winsdk<1.0.0,>=1.0.0b10`。
> 后果：**从 v0.2.17 到 v0.2.18，Windows 上 `python -m pip install -r requirements.txt` 一直是坏的**——
> 而这条命令正是 v0.2.18 新加的体检提示让用户去跑的，等于指了一条走不通的路。
> 修法：上界写成下一个预发布号 `>=1.0.0b10,<1.0.0b11`。已在隔离 venv 里真装验证（含 `winsdk-1.0.0b10`、
> 探针 `ok:true`）。**发现它的正是本版新加的 windows-latest 冒烟 job——第一次跑就抓到了**；
> 同时给 `check:deps` 加了第 9 项断言：预发布依赖的上界也必须带预发布标识（反向验证过会失败）。
> 教训写进了 `requirements.txt` 的约定注释与 README，避免同一个坑再踩。

- **`see(text=true)`：一次调用返回图片 + 可点击元素（含屏幕绝对坐标）**。这是本版最有价值的一项：
  过去要精确点击得先 `ocr` 拿词框，再由模型自己把「图片坐标」折成「屏幕坐标」——中间差了三层
  （`region` 裁剪偏移、窗口/多屏偏移、DPI 缩放），错一点就点偏。现在三层换算固定成代码：
  新增 `cvision/coordinates.py`（`resolve_scale`/`make_mapper`/`capture_origin`/`screen_for_image`）与
  `cvision/ui_elements.py`（同行相邻词**合并成控件**、四周外扩 padding、换算屏幕坐标），
  `see` 的 `text=true` 返回 `elements:[{text,box,center,screen_box,screen_center,word_count}]`，
  **`screen_center` 可直接喂给 `click`**。`max_elements` 默认 40 防止刷屏。
  两条通道形状一致：常驻 server 的 `{op:'capture',text:true}` 与 CLI 的 `--text`；
  一次调用同时拿图与坐标，也避免「先截图再 OCR」之间画面已变导致的错位。
- **`wait_until_changed`：轮询到画面真的变化才返回那一帧**（等进度条/等弹窗）。新增
  `cvision/diff.py`（灰度缩略图 + 变化像素占比 + 变化区域 bbox）与 `cvision/capturer.wait_until_changed`。
  比「连拍 N 张图都塞给模型」省得多：模型不必看相似图，只需知道变没变、变在哪。
  **默认阈值 0.01 是实测调出来的**：先在真机上量到光标/文本插入符闪烁约占 0.5% 像素，所以阈值
  0.002 会让工具**第一次轮询就返回「变了」**（等于毫无用处）；窗口出现这类真实变化通常 ≥5%，留了余量。
  返回 `changed`/`samples`/`elapsed_ms`/`diff_ratio`/`mean_diff`/`diff_bbox`。
  宿主的常驻请求超时相应从 30s 提到 **45s**——该 op 会在 Python 侧阻塞，超时必须大于它，
  否则常驻进程会被自己的超时回收，白等一场还回退到 CLI。
- **修剪贴板破坏性竞态**（本插件唯一会破坏用户数据的路径）：`input._paste_clipboard` 为了输入非 ASCII
  文本会临时占用剪贴板，打完再把旧内容写回——原实现**无条件**恢复，于是**用户在此期间复制的内容会被
  旧备份覆盖**。现在恢复前比对：只有剪贴板仍是「我们写进去的那份」才恢复，被改动过就尊重用户的新内容。
  新增 `tests/test_clipboard_race.py`，并**反向验证**过：用旧实现跑该测试会失败
  （`'agent 的旧备份' != '用户刚复制的内容'`），不是装饰性测试。
- **补 `cli_server` 的 JSON-line 协议契约测试**（此前**一行都没有**）。它是宿主与 Python 之间唯一的
  常驻通道（`see`/`ocr`/`list_windows`/`screen_info`/剪贴板轮询全走它），宿主严格按「一行请求一行响应」
  配对。新增 `tests/test_cli_server.py`（16 条）：响应形状、未知 op、异常边界、`capture_text`/`wait_changed`
  形状，**并真的 spawn 一个进程**跑 stdin/stdout 往返（含坏 JSON 后必须继续服务、空行不产生响应、EOF 干净退出）。
- **补 `cli_input` 参数层测试**（13 个子命令，此前引用数为 0）：`tests/test_cli_input.py`（21 条）逐条断言
  「命令行参数 → `cvision.input` 函数参数」的映射，包括最容易犯的 `--scroll-h` 走 dx 且 dy=0、
  `--drag` 四个坐标的顺序、`--double` 必须带 `double=True`、dispatch 优先级与各类参数错误。
- **`GIF` 不再假装支持**：宿主 `MEDIA_TYPES` 移除了它——`encoding` 用 `img.save(format='GIF')` 保存多帧图
  **只写第一帧**，所以「支持 GIF」是假的（`see(format='GIF')` 曾能选到却静默丢帧）。同时把
  `encoding` 的默认格式从 JPEG 改成 **PNG**（与所有 CLI 的 `--format` 默认一致），3 处 CLI 帮助文案去掉 GIF。
- **平台支持度变成机器可读**：`cvision_status()` 新增 `platform_support`，三态
  `supported`（Windows，已实测）/ `unverified`（macOS，代码写了但没真机验证）/ `unsupported`（Linux）。
  宿主体检提示据此区分「未实现」与「未验证」，不再让模型把「有实现」当成「已验证」。
- **`cvison_dir` 补正确拼写别名**：新增 `cvision_dir`（值相同）。旧键是历史拼写错误，保留兼容。
- **占用输入设备时如实提示**：新增 `busy` 字段挂在页面本来就在轮询的 `/cvision/clipboard` 上（不额外加
  路由）。输入类工具执行期间，截图按钮显示警示色 + 脉冲动画 + 「DSH 正在操作鼠标/键盘，请先不要动」。
  **只提示、不禁用**（禁用会让人以为坏了，用户本来就该能随时取消）。这是「用户与 agent 同时操作电脑」
  的轻量解法：物理上只有一套鼠标键盘，抢互斥锁一旦没释放会把插件卡死，如实暴露状态更安全。
- 测试：JS 62 → **68**（占用提示的渲染/回落/旧版宿主兼容、路由带 `busy`、体检的未验证/未实现/正常三分支）；
  Python 73 → **163**（坐标 19 + 元素合并 14 + 差异 15 + 剪贴板竞态 4 + cli_server 16 + cli_input 21 + 既有增量）。
- CI：新增 **windows-latest 冒烟 job**（此前 Windows 后端的关键路径在 CI 上从不执行）——
  按 `requirements.txt` 真装依赖，再断言 `backend=windows`、`backend_implemented`、
  `platform_support=supported`、`ok=true`。**这个 job 第一次运行就抓到了上面那个 winsdk 回归**，
  证明它值得存在：ubuntu/macOS 装不了 `pywin32`/`winsdk`，所以这类「只有 Windows 才暴露」的问题
  以前没有任何自动化手段能发现。

## v0.2.18

**首次调用前的运行时体检：依赖没装时给出「装什么、怎么装」，而不是一句裸 Python 报错。**

- 背景：装完插件后必须手动跑一次 `pip install -r requirements.txt`（DSH 不跑 pip，见
  [STORE 契约](./README.md#dsh-store-上架契约)）。代价是依赖缺失时 `see`/`ocr` 第一句就抛裸的
  `ModuleNotFoundError` 或子进程报错，用户看不出该做什么——而 `assertCvisionPresent()` 只查
  `cvision/` 目录在不在，**不查依赖**。
- 修法：在**本会话第一次调用工具前**用 `cli_capture --status` 探一次环境，把结论翻译成可操作提示
  （缺什么、运行环境如何、**带绝对路径的 pip 命令**、装完用 `cvision_status()` 复查）。该探针刻意是
  「无依赖」的（`status.py` 不 import PIL，平台后端有 try/except 兜底），所以**一个依赖都没装的
  新环境照样能跑出结论**——这正是它能当体检用的原因。结论缓存在进程内，整个会话只探一次。
- 判定口径：只把**真正会挡住 `see`/`ocr`** 的问题当门——缺 `Pillow`、平台后端未实现、或探针根本跑不起来
  （没有可用解释器 → 提示 `CVISION_PYTHON`/`CVISION_DIR`）。**缺 `pyautogui` 不拦**（它只影响输入类
  工具，拿它当门会让「只想截图」的用户被误伤），仅作为附加说明列出。平台后端未实现时**不给** `pip`
  命令——那不是装包能解决的。
- 体检门加在 5 个「收口函数」上（`captureDataUrl`/`ocrJson`/`listWindowsJson`/`screenInfoJson`/
  `runCliInput`、`runPythonCli`），16 个工具除 `cvision_status` 外全部覆盖。**`cvision_status` 刻意不过门**：
  环境不完整时它正是「唯一还能用」的排错手段，gate 了它用户就失去了查出问题的方法（已有用例钉死）。
- 顺带补掉体检探针的真实盲区：`pyperclip` 看着只影响非 Windows 的文本剪贴板，实际是**导入期**硬依赖
  ——`pyautogui` → `mouseinfo` → 模块顶层 `import pyperclip`。它缺失会让 `pyautogui` 整个 import 失败
  （所有输入类工具都挂），但旧探针只查 `pyautogui`，会**误报 `ok: true`**。现已一并探掉，并加了用例。
- 测试：JS 54 → **62**（`describeRuntimeProblem` 的判定与文案，含「缺 pyautogui 不拦」「后端未实现不给
  pip」「探针结构漂移不得当成没问题」等边界；另有一条真实 `ensureRuntime` 冒烟）；Python 72 → **73**
  （断言探针覆盖 `Pillow`/`pyautogui`/`pyperclip`）。文案是纯函数，所以不必在 CI 里真装/卸依赖。

## v0.2.17

**锁定 Python 依赖（唯一的供应链面）+ 修 `cli_ocr` 丢失词框的契约不一致。**

- **`requirements.txt` 全部改为双向锁定**：原先只有下界（`Pillow>=12.0.0`），同一份清单在不同时间
  `pip install` 会装到不同版本，且可能被静默拖进破坏性升级——重装环境因此不可复现。现在每条都是
  `>=x,<y`，**上界取下一个可能破坏兼容的边界**（常规包 = 下一个主版本；0.x 包 = 下一个次版本；
  逐 build 计数的 pywin32 = 下一个 build）。`winsdk` 从 `>=1.0.0b10` 收敛为 `>=1.0.0b10,<1.0.0`
  （它是 beta 通道，且上游已转向 `winrt` 分包）；macOS 的 pyobjc 两个 framework 从 `>=10.0` 提到
  `>=12.0,<13`（当前线是 12.x，写 `>=10.0` 会允许装到没验证过的 13）。文件头写明锁定约定与
  「为什么不写死精确版本」（保留一段安全补丁可自动装上的窗口）。同时标注 **Python 3.10+** 前提
  （Pillow 12 的下限，插件只用 3.10 起可用的语法）；`pytesseract` 作为可选回退给出注释形态的锁定示例。
- **新增 `npm run check:deps`**（`scripts/check-deps.mjs`，8 项，CI 每次都会跑），把上面的约定变成断言：
  清单存在且随包发布、每条依赖都能解析、**每条都有下界与上界**（不允许裸 `>=`）、上下界都是具体版本
  （拒绝 `~=`/`*`/空版本）、包名不重复、平台 marker 只用已验证的 `sys_platform == win32|darwin`。
  已做反向验证：故意去掉 Pillow 上界即 `❌ → exit 1`，不是空跑。
- **修复 `cli_ocr` 的 stdout 契约不一致**（真实缺陷）：README「内部 CLI 契约」表一直写它返回
  `{ok:true,text,lines,words:[{text,x,y,w,h}]}`，但实现只输出 `{text,lines}` —— 少了 `ok`，也**完全丢掉了
  `words`**。宿主 `src/index.ts` 的 `ocrJson()` CLI 回退分支正是按 `ok`/`words` 读这条路径，于是**常驻
  server 不可用时（回退路径）`ocr` 工具的词级边界框恒为空**，文档承诺的「词框供 computer-use 精确定位
  点击」在回退时静默失效。现在输出与 `cli_server` 的 `{"op":"ocr"}` 响应同形状，两条路径行为一致。
  新增 `tests/test_cli_ocr.py`（4 条，平台无关：stub 截屏与 OCR）钉死该契约——含一条「词框原样透传」的
  核心回归与一条 `--region` 确实被转发到 `crop_region` 的回归；已确认这 4 条在旧实现下**会失败**。
  Python 68 → **72**。

## v0.2.16

**修复：`dsh plugin add` 升级/重装时报 `ERR_PNPM_EPERM`（pnpm 无法替换 `node_modules/vision`）**。

```text
[ERR_PNPM_EPERM] [importPackage …\node_modules\vision] EPERM: operation not permitted,
  rename '…\vision_tmp_23816_2' -> '…\vision'
```

- 根因：插件自己拉起的**常驻 Python 子进程**（`python -m cvision.cli_server`）**以插件安装目录为工作目录**
  （`cwd: CVISION_DIR`）。Windows 下「进程的当前目录」就是该目录上的一个句柄，于是 pnpm 无法把临时目录
  rename 成 `node_modules/vision` —— 用户实际执行 `plugin add github:` 时踩到。
- 修法：所有 Python 子进程的 `cwd` 改为**系统临时目录**（`PY_CWD = tmpdir()`），改用
  `PYTHONPATH = CVISION_DIR`（拼在用户已有值前面，不覆盖）让 `python -m cvision.*` 在任何工作目录下都能
  import 到包内源码。6 处 spawn 全部改掉；实测该组合下 CLI 与常驻 server 均正常应答。
- 附带修正 `cvision_status()` 的 `cvison_dir`：以前报 `os.getcwd()`，改用包根目录（`CVISION_DIR` 或由
  `__file__` 推导）——cwd 已不再代表 cvision 的位置；键名是历史拼写，保留以兼容消费方。新增 1 条 Python
  用例（断言该字段指向含 `cvision/` 的目录），Python 67 → **68**。
- 文档：README「升级后必须做什么」补上这条错误的成因与「0.2.16 起可边跑边升级」，故障排查表加一行。
  ⚠️ **从 ≤0.2.15 升到 0.2.16 这\*一次\*仍需先关 DSH**（旧版本还在用旧行为）。

## v0.2.15

**新增文档一致性自检 `npm run check:docs`**（`scripts/check-docs.mjs`，14 项；CI 每次都会跑）——把「文档不能
漂移」从口头约定变成断言：

- `package.json` 版本 ↔ README 头部版本 ↔ CHANGELOG 最新条目，三者必须一致；
- CHANGELOG 最新条目有实质内容且没有 `TODO/待填` 占位；
- README 里的**长按阈值**必须等于 `src/client.js` 的 `LONG_PRESS_MS`，且不残留旧值；
- `cvision/`、`tests/`、`scripts/` 下**每个文件**都要出现在 README 的目录结构里；
- 全部顶层文档（README/CHANGELOG）都要在 `package.json` 的 `files` 里；
- README 必须覆盖宿主注册的**全部工具**与**全部路由**（从 `lib/index.js` 反查）；
- README 声称的**测试条数**必须等于实际条数；
- README 的**内部锚点**必须都能落到标题上。

每条都对应一次真实漂移：550ms→900ms 的阈值过期、README 漏写 `snip.py`/`clipboard.py`/`test_snip_windows.py`、
README 重写时又漏掉 `__init__.py` 与新脚本——**最后这条正是本脚本第一次运行时就抓到的**。
CI 增一步 `npm run check:docs`；README 增「文档约定（自动校验）」小节。无行为变化（代码逻辑未改）。

## v0.2.14 · `86cb839` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.14)

**文档整理（无行为变化）**：README 从 513 行压到一页能读完的结构，并把版本记录拆到本文件。

- README 新增：**目录**、**快速开始**、**升级后必须做什么**（硬刷新 / 重启宿主 / Python 改动的注意点）、
  **故障排查 FAQ**、**多平台能力矩阵**、**内部 CLI 契约**（跨语言接缝，供维护者）。
- 修正若干**已过期描述**：长按阈值（550ms → 实际 900ms）、示例里的 tarball 版本号、`requirements.txt`
  说明缺少 `pyperclip` / `pyobjc-framework-Cocoa`、目录结构漏了新增文件、CI 说明缺「一次只推一个 tag」。
- 本文件承接原 README 的版本记录（v0.2.13 → v0.2.0），并补上各版本的提交号与 Release 链接。

## v0.2.13 · `713e8f0` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.13)

**修复：取消系统截图之后，之后的剪贴板图片会被自动插进附件栏**（线上反馈：点截图按钮 → 在系统截图界面
点取消 → 再用微信截图 → 那张微信截图**自动**进了附件栏）。

- 根因：`cli_snip` **没有识别「用户取消」**——它只看剪贴板变化，取消后循环一直等到超时（默认 60s），
  期间任何新出现的剪贴板图片都被当成「本次框选的结果」。真机复现：

  ```text
  {"ok": true, "data_url": "data:image/png;base64,..."}    ← 旧实现：把那张微信截图当成了本次截图 ❌
  {"ok": false, "reason": "cancelled"}                     ← 新实现：取消被识别，晚到的图不再被吞 ✅
  ```

- 修法：用**截图覆盖层窗口**作取消判据。实测 Windows 11 上是 `SnippingTool.exe` 的
  `SnipOverlayRootWindow`（**类名与语言无关**；标题「截图工具覆盖」会随语言变，不能用）。
  - 覆盖层**在** → 继续等剪贴板；
  - 覆盖层**消失且剪贴板始终没有新图** → 立即判定取消（不再等到超时）；
  - 覆盖层消失**之后**才出现的图 → 一律不算本次截图；
  - 观测不到覆盖层（老系统 / 类名不同）→ 退回旧行为（只等剪贴板 + 超时），**不会误判成取消**。
  - 已知取舍：判定取消前做一次 0.3s 剪贴板复查（避开「先关窗、后写入」的竞态），故「取消后约 0.5 秒内」
    又复制的新图仍可能被算作本次截图；真人再去打开微信截图远慢于此。
- 测试：Python 61 → **67**（新增 6 条：取消立即返回、取消后的晚到图片绝不算本次、正常框选仍返回图片、
  观测不到覆盖层时退回等待、超时返回 None、拉不起 UI 抛 unsupported）。

## v0.2.12 · `ccad53c` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.12)

**修复：慢点击被当成长按**——单击截图按钮时手按得久一点（>550ms），长按就触发，把剪贴板里那张**旧的**
微信截图直接塞进附件栏；而真正的点击又被「长按已消费」吞掉。三处加固（都为了守住「短按必须是截图」）：

- 阈值 **550ms → 900ms**：人手一次慢点击轻松超过 0.5 秒；
- **按住期间有进度反馈**：按钮底色变化 + 底部进度条按阈值时长走完 + 文案变「继续按住，插入剪贴板图片」；
- **只在按钮已点亮时**（剪贴板里确有新图片）才启动长按计时，否则按住不做任何事、点击仍正常走系统截图；
- 另：短按开始截图时主动收掉未到阈值的按住计时（防御性）。
- 测试：JS 51 → **54**。

## v0.2.11 · `6f63bae` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.11)

**修复 v0.2.10 没修干净的问题：单击系统截图后按钮仍然变蓝**（线上实测复现）。根因是时序——剪贴板是在
**截图返回之前**就被系统写入的（宿主还要 settle 1.2s 等用户标注），而 v0.2.10 把「待归属」标记立在截图
**返回之后**：

```text
t=0.0s 系统把刚截的图写进剪贴板（token=N）
t=0.5s 客户端轮询 → 标记还没立 → served=false → 按钮亮蓝 ❌
t=1.3s 截图返回 → 宿主才立标记
t=1.5s 下一次轮询 → served=true，但 token 没变 → 客户端「变化才更新」→ 蓝色永不消失 ❌
```

现在三个点都覆盖：**① 整个截图请求期间持续归属**（`snipInFlight`，因此**慢截图**也安全，归属窗口绝不能
从「第一次轮询」开始算）；**② 截图完成后再留 4s 窗口**，覆盖截图工具打开编辑器等再次写剪贴板；
**③ 客户端收到 `served: true` 时强制清掉**可能已经亮起的高亮（即使 token 没变）。
测试：JS 49 → **51**（含 15s 慢截图用例）。

## v0.2.10 · `597c550` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.10)

**修复：单击系统截图后，按钮会亮起「长按插入剪贴板图片」**（系统把刚截的图放进剪贴板，而它刚刚已经进过
附件栏了）。

- 判据用**身份**而不是时间窗：宿主记下「我们自己交给页面的那张图的剪贴板 token」，状态路由多回一个
  `served` 字段，客户端只在 `served !== true` 时才算「新图片」。时间窗取多少都是猜（用户可能十几秒后才
  标注完再复制，而窗口内真有别家截图又会被漏掉）；标注后的新版本 token 会变，`served` 自然为 false
  —— 那确实是一张新图，亮起来是合理的。
- 顺带暴露并修掉一个**测试基础设施的坑**：初版实现是在截图路由里直接调 `clipboardProbe.state()`，让宿主
  用例触发了**真实**剪贴板探测（多数截图用例只注入 `snipExecutor`），每个用例都要拉 Python 常驻进程或等
  超时——整个套件几分钟不结束（真实症状：`npm run test:js` 卡住）。改成惰性归属后，宿主用例从「几分钟」
  回到 **88ms**。
- 测试：JS 46 → **49**。

## v0.2.9 · `2414915` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.9)

**修复 React #310（`Rendered more hooks than during the previous render`）**：v0.2.8 把
`useEffect(() => ensureClipboardWatch())` 写在了组件的**提前 `return null` 之后**，于是「能力查询在途
（5 个 hook）→ 拿到可收图（6 个 hook）」直接触顶，槽位渲染崩溃
（`slot entry crashed in 'conversation.input.right'`）。现在 **hook 全部无条件调用**，条件判断放进 effect
内部。

> 教训：**假 React 桩不校验 hook 顺序，所以 45 条测试全绿也没抓到它**。本次给桩加了 hook 计数，并新增
> 回归用例「hook 数量跨渲染必须稳定」——已用错误写法实测它**确实会失败**（不是摆设）。
> 该修复只动客户端半边：**硬刷新即生效**。

## v0.2.8 · `ad3723c` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.8)

**新功能：剪贴板监视 + 长按插入剪贴板图片**；剪贴板能力跨平台化。

- 页面**每秒**轮询宿主 `GET /cvision/clipboard`（廉价：只查剪贴板格式 + token，**不解码图片**；优先走
  常驻 Python 进程）。剪贴板出现**新图片**（其它软件截图、微信/QQ、Win+Shift+S 都算）→ 按钮**变色 +
  右上角圆点 + 悬浮提示**；**长按按钮**（当时阈值 550ms，v0.2.12 起为 900ms）→ 图片作为附件插入。
  **短按仍是系统框选截图**。
- 为什么轮询在宿主：浏览器**不可能**后台读剪贴板（`navigator.clipboard.read()` 需要用户手势与授权，
  也没有剪贴板变更事件）。页面只做同源 HTTP 轮询。
- 新增 `cvision/clipboard.py`（Windows：`IsClipboardFormatAvailable` + 剪贴板序列号 + `ImageGrab`；
  macOS：NSPasteboard/changeCount，**未真机验证**；Linux：Phase 2，如实返回不支持）与
  `cvision/cli_clipboard.py`；常驻进程新增 `clipboard_state` op。
- **剪贴板跨平台**：`requirements.txt` 补 `pyperclip` 与 `pyobjc-framework-Cocoa`；`focus_window` 按平台
  如实从能力清单里消失（`cvision_status` 不再在非 Windows 上高报）。
- 省电与降级：页面隐藏时不轮询；宿主没有这条路由（未升级/未重启）或连续失败 3 次即停止，且只告警一次。
- 安全取舍：取图路由 **POST + 同源**、只在用户长按时调用；但它让页面里的脚本也能读到剪贴板图片——
  「让按钮看见其它软件的截图」的固有代价，已在 README 的 STORE 契约里如实记录。
- 测试：JS 33 → **44**，Python 51 → **60**。

## v0.2.7 · `446d7bf` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.7)

**修复 `focus_window` 会把窗口改成半屏**：它无条件调用 `ShowWindow(SW_RESTORE)`，而 `SW_RESTORE` 不只是
「还原最小化」——**最大化**的窗口也会被降级成普通尺寸。现在拆出可测的 `show_command_for()` /
`bring_to_front()`：**只有最小化**的窗口才 `SW_RESTORE`，最大化/普通窗口一律保持原状，只做置前（与
`capture/windows.py:_prepare_window_for_capture` 的守卫一致）。工具描述与 README 同步写明「只改前后层级，
不改尺寸/最大化状态」。新增 `tests/test_input.py`，Python 单测 44 → 51。

> 起因（诚实记录）：会话中为「让 Chrome 置前好发按键」调了三次 `focus_window`，把用户的全屏浏览器缩成了
> 半屏；最初误判成 DevTools 停靠，是**用户指出后重查抓图尺寸（2560 → 1280 → 2560）才定位到调用点**。

## v0.2.6 · `3229498` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.6)

**截图按钮默认改走系统级框选截图**（人工通道），浏览器抓屏降为回退。

- 宿主新增 `POST /cvision/snip`：拉起系统截图 UI（Windows `Win+Shift+S` / `ms-screenclip:` 兜底、
  macOS `screencapture -i -x`），把用户框选的图交回浏览器半边。**只认调用之后新出现的结果**
  （Windows 用 `GetClipboardSequenceNumber` 前后对比），绝不读用户上一次复制的旧图。
- 安全：仅 POST、仅同源（`Origin` 必须等于 `Host`）、客户端断开即中止 Python 等待；抓屏授权由系统 UI
  承载，因此**没有**「一条请求就能静默抓桌面」的口子（这是放弃宿主静默抓屏的原因）。
- 回退：宿主返回 501（Linux Phase 2）/不可达/旧版宿主 → 自动回退浏览器 `getDisplayMedia`。
- 新增 `cvision/snip.py` + `cvision/cli_snip.py`；真机实测热键 → 覆盖层 → 拖拽框选 → PNG（IHDR 与框选
  尺寸一致）。
- 顺带修掉一个当时未验证到的契约 bug：`cli_snip` 用**退出码**表达 cancelled(2)/unsupported(3)，而
  `execFileAsync` 在非零退出时会 reject——「用户取消」被当成故障去回退浏览器选择器。现在读 stdout。

## v0.2.5 · `6907027` · [Release](https://github.com/cczzyy-cn/c-vision/releases/tag/v0.2.5)

**修复 v0.2.4 插入链的两个真实错误**，截图按钮端到端跑通（浏览器内实测：缩略图进入附件栏）。

1. **取错了 conversation face**：草稿 API 在 **conversation 根服务**上，而
   `ctx.sessions.scope(id).get('conversation')` 是**会话动作面**（send / cancel / updateQueue）。
2. **打错了 API 代次**：新版是 `conversation.createDrafts(sessionId, files)` +
   `inputActions.addAttachments(ids)` + `releaseDraftAttachment(id)`，旧版是 `createDraftImages(files)` +
   `inputActions.addImages(ids)` + `releaseDraftImage(id)`。现在按**能力探测**同时支持两代。

> 定位靠 v0.2.4 加的失败可见性：控制台 `[vision] …` 直接给出原因。

## v0.2.4

**修复 v0.2.3 截图按钮「点了没反应」**——上游遗留的插入链在第三方插件上下文里必然失效：

- 它在 `apply()` 时 eager 缓存 `ctx.conversation`，而第三方插件激活时该服务可能尚未就绪 → 拿到 `undefined`；
- 会话级方法需要**调用方 ctx 的 session 作用域**；
- 它把每一步失败都写成 `return null` / `catch {}`，于是唯一表现就是「什么都没发生」。

现在：服务**惰性解析**；`inject` 补上 `sessions` / `remote.session`（DSH 的 Service tracker 按**调用方
fiber** 校验 inject）；入轨被拒时短暂重试；任何失败都写控制台并在按钮旁给出短提示（用户取消/拒绝授权仍
静默）。JS 单测 21 条。

## v0.2.3

把输入框**截图按钮**（原外部插件 `@deepseek-ai/dsh-client-ui-screenshot`）集成进本包，成为双面包的客户端
半边，并修掉它的视觉判定 bug：上游靠 `model.inputModalities`（浏览器端**根本拿不到**——DSH 的
`buildModelCatalog` 只投影 `id/name/description/reasoning`）加「模型名含 `vision|visual`」兜底，于是声明了
image 但名字不含 vision 的模型永远没有按钮，名字带 vision 的文本模型反而误显示。现在由宿主半边新增只读路由
`GET /cvision/model-capability` 按真实适配器目录回答，宿主不可达时才退回名字启发式。

## v0.2.2 · `c35df09`

**修复 Windows 下中文窗口标题乱码导致窗口匹配失败**：此前 Python 子进程按控制台/ANSI 代码页解码
argv/stdin，中文标题到达时已损坏（`????`）；现所有 spawn 的 Python（server + CLI）强制 `PYTHONUTF8=1` +
`PYTHONIOENCODING=utf-8`。

## v0.2.1 · `b4b181b`

**修复 `ocr` / `see(ocr:true)` 的词级边界框没有真正送到模型**：值里带了 `words`，但渲染回调只发文本；
现在 `words` 非空时追加一个 `word_boxes` 文本块。

## v0.2.0 · `115fd50`

- OCR 返回**词级边界框** `words`；
- 新增 `screen_info` / `cvision_status` / `wait_for_window` / `drag` / `get_clipboard` / `set_clipboard`；
- `scroll` 支持水平；`see(ocr:true)` 一次返回图片 + 文本；
- 新增**持久化 Python server**（复用 D3D 设备/编码，失败自动回退每调用 CLI）。
