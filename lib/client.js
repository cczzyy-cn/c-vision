/**
 * C-Vision 客户端半边（浏览器）：输入框工具栏的「截图」按钮。
 *
 * bundle 形态：DSH 的客户端模块系统把本包 `exports["./client"]` 当**经典脚本**
 * 直接送进浏览器，脚本必须调用 `window.__ModuleLoader__.load({ id, factory })`
 * 注册工厂；`id` 必须等于包名（`vision`）。factory 里只能用模块表提供的
 * `require`（此处仅 `react`），不引入打包器。
 *
 * ── 本次修复（相对上游 @deepseek-ai/dsh-client-ui-screenshot v0.1.1）──
 * 上游的可见性判定是：
 *     if (model.inputModalities?.includes('image') === true) return true
 *     return /vision|visual/.test((model.id + ' ' + model.name).toLowerCase())
 * 但 DSH 给浏览器的模型目录是 `buildModelCatalog()` 投影过的
 * （session-controller/lib/index.js：只留 id/name/description/reasoning），
 * **inputModalities 被刻意剥掉**，因此第一行在浏览器里恒为 false。于是实际生效的
 * 只有「模型名字里有没有 vision|visual」这条字符串猜测：
 *   - 声明了 image、但名字不含 vision 的模型（例如 deepseek-v4.1）永远看不到按钮；
 *   - 名字带 vision、实际不收图的模型反而会误显示。
 * 现在改为向**本插件自己的宿主半边**查真实适配器目录：
 *     GET /cvision/model-capability?provider=<id>&model=<id>
 *     → { source: 'declared', image: boolean }   按真实 inputModalities 回答
 *     → { source: 'unknown' }                    该路由解析不出来
 * 宿主可达时以宿主为准；宿主不可达（非 web 组合，如 Electron 的 file://）时退回
 * 上面的名字启发式，保证按钮不会因为缺少 web 服务器而彻底消失。
 */
window.__ModuleLoader__.load({
  id: 'vision',
  factory: (require) => {
    const module = { exports: {} }
    const exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const react = require('react')

    //#region 能力查询：宿主路由 → 客户端缓存
    /** 与宿主半边 `src/index.ts` 的 MODEL_CAPABILITY_PATH 必须一致。 */
    const CAPABILITY_PATH = '/cvision/model-capability'
    /** 与宿主半边 `src/index.ts` 的 SNIP_PATH 必须一致：系统级框选截图。 */
    const SNIP_PATH = '/cvision/snip'
    /**
     * `provider\0model` → 'yes' | 'no' | 'error'（'pending' 是未落地的在途态）。
     * 'error' 表示宿主答不上来（路由缺失/网络失败/组合里没有 web 服务器），
     * 此时才允许退回名字启发式；'no' 是权威否定，不再猜。
     */
    const verdicts = new Map()
    const listeners = new Set()

    /** 选区在缓存里的键；无选区（会话尚未解析出模型）时为 null。 */
    function selectionKey(selection) {
      if (selection === null || selection === undefined) return null
      return String(selection.provider) + '\u0000' + String(selection.model)
    }

    /** useSyncExternalStore 的订阅面（模块级稳定函数，避免每次渲染换引用）。 */
    function subscribeVerdicts(listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    }

    function notifyVerdicts() {
      for (const listener of Array.from(listeners)) listener()
    }

    /** 查一次宿主能力并落缓存；同一 model 只查一次（含在途态去重）。 */
    function requestVerdict(selection) {
      const key = selectionKey(selection)
      if (key === null || verdicts.has(key)) return
      verdicts.set(key, 'error')
      if (typeof fetch !== 'function') return
      const url =
        CAPABILITY_PATH +
        '?provider=' +
        encodeURIComponent(String(selection.provider)) +
        '&model=' +
        encodeURIComponent(String(selection.model))
      fetch(url, { headers: { accept: 'application/json' } })
        .then(async (response) => {
          if (!response.ok) return
          const body = await response.json()
          if (body === null || typeof body !== 'object' || body.source !== 'declared') return
          verdicts.set(key, body.image === true ? 'yes' : 'no')
        })
        .catch(() => {
          /* 保留 'error'：宿主不可达时退回启发式 */
        })
        .then(notifyVerdicts)
    }

    /**
     * 当前选区的宿主结论。'pending' 与 'no' 都渲染为「不显示」——在途时宁可先不显示，
     * 也不要先按名字猜测闪出一个可能错误的按钮；只有 'error' 才交给启发式。
     */
    function useVerdict(selection) {
      const key = selectionKey(selection)
      const wanted = key === null ? null : selection
      const verdict = react.useSyncExternalStore(subscribeVerdicts, () =>
        key === null ? 'no' : verdicts.get(key) ?? 'pending',
      )
      react.useEffect(() => {
        if (wanted !== null) requestVerdict(wanted)
      }, [key])
      return verdict
    }
    //#endregion

    //#region 名字启发式（仅宿主不可达时的兜底）
    /** 当前模型在目录里的条目（用于兜底判定；找不到返回 null）。 */
    function currentModel(state) {
      const current = state.current
      if (current === null || current === undefined) return null
      for (const group of state.groups) {
        if (group.id !== current.provider) continue
        for (const model of group.models) {
          if (model.id === current.model) return model
        }
      }
      return null
    }

    /** 上游口径的名字猜测：id/name 命中 vision|visual。 */
    function nameSuggestsImage(state) {
      const model = currentModel(state)
      if (model === null) return false
      return /vision|visual/.test((String(model.id) + ' ' + String(model.name || '')).toLowerCase())
    }
    //#endregion

    //#region 屏幕捕获
    /** 当前运行时是否具备屏幕捕获 API。 */
    function canCaptureScreen() {
      return (
        typeof navigator !== 'undefined' &&
        typeof navigator.mediaDevices?.getDisplayMedia === 'function' &&
        typeof document !== 'undefined' &&
        typeof HTMLCanvasElement !== 'undefined'
      )
    }

    /**
     * 等 `<video>` 真正呈现第一帧。首帧未就绪就 drawImage 会得到黑帧，所以要等到
     * `requestVideoFrameCallback`（或尺寸可用后的两帧 requestAnimationFrame）。
     */
    async function waitForFrame(video) {
      const frame = new Promise((resolve) => {
        if (typeof video.requestVideoFrameCallback === 'function') {
          video.requestVideoFrameCallback(() => {
            resolve()
          })
          return
        }
        const poll = (attempt) => {
          if (video.videoWidth > 0 && video.videoHeight > 0) {
            requestAnimationFrame(() =>
              requestAnimationFrame(() => {
                resolve()
              }),
            )
            return
          }
          if (attempt > 100) return
          setTimeout(() => {
            poll(attempt + 1)
          }, 40)
        }
        poll(0)
      })
      await Promise.race([
        frame,
        new Promise((resolve) => {
          setTimeout(resolve, 1500)
        }),
      ])
    }

    /**
     * 让用户选一个屏幕/窗口/标签页，抓一帧为 PNG File。
     * @returns PNG 文件；用户取消、拒绝授权或运行时无捕获 API 时返回 null。
     */
    async function captureScreen() {
      if (!canCaptureScreen()) return null
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true })
      try {
        const video = document.createElement('video')
        video.muted = true
        video.playsInline = true
        video.srcObject = stream
        await video.play().catch(() => {})
        await waitForFrame(video)
        const width = video.videoWidth
        const height = video.videoHeight
        if (width <= 0 || height <= 0) return null
        const canvas = document.createElement('canvas')
        canvas.width = width
        canvas.height = height
        const context = canvas.getContext('2d')
        if (context === null) return null
        context.drawImage(video, 0, 0, width, height)
        const blob = await new Promise((resolve) => {
          canvas.toBlob(resolve, 'image/png')
        })
        if (blob === null) return null
        return new File([blob], 'screen-' + Date.now() + '.png', { type: 'image/png' })
      } finally {
        for (const track of stream.getTracks()) track.stop()
      }
    }
    //#endregion

    //#region 样式
    const BUTTON_CLASS = 'cvision-screenshot-button'
    const NOTICE_CLASS = 'cvision-screenshot-notice'
    const CSS =
      '.' +
      BUTTON_CLASS +
      '{width:28px;height:28px;color:var(--dsh-foreground-2,#000000a6);cursor:pointer;background:0 0;border:1px solid #0000;border-radius:8px;justify-content:center;align-items:center;padding:0;transition:background-color .1s,color .1s;display:inline-flex}' +
      '.' +
      BUTTON_CLASS +
      ':hover{background:var(--dsh-surface-2,#0000000f);color:var(--dsh-foreground-1,#000000e6)}' +
      '.' +
      BUTTON_CLASS +
      ':focus-visible{outline:2px solid var(--dsh-accent,#3b82f6);outline-offset:1px}' +
      '.' +
      NOTICE_CLASS +
      '{max-width:180px;color:var(--dsh-alias-state-error-primary,#d4380d);font-size:12px;line-height:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    const CSS_TAG_ID = 'vision/screenshot-button.css'
    if (typeof document !== 'undefined' && document.querySelector('style[data-plugin-css="' + CSS_TAG_ID + '"]') === null) {
      const tag = document.createElement('style')
      tag.dataset.plugin = 'vision'
      tag.dataset.pluginCss = CSS_TAG_ID
      tag.textContent = CSS
      document.head.appendChild(tag)
    }
    //#endregion

    //#region 语言字典
    /** 字典命名空间（slot 的 locale 与 ctx.locale.register 必须同名）。 */
    const NS = 'vision'
    /** 简体中文（键集基准）。 */
    const zh = {
      'button.aria': '截图并插入到输入框',
      'button.tooltip': '截图（系统框选，可标注）',
      'button.waiting': '请在系统截图里框选（Esc 取消）',
      'failure.capture': '截图失败',
      'failure.draft': '截图无法进入附件栏',
      'failure.busy': '输入框正忙，请稍后重试',
    }
    /** English，与 zh 键集一一对应。 */
    const en = {
      'button.aria': 'Capture a screenshot and insert it into the input',
      'button.tooltip': 'Screenshot (system region capture)',
      'button.waiting': 'Pick a region in the system screenshot tool (Esc cancels)',
      'failure.capture': 'Screenshot failed',
      'failure.draft': 'The screenshot could not enter the attachment rail',
      'failure.busy': 'The composer is busy; try again shortly',
    }
    //#endregion

    //#region 提示与在途态
    /**
     * 最近一次失败的提示文本（模块级小 store，6 秒后自动消失）。
     * 上游把插入链上每个失败点都写成 `return null` / `catch {}`，于是「点了没反应」
     * 是唯一的表现——这里改成控制台 + 按钮旁短提示，失败必须看得见。
     */
    let noticeText = null
    let noticeSeq = 0
    const noticeListeners = new Set()

    /** 系统截图在途（等待用户框选）；期间按钮禁用，免得重复拉起系统 UI。 */
    let snipPending = false
    const snipListeners = new Set()

    function subscribeSnip(listener) {
      snipListeners.add(listener)
      return () => {
        snipListeners.delete(listener)
      }
    }

    function readSnipPending() {
      return snipPending
    }

    function setSnipPending(next) {
      if (snipPending === next) return
      snipPending = next
      for (const listener of Array.from(snipListeners)) listener()
    }

    /** 把任意抛出物整理成一行可读信息。 */
    function describeError(error) {
      return String((error && error.message) || error)
    }

    function subscribeNotice(listener) {
      noticeListeners.add(listener)
      return () => {
        noticeListeners.delete(listener)
      }
    }

    function readNotice() {
      return noticeText
    }

    function reportFailure(text, error) {
      const detail = error === undefined || error === null ? '' : '：' + String((error && error.message) || error)
      console.error('[vision] ' + text + detail)
      noticeSeq += 1
      const seq = noticeSeq
      noticeText = text
      for (const listener of Array.from(noticeListeners)) listener()
      setTimeout(() => {
        if (noticeSeq !== seq) return
        noticeText = null
        for (const listener of Array.from(noticeListeners)) listener()
      }, 6000)
    }
    //#endregion

    //#region 系统截图（默认通道）
    /**
     * 请宿主拉起**系统级框选截图**（Windows 的 Win+Shift+S / macOS 的 screencapture -i），
     * 把用户在系统 UI 里框出来的那张图取回来做成一个浏览器 File。
     *
     * 走这条通道的好处：框选与标注都是系统原生、跨显示器、且**截图授权由系统 UI 天然承载**
     * （宿主只读「用户刚放进剪贴板/文件的那张新图」，不存在静默抓屏）。
     * @returns 抓到图返回 File；用户在系统 UI 里取消（204）返回 null；
     *   宿主这条通道不可用（非桌面平台 / 组合里没有 web 服务器 / 旧版宿主）返回 'unavailable'。
     */
    async function snipViaHost() {
      if (typeof fetch !== 'function') return 'unavailable'
      let response
      try {
        response = await fetch(SNIP_PATH, { method: 'POST', headers: { accept: 'image/png' } })
      } catch (error) {
        console.warn('[vision] 宿主系统截图不可达，回退浏览器抓屏：' + describeError(error))
        return 'unavailable'
      }
      if (response.status === 204) return null
      if (!response.ok) {
        const detail = await response.text().catch(() => '')
        console.warn('[vision] 宿主系统截图不可用（' + String(response.status) + '）→ 回退浏览器抓屏：' + detail)
        return 'unavailable'
      }
      const blob = await response.blob().catch(() => null)
      if (blob === null || blob.size === 0) return null
      const type = blob.type === '' ? 'image/png' : blob.type
      return new File([blob], 'snip-' + Date.now() + '.png', { type })
    }
    //#endregion

    //#region 按钮
    /** 用户取消/拒绝授权是正常操作，不该弹提示。 */
    function isCaptureCancellation(error) {
      const name = error === null || error === undefined ? '' : String(error.name || '')
      return name === 'NotAllowedError' || name === 'AbortError'
    }

    /**
     * 截图落成草稿图并塞进输入框附件栏。
     *
     * **默认通道是宿主系统截图**（用户在自己的系统截图 UI 里框选，可标注）：抓屏授权由系统
     * UI 承载，DPI/多显示器由系统保证，也不用浏览器那一套「选择器 + 分享」。只有宿主这条
     * 通道不可用或明确不支持时，才回退到浏览器 `getDisplayMedia`。
     *
     * 之后三步（建草稿 / 入轨 / 失败提示）与通道无关，且**每一步失败都要让用户看得见**：
     * 上游把三种情况都写成了静默返回，结果就是「点了没反应」——那正是它没被发现的 bug。
     * @param props - slot 注入面。
     */
    async function insertScreenshot(props) {
      let file = null
      const hosted = await snipViaHost()
      if (hosted === 'unavailable') {
        try {
          file = await captureScreen()
        } catch (error) {
          // 取消/拒绝：用户自己的选择，静默；其余（无 API、设备错误）说清楚。
          if (!isCaptureCancellation(error)) reportFailure(props.t('failure.capture'), error)
          return
        }
      } else {
        file = hosted
      }
      if (file === null) return
      let id = null
      try {
        id = props.createDraft(file)
      } catch (error) {
        reportFailure(props.t('failure.draft'), error)
        return
      }
      if (id === null) {
        reportFailure(props.t('failure.draft'))
        return
      }
      // 输入框处于裁决/提交相位时会拒绝入轨并返回 false：短暂重试，别把用户刚截的图丢掉。
      for (let attempt = 0; attempt < 6; attempt += 1) {
        if (addDraftIds(props.inputActions, [id])) return
        await new Promise((resolve) => {
          setTimeout(resolve, 200)
        })
      }
      props.releaseDraft(id)
      reportFailure(props.t('failure.busy'))
    }

    /**
     * 按钮点击入口：置在途态 → 走截图 → 无论成败都收尾。
     * 在途期间按钮 disabled，避免重复拉起系统截图 UI。
     * @param props - slot 注入面。
     */
    async function runScreenshot(props) {
      if (readSnipPending()) return
      setSnipPending(true)
      try {
        await insertScreenshot(props)
      } finally {
        setSnipPending(false)
      }
    }

    /**
     * 输入框工具栏截图按钮。可见性由宿主能力（首选）或名字启发式（兜底）决定；
     * 不支持图片输入的模型渲染为空——按钮消失，插件本身不受影响。
     * @param props - slot 注入面：目录快照、草稿图操作、输入动作与字典。
     */
    function ScreenshotButton(props) {
      const state = react.useSyncExternalStore(props.directory.subscribe, props.directory.getSnapshot)
      const verdict = useVerdict(state.current)
      const notice = react.useSyncExternalStore(subscribeNotice, readNotice)
      const busy = react.useSyncExternalStore(subscribeSnip, readSnipPending)
      if (verdict !== 'yes' && !(verdict === 'error' && nameSuggestsImage(state))) return null
      const button = react.createElement(
        'button',
        {
          type: 'button',
          className: BUTTON_CLASS,
          title: busy ? props.t('button.waiting') : props.t('button.tooltip'),
          'aria-label': props.t('button.aria'),
          'aria-busy': busy ? 'true' : 'false',
          disabled: busy,
          onMouseDown: (event) => {
            event.preventDefault()
          },
          onClick: () => {
            void runScreenshot(props)
          },
        },
        react.createElement(
          'svg',
          { viewBox: '0 0 16 16', width: '16', height: '16', 'aria-hidden': true },
          react.createElement('path', {
            d: 'M4 3h1.6l.8-1h3.2l.8 1H12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zm0 1.5A.5.5 0 0 0 3.5 5v6a.5.5 0 0 0 .5.5h8a.5.5 0 0 0 .5-.5V5a.5.5 0 0 0-.5-.5H4zm4 .5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z',
            fill: 'currentColor',
          }),
        ),
      )
      if (notice === null) return button
      return react.createElement(
        react.Fragment,
        null,
        react.createElement('span', { className: NOTICE_CLASS, role: 'status', title: notice }, notice),
        button,
      )
    }
    //#endregion

    //#region 插件体
    /**
     * 需要的客户端服务。
     *
     * `sessions` / `remote.session` 不是本插件直接使用的功能，而是**被调用的服务在调用方
     * fiber 上校验 inject 时需要的**：DSH 的 cordis Service tracker 会把服务方法的 `this.ctx`
     * 重绑到调用方上下文，于是 `modelDirectories.directoryFor()` 内部读 `ctx.remote.session`、
     * conversation 的会话级方法读 `ctx.sessions` 时，检查的都是**本插件**的 inject 表。
     * 上游只声明了四个服务，因此那条链在第三方 fiber 上会抛
     * `cannot get property "remote.session" without inject`。
     */
    const inject = ['slots', 'locale', 'modelDirectories', 'conversation', 'sessions', 'remote.session']

    /** 惰性取服务：apply 时刻服务可能尚未就绪，eager 缓存会拿到 undefined 然后静默失效。 */
    function serviceOf(ctx, name) {
      try {
        return ctx.get(name)
      } catch {
        return undefined
      }
    }

    /**
     * 取 conversation **根服务**（持有草稿 API 的单例）。
     *
     * ⚠️ 别拿成会话作用域的那个 face：`ctx.sessions.scope(id).get('conversation')` 是
     * **会话动作面**（send / cancel / updateQueue），没有草稿 API——按 sessionId 取它建草稿
     * 会得到 `createDraftAttachments/createDraftImages is not a function`。
     *
     * 服务在**调用时刻**惰性解析：上游在 `apply()` 里 eager 缓存 `ctx.conversation`，
     * 而第三方插件激活时它可能还没就绪，于是缓存下 `undefined`，之后每次插入都静默失败。
     * @param ctx - 本插件根上下文。
     */
    function conversationOwner(ctx) {
      return serviceOf(ctx, 'conversation')
    }

    /**
     * 草稿 API 的**跨版本适配**。DSH 换过一代接口：
     * - 0.1.5-rc 起（Attachments）：`createDrafts(sessionId, files)` +
     *   `inputActions.addAttachments(ids)` + `releaseDraftAttachment(id)`；
     * - 更早（Images）：`createDraftImages(files)` + `inputActions.addImages(ids)` +
     *   `releaseDraftImage(id)`。
     * 按能力探测，不写死任何一代——插件跑在哪个宿主版本上都得能用。
     * @param conversation - conversation 根服务（可能 undefined）。
     * @param sessionId - 目标会话（新 API 建草稿时需要）。
     */
    function draftApi(conversation, sessionId) {
      if (conversation === undefined) return undefined
      if (typeof conversation.createDrafts === 'function') {
        return {
          create: (file) => conversation.createDrafts(sessionId, [file]),
          release: (id) => {
            if (typeof conversation.releaseDraftAttachment === 'function') conversation.releaseDraftAttachment(id)
          },
        }
      }
      if (typeof conversation.createDraftImages === 'function') {
        return {
          create: (file) => conversation.createDraftImages([file]),
          release: (id) => {
            if (typeof conversation.releaseDraftImage === 'function') conversation.releaseDraftImage(id)
          },
        }
      }
      return undefined
    }

    /** 把草稿 id 塞进输入框附件栏；两代 API 的方法名不同。 */
    function addDraftIds(inputActions, ids) {
      if (inputActions === undefined || inputActions === null) return false
      if (typeof inputActions.addAttachments === 'function') return inputActions.addAttachments(ids)
      if (typeof inputActions.addImages === 'function') return inputActions.addImages(ids)
      return false
    }

    /** 模型目录不可用时的空目录快照（按钮优雅隐藏，不让槽位注入整体抛错）。 */
    function inertDirectory() {
      const state = { current: null, routable: null, groups: [], failures: [], status: 'idle', error: null }
      return { subscribe: () => () => {}, getSnapshot: () => state }
    }

    /**
     * 客户端插件体：注册本插件字典，并在输入框右侧挂上截图按钮。
     * @param ctx - 客户端根上下文。
     */
    function apply(ctx) {
      // 一行就绪日志：客户端 bundle 只在页面 boot 时装配，出问题时先确认加载的是哪一版。
      console.log('[vision] 截图按钮客户端半边已就绪')
      ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'vision: screenshot dictionaries')
      ctx.slots.inject('conversation.input.right', () =>
        ctx.slots.register(
          {
            name: 'conversation.input.right',
            id: 'vision-screenshot',
            order: 10,
            locale: NS,
            inject: (sessionId) => {
              let directoryStore = inertDirectory()
              try {
                const directory = ctx.modelDirectories.directoryFor(sessionId)
                directory.load().catch(() => {})
                directoryStore = directory.store
              } catch (error) {
                console.error('[vision] 模型目录不可用：' + String((error && error.message) || error))
              }
              return {
                directory: directoryStore,
                createDraft: (file) => {
                  // 每次现取：不在 apply 时缓存服务引用（上游就是在这里静默丢掉截图的）。
                  const api = draftApi(conversationOwner(ctx), sessionId)
                  if (api === undefined) {
                    console.error('[vision] conversation 服务未提供草稿 API，截图无法进入附件栏')
                    return null
                  }
                  try {
                    const [draft] = api.create(file)
                    return draft === undefined ? null : draft.id
                  } catch (error) {
                    console.error('[vision] 建立草稿失败：' + String((error && error.message) || error))
                    return null
                  }
                },
                releaseDraft: (id) => {
                  const api = draftApi(conversationOwner(ctx), sessionId)
                  if (api !== undefined) api.release(id)
                },
              }
            },
          },
          ScreenshotButton,
        ),
      )
    }
    //#endregion

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
