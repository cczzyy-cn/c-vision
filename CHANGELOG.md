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

## v0.2.27

**修好「WGC 明明能用、却永远抓不到帧」：换用标准 D3D11 互操作，虚拟机上也恢复了抓被遮挡窗口的能力。**

背景：`see(handle=…)` 优先走 Windows Graphics Capture —— 它是唯一能抓**被遮挡窗口**、又不抢前台的
路径。但它一直拿不到 D3D 设备，报 `DXGI_ERROR_UNSUPPORTED`（0x887A0004）。深挖后确认**机器没问题**：

| 判据 | 实测 |
| --- | --- |
| `GraphicsCaptureSession.is_supported()` | **True** |
| `D3D11CreateDevice(HARDWARE, 11_0)` | **成功**（hr=0x00000000） |
| `CreateDirect3D11DeviceFromDXGIDevice` | **成功**（hr=0x00000000） |
| `LearningModelDevice(DIRECT_X_*)` | 全部失败 —— 而旧实现**只走这一条** |

原因：`winsdk` 没有暴露 D3D11 互操作，代码只能借 WinML 的 `LearningModelDevice` 变通拿设备；而 WinML
要求的不只是「能建 D3D11 设备」，虚拟显卡（本机 VMware SVGA 3D）不满足它那套设备接口检查。

- 修法：新增 `cvision/capture/wgc.py`，**优先用 PyWinRT（`winrt` 包）的标准互操作路径**：
  `D3D11CreateDevice`（硬件，失败退 WARP）→ `QI(IDXGIDevice)` → 官方
  `create_direct3d11_device_from_dxgi_device` → `create_for_window` → 抓帧；`winsdk` 的 WinML
  路径保留为回退。两套绑定共用同一份抓帧代码。
- **实测**：同一台虚拟机上，WGC 抓一个**不在前台、被 Chrome 遮挡**的 v2rayN 窗口，拿到了它完整的
  真实界面（此前只能靠 `grab_region` 抓到遮挡者）。
- **`wgc_probe()`**：可用性改为**真实探测**（不再只看 `import winsdk`），并回报实际生效的绑定
  （`binding: winrt` / `winsdk`）。结果做**进程内 + 磁盘双缓存**：首次 ~280ms，之后 <1ms ——
  CLI 每次调用都是新进程，没有磁盘缓存的话每次 `see` 都白付这 280ms（而抓一张图才 ~170ms）。
  缓存带**环境指纹**（解释器/系统/两套绑定），装了 PyWinRT 会立刻失效重探。
- **`cvision_status` 新增 `capture_backends`**：如实说明当前能抓什么、WGC 不可用时原因是什么。
  旧行为只看 `deps.winsdk == true`，会把「抓不了被遮挡窗口」说成能抓。
- **`requirements.txt` 新增 PyWinRT 系列**（Windows，合计约 0.7MB）；装了它 WGC 才走标准路径。
  `winsdk` 仍然必需 —— `Windows.Media.Ocr` 用它。
- 新增测试：Python 209 → **216**（探测缓存的失效语义：指纹不符 / 过期 / 时钟回拨 / 文件损坏，
  以及「可用时不写缓存」）；JS 保持 70。

## v0.2.26

**新增 `click_at(rx, ry)`：按比例点击，让「模型自己看图定位」这条路径真正可用。**

背景：模型读不准图片**像素**。`see()` 整屏抓的是 1920×1080，而模型实际收到的是 **1708×961**：
存储层并不缩图（1920×1080 远在 8192px / 4.19M 像素的限额内），是 **LLM 路由在发往 provider 前**
按 DeepSeek 的图片 token 规则缩的（`deepSeekRequestImageDimensions()`：14px patch 网格、每轴 3:1
下采样、单图 1024 token 上限 —— 1920×1080 算出 1224 token 超限，解出的长边正是 1708）。
照着自己看到的画面数像素直接用，右下角会偏 **212×119 px**；而插件侧**没有任何 API** 能问到
「模型最终看到的尺寸」。

- 修法不是去复现 provider 的缩放规则（那份规则绑在 `v41` 配置上，换版本或换模型就漂移），而是
  **换一套坐标表达**：比例位置在缩放前后**不变**。
- `see` 现在随图回报 `image_screen_box`（这张图覆盖的屏幕矩形），`click_at(rx, ry)` 用它换算
  `screen = (x + rx·width, y + ry·height)`——与图片被缩放到多少像素**完全无关**，也不需要知道
  任何 provider 规则。
- 矩形在 **OCR 失败时依然有效**：纯图标界面一个词都认不出来，照样能按比例点。为此把几何计算从
  OCR 的 `try` 块里拆出来，并抽成 `capturer.image_screen_frame()`，供 `capture_with_text` 与
  `cli_server` 的非 text 抓取共用（两处各写一份迟早漂移）。
- `click_at` 复用既有链路：同样会先把「最近一次 `see` 的窗口」置前、并校验坐标确实属于它，
  复核不过就报错跳过；它还把**换算出的屏幕坐标**回报给模型，便于对照「我按比例指的点落在哪」。
- 精度边界如实写进了工具描述与 README：比例差 1% ≈ 1920 宽屏上 19px，**小控件仍应优先用
  `see(text=true)` 的 `screen_center`（±1px）**；`click_at` 解决的是「OCR 压根没覆盖到的目标」。
- 新增测试：Python 204 → **209**（矩形与 `elements` 同坐标系、负原点、DPI 缩放、不退化成 0 尺寸）；
  JS 保持 70。

## v0.2.25

**修一个真实缺陷：`see` 给的坐标是对的，但点击「点了没反应」或点到别的窗口上。**

现象：`see(text=true)` 回来的 `screen_center` 精确到个位（实测误差 −1 px），照着点却什么都没发生，
或者点中了压在上面的另一个窗口。用户侧的直观印象是「see 和定位点击有问题」，而**坐标那一环其实没问题**。

根因全在「置前」这一步，三处叠加：

1. **两处置前实现都是无效的**，而点击只命中**前台**窗口：
   - `input.bring_to_front` 只调 `SetForegroundWindow` + `BringWindowToTop`——非前台进程调用它会被
     Windows 前台锁定直接拒绝，没有任何兜底；
   - `capture.windows._ensure_foreground` 有 `AttachThreadInput` 兜底，却把当前线程挂到**目标窗口的
     线程**上。前台规则要求的是「调用线程拥有**前台窗口**的输入队列」：实测这样挂 `AttachThreadInput`
     直接返回 0（附加失败），置前依旧失败；改挂**前台线程**返回 1，一击即中。
2. **`focus_window` 无条件返回 `{ok:true}`**——置前失败也报成功，调用方于是放心去点。
3. **`click` / `drag` / `scroll` 只看坐标**，既不校验该坐标此刻属于哪个窗口，也不会把目标置前。

实测（自建标定窗口 + `ClientToScreen` 真值，1920×1080 / 100% 缩放）：

| 环节 | 结果 |
| --- | --- |
| PrintWindow 位图原点 vs `GetWindowRect` | 偏移 **0 px**（坐标换算本身是对的） |
| `see(text=true)` 元素定位 | 误差 −1 / +1 px |
| 被遮挡的目标 + 旧 `focus_window` | 置前失败，点击**落空**（目标窗口收到 0 次点击） |
| 同一坐标 + 修复后置前 | **命中目标**，点击坐标误差 **(0, 0)** |

- 修法：新增 `input.attach_thread_for()`——纯函数，把「只能挂前台线程」这条规则钉死；
  `input._force_foreground_native()`：补线程消息队列（CLI 子进程默认没有，附加必失败）→ 挂前台线程 →
  置前 → 仍失败则轻敲 ALT 兜底 → **以 `GetForegroundWindow` 实测为准**。`focus_window` 失败即抛错。
  `capture.windows._ensure_foreground` 改为复用同一份实现（旧的那份会让 GPU/合成窗口的 `see` 在目标
  不在前台时抓到**遮挡者的画面**，而元素坐标仍按目标矩形换算——图文双错），避免两处再次漂移。
- 新增能力：`cli_input --window-at X Y`（该屏幕点最顶层的窗口）、`--ensure-front H [--at X Y]`
  （置前 + 坐标归属校验）；`cli_capture --text` 与 `cli_server` 的 `capture` 现在附带目标窗口 `handle`。
- 宿主侧：`see` 记住目标窗口；`click`/`double_click`/`drag`/`scroll`/`type_text`/`press_key` 执行前自动
  置前并复核坐标归属，**复核不过直接报错并跳过这次点击**（错误里写明「该点现在属于谁」），绝不静默点偏；
  窗口已销毁时自动丢弃过期记录，不会永久卡住后续点击。`focus_window` 改为读 CLI 的真实结果，失败即报错。
- 新增测试：Python 188 → **204**（`AttachThreadInput` 必须挂前台线程、`--window-at`/`--ensure-front`
  的参数映射与失败非零退出、focus 失败不得被吞、同一行里 x 回退的词不得合并、纯符号碎片必须被丢掉）；
  JS 保持 70。

**同一版还修了 `see(text=true)` 的元素质量——那是「坐标对、却定位不到目标」的另一半原因。**

- **负 gap 合并（主因）**：合并判据写的是 `gap <= join_gap`，而它对**负** gap 恒成立。偏偏 OCR 在
  同一行内并不保证从左到右（两列的竖直中心差 1~2px 就会让右边那列排到前面），于是「修改日期」列被
  硬并进「文件名」列，拼出 `23 2 17 / / ： scripts` 这种跨列碎片——它的中心点正好落在两列之间的空隙上，
  点它等于点在空气里。实测一份 248 词的窗口里这类错误合并 **77 次**、超宽跨列碎片 13 个；修后**均为 0**。
  修法两步：先聚成「行」、再在**行内按 x 排序**，并给 gap 补上负值下限（只容忍词框的轻微重叠）。
- **OCR 放大**：`Windows.Media.Ocr` 对**小字**识别得很差。同一张 1353×782 的窗口图，1x 时把 `scripts`
  认成 `scrlpts`、`2026/9/23` 认成 `2025/g/23`、`文件夹` 认成 `文 仁 夹`。改为先按经验放大 **3x**
  再识别（长边上限 4200px，避免 4K 截图被放大到上亿像素），拿该目录里真实存在的 15 个文件名当真值，
  命中从 **3/15 升到 9/15**，而元素总数几乎不变（103 → 106）。代价是 OCR 耗时 146ms → 331ms。
  `words` 坐标会按倍数**还原成原图坐标**，调用方完全无感。
- **纯符号碎片不再占名额**：`/`、`:`、`--` 这类短碎片不可能被点击，却会挤掉真正能点的控件。
  判据刻意保守（长度 > 2 一律保留，且只认分隔符字符集），所以 `×`（关闭）、`下`、`OK` 都照常保留。
- **截断提示给全**：`max_elements` 默认 40 → **60**，且截断时直接写明「要拿全请传 max_elements=N」——
  实测一个资源管理器窗口就有 100+ 个元素，前 40 个只覆盖屏幕上半部分，模型会误以为目标不在界面上。

## v0.2.24

**修一个真实缺陷：`wait_until_changed` 每一次调用都必然失败——返回值里带着 schema 没声明的键。**

现象：调用 `wait_until_changed` 直接被宿主判为非法输出。

```text
tool "wait_until_changed" returned invalid output: "value.diff_bbox" must be an object;
"value.width" is not a declared property (additionalProperties: false);
"value.height" is not a declared property (additionalProperties: false)
```

根因：DSH 会拿工具自己声明的 `output.schema` **校验返回值**，而这份 schema 写的是
`additionalProperties: false`——多一个没声明的键不会「被忽略」，而是让整次调用失败：

- Python 侧（`cli_capture --wait-changed` 与 `cli_server` 的 `wait_changed`）**每次都**返回
  `width`/`height`，宿主 schema 里却没声明它们；
- 未变化时 Python 给 `diff_bbox: None`，而 schema 声明的是 `type: 'object'`，null 同样非法。

所以这个工具不是「偶尔失败」，而是**从来没能成功过一次**。旧版 DSH 不校验工具输出，因此一直没暴露；
升级到会校验的版本（本机 0.1.7-alpha.1）后立刻显形。

- 修法：新增纯函数 `waitChangedMeta(payload)` 做显式整形（只保留 `WAIT_CHANGED_KEYS` 声明过的键 +
  丢掉 null），server 与 CLI 两条路径共用；schema 补上 `width`/`height`。`render` 本来就把缺失的
  box 当作「没有变化区域」，丢掉 null 语义不变。
- 新增 JS 回归 2 条（68 → **70**）：**返回键必须与声明的 schema 逐字一致**、**null 必须被丢掉**。
  它们钉的是不变量而不是功能：原有 68 条用例只驱动 HTTP 路由，没人把「真正返回的键」和「声明过的键」
  放在一起比过，所以 CI 全绿也没拦住这个「必然失败」。

## v0.2.23

**修一个真实缺陷：抓图会撤销 Windows 的吸附（Snap），把用户的分屏搞坏。**

现象（用户报告）：把两个 Chrome 标签左右分屏后，调用 `see({"handle": …})` 会**同时改变窗口大小和位置**，分屏被破坏。

根因（用监视器以 0.2 秒采样抓到确证）：

```
Windows 吸附态下 —— GetWindowPlacement 返回的是：
    rect      = (-7, 0, 1287, 1399)      ← 吸附【之后】的位置（贴左半屏）
    rcNormal  = (-12, 63, 1282, 1462)    ← 吸附【之前】的位置

capture_window 的 finally 里无条件 _restore_placement()
  → SetWindowPlacement(那份 placement)
  → Windows 把窗口放回 rcNormalPosition
  → 【吸附被撤销，分屏破坏】
```

监视器抓到的现场：`see({"handle": 1117166})` 那一刻，
`rect [-7,0,1287,1399] -> [-12,63,1282,1462]` —— **搬去的位置恰好等于调用前保存的 `rcNormal`**。

- 修法：新增纯函数 `should_restore_placement(maximize, was_iconic)`，**只有我们真的改过窗口状态时才还原**：
  `maximize=True`（调过 `SW_MAXIMIZE`）或抓之前是最小化（调过 `SW_RESTORE`）。
  **普通窗口抓取是纯只读的，不再写回 placement** —— 因而不会撤销用户的吸附/分屏。
  `saved_placement` 在不必要时直接为 `None`，`_restore_placement` 空转返回。
- 实测验证（真实吸附态、必须带错配 `rect != rcNormal`）：
  修复**前** → 窗口被搬到 `rcNormal`、吸附丢失；修复**后** → `rect` 与 `rcNormal` 逐字未变、吸附保住。
- 新增 `tests/test_capture_foreground.py` 6 条：`normal 不还原 / maximize 还原 / iconic 还原 /
  truthy int（win32 返回 BOOL）/ 端到端不调用 SetWindowPlacement`。Python 182 → **188**。

**排查过程中两个必须记录的坑（都不是代码问题，却让我长时间误判）：**

1. **常驻 `cli_server` 会缓存旧代码**。宿主复用一个长驻 Python 进程，它把 `windows.py` 加载进内存后
   **不会重读文件**。我改完代码后连续多轮"重测"，实际跑的一直是**修复前的模块**——所以修复看起来
   "无效"，而我在文件里插的探针也从不触发。**杀掉那个陈旧进程**、让宿主拉起新 server 之后，修复立即生效。
   这一条值得写进开发文档：**改了 Python 侧代码后，必须重启宿主（或杀掉 cli_server）才生效。**
2. **测量必须在同一个瞬间**。我此前的"前后测量"是两个不同进程、隔着工具往返时间做的事——而窗口
   早已在 `finally` 里被还原过了，所以我一直看到"未变"。只有把采样放进插件内部 / 用 0.2 秒的外部
   监视器，才抓到那一刻。

## v0.2.22

**新增窗口跟踪诊断：用插件内部的证据回答「抓图到底有没有挪动窗口」，取代外部反复试探。**

起因：用户报告「两个 Chrome 标签左右分屏后，`see({"handle": …})` 把分屏破坏了」。我做了两轮外部实测，
结果**自相矛盾**且无法收敛：

- 我走代码路径的调用（直接 `capture_with_text`、全新进程冷启动、常驻 `cli_server` 的 `capture`、
  以及 `capture + text:true`）**共 13 次，全部没有移动窗口**；
- 但**真实的工具调用** `see({"handle": 1117166})` 之后，该窗口从 `(-7,0,1287,1399)` 变成
  `(1240,45,2534,1417)`——与用户上次报告给我的坐标**逐字相同**，看起来是确定性行为；
- 再一次同样的调用却没有移动；与此同时**另一个**窗口（对照）反而移动了。

结论：**从外部观测无法区分「窗口是在抓取过程中被移动的」还是「抓取前后被别的因素移动的」**。
继续在外面反复试探只会持续打扰用户的桌面，而且每次实验本身就是一次干扰。

修法（本版）：把观测点放进插件内部。

- 新增 `cvision/diagnose.py`：`snapshot()` 取窗口几何快照（`rect`/`showCmd`/`zoomed`/`iconic`/`foreground`），
  `changed_fields()` 纯函数判定哪些字段变了，`trace_step()` / `trace_capture()` 写 jsonl。
- 接入 `capture_window`：在**任何可能改动窗口的动作之前**取基线快照，并在
  `prepare(maximize=…)` → `wgc` → `printwindow` → `grab_region` 每个阶段之后各采一次，
  最后写一条 `capture` 总结（含 `moved` 与 `changed`）。
- **默认关闭、零开销**：不设 `CVISION_TRACE_WINDOWS=1` 时所有快照函数直接返回，热路径上只是几次属性读取。
  `snapshot()` 在关闭时返回 `None`，`trace_step` 返回 `None`，不影响任何返回值。
- **绝不写 stdout**：stdout 是宿主读的 JSON-line 协议，污染它就等于弄坏工具；日志一律写文件
  （`CVISION_TRACE_FILE`，默认系统临时目录）。
- **诊断失败不影响抓图**：采样与写文件全部包在 try/except 内。
- 新增 `python -m cvision.diagnose`：打印开关状态、日志路径，并汇总「共 N 次抓取、其中 M 次检测到窗口
  被改动」及具体 before/after。
- README 故障排查表新增一行，把这条诊断作为「抓图后窗口位置变了」的**首选取证手段**（而不是猜）。

新增 `tests/test_diagnose.py`（14 条，平台无关）：把诊断工具本身的判定逻辑钉住——改动字段判定与顺序、
只换前台也算改动、`None` 快照安全、开关取值（`1`/`true`/`0`/`false`/空）、关闭时**不创建任何文件**、
逐行追加且字段齐全、**写入失败（非法路径）绝不抛**、`trace_step` 关闭时返回 `None` / 开启时记录 `stage` 与
`note`。理由：这个诊断是我用来判定「谁动了窗口」的**测量仪器**，而我在本会话里已经三次因为
「测量工具本身没验证」得出过错误结论。

Python 168 → **182**。无行为改动（诊断默认关闭）。

## v0.2.21

**修工具说明与 README 规范的两处矛盾——模型实际照办的是工具说明，不是 README。**

背景：README 是给人看的，而**模型行事依据的是注入到它上下文里的工具 `description`**。v0.2.20 只改了
README，工具说明没跟上，于是出现了「README 说 A、工具说明说 B」的矛盾，模型会照 B 办。

- **矛盾一（严重）**：`see` 的说明写着「非最小化窗口……**不切换前台、不抢焦点**」，而 README 已写明
  **目标最小化时抓取会抢走前台**。这正是 v0.2.20 实测推翻的那条断言——但却留在模型真正会读的那份文本里。
  现已在 `see` 的说明中补上完整口径：普通窗口（前台或背景）不抢前台；**最小化窗口会**；兜底路径（WGC 与
  PrintWindow 都失败）也会。
- **矛盾二**：`ocr` 的说明写着词级边界框「供 computer-use **精确定位点击**」——而 README 的规范恰恰是
  **不要**用 `ocr` 的词框折算坐标（那是图片坐标，要叠四层偏移），定位应当用 `see(text=true)`。
  同一段里还紧接着推 `text=true`，自相矛盾。现改为明说「`words` 是图片坐标，不要用它算点击位置；
  `ocr` 的用途是**取文字**，不是定位」。
- 顺带修 `see` 说明里缺失的第三条：坐标正确但点击只命中**前台窗口**，目标不在前台或与其他窗口**重叠**时
  要先 `focus_window`。
- 另修 `src/index.ts` 文件头注释里同一条过期说法（v0.2.0 时代写的「用于精确定位点击点」）。
- `check:docs` 只覆盖 README 与代码注册的工具名/路由，**覆盖不到工具 description 的措辞**，所以这次矛盾
  是它发现不了的——属于现有门禁的盲区，记录在此。
- 无运行时代码逻辑改动（只有说明文本）。Python 168/168、JS 68/68、check:dsh 30/30、check:docs 14/14、check:deps 9/9。

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
