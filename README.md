# Vision · DeepSeek Harness (DSH) 视觉插件

给 DSH 的 agent 提供**视觉（看）**与**用户级操作（操作）**：模型用 `see`/`ocr`/`list_windows` 看清屏幕与
窗口，再用 `click`/`type_text`/`press_key`/`scroll`/`focus_window` 像人一样操作，形成 **看 → 操作 → 看** 的
computer-use 闭环。

本包是一个 **DSH 组合包（bundle）**，通过 `dsh plugin add` 安装。插件注册工具 → 跨语言调用**包内捆绑的
Python 版 cvision** 截屏/OCR/输入 → 写入 Harness 附件服务（`ctx.attachments.saveImage`）或返回文本 →
以 **`image` ContentBlock / `text`** 交给模型。

同一个包还带一个**浏览器半边**：输入框工具栏的「截图」按钮（人工一键抓屏，或把剪贴板里的图片作为附件）。

**版本**：`0.2.30` · **平台**：Windows（完整，实测）/ macOS（Phase 1，未真机验证）/ Linux（Phase 2 占位）·
**许可**：BSD-3-Clause · 变更历史见 [CHANGELOG.md](./CHANGELOG.md)

## 目录

- [快速开始](#快速开始)
- [特性](#特性)
- [工具一览](#工具一览)
- [输入框截图按钮（浏览器半边）](#输入框截图按钮浏览器半边)
- [给 AI 智能体的使用提示](#给-ai-智能体的使用提示重要)
- [computer-use 推荐流程](#电脑使用computer-use推荐流程)
- [多平台支持](#多平台支持)
- [升级后必须做什么](#升级后必须做什么)
- [配置](#配置可选)
- [构建与测试](#构建与测试)
- [CI / 发布](#ci--发布)
- [内部 CLI 契约（维护者）](#内部-cli-契约维护者)
- [目录结构](#目录结构)
- [故障排查](#故障排查)
- [说明与限制](#说明与限制)
- [DSH STORE 上架契约](#dsh-store-上架契约)

## 快速开始

```powershell
# 1) 装插件（公开仓库免认证；--profile web = 浏览器面板/3080，桌面 App 用 --profile desktop）
npx -y @deepseek-ai/dsh plugin --profile web add github:cczzyy-cn/C-Vision

# 2) 装 Python 依赖（依赖清单随包分发；CVISION_DIR 默认指向包内，无需额外配置）
python -m pip install -r <插件安装目录>\requirements.txt
```

3) **重启 DSH**（宿主半边生效）→ 4) **硬刷新页面 `Ctrl+Shift+R`**（浏览器半边生效，仅升级时需要）。

验证：让模型调用 `cvision_status()` 看运行环境探针；或直接说「用 see 看一下屏幕」。

> **依赖只在「装」和「升级」时需要管一次**：`pip install` 是幂等的，依赖装在**你的 Python** 里而不是
> 插件目录里，所以升级插件一般不用重跑（除非换了机器/解释器，或 `requirements.txt` 加了新依赖）。
> 漏装也别慌：**本会话第一次**调用 `see`/`ocr` 等工具时，插件会先探一次环境，直接把「缺什么 + 带绝对
> 路径的 pip 命令」告诉你，而不是抛一句裸的 Python 报错（v0.2.18 起）。

> `dsh` 通常不在系统 PATH（在 npx 缓存里），用 `npx -y @deepseek-ai/dsh …`；`dsh plugin` 是 pnpm 前向器，
> 需本机有 `pnpm`。装完 `npx -y @deepseek-ai/dsh --dump-config` 能看到多出 `# == Vision` 配置层。

## 特性

- **原生看图**：`see` 抓真实截图（WGC 抓窗口合成内容，GPU/被遮挡窗口也稳），模型直接看到。
- **快速读字**：`ocr` 直接返回**文本 + 词级边界框**；`see`/`ocr` 支持 `region="x,y,w,h"` 只取一块，省 token。
- **用户级操作**：鼠标点击/移动/滚动、键盘输入/快捷键、窗口聚焦（模拟人操作，**只置前不改窗口状态**）。
- **输入框截图按钮**（浏览器半边）：
  - **短按 → 系统级框选截图**（Windows `Win+Shift+S` / macOS `screencapture -i`，可框选可标注），
    抓到的图直接进附件栏；宿主这条通道不可用时自动回退浏览器抓屏；
  - **剪贴板监视**：别的软件（微信/QQ/Win+Shift+S）截图进剪贴板 → 按钮**变色 + 圆点 + 提示**；
    **长按 ≥0.9s → 把剪贴板图片作为附件插入**（按住期间有进度条反馈）；
  - 显示与否由**宿主真实 `inputModalities`** 决定（不靠模型名字猜）。
- **跨平台**：Windows 完整实测；macOS Phase 1（代码已写，未真机验证）；Linux Phase 2 占位。
- **开箱即用**：包内自带 Python cvision 与依赖清单，`CVISION_DIR` 默认指向包内。

## 工具一览

### 看（观察）

| 工具 | 说明 |
| --- | --- |
| `see(handle?, window?, region?, delay?, maximize?, format?, ocr?, text?, max_elements?)` | 截屏/窗口 → **图片**返回（模型原生看）。`handle`（来自 `list_windows`）比 `window` 标题**更精确、标题变化时更稳**，二者二选一且**优先 `handle`**；`region="x,y,w,h"` 只取一块（省 token）；`delay=毫秒` 等渲染；`maximize` 默认关；`format` 可选 PNG/JPEG/WEBP；`ocr=true` 同时返回 OCR 文本/词框；**`text=true` 同时返回可点击元素**（见下） |
| `ocr(handle?, window?, region?, delay?)` | 截屏后 **OCR** → **文本 + 词级边界框 `words`**（`{text,x,y,w,h}`，供精确定位点击点）；同样 **`handle` 优先于 `window`** |
| `list_windows()` | 列出可见窗口（标题 + 句柄 + 尺寸） |
| `screen_info()` | 列出显示器/DPI 布局（`x/y/width/height/primary/scale`），高 DPI 折算坐标用 |
| `cvision_status()` | 运行环境健康探针（Python 版本、平台后端、OCR 引擎、依赖/后端是否可用、`platform_support` 三态、本平台能力清单） |
| `wait_for_window(title?, timeout?)` | 轮询等某个窗口出现（默认 500ms/次，10s 超时） |
| `wait_until_stable(window?, handle?, region?, interval?, stable_samples?, timeout?, threshold?, format?)` | 轮询截图，**直到画面连续若干次不再变化**才返回（等加载完成/等动画结束）。返回 `stable`/`diff_ratio`/`max_diff_ratio`/`stable_for`/`width`/`height`；与上一个的区别是「等**变完**」而非「等开始变」（v0.2.29 起） |
| `wait_until_changed(window?, handle?, region?, interval?, timeout?, threshold?, format?)` | 轮询截图，**直到画面真的变了**才把那一帧返回（等进度条/等弹窗）。返回 `changed`/`diff_ratio`/`diff_bbox`/`diff_boxes`/`width`/`height`（未变化时没有 `diff_bbox`）；**返回的图里变化区域已用红框标出**，`diff_boxes` 是**分开的**变化区域（v0.2.30 起）；默认阈值 0.01 |

### 点击定位：`see(text=true)`

给模型**可直接点击的坐标**，不必自己从截图里估算像素：

```text
see(text=true)  →  图片 + elements: [{ text, screen_center:{x,y}, screen_box, box, center, word_count }]
                                          ↑ 直接喂给 click(x, y)
```

为什么需要它：`ocr` 给的是**词框**且坐标相对**那张图片**，而 `click` 吃的是**屏幕绝对坐标**——
两者之间差了三件事，模型很容易算错：**裁剪偏移**（`region`）、**窗口/多屏偏移**、**DPI 缩放**。
`see(text=true)` 把这三层换算固定成代码（`cvision/coordinates.py`），并顺手把同一行的相邻词
**合并成一个控件**（`cvision/ui_elements.py`）、四周外扩一点 padding，让中心点更稳地落在控件内部。

> `max_elements` 默认 40（按从上到下、从左到右取前 N 个），防止一屏几百个元素刷爆上下文。


### 操作（模拟用户级输入）

| 工具 | 说明 |
| --- | --- |
| `click(x, y, button?)` / `double_click(x, y)` | 屏幕绝对坐标单击 / 双击（left/right/middle） |
| `click_at(rx, ry, button?)` | 按**比例**点击**最近一次 `see` 那张图**上的位置（rx/ry 为 0~1，左上角 0,0）：图片被缩放到多少像素都无所谓，换算由插件做（v0.2.26 起） |
| `mouse_move(x, y)` | 移动鼠标到屏幕坐标（不点击） |
| `scroll(x, y, dy?, dx?)` | 在 (x,y) 处滚动（dy>0 上滚；dx 水平滚动） |
| `drag(x1, y1, x2, y2, button?)` | 从 (x1,y1) 拖拽到 (x2,y2)（框选/拖文件） |
| `type_text(text)` | 像键盘一样输入文本到**当前焦点** |
| `press_key(keys)` | 发送快捷键，如 `ctrl+l`、`enter`、`ctrl+shift+t`、`alt+tab` |
| `get_clipboard()` / `set_clipboard(text)` | 读写剪贴板**文本**（Windows 原生；macOS/Linux 走 `pyperclip`） |
| `focus_window(title?, handle?)` | 把窗口**置前**（用户级激活）；`handle` 优先；**只改前后层级，不改窗口尺寸/最大化状态**（仅最小化的窗口会被还原）；**置前失败会报错**（v0.2.25 起） |

> **关键**：默认**不最大化、不切前台**——WGC 抓的是窗口自身的合成内容，与前台/遮挡无关。

## 输入框截图按钮（浏览器半边）

本包是**双面包**：除宿主半边的工具外，还声明 `dsh.client`，由 DSH 客户端模块系统把 `exports["./client"]`
（`lib/client.js`，经典脚本）送进浏览器，在 `conversation.input.right` 挂一个「截图」按钮。

**短按 = 系统级框选截图（默认通道）**：

```text
Windows  Win+Shift+S（`ms-screenclip:` 兜底）→ 系统覆盖层，框选后结果进剪贴板
macOS    screencapture -i -x <tmp.png>        → 交互框选直接写文件（Esc 不留文件）
Linux    Phase 2：返回 501 → 浏览器半边自动回退到浏览器抓屏
```

→ 宿主用 `POST /cvision/snip` 把用户刚框出来的那张图交回浏览器 → 包成 `File` → 作为草稿图进**附件栏** →
跟随消息发给模型。

为什么默认走这条：框选与**标注**都是系统原生、天然跨显示器，**抓屏授权由系统 UI 承载**——宿主只读「用户刚
放进剪贴板/文件的那张新图」，因此不存在「页面里任何脚本都能静默截屏」的口子。代价是这一步会**占用剪贴板**
（`Win+Shift+S` 的固有行为）。

**取消与归属**：`cli_snip` 用**截图覆盖层窗口**判断用户是否取消（Windows 11 是 `SnippingTool.exe` 的
`SnipOverlayRootWindow`；类名与语言无关）。覆盖层消失且剪贴板始终没有新图 → 立即判定取消（宿主回 204，
客户端静默）；**覆盖层消失之后才出现的图一律不算本次截图**（否则「取消后再用微信截图」会被误当成框选结果，
v0.2.13 修的就是这个）。观测不到覆盖层时退回「只等剪贴板 + 超时」，不会误判成取消。

同时宿主会告诉页面**哪些剪贴板图片是我们自己产出的**（状态路由的 `served` 字段）：单击系统截图后，系统把
刚截的图放进剪贴板，按钮**不应该**再亮「长按插入剪贴板图片」——那张图刚刚已经进过附件栏了。别家软件的
截图（token 变了、`served=false`）照常点亮。

**长按 ≥0.9s = 插入剪贴板图片**：

```text
每秒        GET  /cvision/clipboard       宿主原生查：有没有图片 + token（不解码图片）
出现新图片  按钮变色 + 右上角圆点 + 提示「剪贴板有新图片：长按按钮插入」
长按 ≥0.9s POST /cvision/clipboard/image  图片进附件栏 → 配色恢复正常（按住期间有进度条反馈）
短按        系统框选截图（上面的默认通道）
```

两个约束都是为了守住「短按必须是截图」：**只在按钮已点亮时**（剪贴板里确有新图片）才启动长按计时；按住
期间给出进度反馈，慢点击会在进度条走完之前松手。

为什么轮询在宿主：浏览器**不可能**在后台读剪贴板——`navigator.clipboard.read()` 需要用户手势与授权，而且
**没有剪贴板变更事件**。页面只做同源 HTTP 轮询，真正读剪贴板的是宿主原生侧。页面隐藏时不轮询；宿主没有这
条路由（未升级/未重启）或连续失败 3 次即停止，且只告警一次。

> ⚠️ **已知取舍**：取图路由让**页面里的脚本**（包括其它客户端插件）也能读到剪贴板里的图片——这是「让按钮
> 看见其它软件的截图」的固有代价，所以取了 **同源 + 仅 POST + 只在长按时调用**三个约束，并在
> [STORE 契约](#dsh-store-上架契约)里写明。

**回退通道：浏览器 `getDisplayMedia`**。只有宿主那条路不可用（非桌面平台组合、Electron `file://` 里没有
web 服务器、旧版宿主）或明确返回不支持时，才回退浏览器抓屏——功能在任何平台上都不会消失。点按钮后按钮进入
「等待框选」态（期间禁用，Esc 取消即静默恢复）。

**可见性判定**：DSH 给浏览器的模型目录（`buildModelCatalog`）只投影 `id/name/description/reasoning`，
**刻意剥掉了 `inputModalities`**，客户端无法自行判断当前模型收不收图。本包改为向自己宿主半边的**只读**路由
查询：

```text
GET /cvision/model-capability?provider=<id>&model=<id>
→ { "source": "declared", "image": true|false, "modalities": ["text","image"] }   # 按真实适配器目录
→ { "source": "unknown",  "image": false }                                        # 该路由解析不出来
```

判定口径与 Session 的图片准入一致：只有**显式声明**了 `inputModalities` 且不含 `image` 才算不收图（未声明
时 DSH 仍会放行）。宿主答 `declared` 时以宿主为准；答 `unknown`、或路由根本不可达时才退回「id/name 含
`vision|visual`」的名字启发式。同一 `provider/model` 只查一次并缓存。

> ⚠️ 与原外部插件**不能同时挂载**，否则输入框会出现两个截图按钮。若曾装过
> `@deepseek-ai/dsh-client-ui-screenshot`，请从 profile 的 `cordis.patch.yml` 删掉它的 insert 行，并（可选）
> `npx -y @deepseek-ai/dsh plugin --profile web rm @deepseek-ai/dsh-client-ui-screenshot` 清理依赖。

## 给 AI 智能体的使用提示（重要）

- **默认不要传 `maximize=true`**：WGC 抓的是窗口**自身的合成内容**，跟是否前台、是否被遮挡**无关**。
- **不要为了截图去激活/切换前台窗口**：WGC 路径**不抢焦点、不切走你正在用的窗口**。
- ⚠️ **但「不抢前台」只对普通窗口成立**。实测（Windows，非最小化）：目标无论在前台还是**背景**，
  抓取都**不改几何、不抢前台**；而目标**处于最小化**时，「先还原再抓」那条路径**会把它置前、抢走前台**
  （抓完几何会还原回最小化，但前台已经变了）。**所以不要假设抓完前台没变**——尤其在用户正在别处打字时。
  兜底路径（WGC 与 PrintWindow 都失败、改用读合成桌面区域）同样会置前。
- **什么情况才用 `maximize=true`**：仅当窗口已**最小化**、或**太小**、或被完全挡住且内容读不出来时。插件
  抓完会**自动还原**窗口原状态（`GetWindowPlacement` / `SetWindowPlacement` 成对使用）。
- **`focus_window` 只置前**：它不会改窗口尺寸/最大化状态（v0.2.7 起）；**置前失败会如实报错**（v0.2.25 起，
  不再假报成功）。只有真的需要键盘焦点、或要操作一个还没 `see` 过的窗口时才调用它。
- **推荐流程**：先 `list_windows()` → 直接 `see(handle=<句柄>)`（标题会变时优先 `handle`）；整屏用 `see()`。
- **要点击就用 `see(text=true)`**：它返回的 `screen_center` 是**屏幕绝对坐标**，可直接喂给 `click`。
  **不要自己从截图估算像素**——裁剪、窗口位置、多屏、DPI 四层差异都由插件换算好了。
- ✅ **被遮挡的窗口也能点到（v0.2.25 起，v0.2.28 增强）**：`screen_center` 是按屏幕坐标算的，而点击命中的
  是该点**最顶层**的窗口。所以 `click`/`double_click`/`drag`/`scroll`/`type_text`/`press_key` 在动作前会
  **自动**把「你最近一次 `see` 的那个窗口」带到最前，并复核该坐标确实属于它。分三层：
  ① 先激活它；② 该点仍被别的窗口盖着时，**点一下它的标题栏中央**（人遇到这种情况也是这么做的）把它带到
  最前，再复核；③ 仍然不行就**报错并跳过这次点击**，错误里会写明「该点现在属于谁」。
  ⚠️ 第②层会**真的移动并点击一次鼠标**（只点标题栏中央，不碰任何按钮）；第③层是刻意的——
  宁可报错，绝不静默点到压在上面的窗口上。
- ✅ **纯图标/没识别出文字的地方，用 `click_at(rx, ry)`（v0.2.26 起）**：你看到的截图是被 DSH **缩过**
  的（按图片 token 规则，1920×1080 的整屏截图到你眼里只剩 1708×961），所以**按图片像素估坐标一定会偏**
  ——照着自己看到的画面数像素直接用，右下角会差 200 多像素。但**比例**在缩放前后不变：`click_at(0.64, 0.46)`
  表示「横向 64%、纵向 46% 处」，插件按该图覆盖的屏幕矩形换算，与图片被缩到多少像素无关。
  它**不依赖**任何 provider 的缩放规则（那些规则会随版本漂移）。代价是精度受你目测限制：比例差 1%
  在 1920 宽的屏上约等于 19px，所以**小控件仍然优先用 `see(text=true)` 的 `screen_center`**（±1px）。
- ✅ **等「加载完成」用 `wait_until_stable`，不要拿 `wait_until_changed` 去猜（v0.2.29 起）**：前者等
  「**连续 N 次**没变化」，后者只等「变过一次」。加载中的画面一直在动，用后者你只知道「它动过」，
  还得反复 `see` 复查、白烧轮次。**务必配 `region` 只盯结果区**——别处的光标闪烁与时钟走字会让整屏
  永远「不稳定」，那样只能等到超时（返回 `stable: false`，并给出 `max_diff_ratio` 说明动得多厉害）。
- ✅ **`wait_until_changed` 返回的图里，变化区域已经用红框标出（v0.2.30 起）**：直接看框就知道
  「哪里变了」，不必去读 `diff_bbox` 的数字。另外注意 `diff_bbox` 是**把所有变化包在一起**的总框——
  两处同时变时它会横跨整屏、基本没有信息量；要看**分开的**改动请用 `diff_boxes`（最多 5 处，按变化量排序）。

## 电脑使用（computer-use）推荐流程

把「看 → 操作 → 看」写成可复用的循环：

1. **观察**：`list_windows()` 找目标窗口；或 `see(window="<标题>")` / `see(handle=<句柄>)` 看清内容。
2. **定位**：用 **`see(text=true)`** 拿到可点击元素的 `screen_center`（屏幕绝对坐标），直接用于点击。
   > 两步旧做法（`ocr` 取词框 → 自己把图片坐标折算成屏幕坐标）已不推荐：那段换算正是最容易错的地方，
   > 现已由插件承担。只有需要**词级**粒度（而非合并后的控件）时才用 `ocr`。
3. **操作**：`click(x,y)` / `double_click` / `type_text` / `press_key` / `scroll`。点击类动作会**自动**把
   上一步 `see` 的窗口置前并复核坐标归属，所以**不必**手动 `focus_window`；只有要操作一个没 `see` 过的
   窗口、或确实需要键盘焦点时，才先调 `focus_window`。
4. **确认**：再 `see` 看结果；不对就回到 2/3 重试，直到目标达成。

```text
focus_window("Google Chrome") → press_key("ctrl+l") → type_text("https://…") → press_key("enter") → see()
```

> ⚠️ 操作会**真实移动/点击/输入**到你的鼠标键盘；务必先 `see` 确认坐标再操作，避免误触。
> 坐标是「抓取那一刻」的快照——拿到后请**尽快点击**，中间别插其它会改变画面的操作。

## 多平台支持

| 能力 | Windows | macOS | Linux |
| --- | --- | --- | --- |
| 抓窗口 / 抓屏 | ✅ 完整（WGC > PrintWindow > 桌面区域），**实测** | ⚠️ Phase 1 代码已写（Quartz 枚举 + `screencapture -l`），**未真机验证**，需「屏幕录制」授权 | ❌ Phase 2 占位（`capture/linux.py` 三个入口 `NotImplementedError`） |
| `screen_info`（多屏/DPI） | ✅ | ⚠️ 已写未测 | ⚠️ 回退 PIL 单屏、`scale=1` |
| OCR | ✅ `Windows.Media.Ocr`（`winsdk`） | ⚠️ 回退 pytesseract（需另装 Tesseract） | ⚠️ 同上 |
| 鼠标/键盘（`pyautogui`） | ✅ 实测 | ⚠️ 需辅助功能授权，未测 | ⚠️ 需 X11/显示，未测 |
| `focus_window` | ✅ | ❌ 仅 Windows（其他平台明确抛错） | ❌ 同 |
| 文本剪贴板 | ✅ 原生（pywin32） | ⚠️ `pyperclip`（已进 requirements） | ⚠️ 同 |
| **剪贴板图片**（按钮变色/长按插入） | ✅ 实测 | ⚠️ NSPasteboard/changeCount，未真机验证 | ❌ Phase 2（如实返回不支持，按钮就不监视不提示） |
| **系统级框选截图**（短按） | ✅ 实测（含取消识别） | ⚠️ `screencapture -i`，未真机验证 | ❌ 501 → 自动回退浏览器抓屏 |

> ⚠️ **只有 Windows 这条链路经过实测**。在 macOS/Linux 上反馈问题时请附平台、Python 版本与完整报错。

这套「实测 / 未验证 / 未实现」的区分不只在文档里——`cvision_status()` 会把它作为**机器可读**字段给出，
模型据此判断该不该尝试 computer-use、以及出问题时该不该怀疑插件：

| `platform_support` | 含义 | 当前平台 |
| --- | --- | --- |
| `supported` | 代码完整且**已实测** | Windows |
| `unverified` | 代码完整但**未在真机验证**，行为可能与文档有出入 | macOS |
| `unsupported` | 明确未实现，调用会得到清晰报错（不是静默失败） | Linux |


## 升级后必须做什么

本包是双面的，**两半的生效方式不同**——升级后没变化，先看这里：

| 改了什么 | 生效方式 |
| --- | --- |
| `lib/client.js`（浏览器半边：按钮、剪贴板监视、门控） | **硬刷新页面** `Ctrl+Shift+R`（普通刷新可能仍用缓存） |
| `lib/index.js`（宿主半边：工具、四条路由） | **重启 DSH**（宿主进程加载时才注册路由） |
| `cvision/*.py`（Python 侧） | 每次调用是新子进程，一般即改即生效；但**常驻 Python server** 会继续用已加载的旧模块——**重启宿主最稳** |

升级方式：`dsh plugin --profile web add`（重新装）/ `rm` 后再 `add`；或换成新的 Release tarball。

> **装/升级前需要先关掉 DSH 吗？** **v0.2.16 起不需要**。若你遇到过这条错误：
>
> ```text
> [ERR_PNPM_EPERM] [importPackage …\node_modules\vision] EPERM: operation not permitted,
>   rename '…\vision_tmp_23816_2' -> '…\vision'
> ```
>
> 根因是插件自己拉起的常驻 Python 子进程（`python -m cvision.cli_server`）**以安装目录为工作目录**，
> 而 Windows 下「进程的当前目录」就是该目录上的一个句柄 → pnpm 无法把临时目录替换成 `node_modules/vision`。
> 现在子进程的 cwd 改为**系统临时目录**、靠 `PYTHONPATH` 找到包内源码，任何子进程都不再持有安装目录，
> 于是**可以边跑边升级**。
>
> ⚠️ 但从 **≤0.2.15** 升到 **0.2.16** 这**一次**仍然要先关 DSH（旧版本还在用旧行为）。

## 配置（可选）

默认即可用；如需覆盖：

- `CVISION_PYTHON`：Python 可执行文件，默认 `python`。
- `CVISION_DIR`：cvision 项目根（含 `cvision/` 包）。默认 = **本插件安装目录**（包内捆绑版）；若不用包内
  副本，可指向仓库根。

> 插件 spawn 的所有 Python 都强制 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`（否则中文窗口标题在 Windows
> 下会乱码，v0.2.2 修）。

## 构建与测试

DSH 运行时只加载 JS，仓库已提交构建好的 `lib/`：

- `lib/index.js` ← `src/index.ts`（**tsc 编译**，宿主半边：工具 + 四条路由）；
- `lib/client.js` ← `src/client.js`（**逐字节拷贝**）。它不能交给 tsc：本包 `"type": "module"` 会让 tsc 把它
  当 ES 模块并在末尾追加 `export {}`，而 DSH 以**经典脚本**加载该文件，`export` 会直接语法错误
  （见 `scripts/copy-client.mjs`）。

```bash
npm ci
npm run build        # tsc -p tsconfig.json && node scripts/copy-client.mjs
npm run test:js      # node --test：客户端半边 + 宿主四条路由
npm run check:dsh    # DSH 组合包/客户端契约自检（30 项）
npm run check:docs   # 文档一致性自检（14 项）
npm run check:deps   # Python 依赖锁定自检（9 项）
python -m unittest discover -s tests -v   # Python 纯逻辑单测（仅需 Pillow）
```

当前规模：**JS 73 条 + Python 246 条**。

### 文档约定（自动校验）

文档漂移靠人记不住，所以 `npm run check:docs` 把下面这些变成断言（CI 每次都会跑）：

| 断言 | 防止的漂移 |
| --- | --- |
| `package.json` 版本 == README 头部版本 == CHANGELOG 最新条目 | 改了代码忘了升版/记条目 |
| CHANGELOG 最新条目有实质内容、无 `TODO/待填` | 占位条目混进发布 |
| README 里的长按阈值 == `src/client.js` 的 `LONG_PRESS_MS`（且不残留旧值） | 改了行为忘了改文档（真的发生过：550ms → 900ms） |
| `cvision/`、`tests/`、`scripts/` 下每个文件都出现在 README 目录结构里 | 新增文件忘了写文档（也真的发生过） |
| 全部顶层文档都在 `package.json` 的 `files` 里 | 新文档没随包发布 |
| README 覆盖宿主注册的**全部工具**与**全部路由** | 加了工具/路由却没有文档 |
| README 声称的测试条数 == 实际条数 | 测试增减后数字过期 |
| README 内部锚点都能落到标题 | 目录断链 |
| `requirements.txt` 每条依赖都有下界与上界（`npm run check:deps`） | 依赖被静默升级到破坏性版本（供应链面失控） |

## CI / 发布

`.github/workflows/ci.yml` 在每次 `push` / `pull_request` 时：

- `npm ci && npm run build`，并校验 **`lib/` 与源码编译产物一致**（改了 `src` 却忘编译会失败）；
- `npm run check:dsh`（30 项契约）+ `npm run check:docs`（14 项文档一致性）+ `npm run check:deps`（9 项依赖锁定）；
- `npm run test:js`（客户端半边 + 宿主路由）；
- Python 单测跑在 **ubuntu + macOS** 矩阵（仅装 Pillow，不需要桌面）；
- **windows-latest 冒烟**：按 `requirements.txt` 真装依赖，再断言 `backend=windows`、
  `platform_support=supported`、`ok=true`。加它的原因：上面两个平台**装不了 pywin32/winsdk**，于是
  Windows 后端的关键路径（WGC / PrintWindow / 窗口枚举）在 CI 上从不执行——这一条至少保证
  「用户照 README 在 Windows 上装完不会立刻报缺依赖」。

### Python 依赖的定期审查

`requirements.txt` 每条都是双向锁定（见 [STORE 契约](#dsh-store-上架契约)），上界取「下一个可能破坏兼容的
边界」。代价是**上界不会自己变**，所以约定：**每季度审查一次**——

```bash
npm run check:deps -- --list   # 打印当前每条依赖的锁定区间
```

审查点两条：上游是否已发新主版本（该不该跟进）；上界是否过紧导致**安全修复进不来**。
`pywin32` 尤其注意：官方明确建议固定（任意一次 build 号增加都可能有接口破坏），所以它锁到下一个 build，
每次官方发版都得显式决定要不要跟。

> ⚠️ **预发布包的上界必须写「下一个预发布号」，不能写它的正式版本。** `winsdk>=1.0.0b10,<1.0.0` 看着合理，
> 实际**装不上**：PEP 440 下 `<1.0.0` 会排除预发布版，于是把唯一可用的 `1.0.0b10` 也排除了。这个坑
> 在 v0.2.17 真实踩过（Windows 上 `pip install` 直接失败，直到 v0.2.19 的 windows 冒烟 job 才发现）。
> 现在 `check:deps` 有一项断言专门盯它。

**发布**：推一个 `v*` tag → 构建+测试通过后自动 `npm pack` 出 `vision-<version>.tgz` 并创建 GitHub Release：

```bash
git tag v0.2.14
git push origin v0.2.14          # ⚠️ 一次只推一个 tag，见下
```

> ⚠️ **踩过的坑**：一条 `git push origin v0.2.6 v0.2.7 … v0.2.13` 连推多个 tag 时，GitHub **没有为这些 tag
> 创建任何 workflow run**（`actions/runs?branch=<tag>` 的 `total_count=0`），Release 自然也不会生成；逐个
> 推（或分批、间隔几秒）才可靠。判据：`git ls-remote --tags origin` 有 ref ≠ 有 run，要看 Actions 页面。

装 Release 产物：`npx -y @deepseek-ai/dsh plugin --profile web add <下载目录>\vision-0.2.14.tgz`。

## 内部 CLI 契约（维护者）

宿主半边用 `child_process` 调这些 `python -m` 入口，**stdout 恒为一行 JSON**（`ensure_ascii=True`），
契约比退出码更重要——非零退出时宿主仍会读 stdout（v0.2.6 的取消路径 bug 就出在这）。

| 入口 | 参数 | 结果 |
| --- | --- | --- |
| `cvision.cli_capture` | `--list` / `--screen-info` / `--status` / `--window` / `--handle` / `--region` / `--delay` / `--format` / **`--text`** / **`--wait-changed`** | 截图时是**裸 data URL 字符串**（不是 JSON）；`--list`/`--screen-info`/`--status` 是各自的 JSON；`--text` → `{ok:true,kind:"capture_text",data_url,width,height,elements:[{text,box,center,screen_box,screen_center,word_count}]}`；`--wait-changed` → `{ok:true,kind:"wait_changed",data_url,changed,samples,elapsed_ms,diff_ratio,mean_diff,diff_bbox}` |
| `cvision.cli_ocr` | `--window` / `--handle` / `--region` / `--delay` | `{ok:true,text,lines,words:[{text,x,y,w,h}]}` |
| `cvision.cli_input` | `--click/--double/--move/--scroll/--scroll-h/--drag/--type/--keys/--focus/--focus-handle/--get-clipboard/--set-clipboard` | 动作类 `{ok:true}`；`--get-clipboard` → `{text}` |
| `cvision.cli_snip` | `--timeout 60` / `--format` | `{ok:true,data_url}`（退出 0）/ `{ok:false,reason:"cancelled"}`（2）/ `"unsupported"`（3）/ `"error"`（1） |
| `cvision.cli_clipboard` | `--state` / `--image` | `{ok:true,supported,image,token,reason}` / `{ok:true,data_url}`、`{ok:false,reason:"empty"\|"unsupported"\|"error"}` |

> `cli_capture` 这一行以前写错过：它被写成返回 `{ok:true,data_url,width,height}`，实际截图路径输出的是
> **裸 data URL**（宿主 `runCliCapture` 就当整串是 data URL）。现在表格以**代码实际行为**为准，并由
> `test_cli_server.py` / `test_cli_ocr.py` / `test_cli_input.py` 三份契约测试盯住——`cli_server` 的
> JSON-line 协议此前**一行测试都没有**，而它是宿主唯一的常驻通道。

另有**常驻进程** `cvision.cli_server`：stdin 逐行收 JSON 请求、stdout 逐行回响应，复用 WGC 的 D3D 设备与
编码器（避免每次工具调用冷启动解释器）。op：`ping` / `capture`（`text:true` 时返回可点击元素）/
**`wait_changed`** / `ocr` / `list` / `screen_info` / `status` / `clipboard_state` / `quit`。
宿主优先走它，失败自动回退到上面的 CLI。

> ⚠️ 宿主的请求超时是 **45s**（v0.2.19 从 30s 提高）：`wait_changed` 会在 Python 侧**阻塞**到画面变化或
> 超时，这个上限必须大于它，否则常驻进程会被自己的超时回收，白等一场还回退到 CLI。

## 目录结构

```text
vision/                      # 仓库根 = 插件本体
  src/index.ts           # 宿主半边源（工具 + 四条 /cvision/* 路由）
  src/client.js          # 客户端半边源（经典脚本：截图按钮 + 剪贴板监视 + 门控）
  lib/index.js           # tsc 编译产物（DSH 实际加载）
  lib/client.js          # 客户端产物（scripts/copy-client.mjs 逐字节拷贝，不能过 tsc）
  scripts/
    copy-client.mjs      #   把 src/client.js 拷到 lib/
    check-dsh-contract.mjs #  DSH 组合包/客户端契约自检（30 项，CI 跑）
    check-docs.mjs       #   文档一致性自检（14 项：版本号/阈值/目录/工具/路由/测试数/锚点，CI 跑）
    check-deps.mjs       #   Python 依赖锁定自检（9 项：上下界/具体版本/预发布上界/重复/marker，CI 跑）
  tsconfig.json          # TS 配置
  package.json           # 声明 dsh.bundle + dsh.client，files 含 lib/cvision/requirements.txt/CHANGELOG
  cordis.patch.yml       # bundle 的配置层，按包名引用
  requirements.txt       # Python 依赖（全部双向锁定 >=x,<y：Pillow/pyautogui/pyperclip；Windows 加 pywin32/winsdk/winrt-*；macOS 加 pyobjc；需 Python 3.10+）
  cvision/               # 捆绑的 Python 版 cvision（截屏/OCR/用户级输入/系统截图/剪贴板）
    __init__.py          #   包标记
    capturer.py          #   兼容层：转发到平台捕获后端
    capture/             #   平台捕获后端（门面，按 sys.platform 选）
      __init__.py        #     选后端并暴露 list_windows/capture_window/capture_screen
      base.py            #     平台无关 Window + CaptureBackend 协议
      windows.py         #     Windows 后端（WGC > PrintWindow > 读合成桌面区域）
      wgc.py             #     Windows Graphics Capture：优先 PyWinRT 标准 D3D11 互操作，回退 winsdk/WinML
      macos.py           #     macOS 后端（Quartz 枚举 + screencapture -l）
      linux.py           #     Linux 后端（Phase 2 占位）
    detect.py            #   纯逻辑判定（GPU 类/空白帧），不依赖 win32，可跨平台单测
    coordinates.py       #   图片像素 → 屏幕绝对坐标（裁剪/窗口/多屏/DPI 四层换算）
    ui_elements.py       #   词框合并成可点击元素（同行相邻词合并 + padding + 屏幕坐标）
    diff.py              #   帧间差异度量 + 变化区域聚类 + 高亮画框，供两个 wait_until_* 共用
    diagnose.py          #   窗口跟踪诊断（默认关闭、零开销）：定位「抓图是否挪动了窗口」
    encoding.py          #   PIL -> data URL；crop_region；fit_for_attachment（附件缩图）
    screen.py            #   显示器/DPI 布局（Windows/macOS；Linux 回退 PIL 单屏）
    status.py            #   运行环境探针（平台后端/OCR/依赖/能力清单）
    ocr.py               #   OCR（Windows.Media.Ocr 优先 / pytesseract 回退）
    input.py             #   用户级输入（pyautogui）+ focus_window（仅 Windows；只置前不改尺寸）
    snip.py              #   系统级区域截图（人工通道）：拉起系统截图 UI、识别取消、取回框选结果
    clipboard.py         #   剪贴板图片读取 + 「是否变了」判定（Windows/macOS；Linux Phase 2）
    cli_capture.py cli_ocr.py cli_input.py cli_snip.py cli_clipboard.py cli_server.py
  tests/
    test_detect.py test_encoding.py test_ocr_words.py test_pick_window.py test_screen.py test_status.py
    test_input.py         #   置前语义（假 win32：最大化绝不被降级）+ 能力清单按平台
    test_cli_ocr.py       #   cli_ocr stdout 契约（词框透传/字段形状/缺字段兜底/--region 转发）
    test_cli_server.py    #   **常驻 server 的 JSON-line 协议**（响应形状 + 真 spawn 进程往返）
    test_cli_input.py     #   cli_input 参数层（13 个子命令 → input 函数的逐条映射）
    test_coordinates.py   #   坐标换算（DPI/裁剪/窗口/多屏/负坐标）
    test_ui_elements.py   #   词框合并成可点击元素（同行相邻合并/间距切分/坐标换算）
    test_diff.py          #   帧间差异（相同/微变/实变/尺寸变化 + 阈值边界）
    test_clipboard_race.py #  剪贴板竞态回归（用户占用期间复制的内容绝不被覆盖）
    test_capture_foreground.py #  抓图前的窗口准备：普通窗口不碰前台/最小化窗口会被置前
    test_wgc_probe.py      #   WGC 可用性探测的缓存语义（指纹/过期/时钟回拨/损坏文件 → 必须失效）
    test_wait_stable.py    #   wait_until_stable 的判定：必须是**连续**安静，中途变一次要重新计数
    test_diagnose.py      #   窗口跟踪诊断（改动字段判定 / 开关 / 关闭时不写文件 / 写失败不抛）
    test_snip.py          #   系统截图 CLI 的 JSON 契约
    test_snip_windows.py  #   取消识别（假时钟/覆盖层/剪贴板：取消立即返回、晚到图片不算本次）
    test_clipboard.py     #   剪贴板模块与 CLI 契约（平台分支 / empty / unsupported / error）
    vision.client.test.mjs #  客户端半边单测（门控/截图与回退/剪贴板监视与长按/失败可见）
    vision.host.test.mjs   #  宿主四条路由单测（能力判定 + 系统截图 + 剪贴板状态/取图）
  README.md  CHANGELOG.md
```

> 注：MCP server 相关的 `config.py`/`deepseek.py`/`server.py` 已从捆绑包移除（插件截屏无需它们，也免去了
> `DEEPSEEK_API_KEY` 依赖）。

## 故障排查

| 现象 | 先看这里 |
| --- | --- |
| 截图按钮不显示 | 当前模型是否收图（`inputModalities` 不含 `image` 时按设计隐藏）；`cvision_status()`；控制台 `[vision]` 日志；若**出现两个按钮**，是旧插件 `@deepseek-ai/dsh-client-ui-screenshot` 仍在挂载 |
| 点按钮没反应 | 控制台 `[vision] …` 一定给了原因（插入链的失败在 v0.2.4 起不再静默）；附件栏被拒时会自动重试 6 次 |
| 按钮变蓝、长按却没插入 | 长按阈值 **0.9s**；按住时应看到底部进度条；按钮未点亮时按住不做任何事（按设计） |
| 单击截图后按钮变蓝 | 已由 `served` 归属解决（v0.2.10/0.2.11）；若仍出现，见 README「取消与归属」的时序说明 |
| 取消了截图，之后别的截图却进了附件栏 | v0.2.13 起修复（覆盖层判据）；若先前的旧版本仍在跑，重启宿主 |
| 工具报 `python` 找不到 / 依赖缺失 | 装 Python 3.10+ 与 `python -m pip install -r requirements.txt`；**v0.2.18 起首次调用会直接给出带绝对路径的安装命令**；`cvision_status()` 会列出缺哪个模块 |
| 抓窗口是黑图/空白 | 依次看：① `cvision_status()` 的 `capture_backends.wgc` —— 它会**真实探测**并给出 `reason`，别只看依赖装没装；② 装了 PyWinRT（`winrt-*`，见 `requirements.txt`）时 WGC 走标准 D3D11 互操作，**虚拟机上也能抓被遮挡窗口**；只装了 `winsdk` 时它要借 WinML 拿设备，虚拟机常见 `DXGI_ERROR_UNSUPPORTED`，此时 WGC 不可用、自动回退 `PrintWindow`／桌面区域；③ 微信等 Qt 窗口属已知空白帧场景 |
| **抓图后窗口位置/大小变了**（如分屏被破坏） | 用内置**窗口跟踪诊断**取证，别猜：`$env:CVISION_TRACE_WINDOWS='1'; $env:CVISION_TRACE_FILE='C:\Temp\cv-trace.jsonl'` → 复现一次 → `python -m cvision.diagnose`。日志按阶段（`prepare`/`wgc`/`printwindow`/`grab_region`）记录 `rect`/`showCmd`/`zoomed`/`iconic`/`foreground` 的前后值，能区分「抓取过程中动的」与「抓取前后被别的因素动的」。默认关闭、零开销 |
| 中文窗口标题匹配不上 | v0.2.2 起所有 Python 子进程强制 UTF-8；若自行调用 Python，请一并设 `PYTHONUTF8=1` |
| 升级后行为没变 | 见[升级后必须做什么](#升级后必须做什么)：客户端半边要**硬刷新**，宿主半边要**重启** |
| `plugin add` 报 `ERR_PNPM_EPERM … rename '…vision_tmp_…' -> '…vision'` | 安装目录被占用（≤0.2.15 的插件 Python 子进程以它为 cwd）。**先关 DSH** 再装；0.2.16 起不会再有此问题（cwd 已改为系统临时目录） |
| macOS 上窗口标题为空 | 需在「系统设置 → 隐私与安全 → 屏幕录制」授权 |

## 说明与限制

- **跨语言**：插件用 `child_process` 调包内 Python 做截屏/OCR/输入，需目标机器有桌面环境与 Python 3.10+。
- **截图后端**：`capture_window` 依次尝试 **Windows Graphics Capture**（真实合成内容，抓 GPU/Chromium/被遮挡
  窗口最准）→ **PrintWindow** → **读合成桌面区域**（兜底，此时才可能置前，抓完立即还原）。
  WGC 需要一套 Python WinRT 绑定：**推荐 PyWinRT**（`winrt-runtime` + `winrt-Windows.Graphics.*`，见
  `requirements.txt`），它走标准 D3D11 互操作；只装了旧的 `winsdk` 时只能借 WinML 拿设备，在虚拟机/受限
  驱动上会失败（实测 VMware SVGA 3D：`DXGI_ERROR_UNSUPPORTED`）。`cvision_status().capture_backends`
  会**真实探测**并说明当前走的是哪一套——不是「依赖装没装」。
- **附件限制**：Harness attachment 单图源 ≤20MiB、单边 ≤8192px、每条消息 ≤20 张；输出前会自动缩放到限制内
  （`encoding.fit_for_attachment`），超大屏也不会被拒。
- **省 token**：`region="x,y,w,h"` 只处理一块；`ocr` 直接返回文本；超大图自动降采样。
- **无出站网络**：所有捕获/OCR/输入都在本地完成。
- **输入类工具**（`click`/`type_text` 等）会**真实操作你的鼠标键盘**；调用前请先 `see` 确认坐标。
- **macOS/Linux 后端**为编写实现，需在对应平台 + 权限下验证；未支持平台上的边界由各工具显式报错。

---

## DSH STORE 上架契约

> 下面每一条都由 `npm run check:dsh`（`scripts/check-dsh-contract.mjs`）机械校验，CI 每次都会跑；对应 DSH
> 文档 `docs/user/develop/basic/publish.zh.md`（组合包 manifest）与
> `docs/subsystems/client-modules.zh.md`（客户端半边）。

### 依赖

- **Node 运行时**：无 npm 运行时依赖（`dependencies` 为空）。
- **内置组件**：包内捆绑 **Python 版 cvision**（`cvision/**/*.py`）+ 依赖清单 `requirements.txt`；运行时跨语言
  调用该 Python 子进程做截屏/OCR/输入，`CVISION_DIR` 默认指向包内。这是本插件唯一的独立供应链面，由 DSH
  STORE 供应链复查把关。
- **Peer**：`@deepseek-ai/dsh-tools`、`@deepseek-ai/cordis`——由宿主提供，且两者本来就是本仓库的
  devDependency（因此 `npm ci` 不需要额外下载）。`lib/index.js` 运行时**只** import 前者（`cordis` /
  `dsh-attachment` / `node:http` 都是 `import type`，编译后不残留）；`ctx.tools` / `ctx.attachments` 由宿主注入。
- **宿主入站路由**：四条，都通过 `ctx.inject(['webServer', ...])` 可选挂载，组合里没有 web 服务器时整段跳过。
  - `GET /cvision/model-capability`：**只读**、同源、无副作用、不回传任何凭据，供浏览器半边判断当前模型是否
    收图。
  - `POST /cvision/snip`：**仅 POST、仅同源**（`Origin` host 必须等于 `Host`，否则 403），拉起系统截图 UI 并
    把用户框选的那张图回传（200 图片字节 / 204 用户取消 / 501 平台不支持）。抓屏动作由用户在系统 UI 里完成，
    本路由只读「调用之后新出现」的剪贴板图片，因此不构成静默抓屏能力；客户端断开（关页/取消）会中止等待中的
    Python 子进程。
  - `GET /cvision/clipboard`：**只读**、廉价（只查剪贴板格式 + token，不解码图片），供页面每秒轮询「剪贴板里
    有没有图片」，并附 `served` 标记（这张图是不是我们自己刚产出的）。不回传图片内容本身。
  - `POST /cvision/clipboard/image`：**仅 POST、仅同源**，返回剪贴板里的图片（200 图片字节 / 204 没有图片 /
    501 平台不支持）。**注意**：它让页面里的脚本（含其它客户端插件）也能读到剪贴板图片——这是「按钮要看见
    其它软件的截图」的固有代价，故限制为同源 + 仅 POST + 只在用户长按时调用。

### 权限说明（真实高权限）

- 通过**跨语言 spawn 包内 Python cvision** 子进程（`child_process`/进程管理）。
- **用户级操作**：屏幕截图、OCR、鼠标点击/移动/滚动、键盘输入/快捷键、窗口聚焦——属于**设备级输入/捕获权限**。
- **剪贴板**：读取剪贴板图片（仅在按钮长按时）与读写剪贴板文本。
- 把截图写入 Harness 附件服务 `ctx.attachments.saveImage`（由宿主代为落盘），或返回文本。
- 这些是插件正常工作所需的**真实高权限**，DSH STORE 会将其作为 **user-reviewed/guarded** 对待。

### 外部服务

- **无出站网络**：所有捕获/OCR/输入/剪贴板读取均在本地。

### 失败边界

- 包内 Python `cvision` 缺失或 `requirements.txt` 依赖未安装 → `see`/`ocr`/`click` 等工具报错或禁用。
- 系统级屏幕捕获/权限被拒、被遮挡窗口、无窗口 → 对应工具返回失败（不影响宿主主流程）。
- **抓取最小化窗口会置前并抢走前台**（几何抓完会还原回最小化，但前台已经被它拿走）；兜底路径
  （WGC 与 PrintWindow 都失败、改读合成桌面区域）同样会置前。普通窗口（前台或背景）则不改几何、
  不抢前台。口径与实测值见[给 AI 智能体的使用提示](#给-ai-智能体的使用提示重要)。
- **被遮挡/重叠窗口的控件**：`see(text=true)` 给的 `screen_center` 坐标本身正确，而点击按屏幕坐标下发、
  只会命中前台窗口。点击类工具会**自动**把「最近一次 `see` 的那个窗口」置前并复核坐标归属（v0.2.25 起）；
  复核不过就明确报错并**跳过这次点击**，而不是静默点到别的窗口上。目标窗口已销毁时，宿主的记录会自动作废。
- 系统截图被用户取消 → 返回 204，客户端静默、不插入任何附件（v0.2.13 起不再把晚到的剪贴板图片当成本次结果）。
- 剪贴板图片在未支持平台（Linux Phase 2）→ 返回 501，客户端按钮不监视也不提示（功能不报错）。
- 跨平台支持不完整（macOS/Linux 为 Phase 1/2），在未支持平台上报错的边界由各工具显式给出。
- 宿主能力/剪贴板路由不可达（Electron `file://` 组合、`webServer` 未挂载、旧版宿主未重启）→ 浏览器半边退回
  名字启发式或停止轮询（只告警一次）；工具本身不受影响。

### 一次性 Profile 安装-启动-卸载证据

```bash
dsh plugin --profile tmp add github:cczzyy-cn/c-vision   # 作为 bundle 自动挂载
dsh profile start tmp &      # 加载 `vision` bundle，注册 see/ocr/click 等工具
dsh plugin --profile tmp rm vision
dsh profile stop tmp         # 干净退出，无残留
```
