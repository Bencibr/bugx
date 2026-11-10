#!/usr/bin/env node
// postinstall: detect a usable Python 3.11+, else download a pinned
// standalone CPython (python-build-standalone) into runtime/.
// Non-fatal: network failure leaves a "missing" manifest so nothing breaks,
// and bin/bugx.js then requires a system Python at run time.
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createReadStream, existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkgRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const runtimeDir = join(pkgRoot, 'runtime');
const manifestPath = join(runtimeDir, 'python-manifest.json');

function isUsable(cmd) {
  const [executable, ...prefixArgs] = cmd.trim().split(/\s+/);
  const r = spawnSync(executable, [...prefixArgs, '-c', 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'], { stdio: 'ignore' });
  return !r.error && r.status === 0;
}

function writeManifest(m) {
  mkdirSync(runtimeDir, { recursive: true });
  writeFileSync(manifestPath, JSON.stringify(m, null, 2));
}

// 1. System detection: BUGX_PYTHON > high-version candidates > python3/python
const candidates = process.env.BUGX_PYTHON
  ? [process.env.BUGX_PYTHON]
  : (process.platform === 'win32'
      ? ['py -3.14', 'py -3.13', 'py -3.12', 'py -3.11', 'py -3', 'python', 'python3']
      : [
          'python3.14', 'python3.13', 'python3.12', 'python3.11',
          '/opt/homebrew/bin/python3', '/usr/local/bin/python3',
          'python3', 'python'
        ]);
for (const cmd of candidates) {
  if (isUsable(cmd)) {
    writeManifest({ type: 'system', command: cmd });
    console.log(`[bugx-cli] ✓ 使用系统 Python: ${cmd}`);
    process.exit(0);
  }
}

// 2. Standalone download fallback
let lock;
try {
  lock = JSON.parse(readFileSync(join(pkgRoot, 'scripts', 'runtime.lock.json'), 'utf8'));
} catch (err) {
  writeManifest({ type: 'missing', reason: 'runtime lock unavailable' });
  console.warn(`[bugx-cli] ⚠️ 内置 Python 版本锁缺失或损坏: ${err.message}`);
  process.exit(0);
}
const targets = lock.targets;
const key = `${process.platform}-${process.arch}`;
const target = targets[key];
if (!target) {
  writeManifest({ type: 'missing', reason: `unsupported platform ${key}` });
  console.warn(`[bugx-cli] ⚠️ 不支持的平台 ${key}。运行将依赖系统 Python >=3.11。`);
  process.exit(0);
}

const destDir = join(runtimeDir, key);
const okSentinel = join(destDir, '.bugx-python-ok');
if (existsSync(join(destDir, target.bin)) && existsSync(okSentinel)) {
  writeManifest({ type: 'bundled', path: `runtime/${key}/${target.bin}` });
  process.exit(0);
}

const tmp = join(runtimeDir, `.python-${Date.now()}.tar.gz`);
mkdirSync(runtimeDir, { recursive: true });

try {
  console.log(`[bugx-cli] ⏳ 下载内置 Python ${lock.cpython} (${key})…`);
  const res = await fetch(target.url);
  if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
  await writeFileSync?.(tmp, Buffer.from(await res.arrayBuffer()));
  const digest = createHash('sha256').update(readFileSync(tmp)).digest('hex');
  if (digest !== target.sha256) {
    throw new Error(`校验失败: 期望 ${target.sha256} 得到 ${digest}`);
  }
  mkdirSync(destDir, { recursive: true });
  // install_only tar.gz unpacks python/ directly under destDir
  const r = spawnSync('tar', ['-xzf', tmp, '-C', destDir], { stdio: 'ignore' });
  if (r.error || r.status !== 0) throw new Error('tar 解压失败');
  unlinkSync(tmp);
  if (!existsSync(join(destDir, target.bin))) throw new Error(`解压后未找到 ${target.bin}`);
  writeFileSync(okSentinel, new Date().toISOString());
  writeManifest({ type: 'bundled', path: `runtime/${key}/${target.bin}` });
  console.log(`[bugx-cli] ✓ 内置 Python 就绪: ${join(destDir, target.bin)}`);
} catch (err) {
  try { unlinkSync(tmp); } catch {}
  writeManifest({ type: 'missing', reason: err.message.split(':')[0] });
  console.warn(`[bugx-cli] ⚠️ 内置 Python 引导失败: ${err.message}`);
  console.warn('[bugx-cli] ⚠️ 运行将依赖系统 Python >=3.11;可稍后执行 "npm rebuild bugx-cli" 重试。');
  process.exit(0);
}
