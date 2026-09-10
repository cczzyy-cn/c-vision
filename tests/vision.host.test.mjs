/**
 * 宿主半边（`lib/index.js`）中「模型能力路由」的运行时单测。
 *
 * 这条路由是本次修复的权威信号源：浏览器半边拿不到 inputModalities，只能问宿主。
 * 用假 ctx 捕获注册进来的路由，再直接驱动 handler，把判定口径钉死在测试里。
 *
 * 需要先构建（`npm run build`）——这里跑的是 DSH 真正会加载的 `lib/index.js`。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

const { apply } = await import('../lib/index.js')

/** 捕获 apply 注册的路由与工具的数。 */
function mountHost(resolveModelInfo) {
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
      if (deps.includes('webServer') && deps.includes('llm')) callback(hostScope)
    },
  }
  apply(ctx)
  return { routes, tools }
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

/** 驱动一次路由，返回解析后的 JSON。 */
async function callRoute(route, query) {
  const res = fakeResponse()
  await route.handler({ url: '/cvision/model-capability' + query }, res)
  assert.equal(res.statusCode, 200)
  assert.match(res.headers['content-type'], /^application\/json/)
  return JSON.parse(res.body)
}

const CAPABILITY = '/cvision/model-capability'

test('注册唯一一条能力路由，并保留原有工具注册', () => {
  const { routes, tools } = mountHost(async () => ({ inputModalities: ['text'] }))
  assert.equal(routes.length, 1)
  assert.equal(routes[0].kind, 'exact')
  assert.equal(routes[0].path, CAPABILITY)
  assert.ok(tools > 0, 'apply 仍然要注册 vision 的工具')
})

test('组合里没有 webServer/llm 时不注册（也不抛错）', () => {
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

test('显式声明 image 的模型 → 可收图', async () => {
  const { routes } = mountHost(async () => ({ inputModalities: ['text', 'image'] }))
  const payload = await callRoute(routes[0], '?provider=deepseek-official&model=deepseek-v4.1-flash-expires-on-0910')
  assert.equal(payload.image, true)
  assert.equal(payload.source, 'declared')
  assert.deepEqual(payload.modalities, ['text', 'image'])
})

test('显式声明 text-only 的模型 → 不收图', async () => {
  const { routes } = mountHost(async () => ({ inputModalities: ['text'] }))
  const payload = await callRoute(routes[0], '?provider=deepseek-official&model=deepseek-v4-flash')
  assert.equal(payload.image, false)
  assert.equal(payload.source, 'declared')
})

test('未声明 inputModalities → 按 DSH 的图片准入口径算可收图', async () => {
  const { routes } = mountHost(async () => ({}))
  const payload = await callRoute(routes[0], '?provider=custom&model=mystery')
  assert.equal(payload.image, true, 'undefined 在 prompt 准入里是被放行的，不能谎报为不支持')
  assert.equal(payload.source, 'declared')
  assert.deepEqual(payload.modalities, [])
})

test('解析不出该路由 → source=unknown，交给客户端兜底', async () => {
  const { routes } = mountHost(async () => {
    throw new Error('unknown provider')
  })
  const payload = await callRoute(routes[0], '?provider=nope&model=nope')
  assert.equal(payload.image, false)
  assert.equal(payload.source, 'unknown')
})

test('缺参数 → source=unknown，且不调用适配器', async () => {
  let calls = 0
  const { routes } = mountHost(async () => {
    calls += 1
    return { inputModalities: ['text', 'image'] }
  })
  const payload = await callRoute(routes[0], '?provider=deepseek-official')
  assert.equal(payload.source, 'unknown')
  assert.equal(calls, 0)
})
