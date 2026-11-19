#!/usr/bin/env node
// Materialize the npm manifest from BUGX_VERSION for a build tree.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkgRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const version = process.env.BUGX_VERSION?.trim();
if (!version || !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version)) {
  console.error('[bugx] ✗ 必须通过 BUGX_VERSION 提供合法 SemVer');
  process.exit(1);
}

// package.json is a checked-in template; this command materializes it in a
// temporary/build tree before npm reads the manifest.
const pkgJsonPath = join(pkgRoot, 'package.json');
const pkgJson = JSON.parse(readFileSync(pkgJsonPath, 'utf8'));
if (pkgJson.version === '${BUGX_VERSION}') {
  console.error('[bugx] ✗ 源码 package.json 是模板；请先在临时构建树运行 scripts/render_version.py 或 scripts/package_dist.py');
  process.exit(1);
}
if (pkgJson.version !== version) {
  pkgJson.version = version;
  writeFileSync(pkgJsonPath, JSON.stringify(pkgJson, null, 2) + '\n');
  console.log(`[bugx] ✓ 同步 package.json version -> ${version}`);
}
