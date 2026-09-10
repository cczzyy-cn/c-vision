/**
 * DSH 组合包契约自检（不联网、不需要 DSH 运行）。
 *
 * 依据 DSH 文档：
 *  - `docs/user/develop/basic/publish.zh.md`：组合包 manifest（`dsh.bundle.patch`、patch 按**包名**
 *    引用本包、`files` 必须带上 patch 与插件模块、git 安装不会跑构建所以要么给 `prepare` 要么发布
 *    预构建产物）。
 *  - `docs/subsystems/client-modules.zh.md`：客户端半边要声明 `dsh.client`（`platform: 'web'`、可选
 *    `inject` 边）并导出 `exports["./client"]`；浏览器模块 id 由最近的包 manifest 提供，也就是**包名**。
 *
 * 这些约束以前踩过坑（客户端 bundle 被 tsc 追加 `export {}` 就不是经典脚本了；宿主运行时 import
 * 没声明的包会在别人机器上解析失败），所以固化成脚本，交给 CI 跑。
 */
import { existsSync, readFileSync } from 'node:fs'

const root = new URL('../', import.meta.url)
const read = (rel) => readFileSync(new URL(rel, root), 'utf8')
const exists = (rel) => existsSync(new URL(rel, root))

const pkg = JSON.parse(read('package.json'))
const results = []
function check(label, ok, detail = '') {
  results.push({ label, ok, detail })
}

/** 去掉注释后按行扫 `import ... from 'x'`，得到真实外部 import。 */
function externalImports(source) {
  return [...source.matchAll(/^import\s+(?:type\s+)?(?:[^'"]*?\sfrom\s+)?['"]([^'"]+)['"]/gm)]
    .map((match) => match[1])
    .filter((specifier) => !specifier.startsWith('node:'))
}

// ── 1. 包身份与入口 ─────────────────────────────────────────────────────────
check('name / version / description 齐备', Boolean(pkg.name && pkg.version && pkg.description))
check('type=module', pkg.type === 'module')
check('main 指向已构建入口且文件存在', typeof pkg.main === 'string' && exists(pkg.main), String(pkg.main))
check('exports["."] 指向已构建入口', exists(pkg.exports?.['.']?.default ?? ''), String(pkg.exports?.['.']?.default))
check('engines.node 声明（DSH 要求 >=20）', Boolean(pkg.engines?.node), String(pkg.engines?.node))
check('license 声明', Boolean(pkg.license), String(pkg.license))
check('LICENSE 文件存在', exists('LICENSE'))
check('repository 声明（市场按它认领来源）', Boolean(pkg.repository?.url), String(pkg.repository?.url))

// ── 2. 组合包 manifest（dsh.bundle） ────────────────────────────────────────
const patchRel = pkg.dsh?.bundle?.patch
check('dsh.bundle.patch 声明', typeof patchRel === 'string', String(patchRel))
check('patch 文件存在', typeof patchRel === 'string' && exists(patchRel), String(patchRel))

const patchText = typeof patchRel === 'string' && exists(patchRel) ? read(patchRel) : ''
const patchNames = [...patchText.matchAll(/^\s*-?\s*name:\s*['"]?([^'"\s#]+)['"]?\s*$/gm)].map((m) => m[1])
check('patch 至少插入一行', patchNames.length > 0, patchNames.join(', '))
check(
  'patch 行按包名引用本包（Node 才能解析到已安装代码）',
  patchNames.includes(pkg.name),
  `rows=[${patchNames.join(', ')}] pkg=${pkg.name}`,
)

// ── 3. files 覆盖运行所需的一切 ─────────────────────────────────────────────
const files = Array.isArray(pkg.files) ? pkg.files : []
check('files 声明（否则 git/tarball 安装会缺文件）', files.length > 0, `${files.length} entries`)
for (const required of ['lib/index.js', 'cordis.patch.yml', 'requirements.txt']) {
  check(`files 包含 ${required}`, files.includes(required))
}
check('files 包含捆绑的 Python cvision 目录', files.some((entry) => entry.startsWith('cvision/')))
check('files 包含 LICENSE 与 README', files.includes('LICENSE') && files.includes('README.md'))

// ── 4. 预构建产物随包发布（git 安装不跑 build） ─────────────────────────────
check('lib/index.js 已提交（git 安装拿到的就是构建产物）', exists('lib/index.js'))
check('构建脚本存在', typeof pkg.scripts?.build === 'string', String(pkg.scripts?.build))

// ── 5. 宿主半边：外部 import 必须声明为 dependencies/peerDependencies ────────
const hostExternal = [...new Set(externalImports(read('lib/index.js')))]
const declaredRuntime = new Set([
  ...Object.keys(pkg.dependencies ?? {}),
  ...Object.keys(pkg.peerDependencies ?? {}),
])
const undeclared = hostExternal.filter((specifier) => !declaredRuntime.has(specifier))
check(
  '宿主产物的外部 import 都已声明（dependencies / peerDependencies）',
  undeclared.length === 0,
  `imports=[${hostExternal.join(', ')}] undeclared=[${undeclared.join(', ')}]`,
)
check(
  '无运行时 dependencies（依赖由宿主提供，声明为 peer）',
  Object.keys(pkg.dependencies ?? {}).length === 0,
  JSON.stringify(pkg.dependencies ?? {}),
)

// ── 6. 客户端半边（dsh.client） ─────────────────────────────────────────────
const client = pkg.dsh?.client
check('dsh.client.platform = web', client?.platform === 'web', String(client?.platform))
check(
  'dsh.client.inject 是字符串数组（或省略）',
  client?.inject === undefined
    || (Array.isArray(client.inject) && client.inject.every((edge) => typeof edge === 'string')),
  JSON.stringify(client?.inject ?? null),
)
const clientRel = pkg.exports?.['./client']?.default
check('exports["./client"] 声明', typeof clientRel === 'string', String(clientRel))
check('客户端 bundle 文件存在', typeof clientRel === 'string' && exists(clientRel), String(clientRel))

if (typeof clientRel === 'string' && exists(clientRel)) {
  const source = read(clientRel)
  // 经典脚本约束：出现 export/import 就不是经典脚本，浏览器直接语法错误。
  check('客户端 bundle 是经典脚本（无 export/import 语句）', !/^\s*(export|import)\s/m.test(source))
  const registration = source.match(/__ModuleLoader__\.load\(\{\s*id:\s*['"]([^'"]+)['"]/)
  check(
    '客户端 bundle 以包名注册（__ModuleLoader__.load id === 包名）',
    registration?.[1] === pkg.name,
    `id=${String(registration?.[1])} pkg=${pkg.name}`,
  )
  // 模块表之外的 specifier 必须写进 dsh.client.external；react 是基线模块。
  const external = new Set(client?.external ?? [])
  const requires = [...new Set([...source.matchAll(/require\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1]))]
  const unresolvable = requires.filter(
    (specifier) => !specifier.startsWith('react') && !external.has(specifier),
  )
  check(
    '客户端 bundle 只 require 基线模块或 dsh.client.external',
    unresolvable.length === 0,
    `requires=[${requires.join(', ')}]`,
  )
}

// ── 7. 构建产物与源码一致（客户端半边是逐字节拷贝） ─────────────────────────
if (exists('src/client.js') && exists('lib/client.js')) {
  check('lib/client.js 与 src/client.js 逐字节一致', read('src/client.js') === read('lib/client.js'))
}

// ── 输出 ────────────────────────────────────────────────────────────────────
const width = Math.max(...results.map((result) => result.label.length))
for (const result of results) {
  const mark = result.ok ? '✅' : '❌'
  const detail = result.detail === '' ? '' : `  (${result.detail})`
  console.log(`${mark} ${result.label.padEnd(width)}${detail}`)
}
const failed = results.filter((result) => !result.ok)
console.log(`\n${results.length - failed.length}/${results.length} 项通过`)
if (failed.length > 0) {
  console.error(`\n不符合 DSH 契约：\n${failed.map((result) => `  - ${result.label}`).join('\n')}`)
  process.exit(1)
}
