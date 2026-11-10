#!/usr/bin/env node
/**
 * Isolate README_NPM.md for npm pack / npm publish.
 *
 * - prepack: Backup dev README.md to .README.dev.bak, copy README_NPM.md to README.md.
 * - postpack: Restore dev README.md from .README.dev.bak and delete backup.
 */
import { copyFileSync, existsSync, unlinkSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkgRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const devReadme = join(pkgRoot, 'README.md');
const npmReadme = join(pkgRoot, 'docs', '07_distribution', '01_README_NPM.md');
const backupReadme = join(pkgRoot, '.README.dev.bak');

const action = process.argv[2] || 'prepack';

function fail(message) {
  console.error(`[bugx:npm] ✗ ${message}`);
  process.exit(1);
}

if (action === 'prepack') {
  if (!existsSync(npmReadme)) fail(`npm README 不存在: ${npmReadme}`);
  if (!existsSync(devReadme)) fail(`开发 README 不存在: ${devReadme}`);
  if (existsSync(backupReadme)) fail(`检测到未清理的 README 备份: ${backupReadme}`);
  copyFileSync(devReadme, backupReadme);
  copyFileSync(npmReadme, devReadme);
  console.log('[bugx:npm] ✓ Isolated README_NPM.md -> README.md for packaging');
} else if (action === 'postpack') {
  if (existsSync(backupReadme)) {
    copyFileSync(backupReadme, devReadme);
    unlinkSync(backupReadme);
    console.log('[bugx:npm] ✓ Restored dev README.md after packaging');
  }
} else {
  console.error(`[bugx:npm] Unknown action: ${action}`);
  process.exit(1);
}
