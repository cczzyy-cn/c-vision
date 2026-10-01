/**
 * C-Vision 视觉插件（DeepSeek Harness / DSH bundle）
 *
 * 注册观察（see/ocr/list_windows）与 computer-use（click/type_text/...）工具：
 * 跨语言调用本插件包内捆绑的 Python 版 cvision（截屏/OCR/输入），把截图写入 Harness
 * 附件服务（`ctx.attachments.saveImage`）并以 `image` ContentBlock 返回。
 *
 * v0.2.0 增强：
 *   - OCR 返回词级边界框（`words`）——**供读取词级文字**；定位点击请用 `see(text=true)` 的
 *     `screen_center`（屏幕绝对坐标），不要拿 `words` 的图片坐标自己折算（v0.2.19 起）。
 *   - 新增 `screen_info`（显示器/DPI 布局）与 `cvision_status`（运行环境健康）。
 *   - computer-use 补全：`drag`、`scroll` 支持水平、`get_clipboard`/`set_clipboard`、
 *     `see(ocr:true)` 一次返回图片+文本、`wait_for_window`。
 *   - 持久化 Python server（复用 D3D 设备/编码，避免每次冷启动）；失败时回退每调用 CLI。
 *
 * 分发：插件包自带 `cvision/` 与 `requirements.txt`，`CVISION_DIR` 默认定位到本插件
 * 安装目录。目标机器需 Python 3 并 `pip install -r requirements.txt` 一次。
 */
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, delimiter, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineTool } from '@deepseek-ai/dsh-tools';
const execFileAsync = promisify(execFile);
export const name = 'Vision';
export const inject = ['tools', 'attachments'];
/**
 * 允许回传的图片类型。
 *
 * 刻意**不含 GIF**：`encoding.image_to_base64` 用 `img.save(format='GIF')` 保存多帧图时只会写出
 * 第一帧，所以「支持 GIF」是假的——`see(format='GIF')` 曾能选到它，但拿到的是静默退化的单帧。
 * 与其留一个看着支持、实际丢帧的选项，不如在这里拒掉（`parseDataUrl` 会给出明确报错）。
 * 注：Pillow 对多帧有 `save_all=True` + `append_images`，真要做动图得走那条路。
 */
const MEDIA_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
/** 从插件自身位置向上查找含 `cvision/` 的包根（入口在 lib/index.js 时需上移一层）。 */
function findPluginRoot() {
    let dir = dirname(fileURLToPath(import.meta.url));
    for (let i = 0; i < 4; i++) {
        if (existsSync(resolve(dir, 'cvision')))
            return dir;
        const parent = dirname(dir);
        if (parent === dir)
            break;
        dir = parent;
    }
    return dirname(fileURLToPath(import.meta.url));
}
const PLUGIN_DIR = findPluginRoot();
const PYTHON = process.env.CVISION_PYTHON ?? 'python';
const CVISION_DIR = process.env.CVISION_DIR || PLUGIN_DIR;
/**
 * Python 子进程一律强制 UTF-8 stdio，避免 Windows 控制台/ANSI 代码页把中文窗口标题与 OCR 输出弄乱。
 * 同时把插件目录挂到 `PYTHONPATH` 前面，让 `python -m cvision.*` 在**任意工作目录**下都能 import 到包内源码。
 */
const PY_ENV = {
    ...process.env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONPATH: [CVISION_DIR, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
};
/**
 * Python 子进程的工作目录：**绝不能用插件安装目录**。
 *
 * Windows 下「进程的当前目录」就是该目录上的一个句柄，于是插件自己拉起的常驻
 * `python -m cvision.cli_server` 会把安装目录锁住 —— `dsh plugin add` 升级/重装时，pnpm 需要把临时目录
 * rename 成 `node_modules/vision`，会直接失败：
 *
 * ```text
 * [ERR_PNPM_EPERM] [importPackage …\node_modules\vision] EPERM: operation not permitted,
 *   rename '…\vision_tmp_23816_2' -> '…\vision'
 * ```
 *
 * 实测踩到过（用户执行 `dsh plugin add github:` 时）。改成系统临时目录后，任何子进程都不再持有安装目录，
 * 边跑边升级也能成功；`cvision` 仍靠 `PYTHONPATH` 解析得到。
 */
const PY_CWD = tmpdir();
/** 解析 `data:<mime>;base64,<data>` 为附件服务所需的字节与媒体类型。 */
function parseDataUrl(dataUrl) {
    const m = /^data:(image\/[a-z+]+);base64,(.+)$/s.exec(String(dataUrl).trim());
    if (!m || !m[1] || !m[2])
        throw new Error(`无法解析截屏 data URL（长度 ${String(dataUrl).length}）`);
    const mediaType = m[1];
    if (!MEDIA_TYPES.includes(mediaType))
        throw new Error(`不支持的图片类型 ${mediaType}`);
    const ext = {
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/webp': 'webp',
        'image/gif': 'gif',
    }[mediaType];
    return { data: new Uint8Array(Buffer.from(m[2], 'base64')), mediaType, ext };
}
/** 断言包内（或 CVISION_DIR 指向）的 Python 版 cvision 存在。 */
function assertCvisionPresent() {
    if (!existsSync(resolve(CVISION_DIR, 'cvision'))) {
        throw new Error(`未找到 Python 版 cvision（${resolve(CVISION_DIR, 'cvision')} 不存在）。` +
            `请安装配套的 cvision 包，或将环境变量 CVISION_DIR 指向含 cvision/ 的项目根。`);
    }
}
// ── wait_until_changed 的返回值整形（与它声明的 output.schema 严格对齐） ──────
/**
 * Python 侧 `wait_changed` 会给出的键，也正是 `wait_until_changed` 允许返回的**全部**键。
 *
 * 单列一份是为了和 `output.schema` 对齐：DSH 会拿工具自己声明的 schema **校验返回值**，
 * 而 schema 写的是 `additionalProperties: false` —— 多带一个没声明的键（例如 Python 总会
 * 给的 `width`/`height`）不会「被忽略」，而是让**整次调用**以
 * `tool "wait_until_changed" returned invalid output` 失败。
 *
 * ⚠️ 刻意**不含** `image_screen_box` / `handle` / `title`：那两个字段（v0.2.33 起 Python 会一并返回）
 * 是**给宿主自己用的**——宿主拿它们把 `click_at` 的比例基准换到这张新图上（见 `noteCaptureTarget`），
 * 不进模型可见的返回值。于是这份清单与工具 schema 都不用动（v0.2.24 正是在这条路径上炸过）。
 */
const WAIT_CHANGED_KEYS = [
    'changed',
    'samples',
    'elapsed_ms',
    'diff_ratio',
    'mean_diff',
    'diff_bbox',
    'diff_boxes',
    'width',
    'height',
];
/**
 * 把 Python 侧的 `wait_changed` 结果整形为工具返回值（纯函数，导出供回归测试直接调用）。
 *
 * 两条约束都对应真实炸过的缺陷，改这里前请先看 `tests/vision.host.test.mjs` 里的回归用例：
 * - **只保留 `WAIT_CHANGED_KEYS` 声明过的键**：宿主 schema 是 `additionalProperties: false`，
 *   多一个键 = invalid output = 工具整体不可用（v0.2.24 修的正是这个）；
 * - **丢掉 `null`**：未变化时 Python 给 `diff_bbox: None`，而 schema 声明的是 `type: 'object'`，
 *   null 同样非法；`render` 本来就把缺失的 box 当作「没有变化区域」，丢掉语义不变。
 *
 * @param payload - server 或 CLI 返回的原始 JSON。
 * @returns 只含已声明键、且不含 null 的工具返回值。
 */
export function waitChangedMeta(payload) {
    const meta = {};
    for (const key of WAIT_CHANGED_KEYS) {
        const value = payload[key];
        if (value !== undefined && value !== null)
            meta[key] = value;
    }
    return meta;
}
// ── wait_until_stable 的返回值整形（同样与 output.schema 严格对齐） ───────────
/**
 * Python 侧 `wait_stable` 会给出的键，也正是 `wait_until_stable` 允许返回的**全部**键。
 *
 * 与 `WAIT_CHANGED_KEYS` **分开列**是有意的：两者字段本来就不同——`wait_until_changed` 讲
 * 「变没变、变在哪」，`wait_until_stable` 讲「安静下来没有、刚才动得多厉害」。共用一份常量只会
 * 让某一边多带没声明的键，而 `additionalProperties: false` 下那是**整次调用失败**（v0.2.24 炸过）。
 */
const WAIT_STABLE_KEYS = [
    'stable',
    'samples',
    'elapsed_ms',
    'diff_ratio',
    'max_diff_ratio',
    'stable_for',
    'width',
    'height',
];
/** 把 Python 侧的 `wait_stable` 结果整形为工具返回值（纯函数，导出供回归测试调用）。 */
export function stableMeta(payload) {
    const meta = {};
    for (const key of WAIT_STABLE_KEYS) {
        const value = payload[key];
        if (value !== undefined && value !== null)
            meta[key] = value;
    }
    return meta;
}
let runtimeStatus = null;
let runtimeProbeError = null;
let runtimeProbed = false;
/** 上次探测的时刻与结果：**失败**的结论不能当永久结论（见 shouldProbeRuntime）。 */
let runtimeProbedAt = 0;
let runtimeProbeFailed = false;
/**
 * 体检**失败**后的重试间隔。
 *
 * 为什么要允许失败重探：用户看到提示后会去跑 `pip install`，而体检结论缓存在进程里——早期的行为是
 * 「整个进程只探一次」，于是装完依赖后 `see` 会继续报同一个错，而 `cvision_status()`（刻意不过门）
 * 已经显示一切正常，两个工具口径互相矛盾，只能重启 DSH 才能恢复。
 */
export const RUNTIME_PROBE_RETRY_MS = 30000;
/**
 * 现在该不该再探一次环境？（纯函数，导出供单测钉住判定）
 *
 * - 从没探过 → 探；
 * - 上次**成功** → 不探（进程内探一次就够，环境不会自己变坏）；
 * - 上次**失败** → 过了 `retryMs` 再探（用户很可能刚装完依赖）；
 * - 显式 `resetRuntimeProbe()`（`cvision_status()` 成功时调用）→ 立刻重探。
 *
 * 刻意**不**用定时器：重探发生在「下一次真的要用 Python 的时候」，那时才知道用户是不是修好了。
 */
export function shouldProbeRuntime(state) {
    if (!state.everProbed)
        return true;
    if (!state.failed)
        return false;
    return state.now - state.lastAt >= (state.retryMs ?? RUNTIME_PROBE_RETRY_MS);
}
/** 丢掉已缓存的体检结论，让下一次门立刻重探（也供 `cvision_status()` 成功时调用）。 */
export function resetRuntimeProbe() {
    runtimeProbed = false;
    runtimeProbeFailed = false;
    runtimeProbedAt = 0;
    runtimeStatus = null;
    runtimeProbeError = null;
}
/**
 * 当前体检缓存的状态（只读）。导出只为让单测钉住「负缓存的 TTL 与复位」这两条不变量——
 * 它们是「装完依赖不必重启 DSH」的全部实现，而内部变量在外部没法观察。
 */
export function runtimeProbeState() {
    return { everProbed: runtimeProbed, failed: runtimeProbeFailed, lastAt: runtimeProbedAt };
}
/** 体检表**成功**时清掉失败的负缓存：装完依赖不必重启 DSH，下一次门就会重新探测。 */
function invalidateFailedRuntimeProbe() {
    if (runtimeProbeFailed)
        resetRuntimeProbe();
}
/** 体检失败时给出的安装命令：清单随包分发，`--upgrade` 会在区间内挑最新可用版本。 */
export const PIP_HINT = `python -m pip install --upgrade -r "${resolve(CVISION_DIR, 'requirements.txt')}"`;
/** 判断体检结论里某个依赖是否可用；缺 key 一律当作不可用（探针结构变了也不放过）。 */
function depOk(status, name) {
    const deps = status.deps;
    if (deps === null || typeof deps !== 'object')
        return false;
    return deps[name] === true;
}
/**
 * 把一次体检结论整理成**可操作**的错误信息；环境正常则返回 null。
 *
 * 纯函数、不触碰进程，因而可被 `tests/vision.host.test.mjs` 直接注入各种结论来钉死文案
 * （真去 spawn Python 在 CI 上既慢又不稳）。
 * @param status - `cli_capture --status` 的 JSON 结论；探针整体失败时为 null。
 * @param probeError - 探针失败原因（`status` 为 null 时有值），会带上 `PYTHON`/`CVISION_DIR` 便于定位。
 */
export function describeRuntimeProblem(status, probeError) {
    if (status === null) {
        // 连 `--status` 都跑不起来：基本是「没有可用的 python 解释器」或 `cvision/` 路径不对。
        return (`无法运行 Python 版 cvision（${PYTHON} -m cvision.cli_capture --status）：${probeError ?? '未知错误'}\n` +
            `请确认已装 Python 3.10+ 且 \`${PYTHON}\` 可用；插件目录与解释器分别是：` +
            `CVISION_DIR=${CVISION_DIR}、CVISION_PYTHON=${PYTHON}。\n` +
            `若依赖缺失，装一次即可：${PIP_HINT}`);
    }
    const missing = [];
    if (!depOk(status, 'Pillow'))
        missing.push('Pillow');
    const backendMissing = status.backend_implemented !== true;
    // 后端**有实现但导不进来**（缺 win32gui / pyobjc 等）：这既不是「平台未实现」，也不能放行——
    // 放行的话 see/ocr 会在 Python 侧抛裸的 ImportError。处置与缺依赖一致：装依赖。
    const importError = typeof status.backend_import_error === 'string' ? status.backend_import_error.trim() : '';
    const backendBroken = !backendMissing && importError !== '';
    // macOS 这类「有实现但没真机验证」：不是错误，但**必须**说出来——否则模型会把「有实现」
    // 当成「已验证」，出问题时误判成插件 bug 而不是平台差异。
    const unverified = !backendMissing && !backendBroken && status.platform_support === 'unverified';
    // 真正会**挡住**工具的才算错误：缺依赖、平台后端未实现、或后端导不进来。都没有就不拦。
    if (missing.length === 0 && !backendMissing && !backendBroken) {
        return unverified
            ? `提示：本平台后端（${String(status.backend ?? '未知')}）**未在真机验证过**（platform_support=unverified），` +
                `行为可能与文档有出入，遇到问题请当作平台差异排查。`
            : null;
    }
    const lines = [];
    if (missing.length > 0)
        lines.push(`Python 环境不完整：缺少 ${missing.join('、')}。`);
    if (backendMissing) {
        lines.push(`本平台后端未实现（platform=${String(status.platform ?? '未知')}、backend=${String(status.backend ?? '未知')}）——` +
            `截屏/OCR 在该平台尚不可用（详见 README 的平台矩阵）。`);
    }
    if (backendBroken) {
        // 说清「是依赖问题，不是平台问题」：否则用户会去重试或等更新，而正确动作是装依赖。
        lines.push(`本平台捕获后端**有实现但导不进来**（platform=${String(status.platform ?? '未知')}、` +
            `backend=${String(status.backend ?? '未知')}）：${importError}` +
            `——缺的是 Python 依赖（不是平台不支持）。`);
    }
    lines.push(`运行环境：python=${String(status.python ?? '未知')}、platform=${String(status.platform ?? '未知')}、backend=${String(status.backend ?? '未知')}`);
    // 输入类工具另有 pyautogui 这一支依赖；它不阻止 see/ocr，但缺了就该说清后果。
    if (!depOk(status, 'pyautogui'))
        lines.push(`另外：pyautogui 缺失，输入类工具不可用。`);
    // 只有在**确实能靠装包解决**时才给安装命令与复查指引：
    // 平台后端未实现不是装包能解决的，后端导不进来则是。
    if (missing.length > 0 || backendBroken) {
        lines.push(`装一次依赖即可（清单随包分发）：${PIP_HINT}`);
        lines.push(`装完可调 cvision_status() 复查；依赖装在哪个解释器里，就要让 CVISION_PYTHON 指向它。`);
    }
    return lines.join('\n');
}
/** 跑一次体检（成功只跑一次；失败按 shouldProbeRuntime 的节奏重试），失败不抛错——把结论交给 describeRuntimeProblem。 */
async function probeRuntime(exec) {
    const now = Date.now();
    const due = shouldProbeRuntime({
        everProbed: runtimeProbed,
        failed: runtimeProbeFailed,
        lastAt: runtimeProbedAt,
        now,
    });
    if (!due)
        return;
    runtimeProbed = true;
    runtimeProbedAt = now;
    try {
        assertCvisionPresent();
        const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_capture', '--status'], {
            cwd: PY_CWD,
            env: PY_ENV,
            maxBuffer: 4 * 1024 * 1024,
            signal: exec.signal,
        });
        runtimeStatus = JSON.parse(stdout);
        runtimeProbeError = null;
        runtimeProbeFailed = false;
    }
    catch (error) {
        runtimeProbeError = errorMessage(error);
        runtimeStatus = null;
        runtimeProbeFailed = true;
    }
}
/** 首次调用前的体检门：环境有问题就抛出可操作错误，而不是让 Python 抛裸异常。 */
export async function ensureRuntime(exec) {
    await probeRuntime(exec);
    const problem = describeRuntimeProblem(runtimeStatus, runtimeProbeError);
    if (problem !== null)
        throw new Error(problem);
}
/**
 * 当前是否有**输入类操作**正在进行（点击/拖拽/输入/按键/聚焦）。
 *
 * 为什么需要：电脑只有一套鼠标键盘，DSH 与用户会互相干扰——agent 移动鼠标时用户也在动，
 * 或者用户正在打字时 agent 抢走前台。这里不去抢互斥锁（那会带来卡死风险），而是把「占用中」
 * **如实暴露**给浏览器半边，让界面显示出来，把「别碰鼠标键盘」从口头约定变成看得见的状态。
 *
 * 刻意**只统计输入类操作**：截图/OCR 不改用户状态，把它们也算成「占用」会频繁误报，反而没人信。
 */
let inputBusyCount = 0;
/** 跑一个输入类操作，期间把「占用中」置为真（用 try/finally 保证异常时也会复位）。 */
async function withInputBusy(fn) {
    inputBusyCount += 1;
    try {
        return await fn();
    }
    finally {
        inputBusyCount = Math.max(0, inputBusyCount - 1);
    }
}
/**
 * 把会话身份塞进 CLI 参数（`--lock-label`），供持有者记录与超时消息使用。
 *
 * 导出只为单测（同 `snipExecutor` 的套路）：它唯一的调用点在 `runCliInputText`——**所有**输入类 CLI
 * 都从那里发出去，所以身份只会被追加一次，不会出现「有的路径带了、有的没带」。
 */
export function withLockLabel(args, exec) {
    const id = exec?.agent?.id;
    // 截到 64 字符：这是给人看的诊断信息，不值得为它让一条错误消息变成一屏。
    const label = typeof id === 'string' && id !== '' ? `session=${id.slice(0, 64)}` : '';
    return label === '' ? args : [...args, '--lock-label', label];
}
/** 把输入类 CLI 调用包进「占用中」窗口。 */
function runCliInputTracked(args, exec) {
    return withInputBusy(() => runCliInput(args, exec));
}
/**
 * 浏览器半边查询「宿主是否正在驱动输入设备」的路由。
 * 复用现有的剪贴板状态路由（页面本来就在每秒轮询它），不额外加一条要轮询的路由。
 */
function inputBusy() {
    return inputBusyCount > 0;
}
/** 运行一次用户级输入（python -m cvision.cli_input <args>），返回它的 stdout。 */
async function runCliInputText(args, exec) {
    await ensureRuntime(exec);
    assertCvisionPresent();
    const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_input', ...withLockLabel(args, exec)], {
        cwd: PY_CWD,
        env: PY_ENV,
        maxBuffer: 1 * 1024 * 1024,
        signal: exec.signal,
    });
    return stdout.trim();
}
/** 运行一次用户级输入，忽略输出。 */
async function runCliInput(args, exec) {
    await runCliInputText(args, exec);
}
/**
 * 运行一次输入类 CLI 并解析它的 JSON 输出。
 *
 * ``--focus`` / ``--ensure-front`` 的成败**只能**从这个 JSON 里读：失败时 CLI 以非零退出码收场
 * （这样即便调用方只看退出码也不会把「没置前」当成功），但**仍会在 stdout 上给出结构化原因**
 * （``ok:false`` + ``error``），所以这里连非零退出的情况也要把 stdout 捞回来解析。
 */
async function runCliInputJson(args, exec) {
    let out;
    try {
        out = await withInputBusy(() => runCliInputText(args, exec));
    }
    catch (error) {
        const stdout = String(error.stdout ?? '').trim();
        const parsed = parseJsonLoose(stdout);
        if (parsed)
            return parsed;
        throw error;
    }
    const parsed = parseJsonLoose(out);
    if (!parsed)
        throw new Error(`cvision 输入命令未返回 JSON：${out.slice(0, 200)}`);
    return parsed;
}
/**
 * 一次输入类 CLI 调用的出口。默认走 :func:`runCliInputJson`。
 *
 * **导出可变对象只为单测注入假实现**（同 `snipExecutor` 的套路）：真实输入会动用户的鼠标键盘，
 * CI 里不能跑；而「置前与动作必须落在**同一次**调用里」这条不变量也只有在这里能被机械地钉住——
 * 它一旦退回两次调用，跨进程就又有了缝，而那在真实使用中几乎看不出来。
 */
export const inputRunner = {
    run: (args, exec) => runCliInputJson(args, exec),
};
/** 宽松解析 CLI 的一行 JSON：解析不了就返回 null（由调用方决定怎么处理）。 */
function parseJsonLoose(text) {
    if (!text)
        return null;
    try {
        const value = JSON.parse(text);
        return value && typeof value === 'object' ? value : null;
    }
    catch {
        return null;
    }
}
/**
 * 「接下来要操作的那个窗口」：最近一次 ``see`` 看到的句柄，以及那张图覆盖的屏幕矩形
 * （`click_at` 按比例换算坐标的基准；模型说「点图的 64%、46%」时指的就是这张图）。
 *
 * 点击类工具靠句柄把窗口置前并校验坐标归属；置前过的窗口（``focus_window``）同样是操作目标。
 *
 * **导出可变对象只为单测注入与复位**（同 `snipExecutor` 的套路）：这两项状态只能由 ``see`` /
 * ``focus_window`` 更新，而「置前与动作必须落在同一次 CLI 调用里」这条不变量没有别的办法机械地钉住。
 * ⚠️ 它是**进程级**的，不区分会话——同一进程内的多个会话/子代理共用这一份记录（跨进程的互斥见
 * `cvision/input_lock.py`）。
 */
export const operationTarget = {
    handle: null,
    frame: null,
};
/**
 * 把一次抓取的结果记成「接下来的操作目标」——**每次抓取都是权威**。
 *
 * 结果里没有句柄/矩形就**清掉**旧值，绝不沿用上一次的。为什么必须清（v0.2.33 修的真实缺陷）：
 * `see(window=…)`（不带 text）与整屏 `see()` 在修复前拿不到句柄，于是旧句柄一直留着，
 * 后续 `type_text`/`press_key` 会把**上一个窗口**当成目标——它会真的把那个窗口置前并把按键
 * 送进去，而键盘类动作没有坐标可校验（CLI 判据是 `focused`），**整条链路报成功**。
 * 点击类动作虽会被前置校验拦下，但也会先真的动一下鼠标（`--unblock` 点标题栏），且报错指向错误的窗口。
 *
 * 注意「清掉」不是「降级」：没有目标窗口时 `runInputAction` 只跑动作本身（不带 `--ensure-front`），
 * 退化成「按坐标/焦点操作」；而 `click_at` 在没有基准矩形时会明确报错，不会拿上一张图的矩形去算坐标。
 *
 * @param handle - 这次抓到的窗口句柄；整屏抓取或拿不到时为 null。
 * @param frame - 这张图覆盖的屏幕矩形；拿不到时为 null。
 */
export function noteCaptureTarget(handle, frame) {
    operationTarget.handle = handle;
    operationTarget.frame = frame;
}
/**
 * 跑一次输入动作，并把「置前 + 坐标归属校验」**合并进同一次 CLI 调用**。
 *
 * 为什么必须合并（早期是「先调一次 `--ensure-front`、再调一次动作」）：跨进程输入互斥是在 CLI
 * 进程内取的（见 `cvision/input_lock.py`），两次独立调用之间**有缝**——另一个 DSH 实例正好在那条
 * 缝里把它的窗口置前，我们的坐标校验就白做了，这一击会落到它的窗口上（而坐标全对）。合并成一次
 * 调用后，置前、校验、动作落在**同一个持锁区间**内，跨进程也插不进来。
 *
 * ``--unblock`` 是**操作策略**的关键一环：程序化提升层叠顺序在 Windows 上并不成立（实测
 * ``SetWindowPos(HWND_TOP)`` / ``BringWindowToTop`` / ``SwitchToThisWindow`` 都返回成功却不改变
 * 层叠），只有**真实鼠标输入**才会让系统重排。所以目标被盖住时，插件会先点一下它自己的标题栏中央
 * ——正是人遇到这种情况会做的事——把它带到最前，再去点目标位置。代价是那一次点击会真的发生。
 */
async function runInputAction(cmd, exec, at) {
    const handle = operationTarget.handle;
    if (handle == null) {
        await inputRunner.run(cmd, exec);
        return;
    }
    const pre = ['--ensure-front', String(handle), '--unblock'];
    if (at)
        pre.push('--at', String(Math.round(at.x)), String(Math.round(at.y)));
    const info = await inputRunner.run([...pre, ...cmd], exec);
    if (info.ok)
        return;
    if (info.stale) {
        // 窗口已经不存在：这份记录过期了。清掉并只跑动作本身，否则它会永久卡住之后所有的点击。
        operationTarget.handle = null;
        await inputRunner.run(cmd, exec);
        return;
    }
    throw new Error(String(info.error ?? `窗口 0x${handle.toString(16)} 未能置前`));
}
/** 运行一次纯采集类 CLI（python -m cvision.cli_capture <args>），返回 stdout。 */
async function runCliCapture(args, exec) {
    assertCvisionPresent();
    const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_capture', ...args], {
        cwd: PY_CWD,
        env: PY_ENV,
        maxBuffer: 64 * 1024 * 1024,
        signal: exec.signal,
    });
    return stdout.trim();
}
class CvisionServer {
    defaultTimeoutMs;
    child = null;
    buffer = '';
    queue = [];
    busy = false;
    down = false;
    constructor(defaultTimeoutMs) {
        this.defaultTimeoutMs = defaultTimeoutMs;
    }
    ensure() {
        if (this.child)
            return;
        const child = spawn(PYTHON, ['-m', 'cvision.cli_server'], {
            cwd: PY_CWD,
            env: PY_ENV,
            stdio: ['pipe', 'pipe', 'pipe'],
        });
        this.child = child;
        child.stdout.setEncoding('utf8');
        child.stderr.setEncoding('utf8');
        child.stdout.on('data', (chunk) => {
            this.buffer += chunk;
            this.drainLines();
        });
        child.stderr.on('data', () => { });
        child.on('exit', () => this.fatal(new Error('cvision server exited')));
        child.on('error', (e) => this.fatal(new Error(`cvision server spawn failed: ${String(e)}`)));
    }
    drainLines() {
        for (;;) {
            const idx = this.buffer.indexOf('\n');
            if (idx < 0)
                break;
            const line = this.buffer.slice(0, idx).trim();
            this.buffer = this.buffer.slice(idx + 1);
            if (line)
                this.handleLine(line);
        }
    }
    handleLine(line) {
        let resp;
        try {
            resp = JSON.parse(line);
        }
        catch {
            return;
        }
        const next = this.queue.shift();
        if (!next)
            return;
        clearTimeout(next.timer);
        this.busy = false;
        if (resp.ok === false) {
            next.reject(new Error(String(resp.error ?? 'cvision server error')));
        }
        else {
            next.resolve(resp);
        }
        this.drain();
    }
    drain() {
        if (this.busy || this.queue.length === 0)
            return;
        if (!this.child)
            return;
        this.busy = true;
        this.child.stdin.write(JSON.stringify(this.queue[0].req) + '\n');
    }
    request(req, exec) {
        if (this.down)
            return Promise.reject(new Error('cvision server is down'));
        this.ensure();
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                // 超时视为 server 不可用：整体回收并回退 CLI，避免后续响应错位。
                this.fatal(new Error('cvision server timed out'));
                reject(new Error('cvision server timed out'));
            }, this.defaultTimeoutMs);
            const abort = () => reject(new Error('aborted'));
            exec?.signal?.addEventListener('abort', abort, { once: true });
            this.queue.push({
                req,
                resolve: (v) => {
                    exec?.signal?.removeEventListener('abort', abort);
                    resolve(v);
                },
                reject: (e) => {
                    exec?.signal?.removeEventListener('abort', abort);
                    reject(e);
                },
                timer,
            });
            this.drain();
        });
    }
    fatal(err) {
        this.down = true;
        for (const p of this.queue) {
            clearTimeout(p.timer);
            p.reject(err);
        }
        this.queue = [];
        this.busy = false;
        try {
            this.child?.kill();
        }
        catch {
            /* ignore */
        }
        this.child = null;
    }
    dispose() {
        try {
            this.child?.stdin.write(JSON.stringify({ op: 'quit' }) + '\n');
        }
        catch {
            /* ignore */
        }
        try {
            this.child?.kill();
        }
        catch {
            /* ignore */
        }
        this.child = null;
        this.down = true;
    }
}
/**
 * 浏览器半边查询「当前模型是否接受图片输入」的路由路径。
 * 必须与 `src/client.js` 的 CAPABILITY_PATH 保持一致。
 */
const MODEL_CAPABILITY_PATH = '/cvision/model-capability';
/**
 * 宿主侧的图片输入能力投影，供浏览器半边决定截图按钮是否出现。
 *
 * 判定口径与 Session 的图片准入保持一致：只有**显式声明**了 inputModalities 且
 * 不含 `image` 才算不收图；未声明（undefined）在 DSH 里仍会被准入放行，因此按
 * 「可收图」回答。解析不出该 provider/model 时返回 `source: 'unknown'`，交给客户端
 * 退回名字启发式，而不是谎报一个能力。
 * @param host - 已具备 webServer / llm 的作用域上下文。
 * @param provider - 模型提供方 id（来自浏览器目录快照）。
 * @param model - 模型 id（来自浏览器目录快照）。
 */
async function modelImageCapability(host, provider, model) {
    if (provider === '' || model === '')
        return { provider, model, image: false, source: 'unknown' };
    try {
        const info = await host.llm.resolveModelInfo(provider, model);
        const modalities = info.inputModalities;
        return {
            provider,
            model,
            image: modalities === undefined || modalities.includes('image'),
            source: 'declared',
            modalities: [...(modalities ?? [])],
        };
    }
    catch {
        return { provider, model, image: false, source: 'unknown' };
    }
}
/**
 * 浏览器半边请求「系统级框选截图」的路由路径。
 * 必须与 `src/client.js` 的 SNIP_PATH 保持一致。
 */
const SNIP_PATH = '/cvision/snip';
/**
 * 浏览器半边查询「剪贴板里有没有图片」的路由（页面每秒轮询）。
 * 只读且廉价：宿主只查剪贴板格式 + token，不解码图片。必须与 client.js 的 CLIPBOARD_STATE_PATH 一致。
 */
const CLIPBOARD_STATE_PATH = '/cvision/clipboard';
/** 取剪贴板图片的路由（用户长按按钮时才调用）。必须与 client.js 的 CLIPBOARD_IMAGE_PATH 一致。 */
const CLIPBOARD_IMAGE_PATH = '/cvision/clipboard/image';
/** 把任意抛出物整理成一行可读信息。 */
function errorMessage(error) {
    return error instanceof Error ? error.message : String(error);
}
/** 给路由回一个 JSON 响应（截图路由与能力路由共用）。 */
function sendJson(res, status, payload) {
    res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
    res.end(JSON.stringify(payload));
}
/**
 * 只接受同源请求：`Origin` 的 host 必须等于 `Host` 头。
 * 截图会拉起系统 UI 并读取剪贴板，属于高权限动作，不能给跨站页面开口子
 * （与 dshmarket 对写路由的做法一致）。
 */
function isSameOrigin(req) {
    const host = req.headers.host;
    const origin = req.headers.origin;
    if (host === undefined || origin === undefined)
        return false;
    try {
        return new URL(origin).host === host;
    }
    catch {
        return false;
    }
}
/** DSH 桌面版的页面源。桌面版用它承载渲染进程，并把非静态请求转发给回环 HTTP 服务。 */
const DESKTOP_APP_ORIGIN = 'dsh-app://app';
/** 对端是不是本机回环地址（IPv6 / IPv4 / IPv4 映射三种写法都认）。 */
function isLoopbackPeer(req) {
    const address = req.socket?.remoteAddress ?? '';
    return address === '::1' || address === '::ffff:127.0.0.1' || address.startsWith('127.');
}
/**
 * 高权限 POST 路由的调用方判定（系统截图 / 剪贴板取图）。放行三种情况：
 *
 * 1. `Origin` 存在且与 `Host` 同源 —— 浏览器**直接**访问 HTTP 端口的正常情形，web profile 走这条；
 * 2. `Origin` 是桌面版的页面源 `dsh-app://app` —— 桌面版页面直连时才会看到（见下）；
 * 3. `Origin` **缺失**且对端是**本机回环** —— 这正是 DSH 桌面版的转发路径：桌面版页面
 *    （`dsh-app://app`）的所有非静态请求都由主进程的 `forwardWebRequest` 转发到回环 HTTP 服务，
 *    而它**刻意删掉** `host`/`origin`/`cookie`/`sec-fetch-site` 再换上宿主自己的 cookie
 *    （`app.asar` 里 `forwardWebRequest` 的实现）；因此这条路径上永远看不到 `Origin`。
 *
 * 为什么这样切仍守得住原来的口子：**跨站页面的 POST 一定带 http(s) 的 `Origin`**，
 * 于是它只会落到规则 1/2 的比对里、比对不过就被拒；只读的 `Origin`-less 分支还额外限定了
 * 「对端在本机」——而本机进程本来就能直接调 `python -m cvision.cli_snip`（甚至任意别的命令），
 * 所以这条路由从来不是「防本机代码」的边界，它防的是**页面**。
 *
 * ⚠️ 曾经的实现只认规则 1，于是桌面版点上按钮必然 403 → 客户端回退 `getDisplayMedia`，
 * 而桌面版把抓屏权限全关了（`setPermissionCheckHandler(() => false)`、
 * `setDisplayMediaRequestHandler(cb => cb({}))`）——回退也是死的，表现就是「点了没反应」。
 */
function isTrustedRouteCaller(req) {
    const origin = req.headers.origin;
    if (origin === undefined || origin === '')
        return isLoopbackPeer(req);
    if (origin === DESKTOP_APP_ORIGIN)
        return true;
    return isSameOrigin(req);
}
/**
 * 只读路由的同源判定：**只拒绝明确跨站**的 GET（`Origin` 存在且 host ≠ `Host`）。
 *
 * 为什么不复用 :func:`isSameOrigin`（那条「缺 `Origin` 也拒绝」）：浏览器**不给同源 GET 加 `Origin`**
 * （只有 POST / CORS 请求才加）。照 POST 的口径卡 GET，会把插件自己的
 * `fetch('/cvision/model-capability')` 一起挡在门外，结果是能力查询失败、按钮退回名字启发式——
 * 比不设防更糟。而跨站页面发起的请求**一定**带 `Origin`（CORS 语义），所以「只卡带 Origin 且
 * 不匹配的」既守住了这条路由，也不误伤自己。
 *
 * @returns 该拒绝时为 true。
 */
function isCrossOriginRead(req) {
    const origin = req.headers.origin;
    if (origin === undefined || origin === '')
        return false;
    const host = req.headers.host;
    if (host === undefined)
        return true;
    try {
        return new URL(origin).host !== host;
    }
    catch {
        return true;
    }
}
/**
 * 运行一次返回 JSON 的 Python CLI，返回 stdout。
 *
 * **非零退出也返回 stdout**：本插件的 CLI 用退出码表达结果分类（`cli_snip` 2=cancelled、
 * 3=unsupported；`cli_clipboard` 同理），真正的契约在 stdout 的 JSON 里。用 `promisify(execFile)`
 * 直接 await 会在非零退出时 reject，把「用户取消」误判成故障。
 */
async function runPythonCli(module, args, exec) {
    await ensureRuntime(exec);
    assertCvisionPresent();
    try {
        const { stdout } = await execFileAsync(PYTHON, ['-m', module, ...args], {
            cwd: PY_CWD,
            env: PY_ENV,
            maxBuffer: 64 * 1024 * 1024,
            signal: exec.signal,
        });
        return stdout;
    }
    catch (error) {
        const stdout = error.stdout;
        if (typeof stdout === 'string' && stdout.trim() !== '')
            return stdout;
        throw error;
    }
}
/** 运行一次系统截图 CLI（python -m cvision.cli_snip），把 JSON 结果翻译成 SnipOutcome。 */
async function runSnipCli(exec) {
    let stdout;
    try {
        stdout = await runPythonCli('cvision.cli_snip', ['--timeout', '60'], exec);
    }
    catch (error) {
        // 用户中途取消（客户端断开 → signal 中止）不算故障。
        if (exec.signal.aborted)
            return { kind: 'cancelled' };
        return { kind: 'failed', message: errorMessage(error) };
    }
    let parsed;
    try {
        parsed = JSON.parse(stdout.trim());
    }
    catch {
        return { kind: 'failed', message: `无法解析 cli_snip 输出（${stdout.length} 字节）` };
    }
    if (parsed.ok === true) {
        const dataUrl = String(parsed.data_url ?? '');
        return dataUrl === '' ? { kind: 'failed', message: 'cli_snip 未返回图片数据' } : { kind: 'captured', dataUrl };
    }
    const reason = String(parsed.reason ?? '');
    const message = String(parsed.message ?? reason);
    if (reason === 'cancelled')
        return { kind: 'cancelled' };
    if (reason === 'unsupported')
        return { kind: 'unsupported', message };
    return { kind: 'failed', message };
}
/**
 * 截图执行器。默认实现 spawn 包内 Python cvision 拉起系统截图 UI。
 * **导出可变对象只为单测注入假实现**（真实实现需要桌面与人工框选，CI 里跑不了）。
 */
export const snipExecutor = {
    run: runSnipCli,
};
/** 最近一次 apply 创建的常驻 Python server（状态轮询优先走它，免得每秒冷启动一个解释器）。 */
let activeServer = null;
/**
 * 「我们自己刚（单击系统截图时）交给页面的那张剪贴板图片」的归属。
 *
 * 解决的问题：单击系统截图后，系统会把刚截的图放进剪贴板，于是剪贴板监视立刻亮起「长按插入剪贴板
 * 图片」——而那张图刚刚已经进了附件栏，提示自相矛盾。
 *
 * 归属条件（两条都必须有，少一条线上就会漏）：
 * 1. `snipInFlight` ——**整个截图请求期间**都归属。剪贴板是在截图返回**之前**被系统写入的，而用户
 *    拖框/标注可能好几秒，所以不能用「第一次轮询」或固定短窗口去覆盖这段（那种写法对慢截图必漏）；
 * 2. `snipServedUntil` —— 截图**完成后**的一小段窗口，覆盖截图工具打开编辑器等再次写剪贴板的行为。
 * 窗口过期后回到身份比对：剪贴板里还留着我们那张（token 相同）就仍算我们的，换成别家的图（token 变了）
 * 才重新点亮。
 */
const SNIP_SERVED_WINDOW_MS = 4000;
let snipInFlight = false;
let snipServedUntil = 0;
let snipServedToken = null;
/** 把 CLI/常驻进程返回的原始 JSON 收敛成 ClipboardState。 */
function toClipboardState(raw) {
    return {
        supported: raw.supported === true,
        image: raw.image === true,
        token: raw.token === undefined || raw.token === null ? null : String(raw.token),
        reason: String(raw.reason ?? ''),
    };
}
/** 查一次剪贴板状态：优先常驻 server，失败回退一次性 CLI。 */
async function clipboardState(server) {
    if (server !== null) {
        try {
            return toClipboardState(await server.request({ op: 'clipboard_state' }));
        }
        catch {
            /* 常驻进程不可用：回退 CLI */
        }
    }
    const stdout = await runPythonCli('cvision.cli_clipboard', ['--state'], { signal: AbortSignal.timeout(8000) });
    return toClipboardState(JSON.parse(stdout.trim()));
}
/** 取剪贴板图片（只在用户长按按钮时调用，所以一次性 CLI 足够）。 */
async function clipboardImage(exec) {
    let stdout;
    try {
        stdout = await runPythonCli('cvision.cli_clipboard', ['--image'], exec);
    }
    catch (error) {
        if (exec.signal.aborted)
            return { kind: 'failed', message: 'aborted' };
        return { kind: 'failed', message: errorMessage(error) };
    }
    let parsed;
    try {
        parsed = JSON.parse(stdout.trim());
    }
    catch {
        return { kind: 'failed', message: `无法解析 cli_clipboard 输出（${stdout.length} 字节）` };
    }
    if (parsed.ok === true) {
        const dataUrl = String(parsed.data_url ?? '');
        return dataUrl === '' ? { kind: 'failed', message: 'cli_clipboard 未返回图片数据' } : { kind: 'captured', dataUrl };
    }
    const reason = String(parsed.reason ?? '');
    const message = String(parsed.message ?? reason);
    if (reason === 'empty')
        return { kind: 'empty' };
    if (reason === 'unsupported')
        return { kind: 'unsupported', message };
    return { kind: 'failed', message };
}
/**
 * 剪贴板探测（状态 + 取图）。默认实现走包内 Python；**导出可变对象供单测注入假实现**
 * （真实实现要读系统剪贴板，CI 里没有桌面）。
 */
export const clipboardProbe = {
    state: () => clipboardState(activeServer),
    image: (exec) => clipboardImage(exec),
};
export function apply(ctx) {
    // 45s：`wait_changed` 会在 Python 侧阻塞轮询（默认 10s、上限由调用方的 timeout 决定），
    // 这个上限必须**大于**它，否则常驻 server 会被自己的超时回收，白等一场还回退到 CLI。
    const server = new CvisionServer(45000);
    // 剪贴板状态轮询优先复用这个常驻进程（每秒一次，冷启动一个解释器太贵）。
    activeServer = server;
    ctx.effect(() => () => {
        activeServer = null;
        server.dispose();
    });
    // ① 浏览器半边（输入框截图按钮）的权威能力通道。
    //    DSH 给浏览器的模型目录由 buildModelCatalog 主动剥掉了 inputModalities
    //    （只投影 id/name/description/reasoning），客户端因此无法自行判断当前模型
    //    收不收图——上游插件只能按模型名猜 vision|visual，既漏判也误判。这里由宿主
    //    按真实适配器目录回答。组合里没有 web 服务器（如 Electron/headless）时这段
    //    注册整体跳过，客户端会自动退回名字启发式。
    const injectHost = ctx.inject;
    injectHost(['webServer', 'llm'], (host) => {
        host.effect(() => host.webServer.register({
            kind: 'exact',
            path: MODEL_CAPABILITY_PATH,
            handler: async (req, res) => {
                // 只读、无副作用，但同样只服务同源页面：跨站请求（带不匹配 Origin）直接拒绝。
                if (isCrossOriginRead(req)) {
                    sendJson(res, 403, { message: 'cross-origin capability query refused' });
                    return;
                }
                const url = new URL(req.url ?? '/', 'http://localhost');
                const provider = url.searchParams.get('provider') ?? '';
                const model = url.searchParams.get('model') ?? '';
                sendJson(res, 200, await modelImageCapability(host, provider, model));
            },
        }), 'vision: model capability route');
    });
    // ② 截图按钮的默认通道：**系统级框选截图**。
    //    抓屏动作本身由用户在系统 UI 里完成（切片可标注），本路由只负责拉起 UI 并把
    //    用户刚框出来的那张图取回来交给浏览器半边——因此不需要绕过浏览器的截图授权模型，
    //    但仍然按高权限动作设防：仅 POST、仅同源、客户端断开即中止 Python 等待。
    injectHost(['webServer'], (host) => {
        host.effect(() => host.webServer.register({
            kind: 'exact',
            path: SNIP_PATH,
            handler: async (req, res) => {
                if (req.method !== 'POST') {
                    sendJson(res, 405, { message: 'system snip requires POST' });
                    return;
                }
                if (!isTrustedRouteCaller(req)) {
                    sendJson(res, 403, { message: 'cross-origin system snip refused' });
                    return;
                }
                const controller = new AbortController();
                req.on('close', () => {
                    controller.abort();
                });
                // ⚠️ 整个截图请求期间都标为「进行中」：系统是在截图返回**之前**把图写进剪贴板的，而用户
                // 拖框/标注可能好几秒，期间每一次每秒轮询都必须把当前剪贴板内容算成我们自己产出的。
                snipInFlight = true;
                let outcome;
                try {
                    outcome = await snipExecutor.run({ signal: controller.signal });
                }
                finally {
                    // 必须复位：它一旦永久为真，之后**任何**剪贴板图片都会被当成「我们自己刚产出的」，
                    // 按钮再也不亮、长按永远不触发，直到宿主重启。默认执行器自己吞异常，但它是可注入的
                    // （单测/扩展会替换它），路由这里不能假设「不会抛」。
                    snipInFlight = false;
                }
                if (outcome.kind === 'captured') {
                    // 成功后留一个短窗口：截图工具打开编辑器等可能又写一次剪贴板。
                    snipServedUntil = Date.now() + SNIP_SERVED_WINDOW_MS;
                    try {
                        const { data, mediaType } = parseDataUrl(outcome.dataUrl);
                        res.writeHead(200, { 'content-type': mediaType, 'cache-control': 'no-store' });
                        res.end(Buffer.from(data));
                    }
                    catch (error) {
                        sendJson(res, 500, { message: errorMessage(error) });
                    }
                    return;
                }
                if (outcome.kind === 'cancelled') {
                    res.writeHead(204);
                    res.end();
                    return;
                }
                if (outcome.kind === 'unsupported') {
                    sendJson(res, 501, { message: outcome.message });
                    return;
                }
                sendJson(res, 500, { message: outcome.message });
            },
        }), 'vision: system snip route');
    });
    // ③ 剪贴板监视：支撑「剪贴板里出现新图片时按钮变色、长按插入」。
    //    浏览器**无法**在后台读剪贴板（需要用户手势与授权，也没有变更事件），所以由宿主代查：
    //    状态路由廉价（只查格式 + token，不解码），取图路由只在用户长按按钮时调用。
    injectHost(['webServer'], (host) => {
        host.effect(() => {
            const disposeState = host.webServer.register({
                kind: 'exact',
                path: CLIPBOARD_STATE_PATH,
                handler: async (req, res) => {
                    if (req.method !== 'GET') {
                        sendJson(res, 405, { message: 'clipboard state is GET-only' });
                        return;
                    }
                    try {
                        const state = await clipboardProbe.state();
                        const now = Date.now();
                        // 归属：截图请求进行中，或刚完成后的短窗口内 → 此刻剪贴板里那张就是我们产出的。
                        const ours = snipInFlight || now < snipServedUntil;
                        if (ours)
                            snipServedToken = state.token;
                        sendJson(res, 200, {
                            ...state,
                            // `served`：这张图是不是我们自己刚（单击系统截图时）交给页面的？客户端据此不提示。
                            served: state.token !== null && (ours || state.token === snipServedToken),
                            // `busy`：宿主正在操作鼠标/键盘。物理上只有一套输入设备，所以要如实告诉用户
                            // 「现在别碰」——这比试图抢互斥锁安全（锁一旦没释放会把插件卡死）。
                            busy: inputBusy(),
                        });
                    }
                    catch (error) {
                        sendJson(res, 500, { message: errorMessage(error) });
                    }
                },
            });
            const disposeImage = host.webServer.register({
                kind: 'exact',
                path: CLIPBOARD_IMAGE_PATH,
                handler: async (req, res) => {
                    if (req.method !== 'POST') {
                        sendJson(res, 405, { message: 'clipboard image requires POST' });
                        return;
                    }
                    if (!isTrustedRouteCaller(req)) {
                        sendJson(res, 403, { message: 'cross-origin clipboard read refused' });
                        return;
                    }
                    const controller = new AbortController();
                    req.on('close', () => {
                        controller.abort();
                    });
                    const outcome = await clipboardProbe.image({ signal: controller.signal });
                    if (outcome.kind === 'captured') {
                        try {
                            const { data, mediaType } = parseDataUrl(outcome.dataUrl);
                            res.writeHead(200, { 'content-type': mediaType, 'cache-control': 'no-store' });
                            res.end(Buffer.from(data));
                        }
                        catch (error) {
                            sendJson(res, 500, { message: errorMessage(error) });
                        }
                        return;
                    }
                    if (outcome.kind === 'empty') {
                        res.writeHead(204);
                        res.end();
                        return;
                    }
                    if (outcome.kind === 'unsupported') {
                        sendJson(res, 501, { message: outcome.message });
                        return;
                    }
                    sendJson(res, 500, { message: outcome.message });
                },
            });
            return () => {
                disposeState();
                disposeImage();
            };
        }, 'vision: clipboard routes');
    });
    // ① server 优先，失败回退 CLI 的采集类辅助（每个返回统一形态）。
    //    这些是**所有 Python 依赖工具的唯一收口**（16 个工具里除 cvision_status 外都经这里，
    //    输入类与剪贴板类各有一处），所以体检门只需加在这 5 个函数上，不必逐个工具去改。
    /** 把 CLI/server 回报的窗口句柄转成正整数；缺失或非法时返回 null。 */
    function positiveInt(value) {
        const n = Number(value);
        return Number.isFinite(n) && n > 0 ? Math.trunc(n) : null;
    }
    /** 把 CLI/server 回报的 `image_screen_box` 收成屏幕矩形；缺失或非法时返回 null。 */
    function toFrame(value) {
        if (!value || typeof value !== 'object')
            return null;
        const v = value;
        const x = Number(v.x);
        const y = Number(v.y);
        const width = Number(v.width);
        const height = Number(v.height);
        if (![x, y, width, height].every((n) => Number.isFinite(n)))
            return null;
        if (width <= 0 || height <= 0)
            return null;
        return { x, y, width, height };
    }
    /**
     * 抓一张图；`args.text` 为真时**顺带**返回可点击元素（一次调用拿两样东西）。
     *
     * 两条路径形状一致：常驻 server 的 `{op:'capture',text:true}` 与 CLI 的 `--text` 都返回
     * `{ok,kind,data_url,width,height,elements}`。**不这么做的话**，`see(text=true)` 就得先
     * 截一次图、再 OCR 一次，两次截屏之间画面可能已经变了，坐标与图片就对不上了。
     *
     * 另外回报两样东西：**目标窗口句柄**（`handle`，点击前置前与坐标校验靠它）与**图片屏幕矩形**
     * （`image_screen_box`，按比例点击靠它）。两者都在 OCR 失败时依然有效。
     */
    async function captureDataUrl(args, exec) {
        await ensureRuntime(exec);
        if (args.text) {
            try {
                const resp = await server.request({ op: 'capture', ...args }, exec);
                return {
                    dataUrl: String(resp.data_url ?? ''),
                    elements: Array.isArray(resp.elements) ? resp.elements : [],
                    handle: positiveInt(resp.handle),
                    frame: toFrame(resp.image_screen_box),
                };
            }
            catch {
                const cli = buildCaptureCli(args);
                cli.push('--text');
                const out = await runCliCapture(cli, exec);
                const info = JSON.parse(out);
                return {
                    dataUrl: String(info.data_url ?? ''),
                    elements: Array.isArray(info.elements) ? info.elements : [],
                    handle: positiveInt(info.handle),
                    frame: toFrame(info.image_screen_box),
                };
            }
        }
        try {
            const resp = await server.request({ op: 'capture', ...args }, exec);
            return {
                dataUrl: String(resp.data_url ?? ''),
                elements: [],
                handle: positiveInt(resp.handle),
                frame: toFrame(resp.image_screen_box),
            };
        }
        catch {
            // CLI 回退路径也要给出**句柄与屏幕矩形**（`--json`）：拿不到它们的话，这次抓取就只剩一张图，
            // 后续 click/type_text 只能沿用上一次的目标窗口——那正是 v0.2.33 修的「键盘输入静默进错窗口」。
            // 裸 data URL 仍是 `--json` 缺省时的输出（既有契约不变，见 README 的内部 CLI 契约表）。
            const cli = buildCaptureCli(args);
            cli.push('--json');
            const info = JSON.parse(await runCliCapture(cli, exec));
            return {
                dataUrl: String(info.data_url ?? ''),
                elements: [],
                handle: positiveInt(info.handle),
                frame: toFrame(info.image_screen_box),
            };
        }
    }
    /** 把 see/ocr 的公共参数拼成 cli_capture 的参数表（两条分支共用，避免漂移）。 */
    function buildCaptureCli(args) {
        const cli = ['--format', String(args.format ?? 'PNG')];
        if (args.handle != null)
            cli.push('--handle', String(args.handle));
        if (args.window)
            cli.push('--window', String(args.window));
        if (args.maximize)
            cli.push('--maximize');
        if (args.region)
            cli.push('--region', String(args.region));
        if (args.delay)
            cli.push('--delay', String(args.delay));
        return cli;
    }
    async function ocrJson(args, exec) {
        await ensureRuntime(exec);
        try {
            const resp = await server.request({ op: 'ocr', ...args }, exec);
            return {
                text: String(resp.text ?? ''),
                lines: Array.isArray(resp.lines) ? resp.lines : [],
                words: Array.isArray(resp.words) ? resp.words : [],
            };
        }
        catch {
            const cli = [];
            if (args.handle != null)
                cli.push('--handle', String(args.handle));
            if (args.window)
                cli.push('--window', String(args.window));
            if (args.maximize)
                cli.push('--maximize');
            if (args.region)
                cli.push('--region', String(args.region));
            if (args.delay)
                cli.push('--delay', String(args.delay));
            const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_ocr', ...cli], {
                cwd: PY_CWD,
                env: PY_ENV,
                maxBuffer: 4 * 1024 * 1024,
                signal: exec.signal,
            });
            const info = JSON.parse(stdout);
            return { text: info.text ?? '', lines: info.lines ?? [], words: info.words ?? [] };
        }
    }
    async function listWindowsJson(exec) {
        await ensureRuntime(exec);
        try {
            const resp = await server.request({ op: 'list' }, exec);
            return Array.isArray(resp.windows) ? resp.windows : [];
        }
        catch {
            const out = await runCliCapture(['--list'], exec);
            return JSON.parse(out);
        }
    }
    async function screenInfoJson(exec) {
        await ensureRuntime(exec);
        try {
            const resp = await server.request({ op: 'screen_info' }, exec);
            return Array.isArray(resp.displays) ? resp.displays : [];
        }
        catch {
            const out = await runCliCapture(['--screen-info'], exec);
            return JSON.parse(out);
        }
    }
    /**
     * 轮询直到画面变化（供 `wait_until_changed`）。server 优先，失败回退一次性 CLI。
     * server 侧这个 op 会阻塞等待，所以上面 `new CvisionServer(45000)` 的超时必须更大。
     */
    async function waitChangedJson(args, exec) {
        await ensureRuntime(exec);
        try {
            const resp = await server.request({ op: 'wait_changed', ...args }, exec);
            return {
                dataUrl: String(resp.data_url ?? ''),
                meta: waitChangedMeta(resp),
                handle: positiveInt(resp.handle),
                frame: toFrame(resp.image_screen_box),
            };
        }
        catch {
            const cli = buildCaptureCli(args);
            cli.push('--wait-changed');
            if (args.interval)
                cli.push('--interval', String(args.interval));
            if (args.timeout)
                cli.push('--timeout', String(args.timeout));
            if (args.threshold != null)
                cli.push('--threshold', String(args.threshold));
            const out = await runCliCapture(cli, exec);
            const info = JSON.parse(out);
            return {
                dataUrl: String(info.data_url ?? ''),
                meta: waitChangedMeta(info),
                handle: positiveInt(info.handle),
                frame: toFrame(info.image_screen_box),
            };
        }
    }
    /**
     * 轮询直到画面**连续若干次不再变化**（供 `wait_until_stable`）。server 优先，失败回退一次性 CLI。
     *
     * server 侧这个 op 同样会阻塞等待，所以 `new CvisionServer(45000)` 的超时必须更大。
     */
    async function waitStableJson(args, exec) {
        await ensureRuntime(exec);
        try {
            const resp = await server.request({ op: 'wait_stable', ...args }, exec);
            return {
                dataUrl: String(resp.data_url ?? ''),
                meta: stableMeta(resp),
                handle: positiveInt(resp.handle),
                frame: toFrame(resp.image_screen_box),
            };
        }
        catch {
            const cli = buildCaptureCli(args);
            cli.push('--wait-stable');
            if (args.interval)
                cli.push('--interval', String(args.interval));
            if (args.timeout)
                cli.push('--timeout', String(args.timeout));
            if (args.threshold != null)
                cli.push('--threshold', String(args.threshold));
            if (args.stable_samples != null)
                cli.push('--stable-samples', String(args.stable_samples));
            const out = await runCliCapture(cli, exec);
            const info = JSON.parse(out);
            return {
                dataUrl: String(info.data_url ?? ''),
                meta: stableMeta(info),
                handle: positiveInt(info.handle),
                frame: toFrame(info.image_screen_box),
            };
        }
    }
    async function statusJson(exec) {
        // ⚠️ 刻意**不**过体检门：cvision_status 是体检/排错工具，环境不完整时它正是
        // 「唯一还能用」的那条路（cli_capture --status 本身不需要 Pillow）。gate 了它，
        // 用户就失去了查出问题的手段。
        try {
            const resp = await server.request({ op: 'status' }, exec);
            // 这里能成功读到体检表 = 环境此刻是好的：把之前**失败**的负缓存清掉，
            // 于是下一次 see/ocr 会重新探测，而不是继续报「依赖缺失」（用户刚跑完 pip install 的常见情形）。
            invalidateFailedRuntimeProbe();
            return resp.status ?? {};
        }
        catch {
            const out = await runCliCapture(['--status'], exec);
            invalidateFailedRuntimeProbe();
            return JSON.parse(out);
        }
    }
    // ── see ────────────────────────────────────────────────────────────────────
    ctx.tools.register(defineTool({
        name: 'see',
        description: '截取整个屏幕或某个窗口，并把截图以图片形式返回，让模型直接查看画面内容（描述、识别截图文字、读取图表/文档）。' +
            '用 window 指定窗口标题子串（如 "Visual Studio Code"），或用 handle 传入 list_windows 给出的精确句柄（更可靠，避免标题撞车）；留空则截全屏。' +
            '默认尽量别传 maximize=true：非最小化窗口会直接抓到其真实内容，且不切换前台、不抢焦点。' +
            '⚠️ 但「不抢前台」只对**普通窗口**成立：目标**处于最小化**时，抓取会先把它还原、因而**会抢走前台**' +
            '（抓完几何会还原成最小化，但前台已经变了）；兜底抓取路径（WGC 与 PrintWindow 都失败、改读合成桌面区域）' +
            '同样会置前。所以不要假设抓完前台没变——尤其在用户正在别处打字时。' +
            '这些「会动前台」的抓取会先取**跨进程输入锁**（短等待）：另一个会话/实例正在操作电脑时，' +
            '它会**如实报错**而不是硬抓（否则画面与坐标都会错），稍后重试即可。' +
            '仅当窗口已最小化/太小/被遮挡看不清时才用 maximize=true（截图后会自动还原原状态）。' +
            '传 ocr=true 可在返回图片的同时附带 OCR 文本/词框（省去一次 ocr 调用）。' +
            '传 text=true 会额外返回「可点击元素」列表（每个带 text + screen_center 屏幕绝对坐标，可直接传给 click）——' +
            '要点击界面上某个按钮/输入框时用这个，**不要**自己从截图里估算像素：截图内坐标与屏幕坐标之间存在裁剪、窗口位置、多屏与 DPI 缩放差异，已由本工具换算好。' +
            '⚠️ 还有一点：坐标本身是对的，而点击按屏幕坐标下发、只命中**前台窗口**——这一条插件已自动兜住：' +
            'click/double_click/drag/scroll/type_text/press_key 之前会把「你最近一次 see 的那个窗口」置前，' +
            '并校验点击坐标确实落在它上面；校验不过就直接报错，不会静默点偏。' +
            '所以标准流程就是 see 之后直接点它给的 screen_center。要操作的是别的窗口时，先 see 那一个。' +
            '若某处**没识别出文字**（纯图标、自绘界面、或元素被截断没列出来），可以用 click_at(rx, ry) 按' +
            '**比例**点这张图上的位置（rx/ry 为 0~1）：你只需要从图上读出「大约在横向几成、纵向几成」，' +
            '换算由插件完成，你不需要知道这张图被缩放成了多少像素。',
        parameters: {
            window: { type: 'string', description: '窗口标题子串（忽略大小写）；留空则截整屏' },
            handle: { type: 'integer', description: '窗口句柄（来自 list_windows），比 window 更精确；与 window 二选一，优先 handle' },
            maximize: {
                type: 'boolean',
                description: '是否先最大化目标窗口再截图。默认 false：非最小化窗口无需最大化且不切前台；仅当窗口太小/被遮挡看不清时设 true（抓后还原）',
            },
            region: { type: 'string', description: '裁剪区域 x,y,w,h（像素，相对截图），只抓窗口内一小块，省 token' },
            delay: { type: 'number', description: '抓取前等待毫秒（给需要渲染的内容），可选' },
            format: { type: 'string', description: 'PNG/JPEG/WEBP，默认 PNG' },
            ocr: { type: 'boolean', description: '可选：同一截图额外做 OCR 并返回 text/lines/words' },
            text: {
                type: 'boolean',
                description: '可选：返回可点击元素（每个含 text 与屏幕绝对坐标 screen_center，可直接喂给 click）。与 ocr 的区别：ocr 给词框（图片坐标），text 给合并后的控件与屏幕坐标',
            },
            max_elements: {
                type: 'integer',
                description: 'text=true 时最多返回多少个元素（默认 60，防止刷屏；按从上到下、从左到右取前 N 个）。列表被截断时结果里会写明总数与补齐办法',
            },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ref: { type: 'object', additionalProperties: true },
                    text: { type: 'string' },
                    lines: { type: 'array', items: { type: 'string' } },
                    words: { type: 'array', items: { type: 'object', additionalProperties: true } },
                    elements: { type: 'array', items: { type: 'object', additionalProperties: true } },
                    element_total: { type: 'integer' },
                },
                additionalProperties: false,
            },
            render: (_args, value) => {
                const blocks = [
                    { type: 'image', attachment: value.ref },
                ];
                const text = String(value.text ?? '');
                if (text)
                    blocks.push({ type: 'text', text });
                const words = Array.isArray(value.words) ? value.words : [];
                if (words.length) {
                    blocks.push({ type: 'text', text: '\nword_boxes (x,y,w,h):\n' + JSON.stringify(words, null, 2) });
                }
                const elements = Array.isArray(value.elements) ? value.elements : [];
                if (elements.length) {
                    // 只给模型真正要用的：点哪个、点哪里。图片坐标 box 在这里没用（click 吃屏幕坐标），
                    // 全塞进去只会白占 context。
                    const rows = elements.map((element) => {
                        const point = (element.screen_center ?? {});
                        return `  ${String(element.text ?? '')}  → click(${String(point.x ?? '?')}, ${String(point.y ?? '?')})`;
                    });
                    const total = Number(value.element_total ?? elements.length);
                    // 截断时必须告诉模型**怎么把剩下的拿到**：元素按 (y,x) 排序，列表长（实测一个资源管理器
                    // 窗口就有 100+ 个）时前 N 个只覆盖屏幕上半部分，模型会以为「目标不在这个界面上」。
                    const head = total > elements.length
                        ? `\nclickable elements（${elements.length}/${total}，已截断；要拿全请传 max_elements=${total}）:`
                        : `\nclickable elements（${elements.length}）:`;
                    blocks.push({ type: 'text', text: head + '\n' + rows.join('\n') });
                }
                return blocks;
            },
        },
        timeoutMs: 90000,
        async execute(args, exec) {
            assertCvisionPresent();
            const req = { format: (args.format ?? 'PNG').toUpperCase() };
            if (args.handle != null)
                req.handle = args.handle;
            if (args.window)
                req.window = String(args.window);
            if (args.maximize)
                req.maximize = true;
            if (args.region)
                req.region = String(args.region);
            if (args.delay)
                req.delay = Number(args.delay);
            if (args.text)
                req.text = true;
            const { dataUrl, elements, handle, frame } = await captureDataUrl(req, exec);
            // 记住这次看的是哪个窗口：后续 click/scroll/drag/type_text 会先把它置前、并校验点击坐标
            // 确实属于它。返回值没带 handle 时退回参数里的 handle（例如 see(handle=H) 不带 text 的用法）。
            const target = handle ?? positiveInt(args.handle);
            // 这次抓取就是权威：拿不到句柄/矩形就清掉旧值（见 noteCaptureTarget 的说明）。
            noteCaptureTarget(target, frame);
            const { data, mediaType, ext } = parseDataUrl(dataUrl);
            const ref = await ctx.attachments.saveImage({ data, mediaType, name: `vision-capture.${ext}` });
            const out = {
                ref: ref,
            };
            if (args.ocr) {
                const o = await ocrJson(req, exec);
                out.text = o.text;
                out.lines = o.lines;
                out.words = o.words;
            }
            if (args.text) {
                // 元素已按 (y, x) 排序，取前 N 个即「从上到下、从左到右」，正好是最可能先被点的区域。
                const limit = Math.max(1, Number(args.max_elements ?? 60));
                out.elements = elements.slice(0, limit);
                out.element_total = elements.length;
            }
            return out;
        },
    }));
    // ── wait_until_changed（轮询到变化才返回，省 token） ────────────────────────
    ctx.tools.register(defineTool({
        name: 'wait_until_changed',
        description: '轮询截图，直到画面**真的变了**才把那一帧作为图片返回（等进度条跑完、等弹窗出现、等加载完成）。' +
            '比连着截好几张图都塞给模型省得多：你不需要看 N 张相似图，只需要知道「变没变、变在哪」。' +
            '返回 changed（是否等到变化）/ diff_ratio（变化像素占比）/ diff_bbox（把所有变化包在一起的区域）/ ' +
            'diff_boxes（**分开的**变化区域，可能有多处；返回的图里已用红框一一标出，直接看图比读坐标可靠）。' +
            '默认阈值 0.01 是实测调出来的：光标与文本插入符闪烁约占 0.5%，阈值必须高于它，否则第一次轮询就会返回「变了」。' +
            '要更灵敏就调低 threshold，但请同时用 region 把会闪烁的区域排除掉。超时未变化也会返回当前画面（changed=false）。',
        parameters: {
            window: { type: 'string', description: '窗口标题子串；留空则监视整屏' },
            handle: { type: 'integer', description: '窗口句柄（来自 list_windows），优先于 window' },
            maximize: { type: 'boolean', description: '是否先最大化目标窗口（抓后还原）' },
            region: { type: 'string', description: '只监视这一块 x,y,w,h（强烈建议：既省算力又能避开闪烁区域）' },
            interval: { type: 'number', description: '采样间隔毫秒，默认 500' },
            timeout: { type: 'number', description: '总超时毫秒，默认 10000（到这里即使没变也返回当前画面）' },
            threshold: { type: 'number', description: '变化像素占比阈值，默认 0.01（1%）；调低更灵敏但更容易被闪烁误触发' },
            format: { type: 'string', description: 'PNG/JPEG/WEBP，默认 PNG' },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ref: { type: 'object', additionalProperties: true },
                    changed: { type: 'boolean' },
                    samples: { type: 'integer' },
                    elapsed_ms: { type: 'integer' },
                    diff_ratio: { type: 'number' },
                    mean_diff: { type: 'number' },
                    diff_bbox: { type: 'object', additionalProperties: true },
                    // 变化区域的**多个**框（分开的改动分别成框）；返回的图里已用红框标出它们。
                    diff_boxes: { type: 'array', items: { type: 'object', additionalProperties: true } },
                    // Python 侧每次都会给出截图尺寸；漏声明会被 additionalProperties: false 判非法。
                    width: { type: 'integer' },
                    height: { type: 'integer' },
                },
                additionalProperties: false,
            },
            render: (_args, value) => {
                const changed = value.changed === true;
                const ratio = Number(value.diff_ratio ?? 0);
                const head = changed
                    ? `画面已变化（差异 ${(ratio * 100).toFixed(2)}% 像素，用了 ${String(value.elapsed_ms ?? '?')}ms / ${String(value.samples ?? '?')} 次采样）`
                    : `超时且画面未变化（最大差异 ${(ratio * 100).toFixed(2)}% 像素，${String(value.elapsed_ms ?? '?')}ms / ${String(value.samples ?? '?')} 次采样）`;
                const box = value.diff_bbox;
                const boxes = value.diff_boxes;
                const count = Array.isArray(boxes) ? boxes.length : 0;
                const detail = box
                    ? `\n变化区域（原图坐标）: x=${box.x} y=${box.y} w=${box.w} h=${box.h}` +
                        (count > 1 ? `；共 ${count} 处，返回的图里已用红框标出` : '')
                    : '';
                return [
                    { type: 'image', attachment: value.ref },
                    { type: 'text', text: head + detail },
                ];
            },
        },
        timeoutMs: 60000,
        async execute(args, exec) {
            assertCvisionPresent();
            const req = { format: (args.format ?? 'PNG').toUpperCase() };
            if (args.handle != null)
                req.handle = args.handle;
            if (args.window)
                req.window = String(args.window);
            if (args.maximize)
                req.maximize = true;
            if (args.region)
                req.region = String(args.region);
            if (args.interval)
                req.interval = Number(args.interval);
            if (args.timeout)
                req.timeout = Number(args.timeout);
            if (args.threshold != null)
                req.threshold = Number(args.threshold);
            const { dataUrl, meta, handle, frame } = await waitChangedJson(req, exec);
            // `wait_*` 同样是一次抓取：它返回的是一张**新图**，比例点击的基准必须跟着换成这一张
            // （否则随后的 click_at 会按上一次 see 的矩形换算，位置整体偏）。拿不到就清空，绝不留旧值。
            noteCaptureTarget(handle, frame);
            const { data, mediaType, ext } = parseDataUrl(dataUrl);
            const ref = await ctx.attachments.saveImage({ data, mediaType, name: `vision-wait.${ext}` });
            return { ref: ref, ...meta };
        },
    }));
    // ── wait_until_stable（等画面安静下来：加载完成 / 动画结束） ─────────────────
    ctx.tools.register(defineTool({
        name: 'wait_until_stable',
        description: '轮询截图，直到画面**连续若干次不再变化**才返回（等加载完成、等列表渲染完、等动画停下）。' +
            '和 wait_until_changed 是一对：那个等「**开始**变」（等弹窗出现），这个等「**变完**」。' +
            '⚠️ 判断「变完了」只能用这个：知道「变过一次」并不等于「变完了」，拿 wait_until_changed 去猜加载是否结束，' +
            '只能反复 see 复查、白烧轮次。' +
            '返回 stable（超时前是否安静下来）/ diff_ratio（最后一对采样的差异）/ max_diff_ratio（过程中最大差异）' +
            '/ stable_for（最终连续安静了几次）。stable=false 表示超时了画面还在变。' +
            '**强烈建议配 region 只盯结果区**：别处的光标闪烁、时钟走字会让整屏永远「不稳定」。',
        parameters: {
            window: { type: 'string', description: '窗口标题子串；留空则监视整屏' },
            handle: { type: 'integer', description: '窗口句柄（来自 list_windows），优先于 window' },
            maximize: { type: 'boolean', description: '是否先最大化目标窗口（抓后还原）' },
            region: { type: 'string', description: '只监视这一块 x,y,w,h（**强烈建议**：只盯结果区才能忽略别处闪烁）' },
            interval: { type: 'number', description: '采样间隔毫秒，默认 300（比 wait_until_changed 更密，免得漏掉中间的变化）' },
            stable_samples: { type: 'integer', description: '连续多少次「没变」才算稳定，默认 3（约 0.9 秒安静期）' },
            timeout: { type: 'number', description: '总超时毫秒，默认 15000；到这里仍不稳定就返回 stable=false 与当前画面' },
            threshold: { type: 'number', description: '变化像素占比阈值，默认 0.01（1%）' },
            format: { type: 'string', description: 'PNG/JPEG/WEBP，默认 PNG' },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ref: { type: 'object', additionalProperties: true },
                    stable: { type: 'boolean' },
                    samples: { type: 'integer' },
                    elapsed_ms: { type: 'integer' },
                    diff_ratio: { type: 'number' },
                    max_diff_ratio: { type: 'number' },
                    stable_for: { type: 'integer' },
                    // 与 wait_until_changed 同理：Python 每次都会给截图尺寸，漏声明即非法。
                    width: { type: 'integer' },
                    height: { type: 'integer' },
                },
                additionalProperties: false,
            },
            render: (_args, value) => {
                const stable = value.stable === true;
                const quiet = Number(value.stable_for ?? 0);
                const elapsed = String(value.elapsed_ms ?? '?');
                const samples = String(value.samples ?? '?');
                const head = stable
                    ? `画面已稳定（连续 ${quiet} 次无变化，${elapsed}ms / ${samples} 次采样）`
                    : `超时仍不稳定：最大差异 ${(Number(value.max_diff_ratio ?? 0) * 100).toFixed(2)}% 像素（${elapsed}ms / ${samples} 次采样），画面一直在变`;
                return [
                    { type: 'image', attachment: value.ref },
                    { type: 'text', text: head },
                ];
            },
        },
        timeoutMs: 60000,
        async execute(args, exec) {
            assertCvisionPresent();
            const req = { format: (args.format ?? 'PNG').toUpperCase() };
            if (args.handle != null)
                req.handle = args.handle;
            if (args.window)
                req.window = String(args.window);
            if (args.maximize)
                req.maximize = true;
            if (args.region)
                req.region = String(args.region);
            if (args.interval)
                req.interval = Number(args.interval);
            if (args.stable_samples != null)
                req.stable_samples = Number(args.stable_samples);
            if (args.timeout)
                req.timeout = Number(args.timeout);
            if (args.threshold != null)
                req.threshold = Number(args.threshold);
            const { dataUrl, meta, handle, frame } = await waitStableJson(req, exec);
            // 同上：等待「变完」之后，模型最自然的下一步就是按比例点结果区，基准必须换成这张图。
            noteCaptureTarget(handle, frame);
            const { data, mediaType, ext } = parseDataUrl(dataUrl);
            const ref = await ctx.attachments.saveImage({ data, mediaType, name: `vision-stable.${ext}` });
            return { ref: ref, ...meta };
        },
    }));
    // ── ocr（返回 text/lines/words） ──────────────────────────────────────────
    ctx.tools.register(defineTool({
        name: 'ocr',
        description: '截取屏幕/窗口（可 region/delay），用 OCR 识别其中的文字并**返回文本**（含词级边界框 words）。' +
            '⚠️ `words` 是**图片坐标**，**不要**用它来算点击位置——那需要叠加裁剪/窗口/多屏/DPI 四层偏移，极易算错；' +
            '要点击请用 `see(text=true)`，它给的 `screen_center` 已经是屏幕绝对坐标、且把同行相邻词合并成了控件。' +
            '`ocr` 的用途是**取文字**（读内容、取词级粒度），不是定位。' +
            '参数同 see（window/title/maximize/region/delay）。',
        parameters: {
            window: { type: 'string', description: '窗口标题子串；留空则对整屏 OCR' },
            handle: { type: 'integer', description: '窗口句柄（来自 list_windows），比 window 更精确；与 window 二选一，优先 handle' },
            maximize: { type: 'boolean', description: '是否先最大化目标窗口再截（抓后还原）' },
            region: { type: 'string', description: '裁剪区域 x,y,w,h（像素，相对截图）' },
            delay: { type: 'number', description: '抓取前等待毫秒，可选' },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    text: { type: 'string' },
                    lines: { type: 'array', items: { type: 'string' } },
                    words: { type: 'array', items: { type: 'object', additionalProperties: true } },
                },
                additionalProperties: false,
            },
            render: (_args, value) => {
                const text = String(value.text ?? '');
                const lines = Array.isArray(value.lines) ? value.lines.filter(Boolean) : [];
                const blocks = [
                    { type: 'text', text: lines.length > 1 ? lines.join('\n') : text },
                ];
                const words = Array.isArray(value.words) ? value.words : [];
                if (words.length) {
                    blocks.push({ type: 'text', text: '\nword_boxes (x,y,w,h):\n' + JSON.stringify(words, null, 2) });
                }
                return blocks;
            },
        },
        timeoutMs: 90000,
        async execute(args, exec) {
            assertCvisionPresent();
            const req = {};
            if (args.handle != null)
                req.handle = args.handle;
            if (args.window)
                req.window = String(args.window);
            if (args.maximize)
                req.maximize = true;
            if (args.region)
                req.region = String(args.region);
            if (args.delay)
                req.delay = Number(args.delay);
            return await ocrJson(req, exec);
        },
    }));
    // ── list_windows ──────────────────────────────────────────────────────────
    ctx.tools.register(defineTool({
        name: 'list_windows',
        description: '列出当前可见的顶层 Windows 窗口（标题 + 句柄 + 尺寸）。用于让模型先找到要看的窗口，' +
            '再把对应标题传给 see 做截图/视觉分析。',
        parameters: {},
        output: {
            schema: {
                type: 'object',
                properties: { windows: { type: 'array', items: { type: 'object', additionalProperties: true } } },
                additionalProperties: false,
            },
            render: (_args, value) => [{ type: 'text', text: JSON.stringify(value.windows, null, 2) }],
        },
        timeoutMs: 30000,
        async execute(_args, exec) {
            const windows = await listWindowsJson(exec);
            return { windows };
        },
    }));
    // ── screen_info（显示器/DPI 布局） ─────────────────────────────────────────
    ctx.tools.register(defineTool({
        name: 'screen_info',
        description: '列出显示器/DPI 布局（每屏 x/y/width/height/primary/scale）。高 DPI 下模型需据此折算屏幕坐标，' +
            '避免见到的像素与操作坐标错位。',
        parameters: {},
        output: {
            schema: {
                type: 'object',
                properties: { displays: { type: 'array', items: { type: 'object', additionalProperties: true } } },
                additionalProperties: false,
            },
            render: (_args, value) => [{ type: 'text', text: JSON.stringify(value.displays, null, 2) }],
        },
        timeoutMs: 30000,
        async execute(_args, exec) {
            const displays = await screenInfoJson(exec);
            return { displays };
        },
    }));
    // ── cvision_status（运行环境健康） ─────────────────────────────────────────
    ctx.tools.register(defineTool({
        name: 'cvision_status',
        description: '检查 cvision 运行环境：python 版本、平台后端、OCR 引擎、依赖（Pillow/pyautogui/pywin32/winsdk）可达性，' +
            '以及后端是否已实现。其中 capture_backends 会**真实探测**窗口捕获后端（不是「包有没有装上」）：' +
            'wgc.available 为 true 才意味着能抓被遮挡的窗口，false 时 reason 会写明原因（虚拟机上常见）。' +
            '用于安装排错与确认能力。',
        parameters: {},
        output: {
            schema: {
                type: 'object',
                properties: { status: { type: 'object', additionalProperties: true } },
                additionalProperties: false,
            },
            render: (_args, value) => [{ type: 'text', text: JSON.stringify(value.status, null, 2) }],
        },
        timeoutMs: 30000,
        async execute(_args, exec) {
            const status = await statusJson(exec);
            return { status };
        },
    }));
    // ── wait_for_window（轮询等窗口出现） ───────────────────────────────────────
    ctx.tools.register(defineTool({
        name: 'wait_for_window',
        description: '轮询等待某个窗口出现（按标题子串），直到命中或超时。用于「打开某应用后等它出现再抓图」。' +
            '返回命中的窗口（或超时提示）。默认每 500ms 轮询，timeoutMs 默认 10000。',
        parameters: {
            title: { type: 'string', description: '要等待的窗口标题子串' },
            timeout: { type: 'number', description: '超时毫秒，默认 10000' },
        },
        output: {
            schema: {
                type: 'object',
                properties: { found: { type: 'boolean' }, window: { type: 'object', additionalProperties: true }, detail: { type: 'string' } },
                additionalProperties: false,
            },
            render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
        },
        timeoutMs: 30000,
        async execute(args, exec) {
            const title = String(args.title || '').trim();
            if (!title)
                throw new Error('wait_for_window 需要 title');
            const timeout = Math.max(0, Number(args.timeout ?? 10000));
            const start = Date.now();
            const needle = title.toLowerCase();
            for (;;) {
                exec.signal.throwIfAborted();
                const windows = await listWindowsJson(exec);
                const hit = windows.find((w) => String(w.title ?? '').toLowerCase().includes(needle));
                if (hit)
                    return { found: true, window: hit, detail: `found: ${hit.title}` };
                if (Date.now() - start >= timeout)
                    return { found: false, detail: `timeout after ${timeout}ms; no window title contains "${title}"` };
                await new Promise((r) => setTimeout(r, 500));
            }
        },
    }));
    // ── 用户级操作（computer-use） ─────────────────────────────────────────────
    const inputOut = {
        schema: {
            type: 'object',
            properties: { ok: { type: 'boolean' } },
            additionalProperties: false,
        },
        render: (_a, v) => [
            { type: 'text', text: v.ok ? '已执行' : '未执行' },
        ],
    };
    /**
     * `click_at` 的输出：除了成功标志，还回报**换算出来的屏幕坐标**。
     *
     * 回报它是为了让模型能核对自己「按比例指的到底是哪个点」——比例估偏时一眼就能看出落点漂到哪儿了。
     * ⚠️ schema 声明了 `additionalProperties: false`，多一个没声明的键会让**整次调用**失败
     * （v0.2.24 的 `wait_until_changed` 正是栽在这上面），所以 x/y 必须如实声明。
     */
    const pointOut = {
        schema: {
            type: 'object',
            properties: {
                ok: { type: 'boolean' },
                x: { type: 'integer' },
                y: { type: 'integer' },
            },
            additionalProperties: false,
        },
        render: (_a, v) => [
            { type: 'text', text: v.ok ? `已点击屏幕坐标 (${v.x ?? '?'}, ${v.y ?? '?'})` : '未执行' },
        ],
    };
    ctx.tools.register(defineTool({
        name: 'click',
        description: '在屏幕绝对坐标 (x,y) 模拟鼠标单击。先 see 确认目标位置后再点。点击前会自动把「你最近一次 see 的窗口」置前，并校验该坐标确实属于它；校验不过则报错，避免点到压在上面的窗口上。同一时刻只有一个进程能操作这台电脑：另一个会话/实例正在输入时会等一小会儿，超时则**如实报错且不会硬点**（按提示稍后重试，不要改成别的坐标硬试）。',
        parameters: { x: { type: 'integer' }, y: { type: 'integer' }, button: { type: 'string', description: 'left/right/middle，默认 left' } },
        output: inputOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            const cmd = ['--click', String(args.x), String(args.y)];
            if (args.button && args.button !== 'left')
                cmd.push('--button', String(args.button));
            await runInputAction(cmd, exec, { x: Number(args.x), y: Number(args.y) });
            return { ok: true };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'click_at',
        description: '按**比例**点击「你最近一次 see 那张图」上的位置：rx/ry 是 0~1 的比例（左上角 0,0；右下角 1,1）。' +
            '模型读不准图片**像素**（你看到的预览被 DSH 按图片 token 规则缩过：1920×1080 的整屏截图到你手里只剩 1708×961），但读得准**比例**；' +
            '而比例位置在缩放前后不变，所以插件能把它精确换算成屏幕坐标。' +
            '适用：纯图标界面、或 see(text=true) 没给出你要点的那个目标时。' +
            '⚠️ 精度受目测限制——比例差 1% 在 1920 宽的屏上就是约 19px，所以小控件仍应优先用 see(text=true) 返回的 screen_center。' +
            '点击前同样会把最近一次 see 的窗口置前，并校验坐标确实属于它。',
        parameters: {
            rx: { type: 'number', description: '横向比例 0~1（0 = 图片左边缘，1 = 右边缘）' },
            ry: { type: 'number', description: '纵向比例 0~1（0 = 图片上边缘，1 = 下边缘）' },
            button: { type: 'string', description: 'left/right/middle，默认 left' },
        },
        output: pointOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            const frame = operationTarget.frame;
            if (!frame) {
                throw new Error('click_at 需要先 see 一次——比例是相对「你最近看到的那张图」的；若上次抓取没能算出图片覆盖的屏幕矩形，请重新 see 一张');
            }
            const rx = Number(args.rx);
            const ry = Number(args.ry);
            if (!Number.isFinite(rx) || !Number.isFinite(ry) || rx < 0 || rx > 1 || ry < 0 || ry > 1) {
                throw new Error(`click_at 的 rx/ry 必须落在 0~1：收到 rx=${String(args.rx)} ry=${String(args.ry)}`);
            }
            const x = Math.round(frame.x + rx * frame.width);
            const y = Math.round(frame.y + ry * frame.height);
            const cmd = ['--click', String(x), String(y)];
            if (args.button && args.button !== 'left')
                cmd.push('--button', String(args.button));
            await runInputAction(cmd, exec, { x, y });
            return { ok: true, x, y };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'double_click',
        description: '在屏幕绝对坐标 (x,y) 模拟鼠标双击。',
        parameters: { x: { type: 'integer' }, y: { type: 'integer' } },
        output: inputOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            const cmd = ['--double', String(args.x), String(args.y)];
            await runInputAction(cmd, exec, { x: Number(args.x), y: Number(args.y) });
            return { ok: true };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'mouse_move',
        description: '把鼠标移到屏幕绝对坐标 (x,y)（不点击）。',
        parameters: { x: { type: 'integer' }, y: { type: 'integer' } },
        output: inputOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            await runCliInputTracked(['--move', String(args.x), String(args.y)], exec);
            return { ok: true };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'scroll',
        description: '在屏幕坐标 (x,y) 处滚动。dy>0 向上滚，dy<0 向下滚（单位：格）；dx 为水平滚动（可选）。',
        parameters: {
            x: { type: 'integer' },
            y: { type: 'integer' },
            dy: { type: 'integer', description: '竖直滚动格数（dy>0 向上）' },
            dx: { type: 'integer', description: '可选水平滚动格数（dx>0 向右）' },
        },
        output: inputOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            const cmd = args.dx
                ? ['--scroll-h', String(args.x), String(args.y), String(args.dx)]
                : ['--scroll', String(args.x), String(args.y), String(args.dy ?? 0)];
            await runInputAction(cmd, exec, { x: Number(args.x), y: Number(args.y) });
            return { ok: true };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'drag',
        description: '从屏幕坐标 (x1,y1) 拖拽到 (x2,y2)（模拟按住左键拖动，如框选/拖文件）。默认左键。',
        parameters: {
            x1: { type: 'integer' },
            y1: { type: 'integer' },
            x2: { type: 'integer' },
            y2: { type: 'integer' },
            button: { type: 'string', description: 'left/right/middle，默认 left' },
        },
        output: inputOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            const cmd = ['--drag', String(args.x1), String(args.y1), String(args.x2), String(args.y2)];
            if (args.button && args.button !== 'left')
                cmd.push('--button', String(args.button));
            await runInputAction(cmd, exec, { x: Number(args.x1), y: Number(args.y1) });
            return { ok: true };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'type_text',
        description: '像键盘一样输入文本（到当前焦点）。例如输入到地址栏/输入框，可配合 ctrl+l 先聚焦。',
        parameters: { text: { type: 'string', description: '要输入的文本' } },
        output: inputOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            await runInputAction(['--type', String(args.text)], exec);
            return { ok: true };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'press_key',
        description: '发送快捷键/按键，如 "ctrl+l"（聚焦地址栏）、"enter"、"ctrl+shift+t"（新标签）、"alt+tab"。',
        parameters: { keys: { type: 'string', description: '按键组合，如 ctrl+l / enter / ctrl+shift+t' } },
        output: inputOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            await runInputAction(['--keys', String(args.keys)], exec);
            return { ok: true };
        },
    }));
    const clipboardOut = {
        schema: {
            type: 'object',
            properties: { ok: { type: 'boolean' }, text: { type: 'string' } },
            additionalProperties: false,
        },
        render: (_a, v) => [
            { type: 'text', text: v.ok && v.text != null ? String(v.text) : (v.ok ? '已执行' : '未执行') },
        ],
    };
    ctx.tools.register(defineTool({
        name: 'get_clipboard',
        description: '读取当前剪贴板文本（Windows 原生；其他平台需 pyperclip）。',
        parameters: {},
        output: clipboardOut,
        timeoutMs: 30000,
        async execute(_args, exec) {
            // `--get-clipboard` 也走输入锁（Python 侧）：非 ASCII 输入会**临时改写**剪贴板，
            // 不互斥就可能读到别人那次粘贴的临时内容。所以这里同样带上会话身份。
            const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_input', ...withLockLabel(['--get-clipboard'], exec)], {
                cwd: PY_CWD,
                env: PY_ENV,
                maxBuffer: 4 * 1024 * 1024,
                signal: exec.signal,
            });
            const info = JSON.parse(stdout);
            return { ok: true, text: info.text ?? '' };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'set_clipboard',
        description: '把文本写入剪贴板（Windows 原生；其他平台需 pyperclip）。',
        parameters: { text: { type: 'string', description: '要写入剪贴板的文本' } },
        output: clipboardOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            await runCliInputTracked(['--set-clipboard', String(args.text)], exec);
            return { ok: true, text: String(args.text) };
        },
    }));
    ctx.tools.register(defineTool({
        name: 'focus_window',
        description: '把指定窗口置前（用户级：激活它），便于随后对它键盘/鼠标操作。只改前后层级，**不改变窗口尺寸/最大化状态**（仅最小化的窗口会被还原）。用 handle 精确（来自 list_windows），或用 title 按标题（精确标题优先）。**置前失败会如实报错**（不再假报成功）；成功后该窗口即成为后续点击/输入的默认目标。',
        parameters: {
            title: { type: 'string', description: '窗口标题子串，如 "Google Chrome"；与 handle 二选一' },
            handle: { type: 'integer', description: '窗口句柄（来自 list_windows）；与 title 二选一，优先 handle' },
        },
        output: inputOut,
        timeoutMs: 30000,
        async execute(args, exec) {
            if (args.handle == null && !args.title) {
                throw new Error('focus_window 需要提供 handle（窗口句柄）或 title（窗口标题）之一');
            }
            const cmd = args.handle != null ? ['--focus-handle', String(args.handle)] : ['--focus', String(args.title)];
            const info = await runCliInputJson(cmd, exec);
            if (!info.ok) {
                // 如实报错：旧实现无条件返回 {ok:true}，调用方以为窗口已经在前台，接着点击就点到
                // 压在上面的别的窗口上了。
                throw new Error(String(info.error ?? '窗口未能置前'));
            }
            const target = positiveInt(info.handle) ?? positiveInt(args.handle);
            if (target != null)
                operationTarget.handle = target; // 刚置前的窗口就是接下来的操作目标
            return { ok: true };
        },
    }));
}
