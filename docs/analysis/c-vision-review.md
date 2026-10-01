# C-Vision 全景分析报告

> **对象**：`c-vision`（npm 包名 `vision`）v0.2.32 —— DSH（DeepSeek Harness）的视觉 / computer-use 插件
> **方法**：静态阅读为主（逐行核对代码路径），辅以本机实跑门禁与测试（见 §6）
> **范围声明**：本次**只做分析，不改动任何运行时代码**。所有结论都带证据位置；凡未经实跑的判断一律标注为「静态推断」。
> **日期**：2026-02（以仓库当前 HEAD 状态为准）

---

## 0. 结论速览

**一句话**：这是一个工程纪律**远高于同规模开源插件**的项目——文档、门禁、实测记录三者咬合得很紧；但它仍有 **1 个高风险行为缺陷**（会把键盘输入静默送进**错误的窗口**）和 **1 个自我拆台的诊断缺口**（号称「零依赖」的体检探针其实依赖 PIL/pywin32）。

| 判断项 | 结论 |
| --- | --- |
| 架构选型 | ✅ 合理。跨语言（Node 宿主 + Python 采集）是**被需求逼出来的**，不是偷懒；边界清晰、契约显式 |
| 代码质量 | ✅ 高。失败不静默、状态变更可逆、并发边界按「资源」而非「工具」划分 |
| 文档可信度 | 🟡 高但**非完全可信**。README/CHANGELOG 的绝大多数声称都能在代码里对上，但存在若干处「文档说了、代码没做」 |
| 测试策略 | 🟡 覆盖面宽（292 py + 84 js），但**契约面有结构性缺口**：`cli_capture` 参数层零专测 |
| 门禁有效性 | ✅ 真门禁（会 exit 1），且有反向验证记录；但覆盖不到「工具 description 措辞」这类文本语义 |
| 主要风险 | 🔴 P1 ×2 / 🟠 P2 ×2 / 🟡 P3 ×6（详见 §4；其中 P1-2、P2-1、P3-6 已实跑复现） |

**如果只做三件事，按此顺序**：

1. **修 `operationTarget` 旧值污染**（P1-1）——它会让 `type_text`/`press_key` **静默**打到上一个窗口，与项目「宁可报错，绝不静默点偏」的原则正面冲突。
2. **让体检探针真的零依赖**（P1-2）——否则「唯一还能用的排错手段」在需要它的那一刻也挂掉。
3. **修 `see` 的 `format` 说明**（P2-1，一行改动）——工具说明是模型唯一会读的东西，它现在仍承诺 GIF。

---

## 1. 项目是什么

| 维度 | 事实 | 证据 |
| --- | --- | --- |
| 定位 | 给 DSH agent「看屏幕 + 操作电脑」的闭环能力（see → click → see） | `README.md:1-10` |
| 分发形态 | **DSH 组合包（bundle）**，`dsh plugin add` 安装；预构建 `lib/` 随包提交（git 安装不跑构建） | `package.json:33-48`、`scripts/check-dsh-contract.mjs:66-68` |
| 双面结构 | **宿主半边**（`src/index.ts`，1973 行，16 个工具 + 4 条 `/cvision/*` 路由）+ **浏览器半边**（`src/client.js`，963 行，输入框截图按钮） | 工具清单见 `src/index.ts:1256-1972` |
| 跨语言 | `child_process` spawn 包内捆绑的 Python `cvision/`（28 个模块） | `src/index.ts:19`、`requirements.txt` |
| 运行时依赖 | Node 侧 **0 个** runtime dependency（宿主提供 peer）；Python 侧双向锁定区间 | `package.json:49-59`、`requirements.txt:18-38` |
| 平台 | Windows ✅ 实测 / macOS ⚠️ 未真机验证 / Linux ❌ 占位 | `cvision/status.py:33-45` |
| 测试 | JS 84 条 + Python 292 条（由门禁自动核对） | `scripts/check-docs.mjs:137-145` |
| 门禁 | `check:dsh` 30 项 / `check:docs` 14 项 / `check:deps` 9 项 + CI 三 job（含 windows 冒烟） | `.github/workflows/ci.yml` |

**Python 侧模块分组**（`cvision/`，按职责）：

- **捕获**：`capture/{__init__,base,windows,wgc,macos,linux}.py`、`capturer.py`（兼容层 + 组合入口）
- **理解**：`ocr.py`、`ui_elements.py`（词框 → 可点击控件）、`coordinates.py`（图片像素 → 屏幕绝对坐标）、`diff.py`（帧差 + 变化聚类）
- **动作**：`input.py`（pyautogui + 原生置前）、`input_lock.py`（跨进程互斥）、`snip.py`、`clipboard.py`
- **进程边界**：`cli_capture / cli_ocr / cli_input / cli_snip / cli_clipboard / cli_server.py`
- **可观测**：`status.py`、`detect.py`、`diagnose.py`、`encoding.py`、`screen.py`

---

## 2. 架构全景

```text
┌─ 浏览器（DSH Web GUI）─────────────────────────────────────────────┐
│  src/client.js  「截图」按钮                                        │
│   ├─ 短按 → POST /cvision/snip        （系统框选截图，走剪贴板回传）  │
│   ├─ 长按 ≥900ms → POST /cvision/clipboard/image（插入剪贴板图片）   │
│   ├─ 每秒 GET /cvision/clipboard      （新图提示 + 宿主忙提示）      │
│   └─ 可见性 ← GET /cvision/model-capability（问宿主真实 inputModalities）│
└───────────────▲───────────────────────────────┬───────────────────┘
                │ 同源 HTTP（snip/clipboard 仅 POST + Origin 校验）
┌───────────────┴───────────────────────────────▼───────────────────┐
│  DSH 宿主（Node）  src/index.ts                                    │
│   16 个工具（see/ocr/click/...） ──┐                               │
│   4 条只读或同源路由 ─────────────┘                                │
│   进程管理：CvisionServer（常驻，45s 超时）/ 一次性 CLI 回退          │
│   进程级状态：operationTarget{handle, frame}、inputBusy、snip 归属   │
│   附件：ctx.attachments.saveImage → image ContentBlock              │
└───────────────┬───────────────────────────────────────────────────┘
                │ child_process + PYTHONUTF8/PYTHONPATH，cwd = %TEMP%
┌───────────────▼───────────────────────────────────────────────────┐
│  Python cvision（捆绑）                                             │
│   常驻 cli_server：stdin/stdout 一行一请求（复用 D3D 设备/编码器）    │
│   回退 CLI：python -m cvision.cli_*（每次冷启动一个进程）             │
│   真实动作：WGC/PrintWindow/桌面区域、Windows.Media.Ocr、pyautogui    │
│   跨进程锁：命名互斥体（Win）/ flock（POSIX）                        │
└───────────────────────────────────────────────────────────────────┘
```

### 2.1 两条调用通道（这是全项目最关键的一条设计线）

| 通道 | 形态 | 何时走 | 收益 / 代价 |
| --- | --- | --- | --- |
| **常驻 server** | `cli_server` 进程，JSON-line 协议 | 默认优先 | 复用 D3D 设备与编码器，省掉每次调用约 170ms 级的冷启动；代价是**长驻进程会缓存旧代码**（改 Python 必须重启宿主） |
| **一次性 CLI** | `python -m cvision.cli_*` | server 不可用 / 超时 / 崩溃 | 无状态、天然干净；代价是慢 + 形状略不同（非 text 截屏输出**裸 data URL** 而非 JSON） |

> 两条通道**形状必须一致**，因为宿主对它们的解析是同一份代码。项目为此专门把 `capture_with_text` / `image_screen_frame` 抽成共用实现（`cvision/capturer.py:80-120`、`cvision/capturer.py:54-77`），并在 `cli_server.py:75-119` 与 `cli_capture.py:29-59` 保持同字段——这是**正确的**防漂移做法。

### 2.2 一次 `see(text=true)` 的完整数据流

```text
① see(text=true)
② host：拼 op:{capture,text:true,format,...} → 常驻 server
③ python：capture_window（WGC → PrintWindow → 桌面区域）
            └─ 只有「会改前台/窗口状态」的分支才取跨进程锁（ForegroundGuard，2s）
④ python：image_screen_frame  → image_screen_box（这张图覆盖的屏幕矩形）
⑤ python：ocr（先放大 3x 识别，坐标还原原图）
⑥ python：ui_elements 合并同行相邻词 → 控件 + screen_center（屏幕绝对坐标）
⑦ host：parseDataUrl → ctx.attachments.saveImage → 返回 {ref, elements, element_total}
⑧ host：记住 operationTarget.handle / frame（供后续点击置前 + 坐标校验 + 按比例点击）
```

**关键判断**：坐标换算**下沉到插件**（第 ⑤⑥ 步）而不是交给模型，是本项目最重要的产品级设计决策——模型读不准图片像素（它收到的预览被 provider 按 token 规则缩过），但读得准比例。

---

## 3. 关键设计决策（决策 → 理由 → 证据）

| # | 决策 | 为什么（项目自己给出的理由） | 证据 |
| --- | --- | --- | --- |
| 1 | **所有 Python 子进程 cwd = 系统临时目录**，靠 `PYTHONPATH` 找包 | Windows 下「进程的当前目录」是安装目录上的一个句柄 → pnpm 无法 rename 替换 → `plugin add` 升级报 `ERR_PNPM_EPERM` | `src/index.ts:91-106` |
| 2 | **stdout 恒为「一行 JSON」；非零退出仍要读 stdout** | 退出码只表达分类（2=cancelled / 3=unsupported），真正契约在 stdout | `src/index.ts:723-746`、`README.md:390-405` |
| 3 | **工具返回值与 `output.schema` 严格对齐**（多余键 = 整次调用失败） | DSH 会校验返回值且 schema 是 `additionalProperties:false`；v0.2.24 曾因此**每次都失败** | `src/index.ts:133-202`、`scripts` 无、`CHANGELOG.md` v0.2.24 |
| 4 | **坐标换算固定成代码**：`see(text=true).screen_center` + `click_at(比例)` | 图片像素 → 屏幕坐标要叠四层偏移（裁剪/窗口/多屏/DPI），模型自己算必错 | `cvision/coordinates.py`、`cvision/ui_elements.py`、`README.md:86-101` |
| 5 | **输入互斥的边界 = 一个资源**（鼠标键盘 + 前台窗口 + Z 序），而不是「按工具」 | 三者耦合：一次点击改前台，改前台又让另一次纯键盘输入打错窗口 | `cvision/input_lock.py:23-40` |
| 6 | **只读抓图不取锁**；只有真要动前台时才惰性取（2s 短等待） | 否则「另一个会话在打字」时连截图都做不了；等待太久也不值得 | `cvision/input_lock.py:485-533`、`cvision/capture/windows.py:468-486` |
| 7 | **释放顺序：先还原窗口状态、再放锁** | 否则留下「已改前台却没人管」的空档 | `cvision/capture/windows.py:517-522` |
| 8 | **只在我们真改过窗口状态时才还原**（`should_restore_placement`） | 无条件写回 placement 会**撤销用户的吸附/分屏**（实测踩过） | `cvision/capture/windows.py:453-460`、`CHANGELOG.md` v0.2.23 |
| 9 | **前置校验不通过 = 动作根本不执行** | 「宁可报错，绝不静默点到压在上面的窗口上」 | `cvision/cli_input.py:241-246` |
| 10 | **文档即门禁**：版本号/阈值/目录/工具/路由/测试数/锚点全部断言化 | 每一条都对应一次真实漂移（550ms→900ms、文件漏文档、版本号不一致） | `scripts/check-docs.mjs:1-13` |
| 11 | **依赖双向锁定**，且预发布包上界必须写「下一个预发布号」 | 上界写 `<1.0.0` 会把唯一可用的 `1.0.0b10` 也排除 → Windows 上 pip 装不上（真实回归） | `requirements.txt:3-12`、`scripts/check-deps.mjs` |
| 12 | **windows-latest 冒烟 job** | ubuntu/macOS 装不了 pywin32/winsdk，Windows 关键路径在 CI 上从不执行 | `.github/workflows/ci.yml:71-96` |

**评价**：这 12 条里没有一条是「看起来专业」的装饰——每一条都写出了**代价**（会真的点一次标题栏、会占剪贴板、会抢前台、会降采样）与**拒绝的替代方案**。这是本仓库最值得保留的资产。

---

## 4. 风险与问题清单

> 分级：🔴 P1 = 会造成错误行为或让关键自救手段失效；🟠 P2 = 可见的功能/一致性缺陷；🟡 P3 = 健壮性、可维护性、文档漂移。
> 「置信度」栏：**已核实** = 代码路径可逐行复现（含行号）；**待实测** = 需要真机交互才能确证后果。
>
> ⚠️ **本节是修复前的快照。P1-1 / P1-2 / P2-1 已在 v0.2.33 修复**（改动与验证见 §7）；P2-2、P3-1～P3-6 保持原判。

### 🔴 P1-1　`operationTarget` 保留旧值 → 键盘输入会被**静默**送进上一个窗口

| 项 | 内容 |
| --- | --- |
| 现象 | `see(window="B")`（或 `see()` 整屏）之后调 `type_text`/`press_key`，输入可能落到**之前** `see`/`focus_window` 过的窗口 A，并且**报告成功** |
| 触发条件 | ① 先有一次会写入 `operationTarget.handle` 的调用（`see(handle=…)` / `see(text=true)` / `focus_window`）；② 再来一次**不带 text、也不带 handle** 的抓取（`see()` 或 `see(window=…)`）；③ 随后 `type_text` / `press_key` |
| 根因 | 抓取路径拿不到新句柄时**不更新**，于是旧值一直留着：<br>· `see()` 只在 `target != null` 时写 `operationTarget.handle`（`src/index.ts:1356-1357`）<br>· 常驻 server 的**非 text** 抓取响应**不带 handle**（`cvision/cli_server.py:108-119`）<br>· CLI 回退的非 text 路径返回**裸 data URL**，句柄与矩形都拿不到（`src/index.ts:1128-1135`）<br>· `type_text`/`press_key` 调用 `runInputAction` 时**不传 `--at`**（`src/index.ts:1875`、`1889`）<br>· 于是 CLI 判定 `ok = focused`，而 `ensure_front` 确实能激活目标 → 校验「通过」（`cvision/cli_input.py:241`、`cvision/input.py:294-296`）<br>· 动作照样执行 → 输入进了 A |
| 影响 | **静默的错误副作用**（最难归因的一类）：焦点被抢、文本键入错误的程序；恰好违反项目自己写下的「宁可报错，绝不静默点偏」。附带两条：<br>· 点击类（带 `--at`）会被拦下并报错，但**报错指向 A**，误导排查；且 `--unblock` 会**真的移动鼠标并点击 A 的标题栏**（`cvision/input.py:302-314`）<br>· CLI 回退路径连 `frame` 也不更新 → `click_at` 会用**上一张图**的屏幕矩形换算坐标（`src/index.ts:1775`） |
| 建议 | 让「每次抓取都是权威」：抓取结果里缺 `handle`/`frame` 时**显式清空**对应字段（而不是保留旧值）；更彻底的做法是让 server 的非 text 响应也带 `handle`（它本来就能解析）、CLI 非 text 也返回 JSON。补一条 JS 回归：`see(window=…)` 无 text 后 `operationTarget.handle` 必须是 `null`（沿用现有 `operationTarget` 注入式测试套路，`tests/vision.host.test.mjs:694-706`） |
| 置信度 | **已核实**（全链路行号可复现）；后果的严重性待真机确认 |

### 🔴 P1-2　「零依赖体检探针」并非零依赖 → 缺依赖时 `cvision_status()` 也一起挂

| 项 | 内容 |
| --- | --- |
| 现象 | 机器上没装 Pillow（或 Windows 上没装 pywin32）时，`python -m cvision.cli_capture --status` 会在 **import 期**就崩，得不到任何结构化结论 |
| 根因 | 探针链条全程是**模块级 import**：<br>`cvision/status.py:14 from cvision import capturer` → `cvision/capturer.py:15 from cvision import coordinates, encoding, ocr, screen, ui_elements` →（`cvision/encoding.py:8 from PIL import Image`、`cvision/ocr.py:26 from PIL import Image`、`cvision/screen.py:22 from PIL import Image`）→ `cvision/capture/__init__.py:18 from . import windows` → `cvision/capture/windows.py:22 import win32gui`、`:24 from PIL import Image`<br>而 `capture/__init__.py` **没有** CHANGELOG 所称的 try/except 兜底（该文件 38 行，只有裸 import） |
| 影响 | ① 本项目唯一「环境不完整时还能用」的自救工具 **`cvision_status()`** 在缺依赖时也失败（`src/index.ts:1242-1253` 两条路径都到不了结构化输出）；<br>② README 两处承诺不成立：`README.md:50-52`「本会话第一次调用…直接把『缺什么 + 带绝对路径的 pip 命令』告诉你，而不是抛一句裸的 Python 报错」、`README.md:491`「`cvision_status()` 会列出缺哪个模块」；<br>③ `CHANGELOG.md` v0.2.18 的「该探针刻意的『无依赖』（status.py 不 import PIL），所以一个依赖都没装的新环境照样能跑出结论」**与现状不符**；<br>④ 用户仍能看到带 `PIP_HINT` 的兜底文案（`src/index.ts:237-246`），所以不至于完全卡死——但丢失了「缺哪个模块」这一关键信息 |
| 建议 | 把 `status.py` 对捕获层的依赖改为**惰性/容错**（在函数内 import，或把依赖探测与捕获层彻底解耦）；并给 `capture/__init__.py` 的后端选择补上 try/except（把 `backend_implemented=False` + `reason` 作为结构化结果返回）。补一条测试：用 import blocker（或 `sys.modules` 注入）断言「PIL / win32gui 不可用时 `--status` 仍输出合法 JSON」 |
| 置信度 | **已核实 + 已复现**（2026-02 本机故障注入：CLI 与常驻 server 两条通道都失败，原始输出见 §6.2 第 6/7/8 行） |

### 🟠 P2-1　`see` 的工具说明仍承诺支持 GIF，而实现明确拒绝

| 项 | 内容 |
| --- | --- |
| 证据 | 工具 description：`src/index.ts:1288`「PNG/JPEG/WEBP/**GIF**，默认 PNG」；而 `MEDIA_TYPES` 只有三种（`src/index.ts:44`），`parseDataUrl` 遇到 `image/gif` 直接抛「不支持的图片类型」（`src/index.ts:110-113`）；文件头注释自己也写明「刻意**不含 GIF**……不如在这里拒掉」（`src/index.ts:36-43`）；`CHANGELOG.md` v0.2.26 记录该版本已移除 GIF |
| 影响 | 模型**只读工具说明**（这是 v0.2.21 那条教训的原话）→ 它会去传 `format='GIF'` → 截图成功但**回传时抛错**，整次 `see` 失败，且错误信息与说明自相矛盾 |
| 建议 | 一行修掉说明（并从 `MEDIA_TYPES` 单一事实源生成这段文案）；`check:docs` 加一条断言：「工具 description 中出现的格式名必须 ⊆ `MEDIA_TYPES`」——这一类**文本语义漂移**正是现有门禁的盲区（项目自己在 `CHANGELOG.md` v0.2.21 记过这个盲区） |
| 置信度 | **已核实**（两处代码 + 注释 + CHANGELOG 三方对照） |

### 🟠 P2-2　客户端能力门控：在途态与注释不符，且失败结果永久缓存

| 项 | 内容 |
| --- | --- |
| 证据 | `requestVerdict` 在发起 fetch **之前**就把该 key 写成 `'error'`（`src/client.js:74`），而 `'error'` 的语义是「宿主答不上来 → 退回名字启发式」（`src/client.js:95-98`、`:743`）。注释却声称「在途时宁可先不显示，也不要先按名字猜测闪出一个可能错误的按钮」——实际在途期间**就是**在按名字猜 |
| 影响 | ① 名字带 `vision|visual` 但实际不收图的模型：按钮**先闪出来**再消失；② 宿主不可达/瞬时失败时，`'error'` 会被永久缓存（`requestVerdict` 靠 `verdicts.has(key)` 早退），该模型在**本次页面生命周期内**再也不会重查 → 一次瞬时故障 = 永久降级为名字启发式 |
| 建议 | 在途态用独立的 `'pending'`（1 行），并把 `'error'` 改成「可重试」（TTL 或失败计数 + 指数退避）。可参考本项目自己的剪贴板轮询策略：连续失败 3 次即停止且只告警一次（`README.md:157-162`） |
| 置信度 | **已核实**（三处代码 + 注释对照） |

### 🟡 P3-1　`snipInFlight` 未用 try/finally 复位

- 证据：`src/index.ts:947-949`——`snipInFlight = true` → `await snipExecutor.run(...)` → `snipInFlight = false`。默认实现内部已吞掉异常，但 `snipExecutor` 是导出的注入点（单测/扩展会替换它），而路由 handler 是 async 且无 catch。
- 影响：一旦抛出，`snipInFlight` 永久为真 → 之后**任何**剪贴板图片都会被判定为「我们自己刚产出的」（`src/index.ts:995-1000`），按钮再也不亮、长按永远不触发，直到宿主重启。
- 建议：`try { ... } finally { snipInFlight = false }`。

### 🟡 P3-2　体检结论的**负缓存**不可失效

- 证据：`src/index.ts:215-217`（`runtimeProbed` 进程级一次性）+ `:284-300`（`runtimeProbed = true` 在探测**之前**设置）。
- 影响：用户在本会话内 `pip install` 修好依赖后，`see`/`ocr` 等仍持续报同一个错，而 `cvision_status()`（刻意不过体检门）已显示 `ok:true`——两个工具口径互相矛盾，用户会以为插件坏了。README 的确要求「装完重启 DSH」，但这条隐含依赖没有被工具文案说明。
- 建议：`cvision_status` 成功时清掉负缓存，或给缓存加 TTL / 允许「重试一次」。

### 🟡 P3-3　`GET /cvision/model-capability` 未做同源校验

- 证据：该路由只解析 query 就应答（`src/index.ts:908-917`），而同文件的 `snip` 与 `clipboard/image` 都做了 POST + `isSameOrigin` 校验（`:933-940`、`:1014-1021`）。`isSameOrigin` 在缺 `Origin` 时返回 false（`:712-721`）。
- 影响：同机任意网页/其他客户端插件可读取「当前模型是否收图」。信息量小、本身不触发高权限动作，但破坏了「高权限路由一律设防」的一致性。
- 建议：加同源校验（或明确写进 README 说明为何不需要）。

### 🟡 P3-4　版本与文案漂移：`cvision/__init__.py`

- 证据：`cvision/__init__.py:1-3`——`__version__ = "0.1.0"`，而包版本已是 `0.2.32`（`package.json:3`）；同文件的 docstring 仍写「MCP Server」，而 MCP 相关文件早已移除（`README.md:479-480`）。
- 影响：小，但属于「版本号三处一致」这条自身约定的**第四处**未被门禁覆盖（`check:docs` 只比对 `package.json` ↔ README ↔ CHANGELOG）。
- 建议：改为从包元数据派生或直接对齐；docstring 顺手改掉。

### 🟡 P3-5　测试覆盖缺口：`cli_capture` 参数层零专测

- 证据：全仓库测试中对 `cli_capture` 的引用数为 **0**（`tests/**` 中 `cli_capture` 无匹配）；已有专测的是 `tests/test_cli_ocr.py`、`tests/test_cli_input.py`、`tests/test_cli_server.py`、`tests/test_snip.py`、`tests/test_clipboard.py`——**唯独 `cli_capture` 没有**。
- 影响：`--text` / `--wait-changed` / `--wait-stable` / `--maximize` / `--region` / `--delay` / `--format` 的**参数 → 行为**映射，以及「非 text 输出裸 data URL、text 输出 JSON」这条形状契约，只有 server 侧被覆盖（`tests/test_cli_server.py:58`）。CLI 回退路径一旦漂移，宿主解析会直接失败——而这条路径恰恰是 server 崩掉时的兜底。
- 建议：补 `tests/test_cli_capture.py`（stub 掉 `capturer`，断言 argv → kwargs 映射与 stdout 形状），与既有的三份 CLI 契约测试对齐。

### 🟡 P3-6　`diff.py` 用了将在 Pillow 14 移除的 API（实跑暴露）

- 证据：`python -m unittest` 本次实跑输出 `DeprecationWarning: Image.Image.getdata is deprecated and will be removed in Pillow 14 (2027-10-15)`，共 4 处：`cvision/diff.py:60`、`:61`、`:118`、`:119`。
- 影响：当前 `requirements.txt` 把 Pillow 锁在 `>=12.0.0,<13`（`requirements.txt:18`），所以**今天不会坏**；但 README 明确约定「每季度审查一次上界」，一旦上界推进到 14，`wait_until_changed` / `wait_until_stable` 的差异计算会直接报错——而这条路径正是两个 `wait_*` 工具的核心。
- 建议：改用 `get_flattened_data()`（或先做版本分支），并把它列入"上界推进"的前置检查清单；顺手在 CI 里把 `DeprecationWarning` 变成可见项（`-W error::DeprecationWarning` 只针对本仓库代码）。
- 置信度：**已复现**（本次实跑输出，非静态推断）

### 结构性风险（设计取舍，非缺陷；建议**明确写进文档**而不是改掉）

| 项 | 说明 | 现状 |
| --- | --- | --- |
| `src/index.ts` 单文件 1973 行 | 工具注册 + 4 条路由 + 进程管理 + 进程级状态 + 体检门混杂 | 可读性尚可（分区注释清晰），但改一处需理解全局；建议至少把「路由」与「进程管理」抽出 |
| `operationTarget` 是**进程级**全局 | 同进程多会话/子代理共用，会互相覆盖 | README 已如实写明（`README.md:533-536`） |
| 常驻 Python 进程缓存旧代码 | 改 `cvision/*.py` 后必须重启宿主才生效 | `CHANGELOG.md` v0.2.23 用大段篇幅记录了被它误导的经历，但 README 只在升级章节提到 |
| macOS/Linux 未真机验证 | `platform_support=unverified/unsupported` 已机器可读 | 诚实且优秀；但 CI 的 Python 矩阵只装 Pillow，**结构上**无法覆盖真实桌面行为 |
| 页面可读剪贴板图片 | `POST /cvision/clipboard/image` 让同源脚本也能读到剪贴板图片 | 已在 STORE 契约里如实披露（`README.md:564-567`） |
| 文档体量（README 604 行 + 巨量 CHANGELOG） | 维护成本高；但它是本项目最有效的质量杠杆 | 有门禁兜住「结构性漂移」，文本语义靠人 |

---

## 5. 改进建议（按 ROI 排序）

| 优先级 | 动作 | 成本 | 收益 |
| --- | --- | --- | --- |
| 1 | 修 `operationTarget` 语义：抓取结果缺 handle/frame 时**清空**而非沿用；server 非 text 也回 handle | 小（~10 行 + 1 用例） | 消除「静默送错窗口」这一最危险失败类 |
| 2 | `status.py` 去依赖化 + `capture/__init__.py` 加 try/except + blocker 测试 | 中（~40 行 + 1 用例） | 让自救工具在任何残缺环境下都能给出结论，兑现 README 承诺 |
| 3 | 修 `see` 的 `format` 说明；并加断言「description 中的格式名 ⊆ `MEDIA_TYPES`」 | 极小 | 关掉一类模型必然踩的坑，并给「文本语义漂移」补第一道门禁 |
| 4 | 客户端门控：`'pending'` 在途态 + `'error'` 可重试 | 小 | 消除按钮闪烁与永久降级 |
| 5 | `snipInFlight` 改 try/finally | 极小 | 关掉「按钮永久失灵」的极端路径 |
| 6 | 体检负缓存可失效（`cvision_status` 成功即清除 / TTL） | 小 | 「装完依赖不必重启」体验一致 |
| 7 | 补 `tests/test_cli_capture.py`（CLI 回退路径的契约） | 中 | 补上唯一没有专测的 CLI 入口 |
| 8 | 给 `model-capability` 加同源校验 | 极小 | 权限口径一致 |
| 9 | 拆 `src/index.ts`（路由 / 进程管理 / 工具） | 大 | 认知负担与改动风险下降；建议随下一个大版本做 |
| 10 | 新增 `docs/` 开发者章节：改 Python 要重启宿主、门禁盲区清单 | 小 | 减少「改了没生效」类的重复踩坑 |
| 11 | `diff.py` 弃用 API 换 `get_flattened_data`（P3-6），并纳入「依赖上界推进」的检查清单 | 极小 | 避免未来换 Pillow 上界时 `wait_*` 直接报错 |

### 明确**不建议**做的（避免后人走回头路）

- ❌ 不要为了让 `format=GIF` 成立去加多帧编码——`see` 的语义是「看当前画面」，动图没有意义，删说明才是正解。
- ❌ 不要给只读抓图（WGC/PrintWindow/整屏）加锁——会把整个桌面串行化（项目已实测并写明理由，`README.md:528`）。
- ❌ 不要在插件里复刻 provider 的图片缩放规则来「修正」坐标——规则会随版本漂移；现有「比例坐标 + 屏幕矩形」的解耦是对的（`CHANGELOG.md` v0.2.26）。
- ❌ 不要把体检门加在 `cvision_status` 上——它必须在环境残缺时仍可用（`src/index.ts:1242-1245` 的判断是对的，问题只在 Python 侧实现）。

---

## 6. 验证与证据

### 6.1 本次分析的静态证据索引（均为逐行核对）

| 主题 | 位置 |
| --- | --- |
| 进程/环境约定 | `src/index.ts:80-131` |
| schema 严格整形 | `src/index.ts:133-202` |
| 体检门与文案 | `src/index.ts:204-307`、`:1242-1253` |
| 输入忙提示 | `src/index.ts:309-363` |
| 会话身份 → `--lock-label` | `src/index.ts:330-355` |
| 常驻 server 生命周期与超时 | `src/index.ts:491-620` |
| 同源校验 | `src/index.ts:707-721` |
| 四条路由 | `src/index.ts:884-1054` |
| 组合调用与操作目标 | `src/index.ts:443-477`、`:438-441`、`:1354-1359`、`:1967-1968` |
| 形状适配（server/CLI） | `src/index.ts:1088-1147` |
| 工具清单与说明 | `src/index.ts:1256-1972` |
| 锁边界与纪律 | `cvision/input_lock.py:23-57`、`:438-533` |
| 前置校验与失败分类 | `cvision/input.py:255-330`、`cvision/cli_input.py:233-254` |
| 抓图状态可逆 + 锁顺序 | `cvision/capture/windows.py:453-524` |
| 探针依赖链 | `cvision/status.py:14`、`cvision/capturer.py:15`、`cvision/capture/__init__.py:18`、`cvision/capture/windows.py:22,24`、`cvision/encoding.py:8` |
| 门禁实现 | `scripts/check-docs.mjs`、`scripts/check-dsh-contract.mjs`、`.github/workflows/ci.yml` |

### 6.2 本机实跑结果

> 环境：Windows / `Node v24.15.0` / `Python 3.14.0` / `npm 11.12.1` / `Pillow 12.3.0`；仓库 HEAD = `b319b57`（v0.2.32），工作区除本报告外干净。

| # | 命令 | 结果 | 结论 |
| --- | --- | --- | --- |
| 1 | `npm run check:dsh` | **30/30 通过，exit 0** | 组合包/客户端契约完整；含 `lib/client.js` 与 `src/client.js` 逐字节一致、`lib/index.js` 外部 import 仅 `@deepseek-ai/dsh-tools` 且已声明为 peer |
| 2 | `npm run check:docs` | **14/14 通过，exit 0** | 版本号三处一致（0.2.32）；长按阈值 900ms 与 `src/client.js` 一致；README 覆盖 55 个源文件 / 19 个工具名 / 4 条路由；**README 声称的测试条数与实际完全一致：84 JS / 292 Python** |
| 3 | `npm run check:deps` | **9/9 通过，exit 0** | `requirements.txt` 17 条依赖全部双向锁定，平台 marker 合规，预发布上界合规 |
| 4 | `npm run test:js` | **84/84 通过，0 失败，exit 0**（9.4s） | 宿主路由、`operationTarget` 合并调用不变量、会话 id → `--lock-label`、schema 键集对齐、体检文案分支均被覆盖 |
| 5 | `python -m unittest discover -s tests` | **Ran 292 tests … OK**，exit 0（2.8s） | Python 侧全绿；含真 spawn 进程的锁争用、CLI 契约、坐标换算、差异聚类、剪贴板竞态 |
| 6 | 故障注入：伪造一个 `ImportError` 的 `PIL` 包，再跑 `python -m cvision.cli_capture --status` | **崩溃**，`exit=1`，无任何 JSON：<br>`cli_capture.py:24 → capturer.py:15 → encoding.py:8 → ImportError: simulated: Pillow is not installed` | **P1-2 复现**：所谓「无依赖体检探针」在缺 Pillow 时**根本跑不到结论** |
| 7 | 同上注入，跑常驻通道 `'{"op":"status"}' \| python -m cvision.cli_server` | `{"ok": false, "error": "simulated: Pillow is not installed"}`（exit 0） | **P1-2 复现（第二条通道）**：宿主 `server.request` 见 `ok:false` 即 reject → 回退 CLI → 也崩 → `cvision_status()` 两条路全断 |
| 8 | `python -c "import sys, cvision.status; ..."` | `PIL loaded = True`、`win32gui loaded = True` | 探针**必然**拉进 PIL 与 win32gui（依赖链确证，与 §4 P1-2 一致） |
| 9 | 对照：完整环境下 `python -m cvision.cli_capture --status` | `ok:true`、`platform_support:"supported"`、`ocr_engine:"windows-media-ocr"`、`input_lock.backend:"windows-mutex"`+`available:true`、`capture_backends.wgc.available:true`、5 项 deps 全 true | 正常路径健康；**`input_lock` 与 `wgc` 的「真实探测」口径确实生效**（不是只看包在不在） |
| 10 | `python -c "from cvision import encoding; encoding.image_to_data_url(img, format='GIF')"` | 产出 `data:image/gif;base64,R0lGODdhBA…` | **P2-1 复现**：Python 确实会产出 `image/gif`，而宿主 `MEDIA_TYPES`（`src/index.ts:44`）不含它 → `parseDataUrl` 必抛「不支持的图片类型」 |

**实跑中额外发现（本次新证据，已并入 §4 P3-6）**：

- `python -m unittest` 输出中反复出现 `DeprecationWarning: Image.Image.getdata is deprecated and will be removed in Pillow 14 (2027-10-15)`，来源 `cvision/diff.py:60,61,118,119`。
- 在 Windows 控制台直接跑 `python -m unittest discover -s tests`（README 给出的命令）时，测试里的中文输出出现乱码——插件对**自己 spawn** 的 Python 全局强制了 UTF-8（`src/index.ts:84-89`），但 README 的手工命令没带 `PYTHONUTF8=1`。属开发者体验小问题。

该表格已由本次会话实跑回填；所有失败项（若有）都会在 §4 追加条目——本次三门禁与两套测试均通过。

### 6.3 未验证 / 无法验证的部分（诚实边界）

- **macOS / Linux 行为**：代码存在但无真机，本次分析一律按「未验证」对待。
- **真实桌面交互**：`--unblock` 的标题栏点击、抢前台副作用、WGC 抓被遮挡窗口等，需要真机 + 人工观察，本次**未实测**。
- **锁竞争时序**：多进程争用/超时/持有者点名需要并发实测（项目自己的 `test_input_lock.py` 用真 spawn 覆盖了逻辑层，但未覆盖真实鼠标键盘竞争）。
- **CI 的实际运行**：本次只能核对 workflow 定义，未在 GitHub 上触发。
- **P1-1 的后果严重性**：代码路径已逐行核实（§4 中每条都有行号且可复现），但**未做活体复现**——那需要在分析对象的真实桌面上按标题 `see` 一次、再让 `type_text` 打到一个窗口里，属于会对用户桌面产生副作用的操作（移动鼠标、抢前台、真的键入字符）。本次刻意不做，改为给出建议的回归测试（不需要真桌面：把 `operationTarget.handle` 预置成某个句柄，让假 `inputRunner` 记录 argv 即可断言「新抓取后旧句柄必须被清掉」）。
- **静态 vs 实跑的界线**：P1-2 与 P2-1 已实跑复现（§6.2 第 6–10 行）；P2-2、P3-1～P3-5 为静态核实（三处代码/注释互相对照），P3-6 为实跑输出。

---

## 7. 修复记录（v0.2.33，分析之后执行）

按 §5 的建议顺序修了 **P1-1 / P1-2 / P2-1** 三项；用户说「继续」后，又把 **P2-2 与 P3-1、P3-2、P3-3、P3-4、P3-6** 一起清掉了；最后把 §7.5 记的那条相邻缺口（`wait_*` 不更新比例基准）也修了。**§4 清单至此全部关闭。** 之后用户又报告了一个**只在 DSH 桌面版成立**的缺陷，根因与修法见 §7.7（它不属于 §4 清单，是新的输入）。

### 7.1 P1-1　抓取目标不再沿用旧值

| 改动 | 位置 |
| --- | --- |
| 新增导出的纯函数 `noteCaptureTarget(handle, frame)`：每次抓取**覆盖**两项、拿不到就**清空** | `src/index.ts`（`see.execute` 只调它一处） |
| 常驻 server 的**非 text** `capture` 也回 `handle`/`title`（与 `--text` 同形状） | `cvision/cli_server.py` |
| `cli_capture` 新增 **`--json`**：非 text 截图输出 `{ok,kind,data_url,width,height,image_screen_box,handle?,title?}`；缺省仍是裸 data URL（既有契约不破） | `cvision/cli_capture.py` |
| 宿主 CLI 回退路径改用 `--json` 并解析 `handle`/`frame` | `src/index.ts`（`captureDataUrl`） |
| `click_at` 在没有基准矩形时的报错文案（删掉已过期的「CLI 回退请改用 text=true」建议） | `src/index.ts` |

**语义**：`see(handle=…)`/`see(window=…)`/`see(text=true)` 都把**这一次**的窗口记为操作目标；整屏 `see()` 清空目标（此后输入按焦点/坐标，`click_at` 明确报错而不是用旧矩形）。

### 7.2 P1-2　体检探针真的零依赖

| 改动 | 位置 |
| --- | --- |
| 删掉**没人用**的 `from cvision import capturer`（它链式拉进 PIL/win32gui，是崩溃根源） | `cvision/status.py` |
| `_backend()` 改为**按平台名推导**（不再 import 后端）；新增 `backend_import_error`；`capture_backends` 带上 `import_error` + 人话 reason；`ok` 追加「后端真的导得进来」这一条 | `cvision/status.py` |
| 捕获层改为**延迟导入** `_capture_deps()`，报错里带 `pip install` 与 `--status` 指引 | `cvision/cli_capture.py` |
| `describeRuntimeProblem` 新增分支：**有实现但导不进来** → 拦下 + pip 命令，不再错报「本平台后端未实现」 | `src/index.ts` |

### 7.3 P2-1　`see` 的 format 说明与实现对齐

- 说明由 `PNG/JPEG/WEBP/GIF` 改为 `PNG/JPEG/WEBP，默认 PNG`（`src/index.ts`）。
- `check:docs` 新增永久断言：**工具说明里不得出现 `MEDIA_TYPES` 之外的图片格式名**（允许集从产物的 `MEDIA_TYPES` 派生，解析不出来即判失败）。
- 顺手修了门禁自身的计数缺陷：JS 条数原用 `\btest(` 统计，会把 `assert.ok(!/x/.test(v))` 这类正则调用也算成测试（本次加 1 条测试却让计数 +2）；已改为行首口径，与 Python 侧一致。

### 7.4 同一批清掉的另外七项

| 级别 | 改动 | 位置 |
| --- | --- | --- |
| 🟠 P2-2 | 能力门控：在途只记在独立的 `inFlight`（不再乐观写 `'error'`）→ 在途渲染成 `'pending'`、**不按名字猜**；失败改为**有限次重试**（5s × 3 次），重试期间不改动已落地的结论（否则按钮会每 5 秒闪一次） | `src/client.js` |
| 🟡 P3-1 | `snipInFlight` 的复位放进 `try/finally`：执行器是导出注入点，抛异常时它曾会永久为真 → 之后所有剪贴板图片都被当成「我们自己产出的」 | `src/index.ts`（snip 路由） |
| 🟡 P3-2 | 体检负缓存可失效：失败结论只保 30s（`shouldProbeRuntime()` 纯函数 + 单测），且 `cvision_status()` 成功时直接清掉负缓存 | `src/index.ts` |
| 🟡 P3-3 | `GET /cvision/model-capability` 加同源校验——**只拒绝明确跨站**（带 `Origin` 且 host ≠ `Host`），刻意不复用 POST 那条「缺 `Origin` 也拒绝」的口径（浏览器不给同源 GET 带 `Origin`，照 POST 卡会把插件自己挡掉，比不设防更糟） | `src/index.ts` |
| 🟡 P3-4 | `cvision/__init__.py` 的 `__version__` 对齐 `package.json`（此前停在 0.1.0，docstring 还写着已移除的「MCP Server」）；`check:docs` 新增断言把它钉住 | `cvision/__init__.py`、`scripts/check-docs.mjs` |
| 🟡 P3-6 | 新增 `diff.pixel_rows()`：优先 `get_flattened_data()`（实测 12.3 上取值与旧 API **逐字一致**），取不到才回退 `getdata()`——不能直接换名字，因为下界是 Pillow 12.0 | `cvision/diff.py` |
| 🟡 相邻缺口 | `wait_until_changed` / `wait_until_stable` 也回报 `image_screen_box`（+ `handle`/`title`），宿主据此把 `click_at` 的比例基准换成**它们返回的那张图**；几何在 `fit_for_attachment` **之前**用原始帧算，且**不进** `WAIT_*_KEYS` 与工具 schema（`additionalProperties:false` 那条路径能不加键就不加） | `cvision/capturer.py`、`cvision/cli_server.py`、`cvision/cli_capture.py`、`src/index.ts` |

### 7.5 验证（终态实跑）

| 项 | 结果 |
| --- | --- |
| `npm run check:dsh` | **30/30**，exit 0 |
| `npm run check:docs` | **17/17**（新增两条断言：工具说明里的格式名 ⊆ `MEDIA_TYPES`、Python 半边版本号 == `package.json`），exit 0 |
| `npm run check:deps` | **9/9**，exit 0 |
| `npm run test:js` | **96/96**，fail 0（本批共 +12：体检新分支 1、`noteCaptureTarget` 2+1、客户端门控 3、能力路由同源 2、`snipInFlight` 复位 1、体检缓存 1、`wait_*` 键过滤 1） |
| `python -m unittest discover -s tests` | **Ran 308 tests / OK**（本批共 +16：`test_status.py` 2、新建 `tests/test_cli_capture.py` 8（该入口此前零专测）、`tests/test_diff.py` 2、`tests/test_wait_stable.py` 3、`tests/test_cli_server.py` 1+扩 1） |
| 真实链路冒烟（只取 8×8 区域，不打印图像） | `--wait-changed` → `changed=False`、`image_screen_box={x:0,y:0,width:8,height:8}`、无 `handle`；`--wait-stable` → `stable=True` + 同一矩形（证明几何是走真实 `screen_info`/`resolve_window` 算出来的，不只是 mock 通过） |
| 故障注入复验（伪造抛错的 `PIL` 包） | `--status` → exit 0，`backend:"windows"`、`backend_import_error:"ImportError: simulated: Pillow is not installed"`、`deps.Pillow:false`、`ok:false`（修复前：exit 1 + 无 JSON）；探针不再把 `PIL`/`win32gui`/`cvision.capturer` 拉进 `sys.modules` |
| `git status` | 11 个文件改动 + 1 个新增测试文件；`lib/index.js` 已随 `src/index.ts` 重新构建（CI 会校验二者一致） |

### 7.6 仍未做（诚实记录）

- **P1-1 依旧没有活体复现**：那需要往真实窗口里键入字符、移动鼠标。改动把「沿用旧句柄」这条路径从代码里消掉了，并把这个不变量钉成了纯函数测试；真机确认留给后续人工回归。
- **`wait_*` 的接线是「调用点数量」断言**：那条不变量（拿到几何就要更新基准）没法在单测里端到端跑——真实实现要 spawn Python 并阻塞轮询。所以除了纯函数测试与两侧形状测试外，另加了一条源码级断言钉住调用点数量（`see` + 两个 `wait_*`）；**行为**部分由真实链路冒烟（上表最后一行）覆盖。
- **未验证的边界**（与 §6.3 相同）：macOS/Linux 真机行为、真实桌面交互、锁竞争时序、CI 的实际运行。
- **改动仍未提交**：本报告与 v0.2.33 的全部改动都在工作区；提交时 `src/` 与重建后的 `lib/` 必须一起进（CI 会校验二者一致）。

### 7.7 桌面版截图按钮失效（用户报告，属本批之外的新输入）

**现象**：按钮在（也能变色），但点了没反应；长按插入剪贴板图片同样无效。**只在 DSH 桌面版（App）出现，web 面板正常。**

**取证（外部可复核）**：

| # | 事实 | 来源 |
| --- | --- | --- |
| 1 | 桌面版宿主进程是 `dsh-desktop-host`，监听 `127.0.0.1:19387`，用的 profile 是 **desktop**；该 profile 的 `bundles` 里**有** `vision` | `Get-CimInstance Win32_Process` / `profiles/desktop/package.json` |
| 2 | 界面截图确认相机图标**确实渲染**；当时的模型 `DeepSeek-V41-Flash` 名字不含 `vision\|visual`，所以按钮可见只能是**宿主能力路由答的「收图」** | `see(handle=…)` 截图 |
| 3 | `app.asar` 里 `dsh-client-ui-screenshot` **0 命中** → 那个相机图标就是本插件的按钮（客户端半边加载正常） | asar 字符串扫描 |
| 4 | 桌面版页面跑在 `dsh-app://app`，非静态请求由主进程 `forwardWebRequest` 转发到回环 HTTP 服务，而它**删掉** `host`/`origin`/`cookie`/`sec-fetch-site` 再换上宿主 cookie → 插件路由收到的请求**永远没有 `Origin`** | asar 里 `forwardWebRequest` 源码 |
| 5 | 旧判定要求「`Origin` 存在且 host == `Host`」→ 桌面版**必然 403**（实测：不带 `Origin` → 403；只有匹配的 `http://127.0.0.1:<port>` → 204） | 对 `/cvision/clipboard/image` 的真实 HTTP 探测 |
| 6 | 客户端随后回退 `getDisplayMedia`，而桌面版把抓屏权限全关了（`setPermissionCheckHandler(() => false)`、`setDevicePermissionHandler(() => false)`、`setDisplayMediaRequestHandler((_req, cb) => cb({}))`）→ **回退也是死的** | asar 扫描 |

**根因**：`POST /cvision/snip` 与 `POST /cvision/clipboard/image` 的调用方判定把「桌面版转发」这条合法路径拒了；web 面板之所以正常，只是因为它的页面直接跑在 `http://127.0.0.1:<port>` 上、`Origin` 与 `Host` 本来就匹配。

**修法**：新增 `isTrustedRouteCaller(req)`，两条高权限 POST 路由共用，放行三种之一：① `Origin` 存在且 host == `Host`（原行为）；② `Origin` 是 `dsh-app://app`；③ **`Origin` 缺失且对端回环**（IPv4 / `::1` / `::ffff:127.0.0.1`）。跨站页面的 POST 一定带 http(s) `Origin` → 照旧被拒；豁免只给回环，而本机进程本来就能直接调 `python -m cvision.cli_snip`，从来不是这条路由的防护边界。README 的 STORE 契约与故障排查表已同步。

**验证**：3 条新 JS 用例（无 `Origin`+回环 → 放行且真拉起执行器；无 `Origin`+非回环 → 403 且不碰执行器；`dsh-app://app` → 放行）+ 剪贴板取图同口径 1 条；另用**真实 HTTP 往返**（分别绑 `127.0.0.1` 与双栈 `::`，对端写作 `::ffff:127.0.0.1`）复核：无 `Origin` → 204、`dsh-app://app` → 204、跨站 → **403**、同源 HTTP → 204。

**生效要求**：插件需更新到 ≥v0.2.33（含桌面 profile 里的 `vision`）并**重启 DSH 桌面 App**——宿主路由是进程启动时注册的，热更新不生效。

---

## 8. 桌面版全量冒烟测试中新发现的两项缺陷（v0.2.34）

用户在桌面版里实测「截图按钮」修好后，要求「测试桌面板插件其它功能是否正常」。16 个工具 + 4 条路由 + 客户端半边逐个实跑（靶子用自建的系统「运行」对话框，全程不按回车、事后还原剪贴板与鼠标位置），**其它都正常，暴露出两项新缺陷**——它们不在 §4 的清单里，是**跑出来**的。

### 8.1 `wait_for_window` 会等到错的窗口（静默拿错 handle）

- **现象**：`wait_for_window("运行")` 返回 `v2rayN - V7.24.1 - X64 - 以非管理员身份运行`，而标题精确等于「运行」的对话框被跳过。
- **根因**：宿主自写扫描——`src/index.ts` 里 `windows.find(w => w.title.toLowerCase().includes(needle))`，取的是**枚举顺序里第一个命中**。项目本来已有唯一一份匹配语义 `capture.base.pick_window`（精确标题优先 → 非最小化 → 面积大），`see(window=…)`/`ocr(window=…)`/`cli_capture --window` 全走它。
- **危害**：静默。拿错 handle 之后，对着它的抓图/点击全作用在错窗口上；`detail` 里虽写了 found 的是谁，但调用方很容易只读 `found: true`。
- **修法**：新增 server op `{"op":"find_window","window":…}` 与同形状 CLI `--find-window TITLE`（内部都调 `capturer.resolve_window`，不重写匹配），`wait_for_window` 每轮改调它；匹配语义回到一份实现。
- **验证**：Python 4 条（op 形状 / 无命中→`null` 且 `ok:true` / CLI 形状 / CLI 无命中）+ JS 1 条（宿主改调解析器、`includes(needle)` 自写扫描已消失）；并在本机**真实撞车现场**（v2rayN 与「运行」对话框同时在列表里）跑了两条通道，都返回精确标题那个。

### 8.2 `type_text` 逐键输入会被输入法改写（静默写错内容）

- **现象**：`type_text("cvision smoke 12345")` → 实际 `才visionsmoke12345`；同环境下 `type_text("测试中文 123")` 与 `type_text("98765")` 逐字精确。
- **根因**：`input.type_text` 只对**非 ASCII** 走剪贴板粘贴，ASCII 走 `pyautogui.write` 逐键——逐键会被活动 IME 拦截（字母当拼音、空格当候选提交键）。现场证据：任务栏是讯飞输入法指示器（已截图存档）。粘贴路径（Ctrl+V）与输入法无关。
- **修法**：`type_text(text, *, paste=None)` 默认按环境选路径：非 ASCII / 含换行制表符 / **前台是 CJK 布局** → 粘贴；其余 → 逐键。空串直接返回（否则会把剪贴板里已有的内容粘进目标）。探测用 `GetKeyboardLayout(前台窗口线程)`——不用 `ImmGetConversionStatus`：它需要 HIMC，而 HIMC 跨线程取放是未定义行为；代价是只能判到「布局级」，所以判定**偏保守**（CJK 布局就当可能被拦截）。逃生口：`--type-direct`/`--type-paste`/`CVISION_TYPE_DIRECT=1`，工具侧 `type_text(direct=true)`。
- **代价（已写进 README）**：CJK 环境下 ASCII 输入也会短暂占用剪贴板；占用期间的竞态仍由 v0.2.19 的「只在剪贴板仍是我们写的那份时才还原」保护兜着。
- **验证**：Python 9 条路径选择用例（含「空串不粘贴」「en-US 才逐键」「env 强制逐键」）+ CLI 2 条参数映射 + JS 1 条 argv 映射；另在本机跑**真机链路**（`cli_input --ensure-front … --type` → 复制回读），ASCII 文本原样进入目标。

### 8.3 冒烟测试里确认正常的项（记录，供以后回归对照）

`cvision_status`（含 `backend_import_error`、`input_lock.available`、`wgc.available`）、`screen_info`、`list_windows`、`see()`/`see(text=true)`/`see(ocr=true)`、`ocr`、`wait_until_stable`（stable=true）、`wait_until_changed`（超时 unchanged，返回画面）、`click`/`click_at`/`double_click`/`mouse_move`/`drag`/`scroll`/`press_key`/`focus_window`/`get_clipboard`/`set_clipboard`，以及 §7.5 那条修复的**现场复核**：`wait_until_stable(region=…)` 之后 `click_at(0.5,0.5)` 报出的是**该区域**的中心（68,1314），而不是上一次 `see` 整窗矩形的中心（214,1269）。

---

*本报告为分析产物；§7 与 §8 对应的代码改动已分别落地为 **v0.2.33** 与 **v0.2.34**（均已提交并发布 Release）。*
