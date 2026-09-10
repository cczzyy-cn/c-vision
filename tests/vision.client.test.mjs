/**
 * 客户端半边（`src/client.js`）的运行时单测。
 *
 * 这个文件在浏览器里是经典脚本、在 DSH 里是 combo script，没有 DOM 也能测：
 * 用一个最小 React 桩 + 假 `window.__ModuleLoader__` 把 bundle 求值出来，拿到
 * `factory(require)` 的导出，再用假 slot 上下文调 `apply(ctx)` 捕获被注册的组件，
 * 然后直接以组件函数的形式渲染它（React 组件就是函数）。
 *
 * 覆盖两类问题：
 * 1) 可见性判定（本次集成修复）：由宿主路由的真实能力决定，而不是「模型名字里有没有 vision」；
 * 2) 插入链（上游遗留 bug）：服务必须**惰性 + 会话作用域**解析，任何失败都必须可见。
 */
import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
import test, { mock } from 'node:test'

const SOURCE = readFileSync(new URL('../src/client.js', import.meta.url), 'utf8')

/**
 * 每次渲染期间的 hook 调用计数。
 * 真 React 在 hook 数量变化时会报 #310（Rendered more hooks than during the previous render），
 * 所以桩也必须能抓到「条件调用 hook」——v0.2.8 正是把 useEffect 放在 `return null` 之后才炸的，
 * 而当时的桩无条件放过了它。
 */
const hookCounter = { value: 0 }

/** 极简 React 桩：createElement 产出可断言的普通对象，uSES 直接读快照，effect 同步跑。 */
const reactStub = {
  createElement: (type, props, ...children) => ({ type, props, children }),
  Fragment: Symbol('Fragment'),
  useSyncExternalStore: (_subscribe, getSnapshot) => {
    hookCounter.value += 1
    return getSnapshot()
  },
  useEffect: (effect) => {
    hookCounter.value += 1
    effect()
  },
}

/** 渲染一次并记录这次用了几个 hook（用于校验跨渲染稳定）。 */
function renderWithHookCount(component, props) {
  hookCounter.value = 0
  const rendered = component(props)
  return { rendered, hooks: hookCounter.value }
}

const documentStub = {
  // 默认「隐藏」：剪贴板轮询在隐藏页面里根本不启动，这样其它用例（渲染按钮但不管剪贴板）
  // 不会留下真实定时器干扰后续用例；剪贴板用例显式把 visibilityState 设为 visible。
  visibilityState: 'hidden',
  querySelector: () => null,
  createElement: () => ({ dataset: {}, textContent: '', style: {} }),
  head: { appendChild: () => {} },
}

/** 取渲染结果里的按钮元素（有提示时外层是 Fragment）。 */
function buttonOf(rendered) {
  assert.ok(rendered, '能力允许时必须渲染出按钮')
  return rendered.type === reactStub.Fragment ? rendered.children[1] : rendered
}

/**
 * 剪贴板相关的假 fetch：`/cvision/clipboard` 依次返回给定状态（用完重复最后一个），
 * `/cvision/clipboard/image` 按 `image` 选项返回 PNG 或 204，其余（能力查询）照常返回可收图。
 */
function clipboardFetch(options = {}) {
  const states = options.states ?? [{ supported: true, image: false, token: null }]
  const calls = []
  let stateIndex = 0
  const implementation = async (url, init) => {
    const target = String(url)
    calls.push({ url: target, init })
    if (target.includes('/cvision/clipboard/image')) {
      if (options.image === 'png') {
        return {
          ok: true,
          status: 200,
          blob: async () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }),
          json: async () => ({}),
          text: async () => '',
        }
      }
      return { ok: false, status: 204, blob: async () => new Blob([]), json: async () => ({}), text: async () => '' }
    }
    if (target.includes('/cvision/snip')) {
      // 系统截图：返回一张小 PNG，便于断言「短按确实走了截图那条路」。
      return {
        ok: true,
        status: 200,
        blob: async () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }),
        json: async () => ({}),
        text: async () => '',
      }
    }
    if (target.includes('/cvision/clipboard')) {
      const state = states[Math.min(stateIndex, states.length - 1)]
      stateIndex += 1
      return { ok: true, status: 200, json: async () => state, blob: async () => new Blob([]), text: async () => '' }
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ source: 'declared', image: true }),
      blob: async () => new Blob([]),
      text: async () => '',
    }
  }
  implementation.calls = calls
  return implementation
}

/** 可见页面（剪贴板用例用）。 */
function visibleDocument() {
  return {
    visibilityState: 'visible',
    querySelector: () => null,
    createElement: () => ({ dataset: {}, textContent: '', style: {} }),
    head: { appendChild: () => {} },
  }
}

/** 把 bundle 求值一遍，返回它的导出。每次都重新求值，保证内部缓存互不串味。 */
function loadBundle() {
  let registration = null
  const windowStub = { __ModuleLoader__: { load: (value) => { registration = value } } }
  // 用例可以先用 patchGlobal('document', …) 换掉 DOM；这里跟随当前全局，缺省用最小桩。
  new Function('window', 'document', SOURCE)(windowStub, globalThis.document ?? documentStub)
  assert.ok(registration, 'bundle 必须调用 window.__ModuleLoader__.load 注册工厂')
  const exports = registration.factory((specifier) => {
    if (specifier === 'react') return reactStub
    throw new Error(`unexpected require(${specifier})`)
  })
  return { registration, exports }
}

/**
 * 用假客户端上下文跑 apply，拿到被注册的 slot 定义与组件。
 *
 * `conversationReady: false` 模拟「apply 跑完时 conversation 服务还没就绪」——上游在
 * apply 里 eager 缓存 `ctx.conversation`，于是永远拿到 undefined，插入链静默失效。
 * 服务通过 `ctx.get()` 与 `ctx.sessions.scope(id).get('conversation')` 两条路提供。
 * @param options - 初始服务可用性。
 */
function mountClient(options = {}) {
  const bundle = loadBundle()
  const mounted = { locale: null, slot: null, component: null, scopeCalls: [] }
  const flag = { conversationReady: options.conversationReady !== false }
  const generation = options.generation === 'images' ? 'images' : 'attachments'
  const drafts = { created: 0, released: [], sessions: [] }
  /** 运行版本（>=0.1.5-rc）的 Attachments 接口。 */
  const currentApi = {
    createDrafts: (sessionId, files) => {
      drafts.created += 1
      drafts.sessions.push(sessionId)
      return files.map((_file, index) => ({ id: `draft-${index}` }))
    },
    releaseDraftAttachment: (id) => {
      drafts.released.push(id)
    },
  }
  /** 旧版本的 Images 接口（createDraftImages / releaseDraftImage）。 */
  const legacyApi = {
    createDraftImages: (files) => {
      drafts.created += 1
      return files.map((_file, index) => ({ id: `draft-${index}` }))
    },
    releaseDraftImage: (id) => {
      drafts.released.push(id)
    },
  }
  const conversation = generation === 'images' ? legacyApi : currentApi
  /**
   * 会话作用域的 conversation face 是**会话动作面**（send/cancel/…）。按 DSH 的实现它
   * **没有** createDraftImages —— 真实事故：v0.2.4 第一版按 sessionId 取它来建草稿，
   * 报 `conversation.createDraftImages is not a function`。这里放个诱饵，钉住
   * 「草稿 API 只认根单例」这个契约。
   */
  const scopedActionFace = { send: () => {}, cancel: () => {}, updateQueue: () => {} }
  const scoped = { get: (name) => (name === 'conversation' ? scopedActionFace : undefined) }
  const sessions = {
    scope: (sessionId) => {
      mounted.scopeCalls.push(sessionId)
      return scoped
    },
  }
  const ctx = {
    effect: (factory) => {
      factory()
    },
    get: (name) => {
      if (name === 'sessions') return sessions
      if (name === 'conversation' && flag.conversationReady) return conversation
      return undefined
    },
    locale: { register: (namespace, dictionaries) => { mounted.locale = { namespace, dictionaries } } },
    modelDirectories: {
      directoryFor: () => ({
        load: async () => {},
        store: { subscribe: () => () => {}, getSnapshot: () => ({ current: null, groups: [] }) },
      }),
    },
    slots: {
      inject: (_name, callback) => callback(),
      register: (definition, component) => {
        mounted.slot = definition
        mounted.component = component
      },
    },
  }
  bundle.exports.apply(ctx)
  assert.ok(mounted.component, 'apply 必须注册组件')
  return { ...bundle, ...mounted, flag, drafts, conversation }
}

/** 目录快照形状：与 dsh-client-ui-model-selection 的 ModelDirectoryState 一致。 */
function directoryState(provider, modelId, modelName) {
  return {
    current: { provider, model: modelId },
    routable: true,
    groups: [{ id: provider, name: provider, models: [{ id: modelId, name: modelName }] }],
    failures: [],
    status: 'ready',
    error: null,
  }
}

/** 组件 props：测试自己的目录快照 + 真实 slot 注入面 + 标准会话 props。 */
function propsFor(state, injected = {}, overrides = {}) {
  return {
    ...injected,
    directory: { subscribe: () => () => {}, getSnapshot: () => state },
    inputActions: { addAttachments: () => true },
    t: (key) => key,
    ...overrides,
  }
}

/** 让在途的 promise 链落地。 */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

/** 换掉全局 fetch，并在用例结束后恢复。 */
async function withFetch(implementation, run) {
  const original = globalThis.fetch
  globalThis.fetch = implementation
  try {
    return await run()
  } finally {
    globalThis.fetch = original
  }
}

/** 捕获 console.error（失败可见性断言用）。 */
async function withErrorSpy(run) {
  const original = console.error
  const seen = []
  console.error = (...args) => {
    seen.push(args.join(' '))
  }
  try {
    return await run(seen)
  } finally {
    console.error = original
  }
}

const okCapability = (image, source = 'declared') => async () => ({
  ok: true,
  json: async () => ({ provider: 'p', model: 'm', image, source }),
})

/**
 * 按 URL 分流的假 fetch：能力查询返回 JSON，系统截图路由返回可配置结果。
 * `snip.status = 200` → 一段 PNG 字节；`204` → 用户取消；其它 → 宿主不可用（触发浏览器回退）。
 * @param options - 两条路由各自的应答。
 * @returns 假 fetch，并记录每次调用的 `{ url, init }`。
 */
function routeFetch(options = {}) {
  const capability = options.capability ?? { source: 'declared', image: true }
  const snipStatus = options.snip?.status ?? 501
  const calls = []
  const implementation = async (url, init) => {
    calls.push({ url: String(url), init })
    if (String(url).includes('/cvision/snip')) {
      if (snipStatus === 200) {
        return {
          ok: true,
          status: 200,
          blob: async () => new Blob([new Uint8Array([137, 80, 78, 71, 1, 2, 3])], { type: 'image/png' }),
          text: async () => '',
        }
      }
      return {
        ok: false,
        status: snipStatus,
        blob: async () => new Blob([]),
        text: async () => String(options.snip?.message ?? 'snip unavailable'),
      }
    }
    return {
      ok: true,
      status: 200,
      json: async () => capability,
      blob: async () => new Blob([]),
      text: async () => '',
    }
  }
  implementation.calls = calls
  return implementation
}

/** 换掉全局属性并返回恢复函数（Node 的 navigator 是只读访问器，赋值会抛错）。 */
function patchGlobal(name, value) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, name)
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
  return () => {
    if (descriptor === undefined) delete globalThis[name]
    else Object.defineProperty(globalThis, name, descriptor)
  }
}

/** 装好一套能「截到图」的假浏览器环境，返回恢复函数与轨道记录。 */
function patchCaptureEnvironment() {
  const tracks = [{ stopped: false, stop() { this.stopped = true } }]
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage: () => {} }),
    toBlob: (callback) => callback(new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' })),
  }
  return {
    tracks,
    restores: [
      patchGlobal('navigator', { mediaDevices: { getDisplayMedia: async () => ({ getTracks: () => tracks }) } }),
      patchGlobal('HTMLCanvasElement', class {}),
      patchGlobal('requestAnimationFrame', (callback) => callback()),
      patchGlobal('document', {
        querySelector: () => null,
        createElement: (tag) => {
          if (tag === 'video') {
            return { videoWidth: 8, videoHeight: 8, play: async () => {}, requestVideoFrameCallback: (cb) => cb() }
          }
          if (tag === 'canvas') return canvas
          return { dataset: {}, textContent: '', style: {} }
        },
        head: { appendChild: () => {} },
      }),
    ],
  }
}

/** 渲染一次并点击（返回组件渲染出的元素）；`waitMs` 用于等重试链跑完。 */
async function clickScreenshot(component, props, waitMs = 0) {
  const rendered = component(props)
  assert.ok(rendered, '能力允许时必须渲染出按钮')
  const button = rendered.type === reactStub.Fragment ? rendered.children[1] : rendered
  button.props.onClick()
  await settle()
  await settle()
  if (waitMs > 0) await new Promise((resolve) => setTimeout(resolve, waitMs))
  return rendered
}

// ── bundle 与槽位 ────────────────────────────────────────────────────────────

test('以包名 vision 注册客户端 bundle，并声明所需服务', () => {
  const { registration, exports } = loadBundle()
  assert.equal(registration.id, 'vision', 'id 必须等于包名，否则与 boot graph 的行对不上')
  assert.equal(typeof exports.apply, 'function')
  assert.deepEqual(exports.inject, [
    'slots',
    'locale',
    'modelDirectories',
    'conversation',
    'sessions',
    'remote.session',
  ])
})

test('apply 在 conversation.input.right 槽位注册截图按钮并挂上字典', () => {
  const { locale, slot } = mountClient()
  assert.equal(locale.namespace, 'vision')
  assert.ok(locale.dictionaries.zh['button.tooltip'])
  assert.equal(slot.name, 'conversation.input.right')
  assert.equal(slot.id, 'vision-screenshot')
  assert.equal(slot.locale, 'vision')
  assert.equal(typeof slot.inject, 'function')

  const injected = slot.inject('session-1')
  assert.equal(typeof injected.directory.subscribe, 'function')
  assert.equal(typeof injected.createDraft, 'function')
  assert.equal(typeof injected.releaseDraft, 'function')
})

test('草稿图取自根单例 conversation，并按会话 id 建草稿（会话作用域 face 没有草稿 API）', () => {
  const client = mountClient()
  const injected = client.slot.inject('session-42')
  assert.equal(injected.createDraft({ type: 'image/png' }), 'draft-0')
  assert.deepEqual(client.scopeCalls, [], '不该去取会话作用域的动作面')
  assert.deepEqual(client.drafts.sessions, ['session-42'], '新 API 建草稿要带 sessionId')
  injected.releaseDraft('draft-0')
  assert.deepEqual(client.drafts.released, ['draft-0'])
})

test('兼容旧版 Images 接口（createDraftImages + inputActions.addImages）', async () => {
  const env = patchCaptureEnvironment()
  try {
    await withFetch(routeFetch({ snip: { status: 200 } }), async () => {
      const client = mountClient({ generation: 'images' })
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('session-old')
      const added = []
      const props = propsFor(state, injected, {
        inputActions: { addImages: (ids) => { added.push(...ids); return true } },
      })
      client.component(props)
      await settle()
      await clickScreenshot(client.component.bind(client), props)
      assert.deepEqual(added, ['draft-0'], '旧版宿主也要能用')
      injected.releaseDraft('draft-0')
      assert.deepEqual(client.drafts.released, ['draft-0'])
    })
  } finally {
    for (const restore of env.restores.reverse()) restore()
  }
})

// ── 可见性门控（本次集成修复的核心） ────────────────────────────────────────

test('宿主声明图片输入时显示按钮——即使模型名字里没有 vision（上游的漏判）', async () => {
  await withFetch(okCapability(true), async () => {
    const client = mountClient()
    const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
    const props = propsFor(state, client.slot.inject('s'))
    assert.equal(client.component(props), null, '在途态先不显示，避免按名字猜出错误的按钮')
    await settle()
    const rendered = client.component(props)
    assert.ok(rendered, '宿主说收图就必须显示')
    assert.equal(rendered.type, 'button')
    assert.equal(rendered.props.className, 'cvision-screenshot-button')
    assert.equal(rendered.props['aria-label'], 'button.aria')
  })
})

test('hook 数量跨渲染必须稳定（React #310：hook 不能条件调用）', async () => {
  await withFetch(routeFetch({ capability: { source: 'declared', image: true } }), async () => {
    const client = mountClient()
    const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
    const props = propsFor(state, client.slot.inject('s'))
    // 第一次渲染：能力查询在途 → 组件提前 return null（v0.2.8 曾在这里少调一个 useEffect）
    const pending = renderWithHookCount(client.component, props)
    assert.equal(pending.rendered, null)
    await settle()
    // 第二次渲染：拿到「可收图」→ 渲染出按钮。hook 数量必须和上一次一致。
    const ready = renderWithHookCount(client.component, props)
    assert.ok(ready.rendered, '能力允许时必须渲染出按钮')
    assert.equal(
      ready.hooks,
      pending.hooks,
      'hook 数量随渲染变化会让真 React 抛 #310（条件调用 hook）',
    )
  })
})

test('宿主声明纯文本时隐藏按钮——即使模型名字里带 vision（上游的误判）', async () => {
  await withFetch(okCapability(false), async () => {
    const client = mountClient()
    const state = directoryState('local', 'llama-vision-preview', 'Llama Vision Preview')
    client.component(propsFor(state, client.slot.inject('s')))
    await settle()
    assert.equal(client.component(propsFor(state, client.slot.inject('s'))), null, '权威否定必须压过名字启发式')
  })
})

test('宿主路由不可达时退回名字启发式', async () => {
  await withFetch(async () => {
    throw new Error('offline')
  }, async () => {
    const named = mountClient()
    const namedState = directoryState('local', 'foo-vision-1', 'Foo Vision')
    named.component(propsFor(namedState, named.slot.inject('s')))
    await settle()
    assert.ok(named.component(propsFor(namedState, named.slot.inject('s'))), '兜底：名字含 vision 时仍显示')

    const textOnly = mountClient()
    const textState = directoryState('deepseek-official', 'deepseek-v4-flash', 'DeepSeek-V4-Flash')
    textOnly.component(propsFor(textState, textOnly.slot.inject('s')))
    await settle()
    assert.equal(textOnly.component(propsFor(textState, textOnly.slot.inject('s'))), null)
  })
})

test('source 不是 declared（宿主答不上来）时同样走兜底', async () => {
  await withFetch(async () => ({ ok: true, json: async () => ({ source: 'unknown' }) }), async () => {
    const client = mountClient()
    const state = directoryState('local', 'foo-visual-2', 'Foo Visual')
    client.component(propsFor(state, client.slot.inject('s')))
    await settle()
    assert.ok(client.component(propsFor(state, client.slot.inject('s'))), 'unknown 不能被当作否定')
  })
})

test('同一模型只查一次宿主（结果缓存）', async () => {
  let calls = 0
  await withFetch(async (url) => {
    // 只数能力查询：按钮可见后还会轮询剪贴板状态，那是另一条路由。
    if (String(url).includes('/cvision/model-capability')) calls += 1
    return {
      ok: true,
      status: 200,
      json: async () => ({ source: 'declared', image: true }),
      blob: async () => new Blob([]),
      text: async () => '',
    }
  }, async () => {
    const client = mountClient()
    const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
    const props = propsFor(state, client.slot.inject('s'))
    client.component(props)
    await settle()
    client.component(props)
    client.component(props)
    await settle()
    assert.equal(calls, 1)
  })
})

// ── 插入链 ──────────────────────────────────────────────────────────────────

test('默认走宿主系统截图：POST /cvision/snip → 直接入附件栏（不经浏览器抓屏）', async () => {
  const env = patchCaptureEnvironment()
  let screenCaptureCalls = 0
  const restoreSpy = patchGlobal('navigator', {
    mediaDevices: {
      getDisplayMedia: async () => {
        screenCaptureCalls += 1
        return { getTracks: () => [] }
      },
    },
  })
  try {
    const fetchStub = routeFetch({ snip: { status: 200 } })
    await withFetch(fetchStub, async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('session-snip')
      const added = []
      const props = propsFor(state, injected, {
        inputActions: { addAttachments: (ids) => { added.push(...ids); return true } },
      })
      client.component(props)
      await settle()
      await clickScreenshot(client.component.bind(client), props)

      const snipCalls = fetchStub.calls.filter((call) => call.url.includes('/cvision/snip'))
      assert.equal(snipCalls.length, 1, '应当只请求一次系统截图路由')
      assert.equal(snipCalls[0].init?.method, 'POST', '系统截图必须用 POST（宿主按高权限动作设防）')
      assert.equal(screenCaptureCalls, 0, '默认通道不该碰浏览器抓屏')
      assert.deepEqual(added, ['draft-0'], '系统截图的结果要直接进附件栏')
      assert.equal(client.drafts.created, 1)
    })
  } finally {
    restoreSpy()
    for (const restore of env.restores.reverse()) restore()
  }
})

test('系统截图拿到的字节会包成 image/png 的 File（名字带 snip- 前缀）', async () => {
  const env = patchCaptureEnvironment()
  try {
    const fetchStub = routeFetch({ snip: { status: 200 } })
    await withFetch(fetchStub, async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('session-snip')
      let seen = null
      const originalCreateDraft = injected.createDraft
      injected.createDraft = (file) => {
        seen = file
        return originalCreateDraft(file)
      }
      const props = propsFor(state, injected)
      client.component(props)
      await settle()
      await clickScreenshot(client.component.bind(client), props)
      assert.ok(seen, 'createDraft 应当收到一个 File')
      assert.equal(seen.type, 'image/png')
      assert.match(seen.name, /^snip-\d+\.png$/)
      assert.ok(seen.size > 0, 'File 必须带真实字节')
    })
  } finally {
    for (const restore of env.restores.reverse()) restore()
  }
})

test('用户在系统截图里取消（204）→ 静默、不抓屏、不提示', async () => {
  const env = patchCaptureEnvironment()
  let screenCaptureCalls = 0
  const restoreSpy = patchGlobal('navigator', {
    mediaDevices: {
      getDisplayMedia: async () => {
        screenCaptureCalls += 1
        return { getTracks: () => [] }
      },
    },
  })
  try {
    await withFetch(routeFetch({ snip: { status: 204 } }), async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('s')
      const props = propsFor(state, injected)
      client.component(props)
      await settle()
      await clickScreenshot(client.component.bind(client), props)
      assert.equal(screenCaptureCalls, 0, '取消不是「通道不可用」，不该回退抓屏')
      assert.equal(client.drafts.created, 0)
      assert.equal(client.component(props).type, 'button', '取消不该留下提示')
    })
  } finally {
    restoreSpy()
    for (const restore of env.restores.reverse()) restore()
  }
})

test('宿主这条通道不可用（501）→ 回退浏览器抓屏，功能不消失', async () => {
  const env = patchCaptureEnvironment()
  try {
    await withFetch(routeFetch({ snip: { status: 501 } }), async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('session-9')
      const added = []
      const props = propsFor(state, injected, {
        inputActions: { addAttachments: (ids) => { added.push(...ids); return true } },
      })
      client.component(props)
      await settle()
      await clickScreenshot(client.component.bind(client), props)
      assert.deepEqual(added, ['draft-0'])
      assert.equal(client.drafts.created, 1)
      assert.deepEqual(env.tracks.map((track) => track.stopped), [true], '抓完必须释放用户选中的屏幕流')
      assert.deepEqual(client.drafts.released, [], '成功入轨不能释放草稿')
    })
  } finally {
    for (const restore of env.restores.reverse()) restore()
  }
})

test('apply 时 conversation 尚未就绪，之后就绪仍能插入（上游 eager 缓存导致静默丢失）', async () => {
  const env = patchCaptureEnvironment()
  try {
    await withFetch(routeFetch({ snip: { status: 200 } }), async () => {
      // apply 跑完时服务还没注册——上游在这里缓存下 undefined，之后永远插入失败。
      const client = mountClient({ conversationReady: false })
      client.flag.conversationReady = true
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('session-late')
      const added = []
      const props = propsFor(state, injected, {
        inputActions: { addAttachments: (ids) => { added.push(...ids); return true } },
      })
      client.component(props)
      await settle()
      await clickScreenshot(client.component.bind(client), props)
      assert.deepEqual(added, ['draft-0'], '服务迟到也必须能插入')
    })
  } finally {
    for (const restore of env.restores.reverse()) restore()
  }
})

test('输入框暂时拒绝入轨时会重试，不丢用户刚截的图', async () => {
  const env = patchCaptureEnvironment()
  try {
    await withFetch(routeFetch({ snip: { status: 200 } }), async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('s')
      const added = []
      let refusals = 2
      const props = propsFor(state, injected, {
        inputActions: {
          addAttachments: (ids) => {
            if (refusals > 0) {
              refusals -= 1
              return false
            }
            added.push(...ids)
            return true
          },
        },
      })
      client.component(props)
      await settle()
      await clickScreenshot(client.component.bind(client), props, 1800)
      assert.deepEqual(added, ['draft-0'], '重试后必须成功入轨')
      assert.deepEqual(client.drafts.released, [])
    })
  } finally {
    for (const restore of env.restores.reverse()) restore()
  }
})

test('失败必须可见：草稿图建不出来时给出提示并写控制台', async () => {
  const env = patchCaptureEnvironment()
  try {
    await withFetch(routeFetch({ snip: { status: 200 } }), async () => {
      await withErrorSpy(async (errors) => {
        const client = mountClient()
        const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
        const injected = client.slot.inject('s')
        const props = propsFor(state, injected)
        client.component(props)
        await settle()
        // 服务掉线：createDraft 返回 null
        client.flag.conversationReady = false
        await clickScreenshot(client.component.bind(client), props)
        assert.ok(
          errors.some((line) => line.includes('草稿 API')),
          '控制台必须留下原因',
        )
        const next = client.component(props)
        assert.equal(next.type, reactStub.Fragment, '失败后应渲染出提示')
        assert.equal(next.children[0].children[0], 'failure.draft')
      })
    })
  } finally {
    for (const restore of env.restores.reverse()) restore()
  }
})

test('失败必须可见：输入框一直拒绝入轨时提示忙碌并释放草稿', async () => {
  const env = patchCaptureEnvironment()
  try {
    await withFetch(routeFetch({ snip: { status: 200 } }), async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('s')
      const props = propsFor(state, injected, { inputActions: { addAttachments: () => false } })
      client.component(props)
      await settle()
      await clickScreenshot(client.component.bind(client), props, 1800)
      assert.deepEqual(client.drafts.released, ['draft-0'], '最终失败要释放草稿图')
      const next = client.component(props)
      assert.equal(next.children[0].children[0], 'failure.busy')
    })
  } finally {
    for (const restore of env.restores.reverse()) restore()
  }
})

// ── 剪贴板监视与长按插入 ────────────────────────────────────────────────────

/** 剪贴板用例统一：可见页面 + 假剪贴板 fetch + 只模拟 setInterval（setTimeout 保持真实）。 */
async function withClipboardTest(options, run) {
  const restoreDocument = patchGlobal('document', visibleDocument())
  const fetchStub = clipboardFetch(options)
  mock.timers.enable({ apis: ['setInterval'] })
  try {
    return await withFetch(fetchStub, () => run(fetchStub))
  } finally {
    mock.timers.reset()
    restoreDocument()
  }
}

/** 渲染到「按钮可见且监视已启动」：第一次渲染时能力查询还在途，组件会先返回 null。 */
async function renderVisibleButton(client, props) {
  client.component(props) // 能力查询在途 → 还不渲染
  await settle() // 能力返回
  client.component(props) // 按钮可见 → useEffect 启动监视（立即轮询一次建基线）
  await settle()
}

/** 捕获 console.warn（监视降级只该告警一次）。 */
async function withWarnSpy(run) {
  const original = console.warn
  const seen = []
  console.warn = (...args) => {
    seen.push(args.join(' '))
  }
  try {
    return await run(seen)
  } finally {
    console.warn = original
  }
}

test('剪贴板首次轮询只建基线：页面打开前就存在的旧图不点亮按钮', async () => {
  await withClipboardTest({ states: [{ supported: true, image: true, token: '1' }] }, async () => {
    const client = mountClient()
    const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
    const props = propsFor(state, client.slot.inject('s'))
    await renderVisibleButton(client, props)
    const button = buttonOf(client.component(props))
    assert.equal(button.props.className, 'cvision-screenshot-button', '基线不该点亮')
    assert.equal(button.props.title, 'button.tooltip')
  })
})

test('剪贴板出现新图片 → 按钮变色 + 悬浮提示 + 圆点', async () => {
  await withClipboardTest(
    {
      states: [
        { supported: true, image: true, token: '1' },
        { supported: true, image: true, token: '2' },
      ],
    },
    async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const props = propsFor(state, client.slot.inject('s'))
      await renderVisibleButton(client, props)
      mock.timers.tick(1000)
      await settle()
      const button = buttonOf(client.component(props))
      assert.match(button.props.className, /cvision-screenshot-button--clipboard/, '新图片要让按钮变色')
      assert.equal(button.props.title, 'clipboard.hint', '悬浮提示要说明长按插入')
      assert.equal(button.children[1].props.className, 'cvision-screenshot-dot', '右上角要有圆点')
    },
  )
})

test('单击系统截图后：我们自己产出的那张图（served=true）不亮提示，别家的图照旧亮', async () => {
  await withClipboardTest(
    {
      states: [
        { supported: true, image: true, token: '1' }, // 基线
        { supported: true, image: true, token: '2', served: true }, // 我们自己刚截的（已在附件栏里）
        { supported: true, image: true, token: '3', served: false }, // 别家软件放进来的新图
      ],
    },
    async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const props = propsFor(state, client.slot.inject('s'))
      await renderVisibleButton(client, props)

      mock.timers.tick(1000)
      await settle()
      assert.equal(
        buttonOf(client.component(props)).props.className,
        'cvision-screenshot-button',
        '自己刚截的那张不该亮「长按插入剪贴板图片」——它刚刚已经进过附件栏',
      )

      mock.timers.tick(1000)
      await settle()
      assert.match(
        buttonOf(client.component(props)).props.className,
        /--clipboard/,
        '别家软件的截图（served=false）要照常点亮',
      )
    },
  )
})

test('已经亮起之后宿主才告诉我们「那是我们自己产出的」→ 强制清掉（token 没变也要清）', async () => {
  await withClipboardTest(
    {
      states: [
        { supported: true, image: true, token: '1' }, // 基线
        { supported: true, image: true, token: '2' }, // 轮询早于宿主记下归属 → 先亮了
        { supported: true, image: true, token: '2', served: true }, // token 不变，但宿主说是我们产出的
      ],
    },
    async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const props = propsFor(state, client.slot.inject('s'))
      await renderVisibleButton(client, props)

      mock.timers.tick(1000)
      await settle()
      assert.match(buttonOf(client.component(props)).props.className, /--clipboard/, '这张图第一次轮到时先亮了')

      mock.timers.tick(1000)
      await settle()
      assert.equal(
        buttonOf(client.component(props)).props.className,
        'cvision-screenshot-button',
        '宿主确认是自己产出的之后，即使 token 没变也必须清掉高亮（否则蓝色永不消失）',
      )
    },
  )
})

test('短按（远未到阈值）只走系统截图，绝不插入剪贴板图片', async () => {
  await withClipboardTest(
    {
      states: [
        { supported: true, image: true, token: '1' },
        { supported: true, image: true, token: '2' },
      ],
      image: 'png',
    },
    async (stub) => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const props = propsFor(state, client.slot.inject('s'))
      await renderVisibleButton(client, props)
      mock.timers.tick(1000)
      await settle()
      const before = buttonOf(client.component(props))
      assert.match(before.props.className, /--clipboard/)

      before.props.onPointerDown()
      await new Promise((resolve) => {
        setTimeout(resolve, 300) // 人手慢点击也远小于 900ms 阈值
      })
      before.props.onPointerUp()
      before.props.onClick()
      await settle()
      await settle()

      assert.equal(
        stub.calls.some((call) => call.url.includes('/cvision/clipboard/image')),
        false,
        '短按绝不能把剪贴板图片插进附件栏',
      )
      assert.equal(
        stub.calls.some((call) => call.url.includes('/cvision/snip')),
        true,
        '短按必须走系统截图那条路',
      )
    },
  )
})

test('按钮没点亮时按住 → 不插剪贴板图片，点击仍正常截图（慢点击不该塞进旧图）', async () => {
  await withClipboardTest({ states: [{ supported: true, image: true, token: '1' }] }, async (stub) => {
    const client = mountClient()
    const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
    const props = propsFor(state, client.slot.inject('s'))
    await renderVisibleButton(client, props) // 首次轮询只建基线 → 按钮未点亮
    assert.equal(buttonOf(client.component(props)).props.className, 'cvision-screenshot-button')

    const button = buttonOf(client.component(props))
    button.props.onPointerDown()
    await new Promise((resolve) => {
      setTimeout(resolve, 1100) // 超过阈值，但未点亮 → 不该启动长按
    })
    await settle()
    assert.equal(
      stub.calls.some((call) => call.url.includes('/cvision/clipboard/image')),
      false,
      '未点亮说明剪贴板里那张不是「新图片」，按住也不该把它塞进附件栏',
    )

    button.props.onPointerUp()
    button.props.onClick() // 长按没消费掉这次点击 → 应正常走系统截图
    await settle()
    await settle()
    assert.equal(
      stub.calls.some((call) => call.url.includes('/cvision/snip')),
      true,
      '这次点击仍应正常触发系统截图',
    )
  })
})

test('按住期间有进度反馈（--holding + 提示文案），松手即撤销', async () => {
  await withClipboardTest(
    {
      states: [
        { supported: true, image: true, token: '1' },
        { supported: true, image: true, token: '2' },
      ],
    },
    async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const props = propsFor(state, client.slot.inject('s'))
      await renderVisibleButton(client, props)
      mock.timers.tick(1000)
      await settle()

      const button = buttonOf(client.component(props))
      button.props.onPointerDown()
      const holding = buttonOf(client.component(props))
      assert.match(holding.props.className, /cvision-screenshot-button--holding/, '按住要有看得见的反馈')
      assert.equal(holding.props.title, 'clipboard.hold', '按住时提示要说明继续按住会插入')

      button.props.onPointerUp()
      const released = buttonOf(client.component(props))
      assert.doesNotMatch(released.props.className, /--holding/, '松手后反馈要撤销')
    },
  )
})

test('长按按钮：剪贴板图片作为附件插入，随后颜色恢复正常', async () => {
  await withClipboardTest(
    {
      states: [
        { supported: true, image: true, token: '1' },
        { supported: true, image: true, token: '2' },
      ],
      image: 'png',
    },
    async (stub) => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('s')
      const added = []
      const props = propsFor(state, injected, {
        inputActions: { addAttachments: (ids) => { added.push(...ids); return true } },
      })
      await renderVisibleButton(client, props)
      mock.timers.tick(1000)
      await settle()
      const lit = buttonOf(client.component(props))
      assert.match(lit.props.className, /--clipboard/)

      lit.props.onPointerDown()
      await new Promise((resolve) => {
        setTimeout(resolve, 1000) // 等长按阈值（900ms）触发
      })
      await settle()
      await settle()
      lit.props.onPointerUp()
      lit.props.onClick() // 长按已消费：不该再触发短按的系统截图

      assert.deepEqual(added, ['draft-0'], '剪贴板图片要进附件栏')
      const imageCalls = stub.calls.filter((call) => call.url.includes('/cvision/clipboard/image'))
      assert.equal(imageCalls.length, 1)
      assert.equal(imageCalls[0].init?.method, 'POST')
      assert.equal(
        stub.calls.some((call) => call.url.includes('/cvision/snip')),
        false,
        '长按之后不该再走系统截图',
      )
      assert.equal(
        buttonOf(client.component(props)).props.className,
        'cvision-screenshot-button',
        '插入成功后配色要恢复正常',
      )
    },
  )
})

test('剪贴板不再是图片 → 高亮被清掉；再来一张新图又重新点亮', async () => {
  await withClipboardTest(
    {
      states: [
        { supported: true, image: true, token: '1' },
        { supported: true, image: true, token: '2' },
        { supported: true, image: false, token: '3' },
        { supported: true, image: true, token: '4' },
      ],
    },
    async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const props = propsFor(state, client.slot.inject('s'))
      await renderVisibleButton(client, props)

      mock.timers.tick(1000)
      await settle()
      assert.match(buttonOf(client.component(props)).props.className, /--clipboard/, '第二张图点亮')

      mock.timers.tick(1000)
      await settle()
      assert.equal(
        buttonOf(client.component(props)).props.className,
        'cvision-screenshot-button',
        '剪贴板不是图片时要熄灭',
      )

      mock.timers.tick(1000)
      await settle()
      assert.match(buttonOf(client.component(props)).props.className, /--clipboard/, '再来的新图要重新点亮')
    },
  )
})

test('宿主还没这条路由（未升级/未重启）→ 连续失败三次后停止，只告警一次', async () => {
  await withWarnSpy(async (warnings) => {
    const restoreDocument = patchGlobal('document', visibleDocument())
    mock.timers.enable({ apis: ['setInterval'] })
    let clipboardCalls = 0
    const failing = async (url) => {
      if (String(url).includes('/cvision/clipboard')) {
        clipboardCalls += 1
        return {
          ok: false,
          status: 404,
          json: async () => ({}),
          blob: async () => new Blob([]),
          text: async () => '{"message":"Route not found"}',
        }
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ source: 'declared', image: true }),
        blob: async () => new Blob([]),
        text: async () => '',
      }
    }
    try {
      await withFetch(failing, async () => {
        const client = mountClient()
        const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
        const props = propsFor(state, client.slot.inject('s'))
        await renderVisibleButton(client, props) // 第 1 次失败
        mock.timers.tick(1000) // 第 2 次
        mock.timers.tick(1000) // 第 3 次 → 停止
        await settle()
        assert.equal(warnings.filter((line) => line.includes('剪贴板监视已停止')).length, 1)
        const stopped = clipboardCalls
        mock.timers.tick(1000)
        mock.timers.tick(1000)
        await settle()
        assert.equal(clipboardCalls, stopped, '停掉之后不该再空转')
        assert.equal(buttonOf(client.component(props)).props.className, 'cvision-screenshot-button')
      })
    } finally {
      mock.timers.reset()
      restoreDocument()
    }
  })
})

test('宿主不支持读剪贴板（如 Linux）→ 停止轮询，且只告警一次', async () => {
  await withWarnSpy(async (warnings) => {
    await withClipboardTest(
      { states: [{ supported: false, image: false, token: null, reason: 'Phase 2' }] },
      async (stub) => {
        const client = mountClient()
        const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
        const props = propsFor(state, client.slot.inject('s'))
        await renderVisibleButton(client, props)
        const counting = () => stub.calls.filter((call) => call.url.includes('/cvision/clipboard')).length
        assert.equal(counting(), 1)

        mock.timers.tick(1000)
        mock.timers.tick(1000)
        await settle()
        assert.equal(counting(), 1, '不支持就该停掉，不要每秒空转')
        assert.equal(warnings.filter((line) => line.includes('剪贴板监视已停止')).length, 1)
      },
    )
  })
})

test('浏览器抓屏被拒（用户取消授权）保持静默（不该刷提示）', async () => {
  const env = patchCaptureEnvironment()
  try {
    // 宿主系统截图不可用 → 回退浏览器抓屏 → 用户拒绝授权
    await withFetch(routeFetch({ snip: { status: 501 } }), async () => {
      const client = mountClient()
      const state = directoryState('deepseek-official', 'deepseek-v4.1-flash-expires-on-0910', 'deepseek-v4.1')
      const injected = client.slot.inject('s')
      const props = propsFor(state, injected)
      client.component(props)
      await settle()
      const restoreDenied = patchGlobal('navigator', {
        mediaDevices: {
          getDisplayMedia: async () => {
            const error = new Error('Permission denied by user')
            error.name = 'NotAllowedError'
            throw error
          },
        },
      })
      try {
        await clickScreenshot(client.component.bind(client), props)
      } finally {
        restoreDenied()
      }
      const next = client.component(props)
      assert.equal(next.type, 'button', '取消不该留下提示')
    })
  } finally {
    for (const restore of env.restores.reverse()) restore()
  }
})
