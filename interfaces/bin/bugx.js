#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { resolve, join, dirname, delimiter } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const pkgRoot = existsSync(join(__dirname, '..', 'package.json'))
  ? resolve(__dirname, '..')
  : resolve(__dirname, '..', '..');

function commandParts(command) {
  const trimmed = command.trim();
  if (existsSync(trimmed)) return [trimmed, []];
  const [executable, ...prefixArgs] = trimmed.split(/\s+/);
  return [executable, prefixArgs];
}

function resolvePython() {
  const manifestPath = join(pkgRoot, 'runtime', 'python-manifest.json');
  if (existsSync(manifestPath)) {
    try {
      const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
      if (manifest.type === 'bundled') {
        const binPath = join(pkgRoot, manifest.path);
        if (existsSync(binPath)) return binPath;
      }
      if (manifest.type === 'system') return manifest.command;
    } catch { /* fall through to env/PATH resolution */ }
  }
  if (process.env.BUGX_PYTHON) return process.env.BUGX_PYTHON;

  const homedir = process.env.HOME || process.env.USERPROFILE || '';
  const globalStandalone = process.platform === 'win32'
    ? join(homedir, '.bugx', 'python', 'python.exe')
    : join(homedir, '.bugx', 'python', 'bin', 'python3');
  if (existsSync(globalStandalone)) return globalStandalone;

  const candidates = process.platform === 'win32'
    ? ['py -3.14', 'py -3.13', 'py -3.12', 'py -3.11', 'py -3', 'python', 'python3']
    : [
        'python3.14', 'python3.13', 'python3.12', 'python3.11',
        '/opt/homebrew/bin/python3', '/usr/local/bin/python3',
        'python3', 'python'
      ];
  for (const cand of candidates) {
    try {
      const [executable, prefixArgs] = commandParts(cand);
      const check = spawnSync(executable, [...prefixArgs, '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'], { stdio: 'ignore' });
      if (!check.error && check.status === 0) return cand;
    } catch {}
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

const pythonCmd = resolvePython();
const env = { ...process.env };
env.BUGX_ASSET_ROOT = env.BUGX_ASSET_ROOT || join(pkgRoot, 'core');
env.BUGX_PYTHON = pythonCmd;
env.BUGX_CLI_SOURCE = 'npm';
const cliDir = existsSync(join(pkgRoot, 'interfaces', 'cli'))
  ? join(pkgRoot, 'interfaces', 'cli')
  : join(pkgRoot, 'src');
env.PYTHONPATH = env.PYTHONPATH
  ? `${cliDir}${delimiter}${env.PYTHONPATH}`
  : cliDir;

const args = ['-m', 'bugx_cli.cli', ...process.argv.slice(2)];
const [pythonExecutable, pythonPrefixArgs] = commandParts(pythonCmd);
const res = spawnSync(pythonExecutable, [...pythonPrefixArgs, ...args], { stdio: 'inherit', env });

if (res.error) {
  if (res.error.code === 'ENOENT') {
    console.error('[bugx] ✗ 错误: 未找到可用 Python 3.11+ 环境。请安装 Python 3.11+ 或运行 "npm rebuild bugx-cli"。');
  } else {
    console.error(`[bugx] ✗ 启动失败: ${res.error.message}`);
  }
  process.exit(1);
}
process.exit(res.status ?? 0);
