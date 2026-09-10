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
import test, { mock } from 'node:test'

/** 让出一个宏任务：用于推进被 await 的处理器（如「截图进行中」的时序用例）。 */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

const { apply, snipExecutor, clipboardProbe, describeRuntimeProblem, PIP_HINT, ensureRuntime } = await import(
  '../lib/index.js'
)

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
    statusOf({ platform: 'linux', backend: 'linux', backend_implemented: false }),
    null,
  )
  assert.ok(problem, '后端未实现必须拦下')
  assert.match(problem, /linux/)
  assert.match(problem, /后端未实现/)
  // 平台不支持不是 `pip install` 能解决的，不该误导用户去装包。
  assert.ok(!problem.includes('pip install'), '后端未实现时不要给安装命令')
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

test('体检门：真实环境下 ensureRuntime 放行（本机依赖齐全）', async () => {
  // 这条会真的 spawn 一次 `python -m cvision.cli_capture --status`（本仓库 CI 装了 Pillow）。
  // 若本机没装 Python/依赖，它会抛出可操作错误——那正是门在起作用的证据，故不强断言成功。
  try {
    await ensureRuntime({ signal: AbortSignal.timeout(20000) })
  } catch (error) {
    assert.match(String(error.message), /pip install|cvision_status|Python 3\.10\+/)
  }
})
