/**
 * 宿主半边（`lib/index.js`）里两条浏览器路由的运行时单测：
 *
 * 1. `GET /cvision/model-capability` —— 权威能力信号源（浏览器半边拿不到 inputModalities，只能问宿主）；
 * 2. `POST /cvision/snip` —— 截图按钮的默认通道：拉起系统截图 UI，把用户框选的图交回浏览器。
 *
 * 用假 ctx 捕获注册进来的路由再直接驱动 handler。系统截图会真的拉系统 UI，所以执行器通过
 * `snipExecutor` 注入假实现（源码里导出的那个可变对象就是为这个测试缝存在的）。
 * 需要先构建（`npm run build`）——这里跑的是 DSH 真正会加载的 `lib/index.js`。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

const { apply, snipExecutor } = await import('../lib/index.js')

/** 捕获 apply 注册的路由与工具的数。 */
function mountHost(resolveModelInfo = async () => ({ inputModalities: ['text'] })) {
  const routes = []
  let tools = 0
  const webServer = { register: (route) => { routes.push(route); return () => {} } }
  const hostScope = {
    effect: (factory) => {
      factory()
    },
    webServer,
    llm: { resolveModelInfo },
  }
  const ctx = {
    effect: (factory) => {
      factory()
    },
    tools: { register: () => { tools += 1 } },
    inject: (deps, callback) => {
      // 两条路由的依赖都从同一个 webServer 作用域来：能力路由要 webServer+llm，截图路由只要 webServer。
      if (deps.includes('webServer')) callback(hostScope)
    },
  }
  apply(ctx)
  return { routes, tools, route: (path) => routes.find((entry) => entry.path === path) }
}

/** 最小 ServerResponse 替身。 */
function fakeResponse() {
  return {
    statusCode: 0,
    headers: null,
    body: '',
    writeHead(code, headers) {
      this.statusCode = code
      this.headers = headers
    },
    end(chunk) {
      this.body = chunk ?? ''
    },
  }
}

/** 最小 IncomingMessage 替身（路由只用到 method/headers/url/on）。 */
function fakeRequest(options = {}) {
  return {
    method: options.method ?? 'POST',
    url: options.url ?? '/cvision/snip',
    headers: {
      host: '127.0.0.1:3080',
      origin: 'http://127.0.0.1:3080',
      ...options.headers,
    },
    on: () => {},
  }
}

/** 驱动一次路由；返回 { status, headers, body }。 */
async function invoke(route, req = fakeRequest(), res = fakeResponse()) {
  await route.handler(req, res)
  return res
}

/** 驱动一次能力路由，返回解析后的 JSON。 */
async function callCapability(route, query) {
  const res = await invoke(route, fakeRequest({ method: 'GET', url: '/cvision/model-capability' + query }))
  assert.equal(res.statusCode, 200)
  assert.match(res.headers['content-type'], /^application\/json/)
  return JSON.parse(res.body)
}

/** 注入一次性的假截图执行器，并在用例结束后还原。 */
async function withSnipExecutor(outcome, run) {
  const original = snipExecutor.run
  const calls = []
  snipExecutor.run = async (exec) => {
    calls.push(exec)
    return typeof outcome === 'function' ? outcome(exec) : outcome
  }
  try {
    return await run(calls)
  } finally {
    snipExecutor.run = original
  }
}

const CAPABILITY = '/cvision/model-capability'
const SNIP = '/cvision/snip'
const PNG_DATA_URL = `data:image/png;base64,${Buffer.from([137, 80, 78, 71, 1, 2, 3]).toString('base64')}`

// ── 路由注册 ────────────────────────────────────────────────────────────────

test('注册两条精确路由（能力 + 系统截图），并保留原有工具注册', () => {
  const { routes, tools, route } = mountHost()
  assert.equal(routes.length, 2)
  assert.ok(route(CAPABILITY), '能力路由必须存在')
  assert.ok(route(SNIP), '系统截图路由必须存在')
  for (const entry of routes) assert.equal(entry.kind, 'exact')
  assert.ok(tools > 0, 'apply 仍然要注册 vision 的工具')
})

test('组合里没有 webServer 时不注册任何路由（也不抛错）', () => {
  const ctx = {
    effect: (factory) => {
      factory()
    },
    tools: { register: () => {} },
    inject: () => {
      /* 依赖永不满足：模拟 Electron/headless 组合 */
    },
  }
  assert.doesNotThrow(() => apply(ctx))
})

// ── 能力路由 ────────────────────────────────────────────────────────────────

test('显式声明 image 的模型 → 可收图', async () => {
  const { route } = mountHost(async () => ({ inputModalities: ['text', 'image'] }))
  const payload = await callCapability(route(CAPABILITY), '?provider=deepseek-official&model=deepseek-v4.1-flash-expires-on-0910')
  assert.equal(payload.image, true)
  assert.equal(payload.source, 'declared')
  assert.deepEqual(payload.modalities, ['text', 'image'])
})

test('显式声明 text-only 的模型 → 不收图', async () => {
  const { route } = mountHost(async () => ({ inputModalities: ['text'] }))
  const payload = await callCapability(route(CAPABILITY), '?provider=deepseek-official&model=deepseek-v4-flash')
  assert.equal(payload.image, false)
  assert.equal(payload.source, 'declared')
})

test('未声明 inputModalities → 按 DSH 的图片准入口径算可收图', async () => {
  const { route } = mountHost(async () => ({}))
  const payload = await callCapability(route(CAPABILITY), '?provider=custom&model=mystery')
  assert.equal(payload.image, true, 'undefined 在 prompt 准入里是被放行的，不能谎报为不支持')
  assert.equal(payload.source, 'declared')
  assert.deepEqual(payload.modalities, [])
})

test('解析不出该路由 → source=unknown，交给客户端兜底', async () => {
  const { route } = mountHost(async () => {
    throw new Error('unknown provider')
  })
  const payload = await callCapability(route(CAPABILITY), '?provider=nope&model=nope')
  assert.equal(payload.image, false)
  assert.equal(payload.source, 'unknown')
})

test('缺参数 → source=unknown，且不调用适配器', async () => {
  let calls = 0
  const { route } = mountHost(async () => {
    calls += 1
    return { inputModalities: ['text', 'image'] }
  })
  const payload = await callCapability(route(CAPABILITY), '?provider=deepseek-official')
  assert.equal(payload.source, 'unknown')
  assert.equal(calls, 0)
})

// ── 系统截图路由 ────────────────────────────────────────────────────────────

test('系统截图成功 → 200 + image/png + 原始 PNG 字节', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'captured', dataUrl: PNG_DATA_URL }, async () => {
    const res = await invoke(route(SNIP))
    assert.equal(res.statusCode, 200)
    assert.equal(res.headers['content-type'], 'image/png')
    assert.deepEqual([...res.body], [137, 80, 78, 71, 1, 2, 3], '必须原样回传字节，不要二次编码')
  })
})

test('用户在系统 UI 里取消 → 204（无正文）', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'cancelled' }, async () => {
    const res = await invoke(route(SNIP))
    assert.equal(res.statusCode, 204)
  })
})

test('平台不支持 → 501，客户端据此回退浏览器抓屏', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'unsupported', message: 'Linux Phase 2' }, async () => {
    const res = await invoke(route(SNIP))
    assert.equal(res.statusCode, 501)
    assert.match(JSON.parse(res.body).message, /Phase 2/)
  })
})

test('执行器故障 → 500，且原因回传', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'failed', message: 'python 不见了' }, async () => {
    const res = await invoke(route(SNIP))
    assert.equal(res.statusCode, 500)
    assert.match(JSON.parse(res.body).message, /python/)
  })
})

test('非 POST 一律拒绝（405），不触碰执行器', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'captured', dataUrl: PNG_DATA_URL }, async (calls) => {
    const res = await invoke(route(SNIP), fakeRequest({ method: 'GET' }))
    assert.equal(res.statusCode, 405)
    assert.equal(calls.length, 0, '被拒的请求不该拉起系统截图')
  })
})

test('跨站请求一律拒绝（403），不触碰执行器', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'captured', dataUrl: PNG_DATA_URL }, async (calls) => {
    const evil = fakeRequest({ headers: { origin: 'http://evil.example' } })
    const res = await invoke(route(SNIP), evil)
    assert.equal(res.statusCode, 403)
    assert.equal(calls.length, 0)
  })
})

test('缺 Origin 的请求也拒绝（403）——系统截图是高权限动作', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'captured', dataUrl: PNG_DATA_URL }, async () => {
    const res = await invoke(route(SNIP), fakeRequest({ headers: { origin: undefined } }))
    assert.equal(res.statusCode, 403)
  })
})

test('执行器拿到一个未中止的 AbortSignal（供客户端断开时中止 Python）', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'cancelled' }, async (calls) => {
    await invoke(route(SNIP))
    assert.equal(calls.length, 1)
    assert.equal(typeof calls[0].signal?.aborted, 'boolean')
    assert.equal(calls[0].signal.aborted, false)
  })
})
