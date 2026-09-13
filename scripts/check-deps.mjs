/**
 * Python 依赖锁定自检（不联网、不需要安装任何 Python 包）。
 *
 * 背景：`requirements.txt` 是本插件**唯一的独立供应链面**（Node 侧无运行时
 * dependencies，Python 侧全部来自这一个文件）。原先它只有下界（`Pillow>=12.0.0`），
 * 意味着 `pip install -r requirements.txt` 在不同时间会装到不同版本，且可能被静默
 * 拖进破坏性升级——重装环境因此不可复现。
 *
 * 这里把「锁定约定」固化成断言，而不是靠人记得：
 *  - 每条依赖都必须**同时**有下界 `>=` 与上界 `<`（不允许只有下界的裸声明）；
 *  - 上下界都必须是具体版本（不接受 `*`、`~=`、`==` 通配或空版本）；
 *  - 包名不得重复（重复会让 pip 取交集，行为难推理）；
 *  - 平台 marker 只能用 `sys_platform == 'win32' | 'darwin'`（其它写法本仓库没验证）。
 *
 * 用法：`npm run check:deps`（CI 每次都会跑）。
 */
import { existsSync, readFileSync } from 'node:fs'

const root = new URL('../', import.meta.url)
const read = (rel) => readFileSync(new URL(rel, root), 'utf8')
const exists = (rel) => existsSync(new URL(rel, root))

const results = []
function check(label, ok, detail = '') {
  results.push({ label, ok, detail })
}

// ── 1. 清单存在且随包发布 ───────────────────────────────────────────────────
check('requirements.txt 存在', exists('requirements.txt'))
const text = exists('requirements.txt') ? read('requirements.txt') : ''
const pkg = JSON.parse(read('package.json'))
check(
  'files 随包分发 requirements.txt（否则装不到依赖）',
  Array.isArray(pkg.files) && pkg.files.includes('requirements.txt'),
  JSON.stringify(pkg.files ?? null),
)

// ── 2. 解析依赖行 ───────────────────────────────────────────────────────────
/** 解析一条 PEP 508 依赖：返回包名（小写）、marker 与版本说明符列表。 */
function parseRequirement(line) {
  const body = line.split('#')[0].trim() // 去掉行尾注释
  if (body === '') return null
  const semi = body.indexOf(';')
  const spec = (semi < 0 ? body : body.slice(0, semi)).trim()
  const marker = semi < 0 ? '' : body.slice(semi + 1).trim()
  const match = /^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$/.exec(spec)
  if (!match) return { name: spec, marker, specifiers: [], malformed: true }
  const specifiers = match[3] === '' ? [] : match[3].split(',').map((part) => part.trim()).filter(Boolean)
  return { name: match[1].toLowerCase(), marker, specifiers }
}

const lines = text.split(/\r?\n/)
const requirements = []
for (const line of lines) {
  const parsed = parseRequirement(line)
  if (parsed !== null) requirements.push(parsed)
}
check('清单里至少有一条依赖', requirements.length > 0, `${requirements.length} 条`)

// ── 3. 每条依赖都必须双向锁定 ───────────────────────────────────────────────
const malformed = requirements.filter((req) => req.malformed === true)
check('每条依赖都能解析出包名与版本说明符', malformed.length === 0, malformed.map((r) => r.name).join(', '))

/** 版本号必须是具体值：字母数字与 . - _ + 组成（如 1.0.0b10 / 311 / 12.0.0）。 */
const isConcreteVersion = (version) => /^[0-9][0-9A-Za-z.\-_+]*$/.test(version)

const unbounded = []
const notConcrete = []
for (const req of requirements) {
  const lower = req.specifiers.find((part) => part.startsWith('>='))
  const upper = req.specifiers.find((part) => part.startsWith('<'))
  if (lower === undefined || upper === undefined) {
    unbounded.push(`${req.name}${req.specifiers.length ? ` (${req.specifiers.join(',')})` : ' (无版本约束)'}`)
    continue
  }
  for (const part of req.specifiers) {
    const version = part.replace(/^(>=|<=|==|~=|!=|<|>)/, '')
    if (!isConcreteVersion(version)) notConcrete.push(`${req.name}${part}`)
  }
}
check(
  '每条依赖都有下界与上界（>=x,<y，不允许裸 >=）',
  unbounded.length === 0,
  unbounded.length === 0 ? `${requirements.length} 条全部双向锁定` : `未锁定：${unbounded.join('; ')}`,
)
check(
  '上下界都是具体版本（不接受 ~= / * / 空版本）',
  notConcrete.length === 0,
  notConcrete.length === 0 ? '全部为具体版本' : `可疑：${notConcrete.join(', ')}`,
)

// ── 4. 包名不重复 ───────────────────────────────────────────────────────────
const seen = new Set()
const duplicates = []
for (const req of requirements) {
  if (seen.has(req.name)) duplicates.push(req.name)
  seen.add(req.name)
}
check('依赖包名不重复', duplicates.length === 0, duplicates.join(', ') || `${seen.size} 个包`)

// ── 5. 平台 marker 只允许已验证写法 ─────────────────────────────────────────
const allowedMarkers = new Set(["sys_platform == 'win32'", "sys_platform == 'darwin'"])
const badMarkers = requirements
  .filter((req) => req.marker !== '' && !allowedMarkers.has(req.marker))
  .map((req) => `${req.name}; ${req.marker}`)
check('平台 marker 只用 sys_platform == win32/darwin', badMarkers.length === 0, badMarkers.join(' | ') || '全部合规')

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
  console.error(`\nPython 依赖未按约定锁定：\n${failed.map((result) => `  - ${result.label}`).join('\n')}`)
  process.exit(1)
}

// `--list` 打印当前锁定区间，供**定期人工审查**（README 的约定：每季度一次）。
// 为什么不把它做成 CI 断言「区间是否陈旧」：那要联网查 PyPI 最新版本，而本脚本刻意不联网；
// 而且「该不该跟进一个破坏性升级」是判断，不是能机械判定的事。
if (process.argv.includes('--list')) {
  console.log('\n当前锁定区间（审查点：上游是否已发新主版本 / 是否因上界过紧导致安全修复进不来）：')
  for (const req of requirements) {
    const lower = req.specifiers.find((part) => part.startsWith('>=')) ?? ''
    const upper = req.specifiers.find((part) => part.startsWith('<')) ?? ''
    const marker = req.marker === '' ? '' : `  [${req.marker}]`
    console.log(`  ${req.name.padEnd(28)} ${lower} ${upper}${marker}`)
  }
}
