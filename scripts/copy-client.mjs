/**
 * 把客户端半边 `src/client.js` 逐字节拷到 `lib/client.js`。
 *
 * 为什么不能交给 tsc：`package.json` 的 `"type": "module"` 会让 tsc 把任何 .js
 * 都当成 ES 模块，输出末尾追加 `export {}`。而 DSH 的客户端模块系统是以**经典
 * 脚本**加载 `exports["./client"]` 的（见 dsh-client-modules：combo script →
 * `window.__ModuleLoader__.load`），经典脚本里出现 `export` 会直接语法错误。
 * 所以这份文件按源码原样发布，只做拷贝、不做编译。
 */
import { copyFileSync } from 'node:fs'

const source = new URL('../src/client.js', import.meta.url)
const target = new URL('../lib/client.js', import.meta.url)

copyFileSync(source, target)
console.log('copied src/client.js -> lib/client.js')
