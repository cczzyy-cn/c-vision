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
import { readFileSync } from 'node:fs'
import test, { mock } from 'node:test'

/** 让出一个宏任务：用于推进被 await 的处理器（如「截图进行中」的时序用例）。 */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

const {
  apply, snipExecutor, clipboardProbe, inputRunner, operationTarget, noteCaptureTarget,
  withLockLabel, describeRuntimeProblem, PIP_HINT, ensureRuntime, waitChangedMeta, stableMeta,
  shouldProbeRuntime, RUNTIME_PROBE_RETRY_MS, resetRuntimeProbe, runtimeProbeState,
} = await import('../lib/index.js')

/** 捕获 apply 注册的路由与工具定义。 */
function mountHost(resolveModelInfo = async () => ({ inputModalities: ['text'] })) {
  const routes = []
  const definitions = []
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
    tools: { register: (definition) => { definitions.push(definition) } },
    inject: (deps, callback) => {
      // 两条路由的依赖都从同一个 webServer 作用域来：能力路由要 webServer+llm，截图路由只要 webServer。
      if (deps.includes('webServer')) callback(hostScope)
    },
  }
  apply(ctx)
  return {
    routes,
    definitions,
    tool: (name) => definitions.find((definition) => definition.name === name),
    tools: definitions.length,
    route: (path) => routes.find((entry) => entry.path === path),
  }
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

/**
 * 最小 IncomingMessage 替身（路由只用到 method/headers/url/on/socket）。
 *
 * `socket.remoteAddress` 默认 `127.0.0.1`：本机测试与**桌面版的转发**都来自回环，
 * 而「无 Origin + 回环」正是桌面版那条路径（见 `isTrustedRouteCaller`）。
 */
function fakeRequest(options = {}) {
  return {
    method: options.method ?? 'POST',
    url: options.url ?? '/cvision/snip',
    headers: {
      host: '127.0.0.1:3080',
      origin: 'http://127.0.0.1:3080',
      ...options.headers,
    },
    socket: { remoteAddress: options.remoteAddress ?? '127.0.0.1' },
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
const CLIPBOARD = '/cvision/clipboard'
const CLIPBOARD_IMAGE = '/cvision/clipboard/image'
const PNG_DATA_URL = `data:image/png;base64,${Buffer.from([137, 80, 78, 71, 1, 2, 3]).toString('base64')}`

/** 注入一次性的假剪贴板探测，并在用例结束后还原。 */
async function withClipboardProbe(probe, run) {
  const original = { state: clipboardProbe.state, image: clipboardProbe.image }
  if (probe.state !== undefined) clipboardProbe.state = probe.state
  if (probe.image !== undefined) clipboardProbe.image = probe.image
  try {
    return await run()
  } finally {
    Object.assign(clipboardProbe, original)
  }
}

// ── 路由注册 ────────────────────────────────────────────────────────────────

test('注册四条精确路由（能力 + 系统截图 + 剪贴板状态/取图），并保留原有工具注册', () => {
  const { routes, tools, route } = mountHost()
  assert.equal(routes.length, 4)
  assert.ok(route(CAPABILITY), '能力路由必须存在')
  assert.ok(route(SNIP), '系统截图路由必须存在')
  assert.ok(route(CLIPBOARD), '剪贴板状态路由必须存在')
  assert.ok(route(CLIPBOARD_IMAGE), '剪贴板取图路由必须存在')
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

test('能力路由：带外站 Origin → 403，且不调用适配器', async () => {
  let asked = 0
  const { route } = mountHost(async () => {
    asked += 1
    return { inputModalities: ['image'] }
  })
  const res = await invoke(
    route(CAPABILITY),
    fakeRequest({
      method: 'GET',
      url: `${CAPABILITY}?provider=p&model=m`,
      headers: { origin: 'http://evil.example' },
    }),
  )
  assert.equal(res.statusCode, 403)
  assert.equal(asked, 0, '跨站请求不该触发适配器查询')
})

test('能力路由：不带 Origin 的同源 GET 必须放行（浏览器不给同源 GET 加 Origin）', async () => {
  // 若照 POST 路由那条「缺 Origin 也拒绝」的口径来卡这条只读 GET，插件自己的
  // `fetch(CAPABILITY_PATH)` 会被一起挡掉 → 能力查询失败 → 按钮退回名字启发式：比不设防更糟。
  // 跨站页面发起的请求一定带 Origin（CORS 语义），所以只卡「带 Origin 且不匹配」的。
  const { route } = mountHost(async () => ({ inputModalities: ['image'] }))
  const res = await invoke(
    route(CAPABILITY),
    fakeRequest({
      method: 'GET',
      url: `${CAPABILITY}?provider=p&model=m`,
      headers: { origin: undefined },
    }),
  )
  assert.equal(res.statusCode, 200)
  assert.equal(JSON.parse(res.body).image, true)
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

test('系统截图执行器抛异常 → snipInFlight 必须复位（否则按钮永久失灵）', async () => {
  // `snipInFlight` 一旦永久为真，之后**任何**剪贴板图片都会被当成「我们自己刚产出的」，
  // 客户端据此不点亮按钮、长按也不触发，直到宿主重启。默认执行器自己吞异常，但它是可注入的
  // （单测/扩展会替换它），所以路由必须用 try/finally 复位。
  // 观测手段：把「刚截图完的 4 秒归属窗口」推到过期之外（改 Date.now），于是 served 只反映 snipInFlight。
  const { route } = mountHost()
  const realNow = Date.now
  Date.now = () => 9e15
  try {
    await withSnipExecutor(
      () => {
        throw new Error('executor exploded')
      },
      async () => {
        await assert.rejects(invoke(route(SNIP), fakeRequest()), /executor exploded/)
        await withClipboardProbe(
          { state: async () => ({ supported: true, image: true, token: 'tok-after-boom', reason: '' }) },
          async () => {
            const res = await invoke(route(CLIPBOARD), fakeRequest({ method: 'GET', url: CLIPBOARD }))
            assert.equal(
              JSON.parse(res.body).served,
              false,
              'snipInFlight 没复位：别家刚截的图会被误判成我们产出的（按钮不再提示）',
            )
          },
        )
      },
    )
  } finally {
    Date.now = realNow
  }
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

test('桌面版转发路径（无 Origin + 回环对端）必须放行——否则桌面版点按钮永远 403', async () => {
  // DSH 桌面版的 `forwardWebRequest` 把页面（`dsh-app://app`）的非静态请求转发到回环 HTTP 服务，
  // 并**刻意删掉** `host`/`origin`/`cookie`/`sec-fetch-site`，再换上宿主自己的 cookie。
  // 于是这条路径上永远没有 Origin：旧实现只认「Origin 存在且 host 相等」，桌面版必然 403 →
  // 客户端回退 getDisplayMedia，而桌面版把抓屏权限全关了（setPermissionCheckHandler(() => false)
  // + setDisplayMediaRequestHandler(cb => cb({}))）→ 用户看到的就是「点了没反应」。
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'captured', dataUrl: PNG_DATA_URL }, async (calls) => {
    const res = await invoke(route(SNIP), fakeRequest({ headers: { origin: undefined } }))
    assert.equal(res.statusCode, 200, '无 Origin 但来自回环：这是桌面版，必须放行')
    assert.equal(calls.length, 1, '放行后要真的拉起系统截图')
  })
})

test('无 Origin 且**非**回环对端仍然拒绝（403）——豁免只给本机', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'captured', dataUrl: PNG_DATA_URL }, async (calls) => {
    const res = await invoke(
      route(SNIP),
      fakeRequest({ headers: { origin: undefined }, remoteAddress: '203.0.113.7' }),
    )
    assert.equal(res.statusCode, 403)
    assert.equal(calls.length, 0, '被拒的请求不该拉起系统截图')
  })
})

test('桌面版页面源（Origin: dsh-app://app）放行', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'cancelled' }, async () => {
    const res = await invoke(route(SNIP), fakeRequest({ headers: { origin: 'dsh-app://app' } }))
    assert.equal(res.statusCode, 204)
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

// ── 剪贴板路由 ──────────────────────────────────────────────────────────────

test('剪贴板状态：200 + JSON（页面每秒轮询这条）', async () => {
  const { route } = mountHost()
  await withClipboardProbe(
    { state: async () => ({ supported: true, image: true, token: '42', reason: '' }) },
    async () => {
      const res = await invoke(route(CLIPBOARD), fakeRequest({ method: 'GET', url: CLIPBOARD }))
      assert.equal(res.statusCode, 200)
      assert.match(res.headers['content-type'], /^application\/json/)
      const payload = JSON.parse(res.body)
      assert.equal(payload.supported, true)
      assert.equal(payload.image, true)
      assert.equal(payload.token, '42')
      // served 的语义由下面两条专门用例钉住（它们自己控制「先截图、再轮询」的时序）；
      // 这里只要确认字段存在且是布尔值，避免用例之间因「待归属标记」互相干扰。
      assert.equal(typeof payload.served, 'boolean')
    },
  )
})

test('剪贴板状态：单击系统截图之后，我们自己那张图标记为 served=true（客户端据此不亮提示）', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'captured', dataUrl: PNG_DATA_URL }, async () => {
    await withClipboardProbe(
      { state: async () => ({ supported: true, image: true, token: '501', reason: '' }) },
      async () => {
        // 先走一次系统截图：宿主会记下「此刻剪贴板的 token 就是我刚交出去的那张」
        assert.equal((await invoke(route(SNIP))).statusCode, 200)
        const same = JSON.parse((await invoke(route(CLIPBOARD), fakeRequest({ method: 'GET', url: CLIPBOARD }))).body)
        assert.equal(same.token, '501')
        assert.equal(same.served, true, '刚截的那张必须标成我们自己产出的')
      },
    )
  })
})

test('剪贴板状态：截图**还在进行中**出现的图也算我们的（否则期间一次轮询就点亮了）', async () => {
  const { route } = mountHost()
  let release = () => {}
  const gate = new Promise((resolve) => {
    release = resolve
  })
  await withSnipExecutor(
    async () => {
      await gate // 卡住截图，模拟「用户还在框选/标注」
      return { kind: 'captured', dataUrl: PNG_DATA_URL }
    },
    async () => {
      await withClipboardProbe(
        { state: async () => ({ supported: true, image: true, token: '777', reason: '' }) },
        async () => {
          const snipPromise = invoke(route(SNIP)) // 故意不 await：截图进行中
          await settle()
          const during = JSON.parse((await invoke(route(CLIPBOARD), fakeRequest({ method: 'GET', url: CLIPBOARD }))).body)
          assert.equal(during.served, true, '截图进行中就写进剪贴板的图，必须算我们自己产出的')
          // 慢截图：用户拖框/标注十几秒（远超完成后那个 4s 窗口），只要请求还在进行中就必须继续归属。
          mock.timers.enable({ apis: ['Date'] })
          try {
            mock.timers.tick(15000)
            const slow = JSON.parse((await invoke(route(CLIPBOARD), fakeRequest({ method: 'GET', url: CLIPBOARD }))).body)
            assert.equal(slow.served, true, '慢截图期间也得算我们的（窗口不该从第一次轮询开始算）')
          } finally {
            mock.timers.reset()
          }
          release()
          assert.equal((await snipPromise).statusCode, 200)
        },
      )
    },
  )
})

test('剪贴板状态：归属窗口内截图工具再写一次剪贴板仍算我们的；窗口过后换图才 served=false', async () => {
  const { route } = mountHost()
  await withSnipExecutor({ kind: 'captured', dataUrl: PNG_DATA_URL }, async () => {
    let token = '601'
    await withClipboardProbe(
      { state: async () => ({ supported: true, image: true, token, reason: '' }) },
      async () => {
        mock.timers.enable({ apis: ['Date'] })
        try {
          const readServed = async () =>
            JSON.parse((await invoke(route(CLIPBOARD), fakeRequest({ method: 'GET', url: CLIPBOARD }))).body).served
          await invoke(route(SNIP))
          assert.equal(await readServed(), true)
          token = '602' // 截图工具打开编辑器又写了一次剪贴板
          assert.equal(await readServed(), true, '窗口内仍归我们，不该点亮')
          mock.timers.tick(5000) // 归属窗口（4s）过期
          token = '603' // 这次是别家的新图
          assert.equal(await readServed(), false, '窗口过后换了内容，提示该亮')
        } finally {
          mock.timers.reset()
        }
      },
    )
  })
})

test('剪贴板状态：只接受 GET（405），且探测失败时 500 而不是崩掉轮询', async () => {
  const { route } = mountHost()
  await withClipboardProbe({ state: async () => {
    throw new Error('clipboard busy')
  } }, async () => {
    const wrong = await invoke(route(CLIPBOARD), fakeRequest({ method: 'POST', url: CLIPBOARD }))
    assert.equal(wrong.statusCode, 405)
    const failed = await invoke(route(CLIPBOARD), fakeRequest({ method: 'GET', url: CLIPBOARD }))
    assert.equal(failed.statusCode, 500)
    assert.match(JSON.parse(failed.body).message, /busy/)
  })
})

test('剪贴板取图：200 + image/png + 原始字节', async () => {
  const { route } = mountHost()
  await withClipboardProbe(
    { image: async () => ({ kind: 'captured', dataUrl: PNG_DATA_URL }) },
    async () => {
      const res = await invoke(route(CLIPBOARD_IMAGE))
      assert.equal(res.statusCode, 200)
      assert.equal(res.headers['content-type'], 'image/png')
      assert.deepEqual([...res.body], [137, 80, 78, 71, 1, 2, 3])
    },
  )
})

test('剪贴板取图：剪贴板里没有图 → 204（客户端据此熄灭高亮）', async () => {
  const { route } = mountHost()
  await withClipboardProbe({ image: async () => ({ kind: 'empty' }) }, async () => {
    const res = await invoke(route(CLIPBOARD_IMAGE))
    assert.equal(res.statusCode, 204)
  })
})

test('剪贴板取图：桌面版转发路径（无 Origin + 回环）同样放行（长按插入靠它）', async () => {
  const { route } = mountHost()
  await withClipboardProbe(
    { image: async () => ({ kind: 'captured', dataUrl: PNG_DATA_URL }) },
    async () => {
      const res = await invoke(
        route(CLIPBOARD_IMAGE),
        fakeRequest({ method: 'POST', url: CLIPBOARD_IMAGE, headers: { origin: undefined } }),
      )
      assert.equal(res.statusCode, 200)
      assert.equal(res.headers['content-type'], 'image/png')
    },
  )
})

test('剪贴板取图：平台不支持 → 501', async () => {
  const { route } = mountHost()
  await withClipboardProbe(
    { image: async () => ({ kind: 'unsupported', message: 'Linux Phase 2' }) },
    async () => {
      const res = await invoke(route(CLIPBOARD_IMAGE))
      assert.equal(res.statusCode, 501)
      assert.match(JSON.parse(res.body).message, /Phase 2/)
    },
  )
})

test('剪贴板取图：非 POST（405）与跨站（403）都拒绝，且不触碰探测', async () => {
  const { route } = mountHost()
  let calls = 0
  await withClipboardProbe(
    {
      image: async () => {
        calls += 1
        return { kind: 'empty' }
      },
    },
    async () => {
      assert.equal((await invoke(route(CLIPBOARD_IMAGE), fakeRequest({ method: 'GET' }))).statusCode, 405)
      assert.equal(
        (await invoke(route(CLIPBOARD_IMAGE), fakeRequest({ headers: { origin: 'http://evil.example' } }))).statusCode,
        403,
      )
      assert.equal(calls, 0, '被拒的请求不该读剪贴板')
    },
  )
})

// ── 宿主占用输入设备（v0.2.19） ──────────────────────────────────────────────
test('剪贴板状态路由带 busy 字段（客户端据此显示「DSH 正在操作鼠标/键盘」）', async () => {
  const { route } = mountHost()
  await withClipboardProbe(
    { state: async () => ({ supported: true, image: false, token: '7', reason: '' }) },
    async () => {
      const res = await invoke(route(CLIPBOARD), fakeRequest({ method: 'GET', url: CLIPBOARD }))
      assert.equal(res.statusCode, 200)
      const payload = JSON.parse(res.body)
      // 空闲时也要**显式给出** false，而不是省略字段：缺字段虽然行为等价，但消费方无法
      // 从响应看出「这条协议存在」，排查时容易误判成路由没升级。
      assert.equal(typeof payload.busy, 'boolean')
      assert.equal(payload.busy, false, '没有输入操作进行时 busy 应为 false')
    },
  )
})

// ── 运行时体检门（v0.2.18） ──────────────────────────────────────────────────
/**
 * 依赖没装时，`see`/`ocr` 原本会抛裸的 `ModuleNotFoundError`，用户看不出该做什么。
 * `describeRuntimeProblem` 是把体检结论翻译成「可操作提示」的纯函数，这里钉死它的判定与文案
 * （真去 spawn Python 装/卸依赖在 CI 上不可行，所以只测这个纯函数）。
 */
/** 造一份体检结论；`overrides` 用于改单个字段。 */
function statusOf(overrides = {}) {
  return {
    platform: 'win32',
    python: '3.12.0',
    backend: 'windows',
    backend_known: true,
    backend_implemented: true,
    // 空串 = 捕获后端导得进来；非空 = 缺依赖等（v0.2.33 新增字段）。
    backend_import_error: '',
    deps: { Pillow: true, pyautogui: true, pyperclip: true },
    ok: true,
    ...overrides,
  }
}

test('体检：环境完整 → 不拦（返回 null）', () => {
  assert.equal(describeRuntimeProblem(statusOf(), null), null)
})

test('体检：缺 Pillow → 给出缺什么 + 带绝对路径的 pip 命令', () => {
  const problem = describeRuntimeProblem(statusOf({ deps: { Pillow: false, pyautogui: true, pyperclip: true } }), null)
  assert.ok(problem, '缺 Pillow 必须拦下')
  assert.match(problem, /Pillow/)
  assert.match(problem, /pip install/)
  // 命令必须带绝对路径，用户不该自己去猜插件安装目录。
  assert.ok(problem.includes(PIP_HINT), '要给出可直接复制的安装命令')
  assert.match(PIP_HINT, /requirements\.txt"?$/)
  assert.match(problem, /cvision_status\(\)/, '要指向复查手段')
})

test('体检：缺 pyautogui 且其它都正常 → 不拦，避免误伤 see/ocr', () => {
  // pyautogui 只影响输入类工具；若拿它当门，用户只是没装输入依赖就看不到截图，属于过度拦截。
  // 它仍由 cvision_status() 的 deps 报告出来（另有 Python 侧用例钉死探针覆盖）。
  const problem = describeRuntimeProblem(
    statusOf({ deps: { Pillow: true, pyautogui: false, pyperclip: true }, ok: true }),
    null,
  )
  assert.equal(problem, null, '缺 pyautogui 不该阻止 see/ocr')
})

test('体检：已有其它阻塞项时，缺 pyautogui 要作为附加说明列出来', () => {
  const problem = describeRuntimeProblem(
    statusOf({ deps: { Pillow: false, pyautogui: false, pyperclip: false }, ok: false }),
    null,
  )
  assert.ok(problem)
  assert.match(problem, /Pillow/)
  assert.match(problem, /pyautogui/, '既然报错，就该把「输入类工具也不可用」一并说清')
  assert.match(problem, /输入类工具不可用/)
})

test('体检：后端未实现（如 Linux）→ 说清 platform 与 backend，而不是假装依赖问题', () => {
  const problem = describeRuntimeProblem(
    statusOf({ platform: 'linux', backend: 'linux', backend_implemented: false, platform_support: 'unsupported' }),
    null,
  )
  assert.ok(problem, '后端未实现必须拦下')
  assert.match(problem, /linux/)
  assert.match(problem, /后端未实现/)
  // 平台不支持不是 `pip install` 能解决的，不该误导用户去装包，也不该让他去「复查依赖」。
  assert.ok(!problem.includes('pip install'), '后端未实现时不要给安装命令')
  assert.ok(!problem.includes('cvision_status()'), '后端未实现时不要给依赖复查指引')
})

test('体检：未验证平台（macOS）→ 不拦，但必须如实提示未验证', () => {
  // 这条曾经把 `unverified` 写在 `backend_implemented !== true` 的 else-if 里，
  // 于是**永远不可达**——macOS 上既不报错、也拿不到任何提示。现在它是一条独立警告。
  const problem = describeRuntimeProblem(
    statusOf({ platform: 'darwin', backend: 'macos', platform_support: 'unverified' }),
    null,
  )
  assert.ok(problem, '未验证平台必须给出提示，不能静默放行')
  assert.match(problem, /未在真机验证/)
  assert.match(problem, /unverified/)
  assert.ok(!problem.includes('pip install'), '依赖齐全时不该出现安装命令')
})

test('体检：deps 齐全且 platform_support=supported → 不返回任何提示', () => {
  assert.equal(describeRuntimeProblem(statusOf(), null), null)
})

test('体检：探针本身跑不起来 → 提示先查解释器/路径，并附 CVISION_PYTHON 线索', () => {
  const problem = describeRuntimeProblem(null, 'spawn python ENOENT')
  assert.ok(problem, '探针失败必须拦下')
  assert.match(problem, /ENOENT/)
  assert.match(problem, /Python 3\.10\+/)
  assert.match(problem, /CVISION_PYTHON=/)
  assert.ok(problem.includes(PIP_HINT))
})

test('体检：探针结构漂移（deps 缺失）不得被当成「没问题」', () => {
  const problem = describeRuntimeProblem(statusOf({ deps: undefined }), null)
  assert.ok(problem, 'deps 读不出来时必须拦，不能默认放行')
  assert.match(problem, /Pillow/)
})

test('体检：后端「有实现但导不进来」→ 拦下并给装依赖的办法，不许写成「后端未实现」', () => {
  // 对应 v0.2.33 修的真实缺陷：探针旧实现「导入失败就返回 unknown」，于是**缺依赖**被错报成
  // 「本平台后端未实现」——用户会去等更新，而正确动作是 pip install。两者的处置完全不同。
  const problem = describeRuntimeProblem(
    statusOf({ backend_import_error: "ModuleNotFoundError: No module named 'win32gui'", ok: false }),
    null,
  )
  assert.ok(problem, '后端导不进来必须拦（否则 see 会在 Python 侧抛裸的 ImportError）')
  assert.match(problem, /win32gui/, '要把真实的导入失败原因带上——那是用户唯一能自救的线索')
  assert.match(problem, /导不进来/)
  assert.doesNotMatch(problem, /后端未实现/, '这不是「平台未实现」，不能让用户白等一个不存在的修复')
  assert.ok(problem.includes(PIP_HINT), '既然要靠装依赖解决，就必须给出可复制的安装命令')
})

test('体检缓存：成功只探一次；失败按 TTL 重探（装完依赖不必重启 DSH）', () => {
  // 背景（v0.2.33 修）：体检结论曾是「整个进程只探一次」的**负缓存**，于是用户跑完
  // `pip install` 之后 `see` 继续报同一个错，而 cvision_status()（刻意不过门）已经显示一切正常，
  // 两个工具口径互相矛盾，只能重启宿主才能恢复。
  const now = 1_000_000
  const base = { everProbed: false, failed: false, lastAt: 0, now }
  assert.equal(shouldProbeRuntime(base), true, '从没探过 → 探')
  assert.equal(shouldProbeRuntime({ ...base, everProbed: true }), false, '上次成功 → 不再探')
  assert.equal(
    shouldProbeRuntime({ ...base, everProbed: true, failed: true, lastAt: now - 1000 }),
    false,
    '上次失败但还没过 TTL → 先不重探（否则每次工具调用都白 spawn 一个解释器）',
  )
  assert.equal(
    shouldProbeRuntime({ ...base, everProbed: true, failed: true, lastAt: now - RUNTIME_PROBE_RETRY_MS }),
    true,
    '上次失败且已过 TTL → 重探（用户很可能刚跑完 pip install）',
  )
  assert.equal(
    shouldProbeRuntime({ ...base, everProbed: true, failed: true, lastAt: now, retryMs: 50 }),
    false,
    'retryMs 可覆盖（测试/调试用）',
  )
  // 显式复位（cvision_status 成功时走的也是这个）→ 下一次门立刻重探。
  resetRuntimeProbe()
  const after = runtimeProbeState()
  assert.deepEqual(after, { everProbed: false, failed: false, lastAt: 0 })
  assert.equal(shouldProbeRuntime({ ...after, now }), true, '复位后必须立刻重探')
})

test('体检门：真实环境下 ensureRuntime 放行（本机依赖齐全）', async () => {
  // 这条会真的 spawn 一次 `python -m cvision.cli_capture --status`（本仓库 CI 装了 Pillow）。
  // 若本机没装 Python/依赖，它会抛出可操作错误——那正是门在起作用的证据，故不强断言成功。
  try {
    await ensureRuntime({ signal: AbortSignal.timeout(20000) })
  } catch (error) {
    assert.match(String(error.message), /pip install|cvision_status|Python 3\.10\+/)
  }
})

// ── wait_until_changed：返回值必须能通过它自己声明的 output.schema ────────────
//
// 背景（v0.2.23 的真实故障）：DSH 会拿工具声明的 `output.schema` 校验返回值，而这份 schema 写的是
// `additionalProperties: false`。Python 侧每次都多给 `width`/`height`，于是**每一次**
// `wait_until_changed` 都以 `tool "wait_until_changed" returned invalid output` 失败；
// 当时的用例只驱动 HTTP 路由，没人把「真正返回的键」和「声明过的键」放在一起比过。
//
// 所以下面两条不测「功能」，只钉死这条不变量：**返回值 ⊆ 声明的键，且不为 null**。

/** Python 侧 `wait_changed` 的真实返回形状（`cli_server` 与 `cli_capture` 两条路径一致）。 */
const WAIT_CHANGED_PAYLOAD = {
  ok: true,
  kind: 'wait_changed',
  data_url: PNG_DATA_URL,
  width: 900,
  height: 220,
  changed: true,
  samples: 3,
  elapsed_ms: 812,
  diff_ratio: 0.031,
  mean_diff: 12.5,
  diff_bbox: { x: 10, y: 20, w: 30, h: 40 },
  diff_boxes: [{ x: 10, y: 20, w: 30, h: 40 }],
  // v0.2.33 起 Python 还会给这三个：宿主拿它们把 click_at 的比例基准换到这张新图上，
  // 但**不许**进模型可见的返回值（schema 没声明它们）——所以它们出现在「真实返回形状」里正中下怀。
  image_screen_box: { x: 0, y: 0, width: 900, height: 220 },
  handle: 4321,
  title: '记事本',
}

/** `wait_until_changed` 声明的可返回键（`ref` 由 execute 另加，不由整形函数产出）。 */
function declaredWaitChangedKeys() {
  const tool = mountHost().tool('wait_until_changed')
  assert.ok(tool, 'wait_until_changed 必须注册')
  assert.equal(tool.output.schema.additionalProperties, false, '这条不变量只在 additionalProperties:false 下才有意义')
  return Object.keys(tool.output.schema.properties).filter((key) => key !== 'ref').sort()
}

test('wait_until_changed：返回值就是 schema 声明的那套键（多一个键 = 整次调用失败）', () => {
  const declared = declaredWaitChangedKeys()
  const meta = waitChangedMeta({ ...WAIT_CHANGED_PAYLOAD })
  assert.deepEqual(Object.keys(meta).sort(), declared)
  // `additionalProperties: false` 下多键会致命：这里单独再钉一次，失败信息才看得懂。
  for (const key of Object.keys(meta)) {
    assert.ok(declared.includes(key), `返回了 schema 未声明的键 ${key} —— DSH 会判定 invalid output`)
  }
  // width/height 是 Python 侧**每次都给**的，正是 v0.2.23 漏声明的那两个。
  assert.equal(meta.width, 900)
  assert.equal(meta.height, 220)
})

test('wait_until_changed：未变化时 diff_bbox=null 必须被丢掉（schema 声明的是 object）', () => {
  const declared = declaredWaitChangedKeys()
  const meta = waitChangedMeta({ ...WAIT_CHANGED_PAYLOAD, changed: false, diff_ratio: 0, mean_diff: 0, diff_bbox: null, diff_boxes: [] })
  assert.ok(!('diff_bbox' in meta), 'null 不能原样进返回值：schema 写的是 type: object')
  assert.equal(meta.changed, false)
  for (const [key, value] of Object.entries(meta)) {
    assert.notEqual(value, null, `${key} 不应为 null`)
    assert.ok(declared.includes(key), `${key} 必须在 schema 里声明`)
  }
})

test('wait_until_changed：多处变化时 diff_boxes 要如实带上', () => {
  // 这是 diff_bbox 的固有缺陷的补丁：两处同时变时那个总框会横跨整屏、没有信息量，
  // 所以另外给出分开的框（并且已经画进返回的图里，模型直接看图最省事）。
  const boxes = [{ x: 1, y: 2, w: 3, h: 4 }, { x: 50, y: 60, w: 7, h: 8 }]
  const meta = waitChangedMeta({ ...WAIT_CHANGED_PAYLOAD, diff_boxes: boxes })
  assert.deepEqual(meta.diff_boxes, boxes, '分开的变化区域必须原样带出')
})

// ── wait_until_stable：同一条不变量，但**字段是另一套** ────────────────────────
//
// 与 wait_until_changed 分开钉，因为两者的语义本就不同：一个讲「变没变、变在哪」，一个讲
// 「安静下来没有、刚才动得多厉害」。若图省事共用一份整形函数，某一边多带一个键就会让整次调用
// 失败——那正是 v0.2.23 这个故障的形状。

/** Python 侧 `wait_stable` 的真实返回形状。 */
const WAIT_STABLE_PAYLOAD = {
  ok: true,
  kind: 'wait_stable',
  data_url: PNG_DATA_URL,
  width: 900,
  height: 220,
  stable: true,
  samples: 5,
  elapsed_ms: 1234,
  diff_ratio: 0.002,
  max_diff_ratio: 0.42,
  stable_for: 3,
  image_screen_box: { x: 0, y: 0, width: 900, height: 220 },
  handle: 4321,
  title: '记事本',
}

/** `wait_until_stable` 声明的可返回键（`ref` 由 execute 另加）。 */
function declaredWaitStableKeys() {
  const tool = mountHost().tool('wait_until_stable')
  assert.ok(tool, 'wait_until_stable 必须注册')
  assert.equal(tool.output.schema.additionalProperties, false, '这条不变量只在 additionalProperties:false 下才有意义')
  return Object.keys(tool.output.schema.properties).filter((key) => key !== 'ref').sort()
}

test('wait_until_stable：返回值就是 schema 声明的那套键', () => {
  const declared = declaredWaitStableKeys()
  const meta = stableMeta({ ...WAIT_STABLE_PAYLOAD })
  assert.deepEqual(Object.keys(meta).sort(), declared)
  assert.equal(meta.stable, true)
  assert.equal(meta.stable_for, 3)
  assert.equal(meta.width, 900, 'width/height 是 Python 每次都给、最容易漏声明的两个键')
})

test('wait_until_stable：未稳定（超时）也是正常返回值，同样不许带多余键', () => {
  const declared = declaredWaitStableKeys()
  const meta = stableMeta({ ...WAIT_STABLE_PAYLOAD, stable: false, stable_for: 0, diff_ratio: 0.5, max_diff_ratio: 0.9 })
  assert.equal(meta.stable, false, '超时不是错误，要如实表达成 stable:false')
  assert.equal(meta.stable_for, 0)
  for (const [key, value] of Object.entries(meta)) {
    assert.notEqual(value, null, `${key} 不应为 null`)
    assert.ok(declared.includes(key), `${key} 必须在 schema 里声明`)
  }
})

test('wait_*：Python 新给的 image_screen_box/handle/title 必须被整形函数丢掉', () => {
  // v0.2.33 给 wait_* 的 Python 响应加了这三个字段（宿主拿它们更新 click_at 的基准，见 noteCaptureTarget），
  // 但它们**不进模型可见的返回值**：工具 schema 是 `additionalProperties: false`，
  // 多一个未声明的键 = `returned invalid output` = **整次调用失败**（v0.2.23/24 正是这样炸的）。
  const extra = { image_screen_box: { x: 0, y: 0, width: 900, height: 220 }, handle: 4321, title: '记事本' }
  const changed = waitChangedMeta({ ...WAIT_CHANGED_PAYLOAD, ...extra })
  for (const key of Object.keys(extra)) {
    assert.ok(!(key in changed), `${key} 不该出现在返回值里（schema 未声明，多键会致命）`)
  }
  const stable = stableMeta({ ...WAIT_STABLE_PAYLOAD, ...extra })
  for (const key of Object.keys(extra)) {
    assert.ok(!(key in stable), `${key} 不该出现在返回值里（schema 未声明，多键会致命）`)
  }
})

// ── 输入动作：置前与动作必须落在**同一次** CLI 调用里 ────────────────────────────
//
// 为什么这条值得机械地钉住：跨进程输入互斥（cvision/input_lock.py）是在 **CLI 进程内**取的，所以
// 「先调一次 --ensure-front、再调一次动作」中间**有缝**——另一个 DSH 实例正好在那条缝里把它的窗口
// 置前，我们这一击就落到它的窗口上，而坐标全对。这类故障在真实使用中只表现为「偶发点错窗口」，
// 人肉回归根本发现不了，只能断言 argv 的形状。

/** 用假 runner 跑一段输入工具调用，返回 runner 收到的每一批 argv。 */
async function captureInputArgs(outcomes, run) {
  const calls = []
  const original = inputRunner.run
  inputRunner.run = async (args) => {
    calls.push([...args])
    const outcome = outcomes[Math.min(calls.length - 1, outcomes.length - 1)]
    if (outcome instanceof Error) throw outcome
    return outcome
  }
  try {
    return await run(calls)
  } finally {
    inputRunner.run = original
    operationTarget.handle = null
    operationTarget.frame = null
  }
}

/** 工具 execute 需要的执行上下文（只用到 signal）。 */
const fakeExec = () => ({ signal: new AbortController().signal })

test('click：置前与点击必须在同一次 CLI 调用里（否则跨进程互锁中间有缝）', async () => {
  const { tool } = mountHost()
  operationTarget.handle = 4242
  await captureInputArgs([{ ok: true }], async (calls) => {
    await tool('click').execute({ x: 10, y: 20 }, fakeExec())
    assert.equal(calls.length, 1, '置前与动作不许拆成两次调用')
    assert.deepEqual(calls[0], ['--ensure-front', '4242', '--unblock', '--at', '10', '20', '--click', '10', '20'])
  })
})

test('click：没 see 过目标时只跑动作本身（不带 --ensure-front）', async () => {
  const { tool } = mountHost()
  await captureInputArgs([{ ok: true }], async (calls) => {
    await tool('click').execute({ x: 3, y: 4 }, fakeExec())
    assert.deepEqual(calls, [['--click', '3', '4']], '没有目标窗口就没有前置校验可做')
  })
})

test('click：前置校验不过要报错，且绝不单独再点一次', async () => {
  const { tool } = mountHost()
  operationTarget.handle = 4242
  await captureInputArgs([{ ok: false, error: '坐标 (10, 20) 处最顶层的窗口是 0x200「记事本」' }], async (calls) => {
    await assert.rejects(() => tool('click').execute({ x: 10, y: 20 }, fakeExec()), /0x200/)
    assert.equal(calls.length, 1, 'CLI 已经在持锁区间内把动作挡住了，宿主不许再补一次点击')
  })
})

test('click：目标窗口已消失（stale）→ 丢掉过期记录并只跑动作本身', async () => {
  const { tool } = mountHost()
  operationTarget.handle = 9
  await captureInputArgs([{ ok: false, stale: true }, { ok: true }], async (calls) => {
    await tool('click').execute({ x: 1, y: 2 }, fakeExec())
    assert.equal(calls.length, 2)
    assert.ok(calls[0].includes('--ensure-front'), '第一次要带前置校验，才知道窗口已经没了')
    assert.deepEqual(calls[1], ['--click', '1', '2'], '重跑时不得再带已经过期的前置校验')
    assert.equal(operationTarget.handle, null, '过期记录必须清掉，否则会永久卡住之后的点击')
  })
})

test('type_text：置前合并进同一次调用，且不带 --at（键盘输入没有坐标）', async () => {
  const { tool } = mountHost()
  operationTarget.handle = 77
  await captureInputArgs([{ ok: true }], async (calls) => {
    await tool('type_text').execute({ text: 'hello' }, fakeExec())
    assert.deepEqual(calls[0], ['--ensure-front', '77', '--unblock', '--type', 'hello'])
  })
})

test('scroll：水平/竖直都走同一条合并路径，前置校验的坐标就是鼠标所在点', async () => {
  const { tool } = mountHost()
  operationTarget.handle = 100
  await captureInputArgs([{ ok: true }, { ok: true }], async (calls) => {
    await tool('scroll').execute({ x: 5, y: 6, dx: 3 }, fakeExec())
    await tool('scroll').execute({ x: 7, y: 8, dy: -2 }, fakeExec())
    assert.deepEqual(calls[0], ['--ensure-front', '100', '--unblock', '--at', '5', '6', '--scroll-h', '5', '6', '3'])
    assert.deepEqual(calls[1], ['--ensure-front', '100', '--unblock', '--at', '7', '8', '--scroll', '7', '8', '-2'])
  })
})

// ── 跨进程输入锁：会话身份 + 等待上限的预算关系 ─────────────────────────────────

test('输入锁：exec.agent 的会话 id 要变成 --lock-label（超时的人才知道去找谁）', () => {
  const args = withLockLabel(['--click', '1', '2'], { signal: new AbortController().signal, agent: { id: 'sess-42' } })
  const at = args.indexOf('--lock-label')
  assert.ok(at >= 0, '持有者记录里要能看出是哪个会话占着锁')
  assert.equal(args[at + 1], 'session=sess-42')
  assert.deepEqual(args.slice(0, 3), ['--click', '1', '2'], '原参数不许被改动')
})

test('输入锁：拿不到会话身份时就不带 --lock-label（不编一个假身份出来）', () => {
  const bare = { signal: new AbortController().signal }
  assert.deepEqual(withLockLabel(['--click', '1', '2'], bare), ['--click', '1', '2'])
  assert.deepEqual(withLockLabel(['--click', '1', '2'], { ...bare, agent: {} }), ['--click', '1', '2'])
  assert.deepEqual(withLockLabel(['--click', '1', '2'], { ...bare, agent: { id: 123 } }), ['--click', '1', '2'])
})

test('输入锁：超长的会话 id 要截断（它只是给人看的诊断信息，不该把一条错误消息撑成一屏）', () => {
  const args = withLockLabel([], { signal: new AbortController().signal, agent: { id: 'x'.repeat(200) } })
  assert.equal(args[1].length, 'session='.length + 64)
})

test('输入锁：身份只在两处注入（标准输入路径 1 处 + get_clipboard 自己 1 处）', () => {
  // 为什么是源码级断言：`withLockLabel` 是纯函数（上面已单测），真正会漂的是**注入点**——
  // 多一处调用就多一条「有的路径带了身份、有的没带」的路。故意新增注入点时，请同时改这个数字。
  const lib = readFileSync(new URL('../lib/index.js', import.meta.url), 'utf8')
  const occurrences = [...lib.matchAll(/withLockLabel\(/g)].length
  assert.equal(occurrences, 3, `withLockLabel 应为 1 处定义 + 2 处调用，实际 ${occurrences} 处`)
})

test('输入锁的等待上限必须给动作留出足够时间（锁等待 + 动作 < 工具超时）', () => {
  // 跨语言不变量：Python 侧的等待上限一调大，这条就会红。规则：等待上限 ≤ 工具超时的 1/3，
  // 这样即使真的等到最后一刻，动作本身仍有 2/3 的预算——否则工具会在等待中被宿主 abort。
  const source = readFileSync(new URL('../cvision/input_lock.py', import.meta.url), 'utf8')
  const lockWaitMs = Number(source.match(/DEFAULT_TIMEOUT_S\s*=\s*([\d.]+)/)?.[1]) * 1000
  assert.ok(Number.isFinite(lockWaitMs) && lockWaitMs > 0, 'input_lock.py 里要能读到 DEFAULT_TIMEOUT_S')
  const { definitions } = mountHost()
  const inputTools = [
    'click', 'click_at', 'double_click', 'mouse_move', 'scroll', 'drag',
    'type_text', 'press_key', 'focus_window', 'get_clipboard', 'set_clipboard',
  ]
  for (const name of inputTools) {
    const tool = definitions.find((definition) => definition.name === name)
    assert.ok(tool, `${name} 必须注册`)
    assert.ok(
      lockWaitMs * 3 <= tool.timeoutMs,
      `${name}: 锁等待 ${lockWaitMs}ms 超过它超时的 1/3（${tool.timeoutMs}ms）——等待会把动作预算吃光`,
    )
  }
})

// ── 「抓取结果就是权威」（v0.2.33）：旧句柄/旧矩形绝不能被沿用 ────────────────
//
// 背景（真实缺陷）：非 text 抓取与整屏抓取在修复前拿不到句柄，而宿主只在「非 null」时写入
// `operationTarget`——于是旧句柄一直留着，后续 `type_text`/`press_key` 会把**上一个窗口**当成
// 目标：它会真的把那个窗口置前、把按键送进去，而键盘类动作没有坐标可校验（CLI 的判据是
// `focused`），**整条链路报成功**。点击类动作虽会被前置校验拦下，但也会先真的动一下鼠标。
//
// 这条不变量没法靠真桌面回归（那要往窗口里真的打字、真的移鼠标），所以直接钉在纯函数上。

test('抓取目标：拿不到句柄/矩形时必须清掉旧值（否则键盘输入会进上一个窗口）', () => {
  operationTarget.handle = 4242
  operationTarget.frame = { x: 1, y: 2, width: 3, height: 4 }
  noteCaptureTarget(null, null) // 整屏抓取：本来就没有目标窗口
  assert.equal(operationTarget.handle, null, '旧句柄必须被清掉，绝不能留给后续输入使用')
  assert.equal(operationTarget.frame, null, '旧矩形同样要清：click_at 不能用上一张图的矩形换算坐标')
})

test('抓取目标：新的抓取结果覆盖旧值（不是「有值才覆盖」）', () => {
  operationTarget.handle = 111
  operationTarget.frame = { x: 0, y: 0, width: 10, height: 10 }
  noteCaptureTarget(222, { x: 5, y: 6, width: 7, height: 8 })
  assert.equal(operationTarget.handle, 222)
  assert.deepEqual(operationTarget.frame, { x: 5, y: 6, width: 7, height: 8 })
  // 这两项是进程级状态：收尾清掉，别影响后面的用例。
  operationTarget.handle = null
  operationTarget.frame = null
})

test('type_text：默认不带开关，direct=true 才加 --type-direct（路径由 Python 按输入法决定）', async () => {
  // 回归（v0.2.34 现场）：ASCII 文本逐键输入会被输入法改写成拼音/候选（实测
  // `cvision smoke 12345` → `才visionsmoke12345`，且不报错）。默认路径交给 Python 的
  // `input.type_text` 判断（非 ASCII/换行/CJK 布局 → 粘贴），这里只钉「开关确实传下去了」。
  const { tool } = mountHost()
  operationTarget.handle = null
  await captureInputArgs([{ ok: true }, { ok: true }], async (calls) => {
    await tool('type_text').execute({ text: 'abc' }, fakeExec())
    await tool('type_text').execute({ text: 'abc', direct: true }, fakeExec())
    assert.deepEqual(calls[0], ['--type', 'abc'], '默认不带任何路径开关')
    assert.deepEqual(calls[1], ['--type', 'abc', '--type-direct'])
  })
})

test('wait_for_window：按标题找窗口必须交给 Python 解析器，不许自己扫子串', () => {
  // 回归（v0.2.34 现场）：宿主原先取「枚举顺序里第一个标题含子串的窗口」，于是
  // wait_for_window("运行") 拿到的是「v2rayN … 以非管理员身份运行」，而精确标题就叫「运行」的
  // 那个对话框排在它后面。拿错窗口是**静默**的：之后对着这个 handle 的抓图/点击全作用在错窗口上。
  // 真正的匹配语义（精确标题优先 → 非最小化 → 面积大）在 `capture.base.pick_window` 里只有一份，
  // 所以这里钉「宿主改成了调 find_window 解析入口」+「自写扫描已消失」。
  // 端到端跑不了：那要 spawn Python 并真的轮询窗口（CI 的 JS 任务不装 Pillow/pywin32）。
  const lib = readFileSync(new URL('../lib/index.js', import.meta.url), 'utf8')
  assert.ok(lib.includes('find_window'), 'wait_for_window 必须走 find_window 这个解析入口')
  assert.ok(!lib.includes('.includes(needle)'), '不许再出现「标题里含子串」的自写扫描')
})

test('抓取目标：see 与两个 wait_* 都必须调用它（wait 返回的是新图，基准得跟着换）', () => {
  // `wait_*` 返回一张**新图**，而 `click_at` 是按比例点「最近一次抓取那张图」：不更新基准的话，
  // 「等它变完 → 按比例点结果区」会按上一次 see 的矩形换算——位置整体偏，而且不报错。
  // 这条不变量没法在单测里端到端跑（真实实现要 spawn Python 并阻塞轮询），所以钉**调用点数量**：
  // 少一处、或将来多一处没写进这里的，都会让这条红。
  const lib = readFileSync(new URL('../lib/index.js', import.meta.url), 'utf8')
  const occurrences = [...lib.matchAll(/noteCaptureTarget\(/g)].length
  assert.equal(
    occurrences,
    4,
    `noteCaptureTarget 应为 1 处定义 + 3 处调用（see + wait_until_changed + wait_until_stable），实际 ${occurrences} 处`,
  )
})
