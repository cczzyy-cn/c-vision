/**
 * 文档一致性自检（不联网、不需要 DSH 运行）——把「文档约定」固化成断言，而不是靠人记得。
 *
 * 每一条都对应一次**真实踩过的漂移**：
 *  - 版本号漂移：README 写 0.2.12、`package.json` 已是 0.2.13；CHANGELOG 忘了加条目；
 *  - 长按阈值漂移：README 写 550ms，`src/client.js` 早已是 900ms；
 *  - 目录结构漏文件：新增 `snip.py`/`clipboard.py`/`test_snip_windows.py` 却没写进 README；
 *  - 工具/路由漏文档：加了路由或工具却没写进 README；
 *  - 新文档忘了随包发布：加了 `CHANGELOG.md` 却没进 `files`；
 *  - 测试数量漂移：README 声称的条数与实际测试条数不一致；
 *  - 锚点失效：README 里的目录链接指向不存在的标题。
 *
 * 用法：`npm run check:docs`（CI 每次都会跑）。
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs'

const root = new URL('../', import.meta.url)
const read = (rel) => readFileSync(new URL(rel, root), 'utf8')
const exists = (rel) => existsSync(new URL(rel, root))
const list = (rel, suffix) =>
  exists(rel)
    ? readdirSync(new URL(rel, root))
        .filter((name) => name.endsWith(suffix))
        .sort()
    : []

const pkg = JSON.parse(read('package.json'))
const readme = read('README.md')
const changelog = read('CHANGELOG.md')
const results = []
function check(label, ok, detail = '') {
  results.push({ label, ok, detail })
}

// ── 1. 版本号：package.json ↔ CHANGELOG ↔ README 必须一致 ────────────────────
const version = pkg.version
check('package.json 版本号形如 x.y.z', /^\d+\.\d+\.\d+$/.test(version), version)

const changelogVersions = [...changelog.matchAll(/^##\s+v(\d+\.\d+\.\d+)/gm)].map((m) => m[1])
check(
  `CHANGELOG 有当前版本条目（v${version}）`,
  changelogVersions.includes(version),
  `找到 [${changelogVersions.slice(0, 4).join(', ')}…]`,
)
check(
  'CHANGELOG 最新条目就是当前版本（新的在最上面）',
  changelogVersions[0] === version,
  `首条 v${String(changelogVersions[0])}`,
)

// 最新条目不能是空壳或占位符。
const newestBody = changelog.split(/^##\s+v\d+\.\d+\.\d+/m)[1]?.split(/^##\s+v\d+\.\d+\.\d+/m)[0] ?? ''
check('CHANGELOG 最新条目有实质内容（≥80 字）', newestBody.trim().length >= 80, `${newestBody.trim().length} 字`)
// 只看条目**首行**（概要行）：正文里为了说明规则而引用「TODO/待填」这类字样不该被判违规。
const newestSummary = newestBody
  .split('\n')
  .map((line) => line.trim())
  .find((line) => line !== '') ?? ''
check(
  'CHANGELOG 最新条目首行不是占位符',
  !/^(TODO|TBD|待填|xxx)/i.test(newestSummary),
  newestSummary.slice(0, 40),
)

const readmeVersion = readme.match(/\*\*版本\*\*：`([^`]+)`/)?.[1]
check(
  'README 头部版本号与 package.json 一致',
  readmeVersion === version,
  `README=${String(readmeVersion)} pkg=${version}`,
)

// ── 2. 行为参数：README 里写死的阈值必须等于源码里的值 ──────────────────────
if (exists('src/client.js')) {
  const longPressMs = Number(read('src/client.js').match(/LONG_PRESS_MS\s*=\s*(\d+)/)?.[1])
  if (Number.isFinite(longPressMs)) {
    const seconds = String(longPressMs / 1000)
    check(
      `README 写明当前长按阈值（${seconds}s）`,
      readme.includes(`${seconds}s`),
      `LONG_PRESS_MS=${longPressMs}`,
    )
    // 只看「长按」附近 12 字内的数字：正文别处（例如说明规则时引用历史值 550ms）不该被判违规。
    const stale = [...readme.matchAll(/长按[^\n]{0,12}?(\d+(?:\.\d+)?)\s*(ms|s)/g)]
      .map((match) => (match[2] === 'ms' ? Number(match[1]) : Number(match[1]) * 1000))
      .filter((ms) => ms !== longPressMs)
    check('README 中「长按」附近的阈值都是当前值', stale.length === 0, stale.map((ms) => `${ms}ms`).join(', '))
  }
}

// ── 3. 目录结构：仓库里真实存在的源文件都要在 README 里出现 ─────────────────
const described = [
  ...list('cvision', '.py').map((name) => `cvision/${name}`),
  ...list('cvision/capture', '.py').map((name) => `cvision/capture/${name}`),
  ...list('tests', '.py'),
  ...list('tests', '.mjs'),
  ...list('scripts', '.mjs'),
]
const missingInReadme = described.filter((rel) => {
  const base = rel.split('/').pop()
  return !readme.includes(base)
})
check(
  'README 目录结构覆盖全部 cvision/tests/scripts 文件',
  missingInReadme.length === 0,
  missingInReadme.length === 0 ? `${described.length} 个文件` : `缺 ${missingInReadme.join(', ')}`,
)

// ── 4. 文档要随包发布（files） ──────────────────────────────────────────────
const files = Array.isArray(pkg.files) ? pkg.files : []
const docFiles = ['README.md', 'CHANGELOG.md'].filter((name) => exists(name))
const undocumentedFiles = docFiles.filter((name) => !files.includes(name))
check('files 包含全部顶层文档', undocumentedFiles.length === 0, undocumentedFiles.join(', ') || docFiles.join(', '))

// ── 5. 工具与宿主路由都要有文档 ─────────────────────────────────────────────
if (exists('lib/index.js')) {
  const lib = read('lib/index.js')
  const tools = [...new Set([...lib.matchAll(/name:\s*['"]([a-z_][a-z0-9_]*)['"]/g)].map((m) => m[1]))].filter(
    (name) => !['vision', 'web'].includes(name),
  )
  const knownTools = tools.filter((name) => /defineTool|description/.test(lib) && name.length > 2)
  const missingTools = knownTools.filter((name) => !readme.includes(`\`${name}`))
  check(
    'README 覆盖宿主注册的全部工具',
    missingTools.length === 0,
    missingTools.length === 0 ? `${knownTools.length} 个工具` : `缺 ${missingTools.join(', ')}`,
  )

  const routes = [...new Set([...lib.matchAll(/['"](\/cvision\/[a-z/-]+)['"]/g)].map((m) => m[1]))].sort()
  const missingRoutes = routes.filter((route) => !readme.includes(route))
  check(
    'README 覆盖全部宿主路由',
    missingRoutes.length === 0,
    missingRoutes.length === 0 ? routes.join(' ') : `缺 ${missingRoutes.join(', ')}`,
  )
}

// ── 6. 测试数量：README 声称的条数 == 实际条数 ───────────────────────────────
const jsTests = list('tests', '.mjs').reduce((sum, name) => sum + (read(`tests/${name}`).match(/\btest\(/g)?.length ?? 0), 0)
const pyTests = list('tests', '.py').reduce((sum, name) => sum + (read(`tests/${name}`).match(/^\s+def test_/gm)?.length ?? 0), 0)
const claimed = readme.match(/JS\s*(\d+)\s*条\s*\+\s*Python\s*(\d+)\s*条/)
check(
  'README 的测试条数与实际一致',
  Boolean(claimed) && Number(claimed[1]) === jsTests && Number(claimed[2]) === pyTests,
  `README=${claimed ? `${claimed[1]}/${claimed[2]}` : '未声明'} 实际=${jsTests}/${pyTests}`,
)

// ── 7. README 内部锚点都要能落到标题上 ──────────────────────────────────────
/** 按 GitHub 的 slug 规则归一化标题（保留 CJK/字母/数字/空白/连字符/下划线，其余去掉）。 */
const slugify = (text) =>
  text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, '')
    .replace(/\s+/g, '-')

const headingSlugs = new Set(
  [...readme.matchAll(/^#{1,6}\s+(.+)$/gm)].map((m) => slugify(m[1].replace(/\s*#+\s*$/, ''))),
)
const anchors = [...new Set([...readme.matchAll(/\]\(#([^)]+)\)/g)].map((m) => decodeURIComponent(m[1]).toLowerCase()))]
const brokenAnchors = anchors.filter((anchor) => !headingSlugs.has(slugify(anchor.replace(/-/g, ' '))))
check(
  'README 内部锚点都能落到标题',
  brokenAnchors.length === 0,
  brokenAnchors.length === 0 ? `${anchors.length} 个链接` : `断链 ${brokenAnchors.join(', ')}`,
)

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
  console.error(`\n文档与代码不一致：\n${failed.map((result) => `  - ${result.label}`).join('\n')}`)
  process.exit(1)
}
