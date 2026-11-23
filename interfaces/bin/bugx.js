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

// F-327 快路径：--version 只需版本号，绕过 Python 解析与候选探测
// （此前全链路 461ms：node 45ms + resolvePython 串行探测 + Python 冷启动；
//   直接读 metadata.txt 应 <50ms，耗时账目 W-001）
{
  const argv = process.argv.slice(2);
  if (argv.length === 1 && argv[0] === '--version') {
    try {
      const meta = readFileSync(join(pkgRoot, 'core', 'metadata.txt'), 'utf8');
      const ver = (meta.match(/^bugx_version:\s*"([^"]+)"/m) || [])[1];
      const bid = (meta.match(/^build_id:\s*(\d+)/m) || [])[1];
      if (ver && bid) {
        console.log(`bugx ${ver}+build.${bid}`);
        process.exit(0);
      }
    } catch { /* let the Python entry point report its authoritative version */ }
  }
}

// F-328 快路径：顶层 --help/-h 直接输出 argparse 真源快照（耗时账目 W-003）
// 快照 core/cli-help.txt 由 `python3 scripts/bump_version.py --refresh-help`
// 从 Python argparse 导出，--check 会校验快照与真源逐字一致。
// 仅当 argv 只有 help flag 时拦截：带子命令（bugx audit --help）仍走 Python
// 真源，拿到该子命令自己的帮助文本。
{
  const argv = process.argv.slice(2);
  if (argv.length === 1 && (argv[0] === '--help' || argv[0] === '-h')) {
    const snapshot = join(pkgRoot, 'core', 'cli-help.txt');
    if (existsSync(snapshot)) {
      process.stdout.write(readFileSync(snapshot, 'utf8'));
      process.exit(0);
    }
    // 快照资产缺失：走 Python argparse 真源，保证 help 永远可用。
    console.error('[bugx] 提示: core/cli-help.txt 缺失，help 走 Python 真源路径'
      + '（运行 python3 scripts/bump_version.py --refresh-help 恢复快路径）');
  }
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
    console.error('[bugx] ✗ 错误: 未找到可用 Python 3.11+ 环境。请安装 Python 3.11+ 或运行 "npm rebuild @bencibro/bugx"。');
  } else {
    console.error(`[bugx] ✗ 启动失败: ${res.error.message}`);
  }
  process.exit(1);
}
process.exit(res.status ?? 0);
