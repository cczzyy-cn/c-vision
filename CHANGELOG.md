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

## v0.2.14

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
