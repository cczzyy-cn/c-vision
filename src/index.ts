/**
 * C-Vision 视觉插件（DeepSeek Harness / DSH bundle）
 *
 * 注册观察（see/ocr/list_windows）与 computer-use（click/type_text/...）工具：
 * 跨语言调用本插件包内捆绑的 Python 版 cvision（截屏/OCR/输入），把截图写入 Harness
 * 附件服务（`ctx.attachments.saveImage`）并以 `image` ContentBlock 返回。
 *
 * v0.2.0 增强：
 *   - OCR 返回词级边界框（`words`，用于 computer-use 精确定位点击点）。
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

const MEDIA_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'] as const
type MediaType = (typeof MEDIA_TYPES)[number]

type Json = Record<string, unknown>
type Row = Record<string, JsonValue>

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

/** 运行一次用户级输入（python -m cvision.cli_input <args>）。 */
async function runCliInput(args: string[], exec: { signal: AbortSignal }): Promise<void> {
  assertCvisionPresent()
  await execFileAsync(PYTHON, ['-m', 'cvision.cli_input', ...args], {
    cwd: PY_CWD,
    env: PY_ENV,
    maxBuffer: 1 * 1024 * 1024,
    signal: exec.signal,
  })
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
  const server = new CvisionServer(30000)
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
  async function captureDataUrl(args: Json, exec: { signal: AbortSignal }): Promise<string> {
    try {
      const resp = await server.request({ op: 'capture', ...args }, exec)
      return String(resp.data_url ?? '')
    } catch {
      const cli = ['--format', String(args.format ?? 'PNG')]
      if (args.handle != null) cli.push('--handle', String(args.handle))
      if (args.window) cli.push('--window', String(args.window))
      if (args.maximize) cli.push('--maximize')
      if (args.region) cli.push('--region', String(args.region))
      if (args.delay) cli.push('--delay', String(args.delay))
      return runCliCapture(cli, exec)
    }
  }

  async function ocrJson(args: Json, exec: { signal: AbortSignal }): Promise<{ text: string; lines: string[]; words: Row[] }> {
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
    try {
      const resp = await server.request({ op: 'list' }, exec)
      return Array.isArray(resp.windows) ? (resp.windows as Row[]) : []
    } catch {
      const out = await runCliCapture(['--list'], exec)
      return JSON.parse(out) as Row[]
    }
  }

  async function screenInfoJson(exec: { signal: AbortSignal }): Promise<Row[]> {
    try {
      const resp = await server.request({ op: 'screen_info' }, exec)
      return Array.isArray(resp.displays) ? (resp.displays as Row[]) : []
    } catch {
      const out = await runCliCapture(['--screen-info'], exec)
      return JSON.parse(out) as Row[]
    }
  }

  async function statusJson(exec: { signal: AbortSignal }): Promise<Row> {
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
        '仅当窗口已最小化/太小/被遮挡看不清时才用 maximize=true（截图后会自动还原原状态）。' +
        '传 ocr=true 可在返回图片的同时附带 OCR 文本/词框（省去一次 ocr 调用）。',
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
      },
      output: {
        schema: {
          type: 'object',
          properties: {
            ref: { type: 'object', additionalProperties: true },
            text: { type: 'string' },
            lines: { type: 'array', items: { type: 'string' } },
            words: { type: 'array', items: { type: 'object', additionalProperties: true } },
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
          return blocks as any
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
        if (args.delay) req.delay = Number(args.delay)
        const dataUrl = await captureDataUrl(req, exec)
        const { data, mediaType, ext } = parseDataUrl(dataUrl)
        const ref = await ctx.attachments.saveImage({ data, mediaType, name: `vision-capture.${ext}` })
        const out: { ref: Record<string, JsonValue>; text?: string; lines?: string[]; words?: Row[] } = {
          ref: ref as unknown as Record<string, JsonValue>,
        }
        if (args.ocr) {
          const o = await ocrJson(req, exec)
          out.text = o.text
          out.lines = o.lines
          out.words = o.words
        }
        return out
      },
    }),
  )

  // ── ocr（返回 text/lines/words） ──────────────────────────────────────────
  ctx.tools.register(
    defineTool({
      name: 'ocr',
      description:
        '截取屏幕/窗口（可 region/delay），用 OCR 识别其中的文字并**返回文本**（含词级边界框 words，供 computer-use 精确定位点击）。' +
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
        '以及后端是否已实现。用于安装排错与确认能力。',
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

  ctx.tools.register(
    defineTool({
      name: 'click',
      description: '在屏幕绝对坐标 (x,y) 模拟鼠标单击。先 see 确认目标位置后再点。',
      parameters: { x: { type: 'integer' }, y: { type: 'integer' }, button: { type: 'string', description: 'left/right/middle，默认 left' } },
      output: inputOut,
      timeoutMs: 30000,
      async execute(args, exec) {
        const cmd = ['--click', String(args.x), String(args.y)]
        if (args.button && args.button !== 'left') cmd.push('--button', String(args.button))
        await runCliInput(cmd, exec)
        return { ok: true }
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
        await runCliInput(['--double', String(args.x), String(args.y)], exec)
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
        await runCliInput(['--move', String(args.x), String(args.y)], exec)
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
        if (args.dx) {
          await runCliInput(['--scroll-h', String(args.x), String(args.y), String(args.dx)], exec)
        } else {
          await runCliInput(['--scroll', String(args.x), String(args.y), String(args.dy ?? 0)], exec)
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
        await runCliInput(cmd, exec)
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
        await runCliInput(['--type', String(args.text)], exec)
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
        await runCliInput(['--keys', String(args.keys)], exec)
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
        await runCliInput(['--set-clipboard', String(args.text)], exec)
        return { ok: true, text: String(args.text) }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'focus_window',
      description: '把指定窗口置前（用户级：激活它），便于随后对它键盘/鼠标操作。只改前后层级，**不改变窗口尺寸/最大化状态**（仅最小化的窗口会被还原）。用 handle 精确（来自 list_windows），或用 title 按标题（精确标题优先）。',
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
        await runCliInput(cmd, exec)
        return { ok: true }
      },
    }),
  )
}
