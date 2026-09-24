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
import { execFile, spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { promisify } from 'node:util'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { dirname, delimiter, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import type { Context } from '@deepseek-ai/cordis'
import { defineTool, type JsonValue } from '@deepseek-ai/dsh-tools'
import type { ImageAttachmentRef } from '@deepseek-ai/dsh-attachment'

const execFileAsync = promisify(execFile)

export const name = 'Vision'
export const inject = ['tools', 'attachments']

/**
 * 允许回传的图片类型。
 *
 * 刻意**不含 GIF**：`encoding.image_to_base64` 用 `img.save(format='GIF')` 保存多帧图时只会写出
 * 第一帧，所以「支持 GIF」是假的——`see(format='GIF')` 曾能选到它，但拿到的是静默退化的单帧。
 * 与其留一个看着支持、实际丢帧的选项，不如在这里拒掉（`parseDataUrl` 会给出明确报错）。
 * 注：Pillow 对多帧有 `save_all=True` + `append_images`，真要做动图得走那条路。
 */
const MEDIA_TYPES = ['image/jpeg', 'image/png', 'image/webp'] as const
type MediaType = (typeof MEDIA_TYPES)[number]

type Json = Record<string, unknown>
type Row = Record<string, JsonValue>

/**
 * 一张截图覆盖的**屏幕矩形**（物理像素），由 `see` 随图一起回报。
 *
 * 它是「按比例点击」的唯一基准：模型读不准图片像素（它看到的预览被 DSH 按图片 token 规则缩过，
 * 1920×1080 的整屏截图到它手里只剩 1708×961），但读得准**比例**。而比例位置在缩放前后不变，所以
 * 只要矩形对，`screen = (x + rx·width, y + ry·height)` 就与图片被缩到多少像素无关。
 */
interface SeeFrame {
  x: number
  y: number
  width: number
  height: number
}

/** 从插件自身位置向上查找含 `cvision/` 的包根（入口在 lib/index.js 时需上移一层）。 */
function findPluginRoot(): string {
  let dir = dirname(fileURLToPath(import.meta.url))
  for (let i = 0; i < 4; i++) {
    if (existsSync(resolve(dir, 'cvision'))) return dir
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return dirname(fileURLToPath(import.meta.url))
}

const PLUGIN_DIR = findPluginRoot()
const PYTHON = process.env.CVISION_PYTHON ?? 'python'
const CVISION_DIR = process.env.CVISION_DIR || PLUGIN_DIR

/**
 * Python 子进程一律强制 UTF-8 stdio，避免 Windows 控制台/ANSI 代码页把中文窗口标题与 OCR 输出弄乱。
 * 同时把插件目录挂到 `PYTHONPATH` 前面，让 `python -m cvision.*` 在**任意工作目录**下都能 import 到包内源码。
 */
const PY_ENV = {
  ...process.env,
  PYTHONUTF8: '1',
  PYTHONIOENCODING: 'utf-8',
  PYTHONPATH: [CVISION_DIR, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
}

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
const PY_CWD = tmpdir()

/** 解析 `data:<mime>;base64,<data>` 为附件服务所需的字节与媒体类型。 */
function parseDataUrl(dataUrl: string): { data: Uint8Array; mediaType: MediaType; ext: string } {
  const m = /^data:(image\/[a-z+]+);base64,(.+)$/s.exec(String(dataUrl).trim())
  if (!m || !m[1] || !m[2]) throw new Error(`无法解析截屏 data URL（长度 ${String(dataUrl).length}）`)
  const mediaType = m[1] as MediaType
  if (!MEDIA_TYPES.includes(mediaType)) throw new Error(`不支持的图片类型 ${mediaType}`)
  const ext = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'image/gif': 'gif',
  }[mediaType]
  return { data: new Uint8Array(Buffer.from(m[2], 'base64')), mediaType, ext }
}

/** 断言包内（或 CVISION_DIR 指向）的 Python 版 cvision 存在。 */
function assertCvisionPresent(): void {
  if (!existsSync(resolve(CVISION_DIR, 'cvision'))) {
    throw new Error(
      `未找到 Python 版 cvision（${resolve(CVISION_DIR, 'cvision')} 不存在）。` +
        `请安装配套的 cvision 包，或将环境变量 CVISION_DIR 指向含 cvision/ 的项目根。`,
    )
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
 */
const WAIT_CHANGED_KEYS = [
  'changed',
  'samples',
  'elapsed_ms',
  'diff_ratio',
  'mean_diff',
  'diff_bbox',
  'width',
  'height',
] as const

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
export function waitChangedMeta(payload: Json): Row {
  const meta: Row = {}
  for (const key of WAIT_CHANGED_KEYS) {
    const value = payload[key]
    if (value !== undefined && value !== null) meta[key] = value as JsonValue
  }
  return meta
}

// ── 首次调用前的运行时体检（把「裸 Python 报错」换成可操作提示） ──────────────
/**
 * 装完插件后**必须手动跑一次** `pip install -r requirements.txt`（DSH 不跑 pip：包没有
 * prepare/postinstall 钩子，且安装期静默装包属于供应链风险）。代价是：依赖没装时
 * `see`/`ocr` 第一句就会抛裸的 `ModuleNotFoundError` 或子进程报错，用户看不出该做什么。
 *
 * 这里在**本会话第一次调用工具前**用 `cli_capture --status` 探一次环境——该探针刻意是
 * 「无依赖」的（status.py 不 import PIL，platform 后端也有 try/except 兜底），所以**一个
 * 依赖都没装的新环境照样能跑出结论**。结论缓存在 `runtimeStatus`，整个进程只探一次。
 */
type RuntimeDepName = 'Pillow' | 'pyautogui' | 'pyperclip'
let runtimeStatus: Row | null = null
let runtimeProbeError: string | null = null
let runtimeProbed = false

/** 体检失败时给出的安装命令：清单随包分发，`--upgrade` 会在区间内挑最新可用版本。 */
export const PIP_HINT = `python -m pip install --upgrade -r "${resolve(CVISION_DIR, 'requirements.txt')}"`

/** 判断体检结论里某个依赖是否可用；缺 key 一律当作不可用（探针结构变了也不放过）。 */
function depOk(status: Row, name: RuntimeDepName): boolean {
  const deps = status.deps
  if (deps === null || typeof deps !== 'object') return false
  return (deps as Row)[name] === true
}

/**
 * 把一次体检结论整理成**可操作**的错误信息；环境正常则返回 null。
 *
 * 纯函数、不触碰进程，因而可被 `tests/vision.host.test.mjs` 直接注入各种结论来钉死文案
 * （真去 spawn Python 在 CI 上既慢又不稳）。
 * @param status - `cli_capture --status` 的 JSON 结论；探针整体失败时为 null。
 * @param probeError - 探针失败原因（`status` 为 null 时有值），会带上 `PYTHON`/`CVISION_DIR` 便于定位。
 */
export function describeRuntimeProblem(status: Row | null, probeError: string | null): string | null {
  if (status === null) {
    // 连 `--status` 都跑不起来：基本是「没有可用的 python 解释器」或 `cvision/` 路径不对。
    return (
      `无法运行 Python 版 cvision（${PYTHON} -m cvision.cli_capture --status）：${probeError ?? '未知错误'}\n` +
      `请确认已装 Python 3.10+ 且 \`${PYTHON}\` 可用；插件目录与解释器分别是：` +
      `CVISION_DIR=${CVISION_DIR}、CVISION_PYTHON=${PYTHON}。\n` +
      `若依赖缺失，装一次即可：${PIP_HINT}`
    )
  }
  const missing: string[] = []
  if (!depOk(status, 'Pillow')) missing.push('Pillow')
  const backendMissing = status.backend_implemented !== true
  // macOS 这类「有实现但没真机验证」：不是错误，但**必须**说出来——否则模型会把「有实现」
  // 当成「已验证」，出问题时误判成插件 bug 而不是平台差异。
  const unverified = !backendMissing && status.platform_support === 'unverified'

  // 真正会**挡住**工具的才算错误：缺依赖、平台后端未实现。两者都没有就不拦。
  if (missing.length === 0 && !backendMissing) {
    return unverified
      ? `提示：本平台后端（${String(status.backend ?? '未知')}）**未在真机验证过**（platform_support=unverified），` +
          `行为可能与文档有出入，遇到问题请当作平台差异排查。`
      : null
  }

  const lines: string[] = []
  if (missing.length > 0) lines.push(`Python 环境不完整：缺少 ${missing.join('、')}。`)
  if (backendMissing) {
    lines.push(
      `本平台后端未实现（platform=${String(status.platform ?? '未知')}、backend=${String(status.backend ?? '未知')}）——` +
        `截屏/OCR 在该平台尚不可用（详见 README 的平台矩阵）。`,
    )
  }
  lines.push(
    `运行环境：python=${String(status.python ?? '未知')}、platform=${String(status.platform ?? '未知')}、backend=${String(status.backend ?? '未知')}`,
  )
  // 输入类工具另有 pyautogui 这一支依赖；它不阻止 see/ocr，但缺了就该说清后果。
  if (!depOk(status, 'pyautogui')) lines.push(`另外：pyautogui 缺失，输入类工具不可用。`)
  // 只有在**确实缺依赖**时才给安装命令与复查指引：平台后端未实现不是装包能解决的。
  if (missing.length > 0) {
    lines.push(`装一次依赖即可（清单随包分发）：${PIP_HINT}`)
    lines.push(`装完可调 cvision_status() 复查；依赖装在哪个解释器里，就要让 CVISION_PYTHON 指向它。`)
  }
  return lines.join('\n')
}

/** 跑一次体检（进程内只跑一次），失败不抛错——把结论交给 describeRuntimeProblem。 */
async function probeRuntime(exec: { signal: AbortSignal }): Promise<void> {
  if (runtimeProbed) return
  runtimeProbed = true
  try {
    assertCvisionPresent()
    const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_capture', '--status'], {
      cwd: PY_CWD,
      env: PY_ENV,
      maxBuffer: 4 * 1024 * 1024,
      signal: exec.signal,
    })
    runtimeStatus = JSON.parse(stdout) as Row
  } catch (error) {
    runtimeProbeError = errorMessage(error)
    runtimeStatus = null
  }
}

/** 首次调用前的体检门：环境有问题就抛出可操作错误，而不是让 Python 抛裸异常。 */
export async function ensureRuntime(exec: { signal: AbortSignal }): Promise<void> {
  await probeRuntime(exec)
  const problem = describeRuntimeProblem(runtimeStatus, runtimeProbeError)
  if (problem !== null) throw new Error(problem)
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
let inputBusyCount = 0

/** 跑一个输入类操作，期间把「占用中」置为真（用 try/finally 保证异常时也会复位）。 */
async function withInputBusy<T>(fn: () => Promise<T>): Promise<T> {
  inputBusyCount += 1
  try {
    return await fn()
  } finally {
    inputBusyCount = Math.max(0, inputBusyCount - 1)
  }
}

/** 把输入类 CLI 调用包进「占用中」窗口。 */
function runCliInputTracked(args: string[], exec: { signal: AbortSignal }): Promise<void> {
  return withInputBusy(() => runCliInput(args, exec))
}

/**
 * 浏览器半边查询「宿主是否正在驱动输入设备」的路由。
 * 复用现有的剪贴板状态路由（页面本来就在每秒轮询它），不额外加一条要轮询的路由。
 */
function inputBusy(): boolean {
  return inputBusyCount > 0
}

/** 运行一次用户级输入（python -m cvision.cli_input <args>），返回它的 stdout。 */
async function runCliInputText(args: string[], exec: { signal: AbortSignal }): Promise<string> {
  await ensureRuntime(exec)
  assertCvisionPresent()
  const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_input', ...args], {
    cwd: PY_CWD,
    env: PY_ENV,
    maxBuffer: 1 * 1024 * 1024,
    signal: exec.signal,
  })
  return stdout.trim()
}

/** 运行一次用户级输入，忽略输出。 */
async function runCliInput(args: string[], exec: { signal: AbortSignal }): Promise<void> {
  await runCliInputText(args, exec)
}

/**
 * 运行一次输入类 CLI 并解析它的 JSON 输出。
 *
 * ``--focus`` / ``--ensure-front`` 的成败**只能**从这个 JSON 里读：失败时 CLI 以非零退出码收场
 * （这样即便调用方只看退出码也不会把「没置前」当成功），但**仍会在 stdout 上给出结构化原因**
 * （``ok:false`` + ``error``），所以这里连非零退出的情况也要把 stdout 捞回来解析。
 */
async function runCliInputJson(args: string[], exec: { signal: AbortSignal }): Promise<Row> {
  let out: string
  try {
    out = await withInputBusy(() => runCliInputText(args, exec))
  } catch (error) {
    const stdout = String((error as { stdout?: unknown }).stdout ?? '').trim()
    const parsed = parseJsonLoose(stdout)
    if (parsed) return parsed
    throw error
  }
  const parsed = parseJsonLoose(out)
  if (!parsed) throw new Error(`cvision 输入命令未返回 JSON：${out.slice(0, 200)}`)
  return parsed
}

/** 宽松解析 CLI 的一行 JSON：解析不了就返回 null（由调用方决定怎么处理）。 */
function parseJsonLoose(text: string): Row | null {
  if (!text) return null
  try {
    const value = JSON.parse(text) as unknown
    return value && typeof value === 'object' ? (value as Row) : null
  } catch {
    return null
  }
}

/**
 * 最近一次 ``see`` 看到的窗口句柄——也就是「接下来要操作的那个窗口」。
 *
 * 点击类工具靠它把窗口置前并校验坐标归属；置前过的窗口（``focus_window``）同样是操作目标。
 */
let lastSeeHandle: number | null = null

/**
 * 最近一次 `see` 那张图覆盖的屏幕矩形——`click_at`（按比例点击）的换算基准。
 *
 * 与 `lastSeeHandle` 同一次 `see` 一起更新：模型说「点图的 64%、46%」时指的就是这张图。
 */
let lastSeeFrame: SeeFrame | null = null

/**
 * 点击类动作的前置保障：把「最近的 see 目标窗口」拉到前台，并校验坐标确实属于它。
 *
 * 为什么必须有这一步（实测缺陷）：``click`` 下发的是**屏幕坐标**，Windows 只会把它派给该点
 * 最顶层的窗口。目标窗口不在前台时，点下去会打在压着它的窗口上、或者直接落空——**坐标全对却
 * 没反应**，是最难自查的一类失败。所以要么把目标置前，要么明确报错，绝不静默打偏。
 */
async function ensureTargetFront(
  exec: { signal: AbortSignal },
  at?: { x: number; y: number },
): Promise<void> {
  if (lastSeeHandle == null) return
  const handle = lastSeeHandle
  const args = ['--ensure-front', String(handle)]
  if (at) args.push('--at', String(Math.round(at.x)), String(Math.round(at.y)))
  const info = await runCliInputJson(args, exec)
  if (info.ok) return
  if (info.stale) {
    // 窗口已经不存在：这份记录过期了。清掉并放行，否则它会永久卡住之后所有的点击。
    lastSeeHandle = null
    return
  }
  throw new Error(String(info.error ?? `窗口 0x${handle.toString(16)} 未能置前`))
}

/** 运行一次纯采集类 CLI（python -m cvision.cli_capture <args>），返回 stdout。 */
async function runCliCapture(args: string[], exec: { signal: AbortSignal }): Promise<string> {
  assertCvisionPresent()
  const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_capture', ...args], {
    cwd: PY_CWD,
    env: PY_ENV,
    maxBuffer: 64 * 1024 * 1024,
    signal: exec.signal,
  })
  return stdout.trim()
}

// ── 持久化 Python server（复用 D3D 设备/编码；失败自动回退每调用 CLI） ────────────
interface Pending {
  req: Json
  resolve: (v: Json) => void
  reject: (e: Error) => void
  timer: NodeJS.Timeout
}

class CvisionServer {
  private child: ChildProcessWithoutNullStreams | null = null
  private buffer = ''
  private queue: Pending[] = []
  private busy = false
  private down = false

  constructor(private defaultTimeoutMs: number) {}

  private ensure(): void {
    if (this.child) return
    const child = spawn(PYTHON, ['-m', 'cvision.cli_server'], {
      cwd: PY_CWD,
      env: PY_ENV,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.child = child
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk: string) => {
      this.buffer += chunk
      this.drainLines()
    })
    child.stderr.on('data', () => {})
    child.on('exit', () => this.fatal(new Error('cvision server exited')))
    child.on('error', (e) => this.fatal(new Error(`cvision server spawn failed: ${String(e)}`)))
  }

  private drainLines(): void {
    for (;;) {
      const idx = this.buffer.indexOf('\n')
      if (idx < 0) break
      const line = this.buffer.slice(0, idx).trim()
      this.buffer = this.buffer.slice(idx + 1)
      if (line) this.handleLine(line)
    }
  }

  private handleLine(line: string): void {
    let resp: Json
    try {
      resp = JSON.parse(line) as Json
    } catch {
      return
    }
    const next = this.queue.shift()
    if (!next) return
    clearTimeout(next.timer)
    this.busy = false
    if (resp.ok === false) {
      next.reject(new Error(String(resp.error ?? 'cvision server error')))
    } else {
      next.resolve(resp)
    }
    this.drain()
  }

  private drain(): void {
    if (this.busy || this.queue.length === 0) return
    if (!this.child) return
    this.busy = true
    this.child.stdin.write(JSON.stringify(this.queue[0].req) + '\n')
  }

  request(req: Json, exec?: { signal: AbortSignal }): Promise<Json> {
    if (this.down) return Promise.reject(new Error('cvision server is down'))
    this.ensure()
    return new Promise<Json>((resolve, reject) => {
      const timer = setTimeout(() => {
        // 超时视为 server 不可用：整体回收并回退 CLI，避免后续响应错位。
        this.fatal(new Error('cvision server timed out'))
        reject(new Error('cvision server timed out'))
      }, this.defaultTimeoutMs)
      const abort = () => reject(new Error('aborted'))
      exec?.signal?.addEventListener('abort', abort, { once: true })
      this.queue.push({
        req,
        resolve: (v) => {
          exec?.signal?.removeEventListener('abort', abort)
          resolve(v)
        },
        reject: (e) => {
          exec?.signal?.removeEventListener('abort', abort)
          reject(e)
        },
        timer,
      })
      this.drain()
    })
  }

  private fatal(err: Error): void {
    this.down = true
    for (const p of this.queue) {
      clearTimeout(p.timer)
      p.reject(err)
    }
    this.queue = []
    this.busy = false
    try {
      this.child?.kill()
    } catch {
      /* ignore */
    }
    this.child = null
  }

  dispose(): void {
    try {
      this.child?.stdin.write(JSON.stringify({ op: 'quit' }) + '\n')
    } catch {
      /* ignore */
    }
    try {
      this.child?.kill()
    } catch {
      /* ignore */
    }
    this.child = null
    this.down = true
  }
}

/**
 * 浏览器半边查询「当前模型是否接受图片输入」的路由路径。
 * 必须与 `src/client.js` 的 CAPABILITY_PATH 保持一致。
 */
const MODEL_CAPABILITY_PATH = '/cvision/model-capability'

/**
 * 本插件用到的宿主服务的最小结构视图。DSH 把 `webServer` 声明在
 * `@deepseek-ai/dsh-host-webserver`、`llm` 声明在 `@deepseek-ai/dsh-llm`；这里只
 * 声明实际用到的成员，避免为一个只读探针把整个宿主包拉进开发依赖。
 */
type CapabilityHost = {
  webServer: {
    register(route: {
      kind: 'exact'
      path: string
      handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>
    }): () => void
  }
  llm: {
    resolveModelInfo(provider: string, model: string): Promise<{ inputModalities?: readonly string[] }>
  }
}

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
async function modelImageCapability(host: CapabilityHost, provider: string, model: string): Promise<Row> {
  if (provider === '' || model === '') return { provider, model, image: false, source: 'unknown' }
  try {
    const info = await host.llm.resolveModelInfo(provider, model)
    const modalities = info.inputModalities
    return {
      provider,
      model,
      image: modalities === undefined || modalities.includes('image'),
      source: 'declared',
      modalities: [...(modalities ?? [])],
    }
  } catch {
    return { provider, model, image: false, source: 'unknown' }
  }
}

/**
 * 浏览器半边请求「系统级框选截图」的路由路径。
 * 必须与 `src/client.js` 的 SNIP_PATH 保持一致。
 */
const SNIP_PATH = '/cvision/snip'

/**
 * 浏览器半边查询「剪贴板里有没有图片」的路由（页面每秒轮询）。
 * 只读且廉价：宿主只查剪贴板格式 + token，不解码图片。必须与 client.js 的 CLIPBOARD_STATE_PATH 一致。
 */
const CLIPBOARD_STATE_PATH = '/cvision/clipboard'

/** 取剪贴板图片的路由（用户长按按钮时才调用）。必须与 client.js 的 CLIPBOARD_IMAGE_PATH 一致。 */
const CLIPBOARD_IMAGE_PATH = '/cvision/clipboard/image'

/** 一次系统截图的结果。 */
type SnipOutcome =
  | { kind: 'captured'; dataUrl: string }
  | { kind: 'cancelled' }
  | { kind: 'unsupported'; message: string }
  | { kind: 'failed'; message: string }

/** 把任意抛出物整理成一行可读信息。 */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** 给路由回一个 JSON 响应（截图路由与能力路由共用）。 */
function sendJson(res: ServerResponse, status: number, payload: Row): void {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
  res.end(JSON.stringify(payload))
}

/**
 * 只接受同源请求：`Origin` 的 host 必须等于 `Host` 头。
 * 截图会拉起系统 UI 并读取剪贴板，属于高权限动作，不能给跨站页面开口子
 * （与 dshmarket 对写路由的做法一致）。
 */
function isSameOrigin(req: IncomingMessage): boolean {
  const host = req.headers.host
  const origin = req.headers.origin
  if (host === undefined || origin === undefined) return false
  try {
    return new URL(origin).host === host
  } catch {
    return false
  }
}

/**
 * 运行一次返回 JSON 的 Python CLI，返回 stdout。
 *
 * **非零退出也返回 stdout**：本插件的 CLI 用退出码表达结果分类（`cli_snip` 2=cancelled、
 * 3=unsupported；`cli_clipboard` 同理），真正的契约在 stdout 的 JSON 里。用 `promisify(execFile)`
 * 直接 await 会在非零退出时 reject，把「用户取消」误判成故障。
 */
async function runPythonCli(module: string, args: string[], exec: { signal: AbortSignal }): Promise<string> {
  await ensureRuntime(exec)
  assertCvisionPresent()
  try {
    const { stdout } = await execFileAsync(PYTHON, ['-m', module, ...args], {
      cwd: PY_CWD,
      env: PY_ENV,
      maxBuffer: 64 * 1024 * 1024,
      signal: exec.signal,
    })
    return stdout
  } catch (error) {
    const stdout = (error as { stdout?: unknown }).stdout
    if (typeof stdout === 'string' && stdout.trim() !== '') return stdout
    throw error
  }
}

/** 运行一次系统截图 CLI（python -m cvision.cli_snip），把 JSON 结果翻译成 SnipOutcome。 */
async function runSnipCli(exec: { signal: AbortSignal }): Promise<SnipOutcome> {
  let stdout: string
  try {
    stdout = await runPythonCli('cvision.cli_snip', ['--timeout', '60'], exec)
  } catch (error) {
    // 用户中途取消（客户端断开 → signal 中止）不算故障。
    if (exec.signal.aborted) return { kind: 'cancelled' }
    return { kind: 'failed', message: errorMessage(error) }
  }
  let parsed: Row
  try {
    parsed = JSON.parse(stdout.trim()) as Row
  } catch {
    return { kind: 'failed', message: `无法解析 cli_snip 输出（${stdout.length} 字节）` }
  }
  if (parsed.ok === true) {
    const dataUrl = String(parsed.data_url ?? '')
    return dataUrl === '' ? { kind: 'failed', message: 'cli_snip 未返回图片数据' } : { kind: 'captured', dataUrl }
  }
  const reason = String(parsed.reason ?? '')
  const message = String(parsed.message ?? reason)
  if (reason === 'cancelled') return { kind: 'cancelled' }
  if (reason === 'unsupported') return { kind: 'unsupported', message }
  return { kind: 'failed', message }
}

/**
 * 截图执行器。默认实现 spawn 包内 Python cvision 拉起系统截图 UI。
 * **导出可变对象只为单测注入假实现**（真实实现需要桌面与人工框选，CI 里跑不了）。
 */
export const snipExecutor: { run: (exec: { signal: AbortSignal }) => Promise<SnipOutcome> } = {
  run: runSnipCli,
}

/** 剪贴板状态（廉价：只查格式 + token，不解码图片）。`served` 由状态路由附加。 */
type ClipboardState = {
  supported: boolean
  image: boolean
  token: string | null
  reason: string
  /** 这张图是不是我们自己刚（单击系统截图时）交给页面的——客户端据此不把它当「新图片」。 */
  served?: boolean
  /** 宿主此刻是否正在驱动鼠标/键盘（输入类工具执行中）。客户端据此显示「别碰」提示。 */
  busy?: boolean
}

/** 一次剪贴板取图的结果。 */
type ClipboardImage =
  | { kind: 'captured'; dataUrl: string }
  | { kind: 'empty' }
  | { kind: 'unsupported'; message: string }
  | { kind: 'failed'; message: string }

/** 最近一次 apply 创建的常驻 Python server（状态轮询优先走它，免得每秒冷启动一个解释器）。 */
let activeServer: CvisionServer | null = null

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
const SNIP_SERVED_WINDOW_MS = 4000
let snipInFlight = false
let snipServedUntil = 0
let snipServedToken: string | null = null

/** 把 CLI/常驻进程返回的原始 JSON 收敛成 ClipboardState。 */
function toClipboardState(raw: Json): ClipboardState {
  return {
    supported: raw.supported === true,
    image: raw.image === true,
    token: raw.token === undefined || raw.token === null ? null : String(raw.token),
    reason: String(raw.reason ?? ''),
  }
}

/** 查一次剪贴板状态：优先常驻 server，失败回退一次性 CLI。 */
async function clipboardState(server: CvisionServer | null): Promise<ClipboardState> {
  if (server !== null) {
    try {
      return toClipboardState(await server.request({ op: 'clipboard_state' }))
    } catch {
      /* 常驻进程不可用：回退 CLI */
    }
  }
  const stdout = await runPythonCli('cvision.cli_clipboard', ['--state'], { signal: AbortSignal.timeout(8000) })
  return toClipboardState(JSON.parse(stdout.trim()) as Json)
}

/** 取剪贴板图片（只在用户长按按钮时调用，所以一次性 CLI 足够）。 */
async function clipboardImage(exec: { signal: AbortSignal }): Promise<ClipboardImage> {
  let stdout: string
  try {
    stdout = await runPythonCli('cvision.cli_clipboard', ['--image'], exec)
  } catch (error) {
    if (exec.signal.aborted) return { kind: 'failed', message: 'aborted' }
    return { kind: 'failed', message: errorMessage(error) }
  }
  let parsed: Row
  try {
    parsed = JSON.parse(stdout.trim()) as Row
  } catch {
    return { kind: 'failed', message: `无法解析 cli_clipboard 输出（${stdout.length} 字节）` }
  }
  if (parsed.ok === true) {
    const dataUrl = String(parsed.data_url ?? '')
    return dataUrl === '' ? { kind: 'failed', message: 'cli_clipboard 未返回图片数据' } : { kind: 'captured', dataUrl }
  }
  const reason = String(parsed.reason ?? '')
  const message = String(parsed.message ?? reason)
  if (reason === 'empty') return { kind: 'empty' }
  if (reason === 'unsupported') return { kind: 'unsupported', message }
  return { kind: 'failed', message }
}

/**
 * 剪贴板探测（状态 + 取图）。默认实现走包内 Python；**导出可变对象供单测注入假实现**
 * （真实实现要读系统剪贴板，CI 里没有桌面）。
 */
export const clipboardProbe: {
  state: () => Promise<ClipboardState>
  image: (exec: { signal: AbortSignal }) => Promise<ClipboardImage>
} = {
  state: () => clipboardState(activeServer),
  image: (exec) => clipboardImage(exec),
}

export function apply(ctx: Context): void {
  // 45s：`wait_changed` 会在 Python 侧阻塞轮询（默认 10s、上限由调用方的 timeout 决定），
  // 这个上限必须**大于**它，否则常驻 server 会被自己的超时回收，白等一场还回退到 CLI。
  const server = new CvisionServer(45000)
  // 剪贴板状态轮询优先复用这个常驻进程（每秒一次，冷启动一个解释器太贵）。
  activeServer = server
  ctx.effect(() => () => {
    activeServer = null
    server.dispose()
  })

  // ① 浏览器半边（输入框截图按钮）的权威能力通道。
  //    DSH 给浏览器的模型目录由 buildModelCatalog 主动剥掉了 inputModalities
  //    （只投影 id/name/description/reasoning），客户端因此无法自行判断当前模型
  //    收不收图——上游插件只能按模型名猜 vision|visual，既漏判也误判。这里由宿主
  //    按真实适配器目录回答。组合里没有 web 服务器（如 Electron/headless）时这段
  //    注册整体跳过，客户端会自动退回名字启发式。
  const injectHost = ctx.inject as unknown as (
    deps: readonly string[],
    callback: (scoped: Context & CapabilityHost) => void,
  ) => unknown
  injectHost(['webServer', 'llm'], (host) => {
    host.effect(
      () =>
        host.webServer.register({
          kind: 'exact',
          path: MODEL_CAPABILITY_PATH,
          handler: async (req, res) => {
            const url = new URL(req.url ?? '/', 'http://localhost')
            const provider = url.searchParams.get('provider') ?? ''
            const model = url.searchParams.get('model') ?? ''
            sendJson(res, 200, await modelImageCapability(host, provider, model))
          },
        }),
      'vision: model capability route',
    )
  })

  // ② 截图按钮的默认通道：**系统级框选截图**。
  //    抓屏动作本身由用户在系统 UI 里完成（切片可标注），本路由只负责拉起 UI 并把
  //    用户刚框出来的那张图取回来交给浏览器半边——因此不需要绕过浏览器的截图授权模型，
  //    但仍然按高权限动作设防：仅 POST、仅同源、客户端断开即中止 Python 等待。
  injectHost(['webServer'], (host) => {
    host.effect(
      () =>
        host.webServer.register({
          kind: 'exact',
          path: SNIP_PATH,
          handler: async (req, res) => {
            if (req.method !== 'POST') {
              sendJson(res, 405, { message: 'system snip requires POST' })
              return
            }
            if (!isSameOrigin(req)) {
              sendJson(res, 403, { message: 'cross-origin system snip refused' })
              return
            }
            const controller = new AbortController()
            req.on('close', () => {
              controller.abort()
            })
            // ⚠️ 整个截图请求期间都标为「进行中」：系统是在截图返回**之前**把图写进剪贴板的，而用户
            // 拖框/标注可能好几秒，期间每一次每秒轮询都必须把当前剪贴板内容算成我们自己产出的。
            snipInFlight = true
            const outcome = await snipExecutor.run({ signal: controller.signal })
            snipInFlight = false
            if (outcome.kind === 'captured') {
              // 成功后留一个短窗口：截图工具打开编辑器等可能又写一次剪贴板。
              snipServedUntil = Date.now() + SNIP_SERVED_WINDOW_MS
              try {
                const { data, mediaType } = parseDataUrl(outcome.dataUrl)
                res.writeHead(200, { 'content-type': mediaType, 'cache-control': 'no-store' })
                res.end(Buffer.from(data))
              } catch (error) {
                sendJson(res, 500, { message: errorMessage(error) })
              }
              return
            }
            if (outcome.kind === 'cancelled') {
              res.writeHead(204)
              res.end()
              return
            }
            if (outcome.kind === 'unsupported') {
              sendJson(res, 501, { message: outcome.message })
              return
            }
            sendJson(res, 500, { message: outcome.message })
          },
        }),
      'vision: system snip route',
    )
  })

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
            sendJson(res, 405, { message: 'clipboard state is GET-only' })
            return
          }
          try {
            const state = await clipboardProbe.state()
            const now = Date.now()
            // 归属：截图请求进行中，或刚完成后的短窗口内 → 此刻剪贴板里那张就是我们产出的。
            const ours = snipInFlight || now < snipServedUntil
            if (ours) snipServedToken = state.token
            sendJson(res, 200, {
              ...state,
              // `served`：这张图是不是我们自己刚（单击系统截图时）交给页面的？客户端据此不提示。
              served: state.token !== null && (ours || state.token === snipServedToken),
              // `busy`：宿主正在操作鼠标/键盘。物理上只有一套输入设备，所以要如实告诉用户
              // 「现在别碰」——这比试图抢互斥锁安全（锁一旦没释放会把插件卡死）。
              busy: inputBusy(),
            })
          } catch (error) {
            sendJson(res, 500, { message: errorMessage(error) })
          }
        },
      })
      const disposeImage = host.webServer.register({
        kind: 'exact',
        path: CLIPBOARD_IMAGE_PATH,
        handler: async (req, res) => {
          if (req.method !== 'POST') {
            sendJson(res, 405, { message: 'clipboard image requires POST' })
            return
          }
          if (!isSameOrigin(req)) {
            sendJson(res, 403, { message: 'cross-origin clipboard read refused' })
            return
          }
          const controller = new AbortController()
          req.on('close', () => {
            controller.abort()
          })
          const outcome = await clipboardProbe.image({ signal: controller.signal })
          if (outcome.kind === 'captured') {
            try {
              const { data, mediaType } = parseDataUrl(outcome.dataUrl)
              res.writeHead(200, { 'content-type': mediaType, 'cache-control': 'no-store' })
              res.end(Buffer.from(data))
            } catch (error) {
              sendJson(res, 500, { message: errorMessage(error) })
            }
            return
          }
          if (outcome.kind === 'empty') {
            res.writeHead(204)
            res.end()
            return
          }
          if (outcome.kind === 'unsupported') {
            sendJson(res, 501, { message: outcome.message })
            return
          }
          sendJson(res, 500, { message: outcome.message })
        },
      })
      return () => {
        disposeState()
        disposeImage()
      }
    }, 'vision: clipboard routes')
  })

  // ① server 优先，失败回退 CLI 的采集类辅助（每个返回统一形态）。
  //    这些是**所有 Python 依赖工具的唯一收口**（16 个工具里除 cvision_status 外都经这里，
  //    输入类与剪贴板类各有一处），所以体检门只需加在这 5 个函数上，不必逐个工具去改。
  /** 把 CLI/server 回报的窗口句柄转成正整数；缺失或非法时返回 null。 */
  function positiveInt(value: unknown): number | null {
    const n = Number(value)
    return Number.isFinite(n) && n > 0 ? Math.trunc(n) : null
  }

  /** 把 CLI/server 回报的 `image_screen_box` 收成屏幕矩形；缺失或非法时返回 null。 */
  function toFrame(value: unknown): SeeFrame | null {
    if (!value || typeof value !== 'object') return null
    const v = value as Record<string, unknown>
    const x = Number(v.x)
    const y = Number(v.y)
    const width = Number(v.width)
    const height = Number(v.height)
    if (![x, y, width, height].every((n) => Number.isFinite(n))) return null
    if (width <= 0 || height <= 0) return null
    return { x, y, width, height }
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
  async function captureDataUrl(
    args: Json,
    exec: { signal: AbortSignal },
  ): Promise<{ dataUrl: string; elements: Row[]; handle: number | null; frame: SeeFrame | null }> {
    await ensureRuntime(exec)
    if (args.text) {
      try {
        const resp = await server.request({ op: 'capture', ...args }, exec)
        return {
          dataUrl: String(resp.data_url ?? ''),
          elements: Array.isArray(resp.elements) ? (resp.elements as Row[]) : [],
          handle: positiveInt(resp.handle),
          frame: toFrame(resp.image_screen_box),
        }
      } catch {
        const cli = buildCaptureCli(args)
        cli.push('--text')
        const out = await runCliCapture(cli, exec)
        const info = JSON.parse(out) as {
          data_url?: string
          elements?: Row[]
          handle?: unknown
          image_screen_box?: unknown
        }
        return {
          dataUrl: String(info.data_url ?? ''),
          elements: Array.isArray(info.elements) ? info.elements : [],
          handle: positiveInt(info.handle),
          frame: toFrame(info.image_screen_box),
        }
      }
    }
    try {
      const resp = await server.request({ op: 'capture', ...args }, exec)
      return {
        dataUrl: String(resp.data_url ?? ''),
        elements: [],
        handle: positiveInt(resp.handle),
        frame: toFrame(resp.image_screen_box),
      }
    } catch {
      return {
        dataUrl: await runCliCapture(buildCaptureCli(args), exec),
        elements: [],
        handle: null,
        frame: null,
      }
    }
  }

  /** 把 see/ocr 的公共参数拼成 cli_capture 的参数表（两条分支共用，避免漂移）。 */
  function buildCaptureCli(args: Json): string[] {
    const cli = ['--format', String(args.format ?? 'PNG')]
    if (args.handle != null) cli.push('--handle', String(args.handle))
    if (args.window) cli.push('--window', String(args.window))
    if (args.maximize) cli.push('--maximize')
    if (args.region) cli.push('--region', String(args.region))
    if (args.delay) cli.push('--delay', String(args.delay))
    return cli
  }

  async function ocrJson(args: Json, exec: { signal: AbortSignal }): Promise<{ text: string; lines: string[]; words: Row[] }> {
    await ensureRuntime(exec)
    try {
      const resp = await server.request({ op: 'ocr', ...args }, exec)
      return {
        text: String(resp.text ?? ''),
        lines: Array.isArray(resp.lines) ? (resp.lines as string[]) : [],
        words: Array.isArray(resp.words) ? (resp.words as Row[]) : [],
      }
    } catch {
      const cli = []
      if (args.handle != null) cli.push('--handle', String(args.handle))
      if (args.window) cli.push('--window', String(args.window))
      if (args.maximize) cli.push('--maximize')
      if (args.region) cli.push('--region', String(args.region))
      if (args.delay) cli.push('--delay', String(args.delay))
      const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_ocr', ...cli], {
        cwd: PY_CWD,
        env: PY_ENV,
        maxBuffer: 4 * 1024 * 1024,
        signal: exec.signal,
      })
      const info = JSON.parse(stdout) as { text?: string; lines?: string[]; words?: Row[] }
      return { text: info.text ?? '', lines: info.lines ?? [], words: info.words ?? [] }
    }
  }

  async function listWindowsJson(exec: { signal: AbortSignal }): Promise<Row[]> {
    await ensureRuntime(exec)
    try {
      const resp = await server.request({ op: 'list' }, exec)
      return Array.isArray(resp.windows) ? (resp.windows as Row[]) : []
    } catch {
      const out = await runCliCapture(['--list'], exec)
      return JSON.parse(out) as Row[]
    }
  }

  async function screenInfoJson(exec: { signal: AbortSignal }): Promise<Row[]> {
    await ensureRuntime(exec)
    try {
      const resp = await server.request({ op: 'screen_info' }, exec)
      return Array.isArray(resp.displays) ? (resp.displays as Row[]) : []
    } catch {
      const out = await runCliCapture(['--screen-info'], exec)
      return JSON.parse(out) as Row[]
    }
  }

  /**
   * 轮询直到画面变化（供 `wait_until_changed`）。server 优先，失败回退一次性 CLI。
   * server 侧这个 op 会阻塞等待，所以上面 `new CvisionServer(45000)` 的超时必须更大。
   */
  async function waitChangedJson(args: Json, exec: { signal: AbortSignal }): Promise<{ dataUrl: string; meta: Row }> {
    await ensureRuntime(exec)
    try {
      const resp = await server.request({ op: 'wait_changed', ...args }, exec)
      return { dataUrl: String(resp.data_url ?? ''), meta: waitChangedMeta(resp) }
    } catch {
      const cli = buildCaptureCli(args)
      cli.push('--wait-changed')
      if (args.interval) cli.push('--interval', String(args.interval))
      if (args.timeout) cli.push('--timeout', String(args.timeout))
      if (args.threshold != null) cli.push('--threshold', String(args.threshold))
      const out = await runCliCapture(cli, exec)
      const info = JSON.parse(out) as Row
      return { dataUrl: String(info.data_url ?? ''), meta: waitChangedMeta(info) }
    }
  }

  async function statusJson(exec: { signal: AbortSignal }): Promise<Row> {
    // ⚠️ 刻意**不**过体检门：cvision_status 是体检/排错工具，环境不完整时它正是
    // 「唯一还能用」的那条路（cli_capture --status 本身不需要 Pillow）。gate 了它，
    // 用户就失去了查出问题的手段。
    try {
      const resp = await server.request({ op: 'status' }, exec)
      return (resp.status as Row) ?? {}
    } catch {
      const out = await runCliCapture(['--status'], exec)
      return JSON.parse(out) as Row
    }
  }

  // ── see ────────────────────────────────────────────────────────────────────
  ctx.tools.register(
    defineTool({
      name: 'see',
      description:
        '截取整个屏幕或某个窗口，并把截图以图片形式返回，让模型直接查看画面内容（描述、识别截图文字、读取图表/文档）。' +
        '用 window 指定窗口标题子串（如 "Visual Studio Code"），或用 handle 传入 list_windows 给出的精确句柄（更可靠，避免标题撞车）；留空则截全屏。' +
        '默认尽量别传 maximize=true：非最小化窗口会直接抓到其真实内容，且不切换前台、不抢焦点。' +
        '⚠️ 但「不抢前台」只对**普通窗口**成立：目标**处于最小化**时，抓取会先把它还原、因而**会抢走前台**' +
        '（抓完几何会还原成最小化，但前台已经变了）；兜底抓取路径（WGC 与 PrintWindow 都失败、改读合成桌面区域）' +
        '同样会置前。所以不要假设抓完前台没变——尤其在用户正在别处打字时。' +
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
        format: { type: 'string', description: 'PNG/JPEG/WEBP/GIF，默认 PNG' },
        ocr: { type: 'boolean', description: '可选：同一截图额外做 OCR 并返回 text/lines/words' },
        text: {
          type: 'boolean',
          description:
            '可选：返回可点击元素（每个含 text 与屏幕绝对坐标 screen_center，可直接喂给 click）。与 ocr 的区别：ocr 给词框（图片坐标），text 给合并后的控件与屏幕坐标',
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
          const blocks: Array<{ type: string; attachment?: ImageAttachmentRef; text?: string }> = [
            { type: 'image', attachment: (value as any).ref as ImageAttachmentRef },
          ]
          const text = String((value as any).text ?? '')
          if (text) blocks.push({ type: 'text', text })
          const words = Array.isArray((value as any).words) ? ((value as any).words as Row[]) : []
          if (words.length) {
            blocks.push({ type: 'text', text: '\nword_boxes (x,y,w,h):\n' + JSON.stringify(words, null, 2) })
          }
          const elements = Array.isArray((value as any).elements) ? ((value as any).elements as Row[]) : []
          if (elements.length) {
            // 只给模型真正要用的：点哪个、点哪里。图片坐标 box 在这里没用（click 吃屏幕坐标），
            // 全塞进去只会白占 context。
            const rows = elements.map((element) => {
              const point = ((element as any).screen_center ?? {}) as { x?: number; y?: number }
              return `  ${String((element as any).text ?? '')}  → click(${String(point.x ?? '?')}, ${String(point.y ?? '?')})`
            })
            const total = Number((value as any).element_total ?? elements.length)
            // 截断时必须告诉模型**怎么把剩下的拿到**：元素按 (y,x) 排序，列表长（实测一个资源管理器
            // 窗口就有 100+ 个）时前 N 个只覆盖屏幕上半部分，模型会以为「目标不在这个界面上」。
            const head =
              total > elements.length
                ? `\nclickable elements（${elements.length}/${total}，已截断；要拿全请传 max_elements=${total}）:`
                : `\nclickable elements（${elements.length}）:`
            blocks.push({ type: 'text', text: head + '\n' + rows.join('\n') })
          }
          return blocks as any
        },
      },
      timeoutMs: 90000,
      async execute(args, exec) {
        assertCvisionPresent()
        const req: Json = { format: (args.format ?? 'PNG').toUpperCase() }
        if (args.handle != null) req.handle = args.handle
        if (args.window) req.window = String(args.window)
        if (args.maximize) req.maximize = true
        if (args.region) req.region = String(args.region)
        if (args.delay) req.delay = Number(args.delay)
        if (args.text) req.text = true
        const { dataUrl, elements, handle, frame } = await captureDataUrl(req, exec)
        // 记住这次看的是哪个窗口：后续 click/scroll/drag/type_text 会先把它置前、并校验点击坐标
        // 确实属于它。返回值没带 handle 时退回参数里的 handle（例如 see(handle=H) 不带 text 的用法）。
        const target = handle ?? positiveInt(args.handle)
        if (target != null) lastSeeHandle = target
        // 记住这张图占了屏幕哪一块：click_at 按比例换算屏幕坐标全靠它。
        if (frame) lastSeeFrame = frame
        const { data, mediaType, ext } = parseDataUrl(dataUrl)
        const ref = await ctx.attachments.saveImage({ data, mediaType, name: `vision-capture.${ext}` })
        const out: {
          ref: Record<string, JsonValue>
          text?: string
          lines?: string[]
          words?: Row[]
          elements?: Row[]
          element_total?: number
        } = {
          ref: ref as unknown as Record<string, JsonValue>,
        }
        if (args.ocr) {
          const o = await ocrJson(req, exec)
          out.text = o.text
          out.lines = o.lines
          out.words = o.words
        }
        if (args.text) {
          // 元素已按 (y, x) 排序，取前 N 个即「从上到下、从左到右」，正好是最可能先被点的区域。
          const limit = Math.max(1, Number(args.max_elements ?? 60))
          out.elements = elements.slice(0, limit)
          out.element_total = elements.length
        }
        return out
      },
    }),
  )

  // ── wait_until_changed（轮询到变化才返回，省 token） ────────────────────────
  ctx.tools.register(
    defineTool({
      name: 'wait_until_changed',
      description:
        '轮询截图，直到画面**真的变了**才把那一帧作为图片返回（等进度条跑完、等弹窗出现、等加载完成）。' +
        '比连着截好几张图都塞给模型省得多：你不需要看 N 张相似图，只需要知道「变没变、变在哪」。' +
        '返回 changed（是否等到变化）/ diff_ratio（变化像素占比）/ diff_bbox（变化区域，原图坐标）。' +
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
            // Python 侧每次都会给出截图尺寸；漏声明会被 additionalProperties: false 判非法。
            width: { type: 'integer' },
            height: { type: 'integer' },
          },
          additionalProperties: false,
        },
        render: (_args, value) => {
          const changed = (value as any).changed === true
          const ratio = Number((value as any).diff_ratio ?? 0)
          const head = changed
            ? `画面已变化（差异 ${(ratio * 100).toFixed(2)}% 像素，用了 ${String((value as any).elapsed_ms ?? '?')}ms / ${String((value as any).samples ?? '?')} 次采样）`
            : `超时且画面未变化（最大差异 ${(ratio * 100).toFixed(2)}% 像素，${String((value as any).elapsed_ms ?? '?')}ms / ${String((value as any).samples ?? '?')} 次采样）`
          const box = (value as any).diff_bbox
          const detail = box ? `\n变化区域（原图坐标）: x=${box.x} y=${box.y} w=${box.w} h=${box.h}` : ''
          return [
            { type: 'image', attachment: (value as any).ref as ImageAttachmentRef },
            { type: 'text', text: head + detail },
          ] as any
        },
      },
      timeoutMs: 60000,
      async execute(args, exec) {
        assertCvisionPresent()
        const req: Json = { format: (args.format ?? 'PNG').toUpperCase() }
        if (args.handle != null) req.handle = args.handle
        if (args.window) req.window = String(args.window)
        if (args.maximize) req.maximize = true
        if (args.region) req.region = String(args.region)
        if (args.interval) req.interval = Number(args.interval)
        if (args.timeout) req.timeout = Number(args.timeout)
        if (args.threshold != null) req.threshold = Number(args.threshold)
        const { dataUrl, meta } = await waitChangedJson(req, exec)
        const { data, mediaType, ext } = parseDataUrl(dataUrl)
        const ref = await ctx.attachments.saveImage({ data, mediaType, name: `vision-wait.${ext}` })
        return { ref: ref as unknown as Record<string, JsonValue>, ...meta }
      },
    }),
  )

  // ── ocr（返回 text/lines/words） ──────────────────────────────────────────
  ctx.tools.register(
    defineTool({
      name: 'ocr',
      description:
        '截取屏幕/窗口（可 region/delay），用 OCR 识别其中的文字并**返回文本**（含词级边界框 words）。' +
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
          const text = String(value.text ?? '')
          const lines = Array.isArray(value.lines) ? (value.lines as string[]).filter(Boolean) : []
          const blocks: Array<{ type: string; text?: string }> = [
            { type: 'text', text: lines.length > 1 ? lines.join('\n') : text },
          ]
          const words = Array.isArray((value as any).words) ? ((value as any).words as Row[]) : []
          if (words.length) {
            blocks.push({ type: 'text', text: '\nword_boxes (x,y,w,h):\n' + JSON.stringify(words, null, 2) })
          }
          return blocks as any
        },
      },
      timeoutMs: 90000,
      async execute(args, exec) {
        assertCvisionPresent()
        const req: Json = {}
        if (args.handle != null) req.handle = args.handle
        if (args.window) req.window = String(args.window)
        if (args.maximize) req.maximize = true
        if (args.region) req.region = String(args.region)
        if (args.delay) req.delay = Number(args.delay)
        return await ocrJson(req, exec)
      },
    }),
  )

  // ── list_windows ──────────────────────────────────────────────────────────
  ctx.tools.register(
    defineTool({
      name: 'list_windows',
      description:
        '列出当前可见的顶层 Windows 窗口（标题 + 句柄 + 尺寸）。用于让模型先找到要看的窗口，' +
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
        const windows = await listWindowsJson(exec)
        return { windows }
      },
    }),
  )

  // ── screen_info（显示器/DPI 布局） ─────────────────────────────────────────
  ctx.tools.register(
    defineTool({
      name: 'screen_info',
      description:
        '列出显示器/DPI 布局（每屏 x/y/width/height/primary/scale）。高 DPI 下模型需据此折算屏幕坐标，' +
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
        const displays = await screenInfoJson(exec)
        return { displays }
      },
    }),
  )

  // ── cvision_status（运行环境健康） ─────────────────────────────────────────
  ctx.tools.register(
    defineTool({
      name: 'cvision_status',
      description:
        '检查 cvision 运行环境：python 版本、平台后端、OCR 引擎、依赖（Pillow/pyautogui/pywin32/winsdk）可达性，' +
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
        const status = await statusJson(exec)
        return { status }
      },
    }),
  )

  // ── wait_for_window（轮询等窗口出现） ───────────────────────────────────────
  ctx.tools.register(
    defineTool({
      name: 'wait_for_window',
      description:
        '轮询等待某个窗口出现（按标题子串），直到命中或超时。用于「打开某应用后等它出现再抓图」。' +
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
        const title = String(args.title || '').trim()
        if (!title) throw new Error('wait_for_window 需要 title')
        const timeout = Math.max(0, Number(args.timeout ?? 10000))
        const start = Date.now()
        const needle = title.toLowerCase()
        for (;;) {
          exec.signal.throwIfAborted()
          const windows = await listWindowsJson(exec)
          const hit = (windows as any[]).find((w: any) => String(w.title ?? '').toLowerCase().includes(needle))
          if (hit) return { found: true, window: hit, detail: `found: ${hit.title}` }
          if (Date.now() - start >= timeout) return { found: false, detail: `timeout after ${timeout}ms; no window title contains "${title}"` }
          await new Promise((r) => setTimeout(r, 500))
        }
      },
    }),
  )

  // ── 用户级操作（computer-use） ─────────────────────────────────────────────
  const inputOut = {
    schema: {
      type: 'object' as const,
      properties: { ok: { type: 'boolean' as const } },
      additionalProperties: false as const,
    },
    render: (_a: unknown, v: { ok?: boolean }) => [
      { type: 'text' as const, text: v.ok ? '已执行' : '未执行' },
    ],
  }

  /**
   * `click_at` 的输出：除了成功标志，还回报**换算出来的屏幕坐标**。
   *
   * 回报它是为了让模型能核对自己「按比例指的到底是哪个点」——比例估偏时一眼就能看出落点漂到哪儿了。
   * ⚠️ schema 声明了 `additionalProperties: false`，多一个没声明的键会让**整次调用**失败
   * （v0.2.24 的 `wait_until_changed` 正是栽在这上面），所以 x/y 必须如实声明。
   */
  const pointOut = {
    schema: {
      type: 'object' as const,
      properties: {
        ok: { type: 'boolean' as const },
        x: { type: 'integer' as const },
        y: { type: 'integer' as const },
      },
      additionalProperties: false as const,
    },
    render: (_a: unknown, v: { ok?: boolean; x?: number; y?: number }) => [
      { type: 'text' as const, text: v.ok ? `已点击屏幕坐标 (${v.x ?? '?'}, ${v.y ?? '?'})` : '未执行' },
    ],
  }

  ctx.tools.register(
    defineTool({
      name: 'click',
      description: '在屏幕绝对坐标 (x,y) 模拟鼠标单击。先 see 确认目标位置后再点。点击前会自动把「你最近一次 see 的窗口」置前，并校验该坐标确实属于它；校验不过则报错，避免点到压在上面的窗口上。',
      parameters: { x: { type: 'integer' }, y: { type: 'integer' }, button: { type: 'string', description: 'left/right/middle，默认 left' } },
      output: inputOut,
      timeoutMs: 30000,
      async execute(args, exec) {
        const cmd = ['--click', String(args.x), String(args.y)]
        if (args.button && args.button !== 'left') cmd.push('--button', String(args.button))
        await ensureTargetFront(exec, { x: Number(args.x), y: Number(args.y) })
        await runCliInputTracked(cmd, exec)
        return { ok: true }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'click_at',
      description:
        '按**比例**点击「你最近一次 see 那张图」上的位置：rx/ry 是 0~1 的比例（左上角 0,0；右下角 1,1）。' +
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
        const frame = lastSeeFrame
        if (!frame) {
          throw new Error('click_at 需要先 see 一次——比例是相对「你最近看到的那张图」的（若上次 see 走的是 CLI 回退路径，请改用 see(text=true)）')
        }
        const rx = Number(args.rx)
        const ry = Number(args.ry)
        if (!Number.isFinite(rx) || !Number.isFinite(ry) || rx < 0 || rx > 1 || ry < 0 || ry > 1) {
          throw new Error(`click_at 的 rx/ry 必须落在 0~1：收到 rx=${String(args.rx)} ry=${String(args.ry)}`)
        }
        const x = Math.round(frame.x + rx * frame.width)
        const y = Math.round(frame.y + ry * frame.height)
        const cmd = ['--click', String(x), String(y)]
        if (args.button && args.button !== 'left') cmd.push('--button', String(args.button))
        await ensureTargetFront(exec, { x, y })
        await runCliInputTracked(cmd, exec)
        return { ok: true, x, y }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'double_click',
      description: '在屏幕绝对坐标 (x,y) 模拟鼠标双击。',
      parameters: { x: { type: 'integer' }, y: { type: 'integer' } },
      output: inputOut,
      timeoutMs: 30000,
      async execute(args, exec) {
        await ensureTargetFront(exec, { x: Number(args.x), y: Number(args.y) })
        await runCliInputTracked(['--double', String(args.x), String(args.y)], exec)
        return { ok: true }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'mouse_move',
      description: '把鼠标移到屏幕绝对坐标 (x,y)（不点击）。',
      parameters: { x: { type: 'integer' }, y: { type: 'integer' } },
      output: inputOut,
      timeoutMs: 30000,
      async execute(args, exec) {
        await runCliInputTracked(['--move', String(args.x), String(args.y)], exec)
        return { ok: true }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
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
        await ensureTargetFront(exec, { x: Number(args.x), y: Number(args.y) })
        if (args.dx) {
          await runCliInputTracked(['--scroll-h', String(args.x), String(args.y), String(args.dx)], exec)
        } else {
          await runCliInputTracked(['--scroll', String(args.x), String(args.y), String(args.dy ?? 0)], exec)
        }
        return { ok: true }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
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
        const cmd = ['--drag', String(args.x1), String(args.y1), String(args.x2), String(args.y2)]
        if (args.button && args.button !== 'left') cmd.push('--button', String(args.button))
        await ensureTargetFront(exec, { x: Number(args.x1), y: Number(args.y1) })
        await runCliInputTracked(cmd, exec)
        return { ok: true }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'type_text',
      description: '像键盘一样输入文本（到当前焦点）。例如输入到地址栏/输入框，可配合 ctrl+l 先聚焦。',
      parameters: { text: { type: 'string', description: '要输入的文本' } },
      output: inputOut,
      timeoutMs: 30000,
      async execute(args, exec) {
        await ensureTargetFront(exec)
        await runCliInputTracked(['--type', String(args.text)], exec)
        return { ok: true }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'press_key',
      description: '发送快捷键/按键，如 "ctrl+l"（聚焦地址栏）、"enter"、"ctrl+shift+t"（新标签）、"alt+tab"。',
      parameters: { keys: { type: 'string', description: '按键组合，如 ctrl+l / enter / ctrl+shift+t' } },
      output: inputOut,
      timeoutMs: 30000,
      async execute(args, exec) {
        await ensureTargetFront(exec)
        await runCliInputTracked(['--keys', String(args.keys)], exec)
        return { ok: true }
      },
    }),
  )

  const clipboardOut = {
    schema: {
      type: 'object' as const,
      properties: { ok: { type: 'boolean' as const }, text: { type: 'string' as const } },
      additionalProperties: false as const,
    },
    render: (_a: unknown, v: { ok?: boolean; text?: string }) => [
      { type: 'text' as const, text: v.ok && v.text != null ? String(v.text) : (v.ok ? '已执行' : '未执行') },
    ],
  }

  ctx.tools.register(
    defineTool({
      name: 'get_clipboard',
      description: '读取当前剪贴板文本（Windows 原生；其他平台需 pyperclip）。',
      parameters: {},
      output: clipboardOut,
      timeoutMs: 30000,
      async execute(_args, exec) {
        const { stdout } = await execFileAsync(PYTHON, ['-m', 'cvision.cli_input', '--get-clipboard'], {
          cwd: PY_CWD,
          env: PY_ENV,
          maxBuffer: 4 * 1024 * 1024,
          signal: exec.signal,
        })
        const info = JSON.parse(stdout) as { text?: string }
        return { ok: true, text: info.text ?? '' }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'set_clipboard',
      description: '把文本写入剪贴板（Windows 原生；其他平台需 pyperclip）。',
      parameters: { text: { type: 'string', description: '要写入剪贴板的文本' } },
      output: clipboardOut,
      timeoutMs: 30000,
      async execute(args, exec) {
        await runCliInputTracked(['--set-clipboard', String(args.text)], exec)
        return { ok: true, text: String(args.text) }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
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
          throw new Error('focus_window 需要提供 handle（窗口句柄）或 title（窗口标题）之一')
        }
        const cmd = args.handle != null ? ['--focus-handle', String(args.handle)] : ['--focus', String(args.title)]
        const info = await runCliInputJson(cmd, exec)
        if (!info.ok) {
          // 如实报错：旧实现无条件返回 {ok:true}，调用方以为窗口已经在前台，接着点击就点到
          // 压在上面的别的窗口上了。
          throw new Error(String(info.error ?? '窗口未能置前'))
        }
        const target = positiveInt(info.handle) ?? positiveInt(args.handle)
        if (target != null) lastSeeHandle = target // 刚置前的窗口就是接下来的操作目标
        return { ok: true }
      },
    }),
  )
}
