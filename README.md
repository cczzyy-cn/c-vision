# Vision · DeepSeek Harness (DSH) 视觉插件

给 DeepSeek Harness 的 agent 提供**自动视觉能力（看）+ 用户级操作（操作）**：模型调用 `see`/`ocr`/`list_windows`
看清屏幕/窗口，再用 `click`/`type_text`/`press_key`/`scroll`/`focus_window` 像人一样操作，形成 **看→操作→看** 的 computer-use 闭环。

这是一个 **DSH 组合包（bundle）**，通过 `dsh plugin add` 安装。插件注册工具 → 跨语言调用**包内捆绑的 Python 版 cvision** 截屏/OCR/输入 → 写入 Harness 附件服务（`ctx.attachments.saveImage`）或返回文本 → 以 **`image` ContentBlock / `text`** 返回给模型。

同一个包还带一个**浏览器半边**：输入框工具栏的「截图」按钮（人工一键抓屏 → 草稿图 → 随消息发给模型）。

## 特性

- **原生看图**：`see` 抓真实截图（WGC 抓窗口合成内容，GPU/被遮挡窗口也稳），模型直接看到。
- **快速读字**：`ocr` 直接返回文本；`see`/`ocr` 支持 `region="x,y,w,h"` 只取一块，省 token。
- **用户级操作**：鼠标点击/移动/滚动、键盘输入/快捷键、窗口聚焦（模拟人操作）。
- **输入框截图按钮**：浏览器半边在输入框工具栏加「截图」按钮，**默认走系统级框选截图**
  （Windows Win+Shift+S / macOS screencapture -i，可框选可标注），抓到的图直接进附件栏；
  宿主这条通道不可用时自动回退浏览器抓屏。显示与否由**宿主真实 `inputModalities`** 决定。
- **跨平台**：Windows（完整）/ macOS(Phase 1) / Linux(Phase 2)。
- **开箱即用**：包内自带 Python cvision 与依赖清单，`CVISION_DIR` 默认指向包内。

## 版本

> 维护约定：凡是改行为，就升 `package.json` 版本并在此追加一条（附提交号），避免版本与文档漂移。

- **v0.2.8**：截图按钮新增**剪贴板监视 + 长按插入**；剪贴板能力跨平台化。
  - 页面**每秒**轮询宿主 `GET /cvision/clipboard`（廉价：只查剪贴板格式 + token，**不解码图片**；
    优先走常驻 Python 进程，免得每秒冷启动解释器）。剪贴板里**出现新图片**（其它软件截图、微信/QQ、
    Win+Shift+S 都算）→ 按钮**变色 + 右上角圆点 + 悬浮提示**；**长按按钮（≥550ms）**→ 把剪贴板图片
    作为附件插入 → 配色恢复正常。**短按仍是系统框选截图**。
  - 为什么轮询在宿主：浏览器**不可能**后台读剪贴板（`navigator.clipboard.read()` 需要用户手势与授权，
    而且没有剪贴板变更事件）。页面只做同源 HTTP 轮询，真正读剪贴板的是宿主原生侧。
  - 新增 `cvision/clipboard.py`（Windows：`IsClipboardFormatAvailable` + 剪贴板序列号 + `ImageGrab`；
    macOS：NSPasteboard/changeCount，**未在真机验证**；Linux：Phase 2，如实返回不支持——按钮就不监视
    也不提示）与 `cvision/cli_clipboard.py`；常驻进程新增 `clipboard_state` op。
  - **剪贴板跨平台**：`requirements.txt` 补 `pyperclip`（非 Windows 的文本剪贴板）与
    `pyobjc-framework-Cocoa`（macOS 读剪贴板图片）；`focus_window` 按平台如实从能力清单里消失
    （`cvision_status` 不再在非 Windows 上高报）。
  - 省电与降级：页面隐藏时不轮询；宿主没有这条路由（未升级/未重启）或连续失败 3 次即停止，且只告警一次。
  - 安全：状态路由只读；取图路由 **POST + 同源**，且只在用户长按按钮时调用。**已知取舍**：页面里的脚本
    （含其它客户端插件）可以通过这条路由读到剪贴板里的图片——这是「让按钮看见其它软件的截图」的固有
    代价，已在下面 STORE 契约里如实记录。
  - 测试：JS 33 → **44** 条（剪贴板监视/长按/降级 + 宿主两条新路由），Python 51 → **60** 条。
  - 实测：真机模拟「其它软件把图放进剪贴板」→ 状态 `image: true` 且 token 变化 → `--image` 返回 PNG
    data URL（160×60 测试图）。
- **v0.2.7**：修复 **`focus_window` 会把窗口改成半屏** —— 它无条件调用
  `ShowWindow(SW_RESTORE)`，而 `SW_RESTORE` 不只是「还原最小化」：**最大化**的窗口也会被降级成
  普通尺寸（全屏浏览器缩回上次的普通大小，看起来就是「窗口被改成半屏」）。现在拆出可测的
  `show_command_for()` / `bring_to_front()`：**只有最小化**的窗口才 `SW_RESTORE`，最大化/普通窗口
  一律保持原状，只做置前（与 `capture/windows.py:_prepare_window_for_capture` 的守卫一致）。
  工具描述与 README 同步写明「只改前后层级，不改尺寸/最大化状态」。新增 `tests/test_input.py` 7 条
  （用假 win32 驱动，平台无关），Python 单测 44 → 51。
  > 起因（诚实记录）：我在会话里为「让 Chrome 置前好发按键」调了三次 `focus_window`，把你的全屏
  > 浏览器缩成了半屏；我一开始误判成 DevTools 停靠，是**你指出后重查抓图尺寸（2560 → 1280 → 2560）
  > 才定位到真正的调用点**。
- **v0.2.6**：截图按钮**默认改走系统级框选截图**（人工通道），浏览器抓屏降为回退。
  - 宿主新增 `POST /cvision/snip`：拉起系统截图 UI（Windows `Win+Shift+S` / `ms-screenclip:` 兜底、
    macOS `screencapture -i -x`），把用户框选的图交回浏览器半边。**只认调用之后新出现的结果**
    （Windows 用 `GetClipboardSequenceNumber` 前后对比），绝不读用户上一次复制的旧图。
  - 安全：仅 POST、仅同源（`Origin` 必须等于 `Host`）、客户端断开即中止 Python 等待；抓屏授权由
    系统 UI 承载，因此**没有**「一条请求就能静默抓桌面」的口子（这是放弃宿主静默抓屏的原因）。
  - 回退：宿主返回 501（Linux Phase 2）/不可达/旧版宿主 → 自动回退浏览器 `getDisplayMedia`；
    用户 Esc 取消（204）→ 静默恢复，不弹提示。
  - UX：等待框选期间按钮 `aria-busy` + 禁用，避免重复拉起系统 UI；`see` 工具与按钮的图片都经
    `encoding.fit_for_attachment` 缩放，成本模型一致。
  - 新增 `cvision/snip.py` + `cvision/cli_snip.py`；JS 单测 33 条（含系统截图 501 回退、204 取消、
    同源/方法拒绝、执行器注入）。真机实测：热键 → 覆盖层 → 拖拽框选 → PNG（IHDR 与框选尺寸一致）。
- **v0.2.5**：修掉 v0.2.4 插入链的两个真实错误，截图按钮端到端跑通（浏览器内实测：缩略图进入附件栏）。
  1. **取错了 conversation face**：草稿 API 在 **conversation 根服务**上，而
     `ctx.sessions.scope(id).get('conversation')` 是**会话动作面**（send / cancel / updateQueue）。
  2. **打错了 API 代次**：DSH 换过一代草稿接口 —— 新版是
     `conversation.createDrafts(sessionId, files)` + `inputActions.addAttachments(ids)` +
     `releaseDraftAttachment(id)`，旧版是 `createDraftImages(files)` + `inputActions.addImages(ids)` +
     `releaseDraftImage(id)`。现在按**能力探测**同时支持两代，不写死任何一代
     （插件跑在哪个宿主版本上都得能用）。
  > 定位靠的是 v0.2.4 加的失败可见性：控制台 `[vision] …` 直接给出原因；`directoryFor`/`modelDirectories`
  > 仍是惰性、每次现取，并在 `inject` 里声明 `sessions` / `remote.session`（Service tracker 按**调用方
  > fiber** 校验 inject）。JS 单测 22 条，含两代 API 与「会话作用域 face 是诱饵」的回归用例。
- **v0.2.4**：修复 **v0.2.3 截图按钮「点了没反应」** —— 上游遗留的插入链在第三方插件上下文里必然失效：
  (a) 它在 `apply()` 时 eager 缓存 `ctx.conversation`，而第三方插件激活时该服务可能尚未就绪 → 拿到 `undefined`；
  (b) 会话级方法需要**调用方 ctx 的 session 作用域**；
  (c) 它把每一步失败都写成 `return null` / `catch {}`，于是唯一表现就是「什么都没发生」。
  现在：服务**惰性解析**；`inject` 补上 `sessions` / `remote.session`
  （DSH 的 Service tracker 按**调用方 fiber** 校验 inject，`directoryFor()` 内部就会读 `remote.session` / `ctx.sessions`）；
  入轨被拒时短暂重试；任何失败都写控制台并在按钮旁给出短提示（用户取消/拒绝授权仍保持静默）。
  JS 单测 21 条，含「apply 时服务未就绪、之后就绪仍能插入」的回归用例。
- **v0.2.3**：把输入框**截图按钮**（原外部插件 `@deepseek-ai/dsh-client-ui-screenshot`）集成进本包，
  成为双面包的客户端半边，并修掉它的视觉判定 bug：上游靠 `model.inputModalities`（浏览器端**根本拿不到**——
  DSH 的 `buildModelCatalog` 只投影 `id/name/description/reasoning`）加「模型名含 `vision|visual`」兜底，
  于是声明了 image 但名字不含 vision 的模型（如 `deepseek-v4.1`）永远没有按钮，名字带 vision 的文本模型
  反而误显示。现在由宿主半边新增只读路由 `GET /cvision/model-capability` 按真实适配器目录回答，
  宿主不可达时才退回名字启发式。
- **v0.2.2**（`c35df09`）：修复 **Windows 下中文窗口标题乱码导致窗口匹配失败** —— 此前 Python 子进程按
  控制台/ANSI 代码页解码 argv/stdin，中文标题到达时已损坏（`????`）；现所有 spawn 的 Python
  （server + CLI）强制 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`。
- **v0.2.1**（`b4b181b`）：修复 `ocr` / `see(ocr:true)` 的**词级边界框没有真正送到模型** ——
  值里带了 `words`，但渲染回调只发文本，模型看不到词框；现在 `words` 非空时追加一个 `word_boxes` 文本块。
- **v0.2.0**（`115fd50`）：OCR 返回**词级边界框** `words`；新增 `screen_info` / `cvision_status` / `wait_for_window` /
  `drag` / `get_clipboard` / `set_clipboard`；`scroll` 支持水平、`see(ocr:true)` 一次返回图片+文本；
  新增**持久化 Python server**（复用 D3D 设备/编码，失败自动回退每调用 CLI）。

## 工具一览

### 看（观察）
| 工具 | 说明 |
| --- | --- |
| `see(handle?, window?, region?, delay?, maximize?, format?, ocr?)` | 截屏/窗口 → **图片**返回（模型原生看）。`handle`（来自 `list_windows`）比 `window` 标题**更精确、标题变化时更稳**，二者二选一且**优先 `handle`**；`region="x,y,w,h"` 只取一块（省 token）；`delay=毫秒` 等渲染；`maximize` 默认关；`format` 可选 PNG/JPEG/WEBP/GIF（默认 PNG）；`ocr=true` 同时返回 OCR 文本/词框 |
| `ocr(handle?, window?, region?, delay?)` | 截屏后 **OCR** → 返回**文本 + 词级边界框 `words`**（`{text,x,y,w,h}`，供 computer-use 精确定位点击点）；同样 **`handle` 优先于 `window`** |
| `list_windows()` | 列出可见窗口（标题+句柄+尺寸） |
| `screen_info()` | 列出显示器/DPI 布局（`x/y/width/height/primary/scale`），高 DPI 折算坐标用 |
| `cvision_status()` | 运行环境健康探针（python 版本、平台后端、OCR 引擎、依赖/后端可用性） |
| `wait_for_window(title?, timeout?)` | 轮询等某个窗口出现（默认 500ms/次，10s 超时） |

### 操作（computer-use，模拟用户级输入）
| 工具 | 说明 |
| --- | --- |
| `click(x, y, button?)` | 屏幕绝对坐标**单击**（left/right/middle） |
| `double_click(x, y)` | 屏幕绝对坐标**双击** |
| `mouse_move(x, y)` | 移动鼠标到屏幕坐标（不点击） |
| `scroll(x, y, dy?, dx?)` | 在 (x,y) 处滚动（dy>0 上滚，<0 下滚；dx 为水平滚动） |
| `drag(x1, y1, x2, y2, button?)` | 从 (x1,y1) 拖拽到 (x2,y2)（框选/拖文件） |
| `type_text(text)` | 像键盘一样**输入文本**到当前焦点 |
| `press_key(keys)` | 发送**快捷键**，如 `ctrl+l`、`enter`、`ctrl+shift+t`、`alt+tab` |
| `get_clipboard()` / `set_clipboard(text)` | 读写剪贴板文本（Windows 原生；macOS/Linux 走 `pyperclip`） |
| `focus_window(title?, handle?)` | 把窗口**置前**（用户级激活）；**`handle` 优先于 `title`**；**只改前后层级，不改窗口尺寸/最大化状态**（仅最小化的窗口会被还原） |

> **关键**：默认**不最大化、不切前台**——WGC 抓窗口合成内容，与前台/遮挡无关。
> **computer-use 闭环**：`see` 看清 → `click`/`type_text`/`press_key`/`scroll` 操作 → 再 `see` 确认 …（"看→操作→看"循环）。

## 输入框截图按钮（浏览器半边）

本包是**双面包**：除宿主半边的工具外，还声明了 `dsh.client`，由 DSH 的客户端模块系统把
`exports["./client"]`（`lib/client.js`，经典脚本）送进浏览器，在输入框工具栏
`conversation.input.right` 挂一个「截图」按钮。

**默认通道：系统级框选截图**（v0.2.6 起）。点按钮 → 宿主拉起系统截图 UI →

```text
Windows  Win+Shift+S（`ms-screenclip:` 兜底）→ 系统覆盖层，框选后结果进剪贴板
macOS    screencapture -i -x <tmp.png>          → 交互框选直接写文件（Esc 不留文件）
Linux    Phase 2：返回 501，浏览器半边自动回退到下面的浏览器抓屏
```

→ 宿主把用户刚框出来的那张图（`POST /cvision/snip`）交回浏览器 → 包成 `File` → 作为草稿图进
附件栏 → 跟随消息发给模型。

为什么默认走这条：框选与**标注**（箭头/高亮/文字）都是系统原生、天然跨显示器，**抓屏授权由系统 UI
承载**——宿主只读「用户刚放进剪贴板/文件的那张新图」（Windows 用 `GetClipboardSequenceNumber`
做前后对比，绝不读用户上一次复制的旧图），因此不存在「页面里任何脚本都能静默截屏」的口子。
代价是这一步会**占用剪贴板**（Win+Shift+S 本身的固有行为）。

**回退通道：浏览器 `getDisplayMedia`**。只有宿主那条路不可用（非桌面平台组合、Electron
`file://` 里没有 web 服务器、旧版宿主）或明确返回不支持时，才回退到浏览器抓屏——功能在任何平台上
都不会消失。点按钮后按钮进入「等待框选」态（期间禁用，Esc 取消即静默恢复）。

**剪贴板监视与长按插入**（v0.2.8）：

```text
每秒        GET  /cvision/clipboard         宿主原生查：有没有图片 + token（不解码）
出现新图片  按钮变色 + 右上角圆点 + 提示「剪贴板有新图片：长按按钮插入」
长按 ≥550ms POST /cvision/clipboard/image   图片进附件栏 → 配色恢复正常
短按        系统框选截图（上面的默认通道）
```

平台：读剪贴板图片 **Windows 完整（实测）** / macOS 代码已写（NSPasteboard，未真机验证）/
Linux Phase 2（如实返回不支持，按钮就不监视也不提示）。文本剪贴板（`get_clipboard`/`set_clipboard`）
Windows 走原生，其它平台走 `pyperclip`（已进 `requirements.txt`）。

> ⚠️ **已知取舍**：这条取图路由让**页面里的脚本**（包括其它客户端插件）也能读到剪贴板里的图片——
> 这是「让按钮看见其它软件的截图」的固有代价（浏览器本身不允许后台读剪贴板），所以取了
> **同源 + 仅 POST + 只在长按时调用**三个约束，并在下面的 STORE 契约里写明。

**可见性判定**：DSH 给浏览器的模型目录（`buildModelCatalog`）只投影
`id/name/description/reasoning`，**刻意剥掉了 `inputModalities`**，所以客户端无法自行判断当前模型
收不收图。本包改为向自己宿主半边的**只读**路由查询：

```text
GET /cvision/model-capability?provider=<id>&model=<id>
→ { "source": "declared", "image": true|false, "modalities": ["text","image"] }   # 按真实适配器目录
→ { "source": "unknown",  "image": false }                                        # 该路由解析不出来
```

判定口径与 Session 的图片准入一致：只有**显式声明**了 `inputModalities` 且不含 `image` 才算不收图
（未声明时 DSH 仍会放行）。宿主答 `declared` 时以宿主为准；答 `unknown`、或路由根本不可达
（例如 Electron 的 `file://` 组合里没有 web 服务器）时才退回「id/name 含 `vision|visual`」的名字
启发式。同一 `provider/model` 只查一次并缓存。

> ⚠️ 与原外部插件**不能同时挂载**，否则输入框会出现两个截图按钮。若曾装过
> `@deepseek-ai/dsh-client-ui-screenshot`，请从 profile 的 `cordis.patch.yml` 删掉它的 insert 行，
> 并（可选）`dsh plugin --profile web rm @deepseek-ai/dsh-client-ui-screenshot` 清理依赖。

## 分发：自带 Python 版 cvision

插件**打包了 Python 版 cvision**（目录 `cvision/`）与 `requirements.txt`。因此：

- 安装者**无需克隆 / 拷贝本仓库**，也**不需要设置 `CVISION_DIR` 指向某个绝对路径**；
- `CVISION_DIR` 默认解析为**本插件安装目录**（`import.meta.url` 推导），即包内捆绑版；
- 目标机器只需有 **Python 3**，并执行一次依赖安装。

> 注意：`cvision/` 是随包复制的源码快照。仓库根 `cvision/` 的改动不会自动同步到包内；
> 升级时重新拷贝 → 重新 `dsh plugin add` 即可。

## 构建（改了 `src/` 才需要）

DSH 运行时只加载 JS，仓库已提交构建好的 `lib/`：

- `lib/index.js` ← `src/index.ts`（**tsc 编译**，宿主半边：工具 + 模型能力路由）；
- `lib/client.js` ← `src/client.js`（**逐字节拷贝**，浏览器半边）。它不能交给 tsc：本包
  `"type": "module"` 会让 tsc 把它当 ES 模块并在末尾追加 `export {}`，而 DSH 是以**经典脚本**
  加载该文件的，`export` 会直接语法错误（见 `scripts/copy-client.mjs`）。

```bash
npm run build        # tsc -p tsconfig.json && node scripts/copy-client.mjs
npm run test:js      # node --test：客户端门控 + 宿主能力路由
```

## CI / 发布

- **CI**（`.github/workflows/ci.yml`）：每次 `push` / `pull_request` 自动：
  - `npm ci && npm run build`，并校验 `lib/` 编译产物与提交一致（改了 `src` 却忘编译会失败）；
  - `npm run check:dsh`：DSH 组合包/客户端契约自检（30 项，见下文「DSH STORE 上架契约」）；
  - 跑 `node --test` 的 JS 单测：客户端截图按钮门控（含宿主不可达时的兜底）+ 宿主能力路由判定口径；
  - 跑 Python 纯逻辑单测（`encoding`/`detect`，仅需 Pillow），在 **ubuntu + macOS** 矩阵上运行。
- **发布**：打一个 `v*` 标签（如 `v0.1.9`）推送到 GitHub，CI 在构建+测试通过后自动
  `npm pack` 出 `vision-<version>.tgz` 并创建 GitHub Release 上传该产物，
  可直接 `dsh plugin add ./vision-0.1.9.tgz` 安装。

```bash
git tag v0.1.9 && git push origin v0.1.9
```

## 前提

- 目标机器：Windows 桌面 + 有 Python 3（插件靠 `child_process` 调 `python -m cvision.cli_capture`）。
- 安装依赖（依赖文件已随插件打包）：

```powershell
python -m pip install -r requirements.txt
```

> 放到插件目录下执行，或先 `cd` 到该目录。

## 安装（DSH Desktop）

> ⚠️ `dsh` 命令通常**不在系统 PATH**（它是 npx 缓存里的 CLI），要用 **`npx -y @deepseek-ai/dsh`** 调用；
> 且 `dsh plugin` 是 **pnpm 前向器**，需本机有 `pnpm`（`pnpm -v` 确认）。
> `--profile web` 表示装进 **web**（浏览器面板 / 3080）profile；DSH Desktop 原生 App 用 `--profile desktop`。

装**最新**（从 GitHub 源码，公开仓库免认证）：

```powershell
npx -y @deepseek-ai/dsh plugin --profile web add github:cczzyy-cn/C-Vision
```

或装**指定 tarball**（从 Release 下载 `vision-0.1.9.tgz` 后）：

```powershell
npx -y @deepseek-ai/dsh plugin --profile web add C:\Users\14339\Downloads\vision-0.1.9.tgz
```

或装**本地目录**（已 clone 本仓库）：

```powershell
npx -y @deepseek-ai/dsh plugin --profile web add C:\Users\14339\Desktop\git\C-Vision\C-Vision
```

装完**重启 DSH Desktop**。用 `npx -y @deepseek-ai/dsh --dump-config` 可看到多出 `# == Vision` 配置层。

> 也可 `pnpm pack` 打成 tarball 分发：`npx -y @deepseek-ai/dsh plugin --profile web add ./vision-0.1.9.tgz`（无需构建权限）。
> 若 `add` 因已存在同名 `vision` 依赖报错，先 `npx -y @deepseek-ai/dsh plugin --profile web rm vision` 再装。

## 配置（可选）

默认即可用；如需覆盖：

- `CVISION_PYTHON`：Python 可执行文件，默认 `python`
- `CVISION_DIR`：cvision 项目根（含 `cvision/` 包）。默认 = 本插件安装目录（包内捆绑版）。若不使用包内副本，可指向仓库根。

## 模型怎么用

模型选择支持图片的 `deepseek-v4-flash-vision-exp` 后：

- "列一下可见窗口" → `list_windows()`（先找到目标窗口）→ "用 see 看 VS Code" → `see(window="Visual Studio Code")`。
- 直接说 "用 see 看一下屏幕" → `see()`。
- 只看窗口内一小块 → `see(window="X", region="x,y,w,h")`；需要渲染慢的页面 → `see(..., delay=800)`。
- 只要读文字 → `ocr(window="X")`（识别屏幕/窗口中的文本并返回，省去整图 token）。

> 请遵守下面的「给 AI 智能体的使用提示」——**默认不要 `maximize`，也不要用它去切换/激活前台窗口**。

## 给 AI 智能体的使用提示（重要）

- **默认不要传 `maximize=true`**：Windows Graphics Capture 抓的是窗口**自身的合成内容**，
  跟窗口是否在前台、是否被其它窗口遮挡**无关**。因此**不需要**把窗口切到前台，也**不需要**最大化。
- **不要为了截图去激活/切换前台窗口**：WGC 路径**不抢焦点、不切走你正在用的窗口**，全程无打扰。
- **什么情况才用 `maximize=true`**：仅当窗口已**最小化**（内容很小/看不清）、或**太小**、
  或**被其它窗口完全挡住且内容读不出来**时才用。插件抓完会**自动还原**窗口原状态。
- **推荐流程**：先 `list_windows()` 看有哪些窗口 → 直接 `see(handle=<句柄>)` 抓目标窗口
  （或 `see(window="<窗口标题>")`；**标题会变时（如文档名带时间戳）优先用 `handle`**）；
  需要整屏用 `see()`。

## 电脑使用（computer-use）推荐流程

把"看 → 操作 → 看"写成可复用的循环（配合上面的鼠标/键盘工具）：

1. **观察**：`list_windows()` 找到目标窗口；或 `see(window="<标题>")` 看清内容。
2. **定位**：从截图读出目标的**屏幕绝对坐标 (x, y)**。
3. **操作**：`focus_window`（需要时）→ `click(x,y)` / `double_click` / `type_text` / `press_key` / `scroll`。
4. **确认**：再 `see` 看结果；不对就回到 2/3 重试，直到目标达成（循环）。

示例（浏览器打开一个页面并看内容）：

```
focus_window("Google Chrome") → press_key("ctrl+l") → type_text("https://…") → press_key("enter") → see()  # 再看结果
```

> ⚠️ 操作会**真实移动/点击/输入**到你的鼠标键盘；务必先 `see` 确认坐标再操作，避免误触。

## OCR 文本识别

`ocr` 工具对截屏/窗口做文字识别：
- **Windows**：用 `Windows.Media.Ocr`（`winsdk`，系统语言包，免额外二进制）；
- 回退：装 `pytesseract` + Tesseract 后用其识别（跨平台）。

## 多平台支持

> ⚠️ **只有 Windows 这条链路经过实测。** macOS 为 Phase 1（代码已写，**未在真机验证**，
> 且需「屏幕录制」授权）；Linux 为 Phase 2 占位（调用即 `NotImplementedError`）。
> 在 macOS/Linux 上反馈问题时请附平台、Python 版本与完整报错。

- **Windows**：完整支持（WGC/PrintWindow/桌面区域回退），最稳。
- **macOS**：Phase 1 已支持（`Quartz/CGWindowList` 枚举 + `screencapture -l` 抓窗口，与前台无关）；需在「系统设置 → 隐私与安全 → 屏幕录制」授权，否则标题为空/只能抓到壁纸。
- **Linux**：Phase 2 占位（调用 `capture/linux.py` 会 `NotImplementedError`）。

## 目录结构

```
vision/                      # 仓库根 = 插件本体
  src/index.ts          # TypeScript 源（宿主半边：工具 + /cvision/model-capability 能力路由）
  src/client.js         # 客户端半边源（经典脚本：浏览器输入框里的截图按钮 + 门控）
  lib/index.js          # tsc 编译产物（DSH 实际加载；main/exports 指向它）
  lib/client.js         # 客户端产物（scripts/copy-client.mjs 逐字节拷贝，不能过 tsc）
  scripts/
    copy-client.mjs     #   把 src/client.js 拷到 lib/（避免 tsc 追加 export {}）
  tsconfig.json         # TS 配置（npm run build -> tsc）
  package.json          # 声明 dsh.bundle + dsh.client，files 含 lib/cvision/requirements.txt
  cordis.patch.yml      # bundle 的配置层，按包名引用
  requirements.txt      # Python 依赖（随包分发；Windows 含 pywin32/winsdk，macOS 含 pyobjc-Quartz）
  cvision/              # 捆绑的 Python 版 cvision（截屏 / OCR / 用户级输入；已裁剪为插件所需）
    __init__.py         #   包标记
    capturer.py         #   兼容层：转发到平台捕获后端（cvision.capture）
    capture/            #   平台捕获后端（门面，按 sys.platform 选）
      __init__.py       #     选后端并暴露 list_windows/capture_window/capture_screen
      base.py           #     平台无关 Window + CaptureBackend 协议
      windows.py        #     Windows 后端（WGC > PrintWindow > 读合成桌面区域 回退）
      macos.py          #     macOS 后端（Quartz 枚举 + screencapture -l 抓窗口）
      linux.py          #     Linux 后端（Phase 2，暂为占位）
    detect.py           #   纯逻辑判定（GPU 类/空白帧），不依赖 win32，可跨平台单测
    encoding.py         #   PIL -> base64 data URL；crop_region；fit_for_attachment(附件缩图)
    ocr.py              #   OCR（Windows.Media.Ocr 优先 / pytesseract 回退）
    input.py            #   用户级输入（pyautogui：点击/移动/滚动/输入/快捷键/聚焦）
    snip.py             #   系统级区域截图（人工通道）：拉起系统截图 UI 并取回框选结果
    clipboard.py        #   剪贴板图片读取 + 「是否变了」判定（Windows/macOS；Linux Phase 2）
    cli_capture.py      #   跨语言 CLI：python -m cvision.cli_capture [--list] [--region] [--delay]
    cli_ocr.py          #   OCR CLI：python -m cvision.cli_ocr [--window] [--region]
    cli_input.py        #   输入 CLI：python -m cvision.cli_input --click/--type/--keys/--focus ...
    cli_snip.py         #   系统截图 CLI：python -m cvision.cli_snip [--timeout 60]（JSON 结果）
    cli_clipboard.py    #   剪贴板 CLI：--state（廉价状态）/ --image（取图），都是 JSON 契约
  tests/
    test_detect.py      #   detect 模块单测（PIL only）
    test_encoding.py    #   encoding 模块单测（dataURL/crop/fit，PIL only）
    test_input.py       #   置前语义单测（假 win32：最大化绝不被降级）+ 能力清单按平台
    test_snip.py        #   系统截图 CLI 的 JSON 契约
    test_clipboard.py   #   剪贴板模块与 CLI 契约（平台分支 / empty / unsupported / error）
    vision.client.test.mjs # 客户端半边单测（门控/系统截图与回退/剪贴板监视与长按/失败可见，node --test）
    vision.host.test.mjs   # 宿主四条路由单测（能力判定 + 系统截图 + 剪贴板状态/取图，node --test）
  README.md
```

> 注：MCP server 相关的 `config.py`/`deepseek.py`/`server.py` 已从捆绑包移除（插件截屏无需它们，也免去了 `DEEPSEEK_API_KEY` 依赖）。

## 说明与限制

- 跨语言：插件用 `child_process` 调 `python -m cvision.cli_capture`，需目标机器桌面 + Python（Windows 用 pywin32，macOS 用 pyobjc-Quartz）。
- 截图能力：`capture_window` 依次尝试：**Windows Graphics Capture**（真实合成内容，抓 GPU/Chromium/被遮挡窗口最准，需 `winsdk`）→ `PrintWindow`（普通 GDI 窗口）→ 读合成桌面区域（兜底）。见 `cvision/capture/windows.py`。未装 `winsdk` 时自动跳过 WGC。
- 附件限制：Harness attachment 单图源 ≤20MiB、单边 ≤8192px、每条消息 ≤20 张；截图输出前会自动缩放到限制内（`encoding.fit_for_attachment`），超大屏也不会被拒。
- 省 token：`see`/`ocr` 支持 `region="x,y,w,h"` 只处理一块；`ocr` 直接返回文本；超大图自动降采样。
- 截图尽量不打扰：**WGC 抓取不切前台、不抢焦点、默认不最大化**；仅当 WGC 失效回退到"读合成桌面区域"时才可能置前，且抓完立即还原窗口状态。
- WGC 设备复用：单进程内缓存 Direct3D 设备，多次抓屏更快（CLI 每次独立进程用不到；MCP/循环采集受益）。
- 输入/操作类工具（`click`/`type_text` 等）依赖 `pyautogui`，会**真实操作你的鼠标键盘**；调用前请先 `see` 确认屏幕坐标。
- 工具本体（`see`/`ocr`/`list_windows` 及输入工具）已在 DSH 会话中直接调用过；**macOS/Linux 后端**为编写实现，需在对应平台 + 权限（屏幕录制等）下验证。

---

## DSH STORE 上架契约

> 下面每一条都由 `npm run check:dsh`（`scripts/check-dsh-contract.mjs`）机械校验，CI 每次都会跑；
> 对应 DSH 文档 `docs/user/develop/basic/publish.zh.md`（组合包 manifest）与
> `docs/subsystems/client-modules.zh.md`（客户端半边）。

### 依赖
- **Node 运行时**：无 npm 运行时依赖（`dependencies` 为空）。
- **内置组件**：包内捆绑 **Python 版 cvision**（`cvision/**/*.py`）+ 依赖清单 `requirements.txt`；
  运行时跨语言调用该 Python 子进程做截屏/OCR/输入，`CVISION_DIR` 默认指向包内。这是本插件唯一的
  独立供应链面，由 DSH STORE 供应链复查把关。
- **Peer**：`@deepseek-ai/dsh-tools`、`@deepseek-ai/cordis`——由宿主提供，且两者本来就是本仓库的
  devDependency（因此 `npm ci` 不需要额外下载）。`lib/index.js` 运行时**只** import 前者
  （`cordis` / `dsh-attachment` / `node:http` 都是 `import type`，编译后不残留）；`ctx.tools`
  / `ctx.attachments` 由宿主注入。
- **宿主入站路由**：四条，都通过 `ctx.inject(['webServer', ...])` 可选挂载，组合里没有 web 服务器时
  整段跳过。
  - `GET /cvision/model-capability`：**只读**、同源、无副作用、不回传任何凭据，供浏览器半边判断当前
    模型是否收图。
  - `POST /cvision/snip`：**仅 POST、仅同源**（`Origin` host 必须等于 `Host`，否则 403），拉起系统
    截图 UI 并把用户框选的那张图回传（200 图片字节 / 204 用户取消 / 501 平台不支持）。抓屏动作由用户
    在系统 UI 里完成，本路由只读「调用之后新出现」的剪贴板图片，因此不构成静默抓屏能力；客户端断开
    （关页/取消）会中止等待中的 Python 子进程。
  - `GET /cvision/clipboard`：**只读**、廉价（只查剪贴板格式 + token，不解码图片），供页面每秒轮询
    「剪贴板里有没有图片」。不回传图片内容本身。
  - `POST /cvision/clipboard/image`：**仅 POST、仅同源**，返回剪贴板里的图片（200 图片字节 / 204 没有
    图片 / 501 平台不支持）。**注意**：它让页面里的脚本（含其它客户端插件）也能读到剪贴板图片——这是
    「按钮要看见其它软件的截图」的固有代价，故限制为同源 + 仅 POST + 只在用户长按时调用。

### 权限说明（真实高权限）
- 通过**跨语言 spawn 包内 Python cvision** 子进程（`child_process`/进程管理）。
- **用户级操作**：屏幕截图、OCR、鼠标点击/移动/滚动、键盘输入/快捷键、窗口聚焦——属于**设备级输入/捕获权限**。
- 把截图写入 Harness 附件服务 `ctx.attachments.saveImage`（由宿主代为落盘），或返回文本。
- 这些是插件正常工作所需的**真实高权限**，DSH STORE 会将其作为 **user-reviewed/guarded** 对待。

### 外部服务
- **无出站网络**：所有捕获/OCR/输入均在本地（Windows 完整；macOS Phase 1 / Linux Phase 2）。

### 失败边界
- 包内 Python `cvision` 缺失或 `requirements.txt` 依赖未安装 → `see`/`ocr`/`click` 等工具报错或禁用。
- 系统级屏幕捕获/权限被拒、被遮挡窗口、无窗口 → 对应工具返回失败（不影响宿主主流程）。
- 跨平台支持不完整（macOS/Linux 为 Phase 1/2），在未支持平台上报错的边界由各工具显式给出。
- 宿主能力路由不可达（如 Electron `file://` 组合、`webServer` 未挂载）→ 浏览器半边退回名字启发式，
  截图按钮可能少显示或误显示；工具本身不受影响。

### 一次性 Profile 安装-启动-卸载证据
```bash
dsh plugin --profile tmp add github:cczzyy-cn/c-vision   # 作为 bundle 自动挂载
dsh profile start tmp &      # 加载 `vision` bundle，注册 see/ocr/click 等工具
dsh plugin --profile tmp rm vision
dsh profile stop tmp         # 干净退出，无残留
```
