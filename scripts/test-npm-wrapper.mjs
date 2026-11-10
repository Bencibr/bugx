#!/usr/bin/env node
// Test suite for bugx npm thin wrapper
import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkgRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const binScript = existsSync(join(pkgRoot, 'interfaces', 'bin', 'bugx.js'))
  ? join(pkgRoot, 'interfaces', 'bin', 'bugx.js')
  : join(pkgRoot, 'bin', 'bugx.js');
const testTmpDir = join(pkgRoot, '.tmp-test-npm-wrapper');

let passed = 0;
let failed = 0;

function assert(name, fn) {
  try {
    fn();
    console.log(`  ✓ ${name}`);
    passed++;
  } catch (err) {
    console.error(`  ✗ ${name}: ${err.message}`);
    failed++;
  }
}

function spawnCommand(command, args, options) {
  const [executable, ...prefixArgs] = command.trim().split(/\s+/);
  return spawnSync(executable, [...prefixArgs, ...args], options);
}

function findUsablePython() {
  const candidates = process.platform === 'win32'
    ? ['py -3.14', 'py -3.13', 'py -3.12', 'py -3.11', 'py -3']
    : ['python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3', 'python'];
  return candidates.find((cmd) => {
    const check = spawnCommand(cmd, ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'], {
      env: testEnv,
      stdio: 'ignore',
    });
    return !check.error && check.status === 0;
  });
}

console.log('[test-npm-wrapper] 开始端到端包装器校验…');

rmSync(testTmpDir, { recursive: true, force: true });
mkdirSync(testTmpDir, { recursive: true });

const testEnv = {
  ...process.env,
  BUGX_DEV_ASSET_ROOT: '1',
  BUGX_GLOBAL_KNOWLEDGE_ROOT: join(testTmpDir, 'global_knowledge'),
};

try {
  // Test 1: Version command
  assert('1. bin/bugx.js --version 返回 0 代码且输出包含版本号', () => {
    const res = spawnSync('node', [binScript, '--version'], { env: testEnv, encoding: 'utf8' });
    if (res.status !== 0) throw new Error(`Exit code ${res.status}: ${res.stderr}`);
    if (!res.stdout.includes('bugx-cli') && !res.stdout.includes('0.')) {
      throw new Error(`Unexpected stdout: ${res.stdout}`);
    }
  });

  // Test 2: Doctor command (JSON format validity)
  assert('2. bin/bugx.js doctor --installation-only --json 输出合法 JSON 结构', () => {
    const res = spawnSync('node', [binScript, 'doctor', '--installation-only', '--json'], { env: testEnv, encoding: 'utf8' });
    // Note: doctor exits with 1 when ready is false (uninitialized project root); stdout still carries full JSON
    const parsed = JSON.parse(res.stdout);
    if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.checks)) {
      throw new Error('Stdout is not a valid Doctor JSON object');
    }
  });

  // Test 3: Init and Install
  assert('3. bin/bugx.js init & install --agent opencode 成功初始化并配置宿主', () => {
    const projDir = join(testTmpDir, 'sample_project');
    mkdirSync(projDir, { recursive: true });
    const initRes = spawnSync('node', [binScript, 'init', '--root', projDir, '--cloud-sync', 'local'], { env: testEnv, encoding: 'utf8' });
    if (initRes.status !== 0) throw new Error(`init failed (${initRes.status}): ${initRes.stderr}`);

    const instRes = spawnSync('node', [binScript, 'install', '--root', projDir, '--agent', 'opencode'], { env: testEnv, encoding: 'utf8' });
    if (instRes.status !== 0) throw new Error(`install failed (${instRes.status}): ${instRes.stderr}`);

    const manifestPath = join(projDir, '.bugx', 'manifest.json');
    if (!existsSync(manifestPath)) throw new Error(`.bugx/manifest.json missing at ${manifestPath}`);
    const hostsPath = join(projDir, '.bugx', 'hosts.json');
    if (!existsSync(hostsPath)) throw new Error(`.bugx/hosts.json missing at ${hostsPath}`);
  });

  // Test 4: Missing Python Error Path
  assert('4. BUGX_PYTHON 指向不存在文件时打印友好错误且退出码为 1', () => {
    const invalidEnv = {
      ...testEnv,
      BUGX_PYTHON: '/nonexistent/path/to/python_cmd_xyz_123',
    };
    const res = spawnSync('node', [binScript, '--version'], { env: invalidEnv, encoding: 'utf8' });
    if (res.status !== 1) throw new Error(`Expected exit status 1, got ${res.status}`);
    if (!res.stderr.includes('未找到可用 Python') && !res.stderr.includes('错误')) {
      throw new Error(`Expected friendly error in stderr, got: ${res.stderr}`);
    }
  });

  // Test 5: The fallback lock must ship in the npm package.
  assert('5. npm 包包含独立 Python 下载锁文件', () => {
    const res = spawnSync('npm', ['pack', '--dry-run', '--ignore-scripts', '--json'], {
      cwd: pkgRoot,
      env: testEnv,
      encoding: 'utf8',
    });
    if (res.status !== 0) throw new Error(`npm pack failed (${res.status}): ${res.stderr}`);
    const files = JSON.parse(res.stdout)[0]?.files || [];
    if (!files.some((file) => file.path === 'scripts/runtime.lock.json')) {
      throw new Error('scripts/runtime.lock.json is missing from the npm package');
    }
  });

  // Test 6: System Python must work even when the optional fallback lock is absent.
  assert('6. 系统 Python 可用时不依赖独立 Python 锁文件', () => {
    const python = findUsablePython();
    if (!python) throw new Error('No usable system Python >=3.11 found for regression test');

    const isolatedRoot = mkdtempSync(join(tmpdir(), 'bugx-ensure-python-'));
    try {
      const isolatedScripts = join(isolatedRoot, 'scripts');
      mkdirSync(isolatedScripts, { recursive: true });
      copyFileSync(join(pkgRoot, 'scripts', 'ensure-python.mjs'), join(isolatedScripts, 'ensure-python.mjs'));
      const res = spawnSync('node', [join(isolatedScripts, 'ensure-python.mjs')], {
        env: { ...testEnv, BUGX_PYTHON: `${python} -E` },
        encoding: 'utf8',
      });
      if (res.status !== 0) throw new Error(`ensure-python failed (${res.status}): ${res.stderr}`);
      if (!res.stdout.includes('使用系统 Python')) {
        throw new Error(`Expected system Python path, got: ${res.stdout}`);
      }
    } finally {
      rmSync(isolatedRoot, { recursive: true, force: true });
    }
  });

  // Test 7: The launcher must also execute commands with interpreter arguments.
  assert('7. 启动器支持带参数的 Python 命令', () => {
    const python = findUsablePython();
    if (!python) throw new Error('No usable system Python >=3.11 found for regression test');
    const res = spawnSync('node', [binScript, '--version'], {
      env: { ...testEnv, BUGX_PYTHON: `${python} -E` },
      encoding: 'utf8',
    });
    if (res.status !== 0) throw new Error(`bin/bugx.js failed (${res.status}): ${res.stderr}`);
    if (!res.stdout.includes('0.')) throw new Error(`Unexpected stdout: ${res.stdout}`);
  });

  // Test 8: Publication metadata and README isolation must match the repository layout.
  assert('8. npm 发布元数据与 README 隔离路径有效且可恢复', () => {
    const packageJson = JSON.parse(readFileSync(join(pkgRoot, 'package.json'), 'utf8'));
    if (packageJson.bin?.bugx !== 'interfaces/bin/bugx.js') {
      throw new Error(`Unexpected npm bin path: ${packageJson.bin?.bugx}`);
    }
    if (!packageJson.files?.includes('docs/07_distribution/01_README_NPM.md')) {
      throw new Error('The npm README source is missing from package.files');
    }
    if (packageJson.publishConfig?.access !== 'public') {
      throw new Error('publishConfig.access must be public for the scoped package');
    }

    const devReadme = join(pkgRoot, 'README.md');
    const npmReadme = join(pkgRoot, 'docs', '07_distribution', '01_README_NPM.md');
    const backupReadme = join(pkgRoot, '.README.dev.bak');
    const devBefore = readFileSync(devReadme);
    const npmContents = readFileSync(npmReadme);
    const prepack = spawnSync('node', [join(pkgRoot, 'scripts', 'isolate-npm-readme.mjs'), 'prepack'], {
      cwd: pkgRoot,
      env: testEnv,
      encoding: 'utf8',
    });
    try {
      if (prepack.status !== 0) throw new Error(`prepack README isolation failed: ${prepack.stderr}`);
      if (!readFileSync(devReadme).equals(npmContents)) {
        throw new Error('prepack did not replace README.md with the npm README');
      }
      if (!existsSync(backupReadme)) throw new Error('prepack did not create the README backup');
    } finally {
      const postpack = spawnSync('node', [join(pkgRoot, 'scripts', 'isolate-npm-readme.mjs'), 'postpack'], {
        cwd: pkgRoot,
        env: testEnv,
        encoding: 'utf8',
      });
      if (postpack.status !== 0) throw new Error(`postpack README restoration failed: ${postpack.stderr}`);
    }
    if (existsSync(backupReadme)) throw new Error('README backup was not cleaned up');
    if (!readFileSync(devReadme).equals(devBefore)) throw new Error('dev README was not restored byte-for-byte');
  });

  // Test 9: The npm package must retain every engine asset required by the
  // integrity manifest, including the catalog and the bundled Web UI shell.
  assert('9. npm 包包含引擎完整性清单与 Web UI 静态资源', () => {
    const res = spawnSync('npm', ['pack', '--dry-run', '--ignore-scripts', '--json'], {
      cwd: pkgRoot,
      env: testEnv,
      encoding: 'utf8',
    });
    if (res.status !== 0) throw new Error(`npm pack failed (${res.status}): ${res.stderr}`);
    const files = new Set((JSON.parse(res.stdout)[0]?.files || []).map((file) => file.path));
    for (const required of [
      'core/CATALOG.json',
      'core/engine-integrity.json',
      'core/webui/static/index.html',
      'core/webui/static/manifest.webmanifest',
    ]) {
      if (!files.has(required)) throw new Error(`${required} is missing from the npm package`);
    }
  });

  // Test 10: A real installed npm copy must bootstrap a fresh project. This
  // deliberately clears the source-development switch so the npm payload
  // exercises the production installation path.
  assert('10. 隔离 npm 安装体可初始化项目并接入 OpenCode', () => {
    const pack = spawnSync('npm', [
      'pack', '--ignore-scripts', '--pack-destination', testTmpDir, '--json',
    ], { cwd: pkgRoot, env: testEnv, encoding: 'utf8' });
    if (pack.status !== 0) throw new Error(`npm pack failed (${pack.status}): ${pack.stderr}`);
    const tarball = join(testTmpDir, JSON.parse(pack.stdout)[0]?.filename || '');
    if (!existsSync(tarball)) throw new Error(`npm tarball missing at ${tarball}`);

    const installPrefix = join(testTmpDir, 'installed-package');
    const project = join(testTmpDir, 'installed-project');
    mkdirSync(project, { recursive: true });
    const { BUGX_DEV_ASSET_ROOT: _ignoredDevSwitch, ...installedEnv } = testEnv;
    installedEnv.HOME = join(testTmpDir, 'installed-home');
    installedEnv.BUGX_HOME = join(installedEnv.HOME, '.bugx');
    const install = spawnSync('npm', [
      'install', '--prefix', installPrefix, tarball, '--no-audit', '--no-fund',
    ], { cwd: pkgRoot, env: installedEnv, encoding: 'utf8' });
    if (install.status !== 0) throw new Error(`npm install failed (${install.status}): ${install.stdout}`);

    const installedBin = join(installPrefix, 'node_modules', '.bin', 'bugx');
    const init = spawnSync(installedBin, [
      'init', '--root', project, '--cloud-sync', 'local', '--commit-policy', 'none',
    ], { cwd: project, env: installedEnv, encoding: 'utf8' });
    if (init.status !== 0) throw new Error(`installed bugx init failed (${init.status}): ${init.stdout}`);

    const agentInstall = spawnSync(installedBin, [
      'install', '--root', project, '--agent', 'opencode',
      '--non-interactive', '--allow-missing', '--json',
    ], { cwd: project, env: installedEnv, encoding: 'utf8' });
    if (agentInstall.status !== 0) {
      throw new Error(`installed bugx agent install failed (${agentInstall.status}): ${agentInstall.stdout}`);
    }
    for (const required of [
      join(project, '.bugx', 'manifest.json'),
      join(project, '.bugx', 'hosts.json'),
    ]) {
      if (!existsSync(required)) throw new Error(`installed package did not create ${required}`);
    }
  });

} finally {
  rmSync(testTmpDir, { recursive: true, force: true });
}

console.log(`\n[test-npm-wrapper] 结果: ${passed} 通过, ${failed} 失败.`);
if (failed > 0) process.exit(1);
