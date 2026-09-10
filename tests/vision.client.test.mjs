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
import test from 'node:test'

const SOURCE = readFileSync(new URL('../src/client.js', import.meta.url), 'utf8')

/** 极简 React 桩：createElement 产出可断言的普通对象，uSES 直接读快照，effect 同步跑。 */
const reactStub = {
  createElement: (type, props, ...children) => ({ type, props, children }),
  Fragment: Symbol('Fragment'),
  useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
  useEffect: (effect) => {
    effect()
  },
}

const documentStub = {
  querySelector: () => null,
  createElement: () => ({ dataset: {}, textContent: '', style: {} }),
  head: { appendChild: () => {} },
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
  await withFetch(async () => {
    calls += 1
    return { ok: true, json: async () => ({ source: 'declared', image: true }) }
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
