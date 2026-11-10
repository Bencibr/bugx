#!/usr/bin/env node
// Sync version from pyproject.toml -> package.json & interfaces/cli/bugx_cli/__init__.py
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkgRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const pyprojectPath = join(pkgRoot, 'pyproject.toml');
const pyprojectContent = readFileSync(pyprojectPath, 'utf8');

const match = pyprojectContent.match(/^version\s*=\s*"([^"]+)"/m);
if (!match) {
  console.error('[bugx] ✗ 未在 pyproject.toml 中找到 version');
  process.exit(1);
}
const version = match[1];

// 1. package.json
const pkgJsonPath = join(pkgRoot, 'package.json');
const pkgJson = JSON.parse(readFileSync(pkgJsonPath, 'utf8'));
if (pkgJson.version !== version) {
  pkgJson.version = version;
  writeFileSync(pkgJsonPath, JSON.stringify(pkgJson, null, 2) + '\n');
  console.log(`[bugx] ✓ 同步 package.json version -> ${version}`);
}

// 2. interfaces/cli/bugx_cli/__init__.py
const initPyPath = join(pkgRoot, 'interfaces', 'cli', 'bugx_cli', '__init__.py');
const initPyContent = readFileSync(initPyPath, 'utf8');
const updatedInitPy = initPyContent.replace(
  /__version__\s*=\s*"[^"]+"/,
  `__version__ = "${version}"`,
);
if (updatedInitPy !== initPyContent) {
  writeFileSync(initPyPath, updatedInitPy);
  console.log(`[bugx] ✓ 同步 interfaces/cli/bugx_cli/__init__.py __version__ -> ${version}`);
}
