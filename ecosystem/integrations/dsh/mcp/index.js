/*
 * BugX DSH Plugin — Bundles BugX skills (/bugx, /bugx-dsh) and direct
 * /bugx-cli command for DeepSeek Harness (DSH), and ensures the bundled
 * `bugx-cli` binary (node_modules/.bin/bugx) is on PATH for DSH's official
 * @deepseek-ai/dsh-mcp-client entries declared in cordis.patch.yml.
 */
import { spawn } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { scrubbedParentEnv } from "@deepseek-ai/dsh-subprocess";
import z from "@deepseek-ai/schemastery";

const BUNDLED_SKILL_RANK = 600;

function ensureBugxBinOnPath() {
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    join(here, "node_modules", ".bin"),
    join(here, "..", ".bin"),
    join(here, "..", "..", ".bin"),
  ];
  const current = process.env.PATH || "";
  const sep = process.platform === "win32" ? ";" : ":";
  const parts = current.split(sep).filter(Boolean);
  for (const binDir of candidates) {
    if (existsSync(binDir) && !parts.includes(binDir)) {
      parts.push(binDir);
    }
  }
  process.env.PATH = parts.join(sep);
}

// Ensure bundled `bugx` binary is discoverable on PATH before downstream
// @deepseek-ai/dsh-mcp-client entries in cordis.patch.yml spawn `bugx`.
ensureBugxBinOnPath();

function resolveSkillsDir() {
  const sameDir = fileURLToPath(new URL("./skills/", import.meta.url));
  if (existsSync(sameDir)) return sameDir;
  const parentDir = fileURLToPath(new URL("../skills/", import.meta.url));
  if (existsSync(parentDir)) return parentDir;
  return sameDir;
}

function canonicalPath(p) {
  try {
    return realpathSync(p);
  } catch {
    return p;
  }
}

function runCommand(command, args, { cwd, timeoutMs = 60000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      env: { ...scrubbedParentEnv() },
      ...(cwd ? { cwd } : {}),
      stdio: ["ignore", "pipe", "pipe"],
      shell: false,
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`Command timed out after ${timeoutMs}ms: ${command} ${args.join(" ")}`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, stdout, stderr });
    });
  });
}

function splitShellArgs(raw) {
  const args = [];
  let current = "";
  let quote = null;
  let escaped = false;
  for (const ch of String(raw ?? "").trim()) {
    if (escaped) {
      current += ch;
      escaped = false;
      continue;
    }
    if (ch === "\\" && quote !== "'") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
      continue;
    }
    if (ch === "'" || ch === "\"") {
      quote = ch;
      continue;
    }
    if (/\s/.test(ch)) {
      if (current.length > 0) {
        args.push(current);
        current = "";
      }
      continue;
    }
    current += ch;
  }
  if (current.length > 0) args.push(current);
  return args;
}

function registerBundledSkills(ctx) {
  const skillsDir = resolveSkillsDir();
  const defs = [
    {
      name: "bugx",
      description: "BugX 自主代码审查与智能测试：排查深层 Bug、运行黑盒/回归/安全验证与多 Worker 并发审计。支持 /bugx 直接启动。",
      file: `${skillsDir}bugx/SKILL.md`,
    },
    {
      name: "bugx-dsh",
      description: "BugX 在 DeepSeek Harness (DSH) 下的专属工作流、确认协议与排障手册。",
      file: `${skillsDir}bugx-dsh/SKILL.md`,
    },
  ];
  const candidates = defs
    .filter((d) => existsSync(d.file))
    .map((d) => ({
      name: d.name,
      description: d.description,
      invocation: { modelInvocable: true, userInvocable: true },
      provider: "bugx-dsh-plugin",
      source: "bundled",
      resourceBase: { kind: "directory", path: skillsDir },
      rank: BUNDLED_SKILL_RANK,
      locator: d.file,
    }));

  if (candidates.length === 0 || typeof ctx.inject !== "function") return;

  ctx.inject(["skills"], (skillCtx) => {
    const provider = {
      name: "bugx-dsh-plugin",
      list: () => Promise.resolve(candidates),
      async get(candidate) {
        const match = candidates.find((c) => c.name === candidate?.name) ?? candidates[0];
        return {
          name: match.name,
          description: match.description,
          invocation: match.invocation,
          provider: match.provider,
          source: match.source,
          resourceBase: match.resourceBase,
          content: await readFile(match.locator, "utf8"),
        };
      },
    };
    skillCtx.effect(
      () => skillCtx.skills.registerProvider(() => provider),
      "bugx-dsh-plugin: skills",
    );
  });
}

function registerCliCommand(ctx, config) {
  if (typeof ctx.inject !== "function") return;
  ctx.inject(["commands"], (cmdCtx) => {
    cmdCtx.effect(
      () => cmdCtx.commands.register({
        name: "bugx-cli",
        description: "Run bugx CLI command directly in the current workspace (e.g. /bugx-cli status, /bugx-cli doctor)",
        input: { hint: "<subcommand> [args...]" },
        handler: async (invocation) => {
          ensureBugxBinOnPath();
          const argv = splitShellArgs(invocation.rawInput);
          const args = argv.length > 0 ? argv : ["--help"];
          const rawCwd = invocation.agent?.session?.header?.cwd || process.env.DSH_CWD || process.cwd();
          const cwd = existsSync(rawCwd) ? canonicalPath(rawCwd) : process.cwd();
          try {
            const res = await runCommand(config.command, args, {
              cwd,
              timeoutMs: config.commandTimeoutMs,
            });
            const combined = [res.stdout.trim(), res.stderr.trim()].filter(Boolean).join("\n");
            if (res.code !== 0) {
              return {
                kind: "error",
                text: combined || `bugx exited with code ${res.code}`,
              };
            }
            return {
              kind: "success",
              text: combined || "(no output)",
            };
          } catch (error) {
            if (error?.code === "ENOENT") {
              return {
                kind: "error",
                text: `未找到 '${config.command}' 命令。请确认 bugx-cli 依赖已安装，或执行 npm install -g bugx-cli。`,
              };
            }
            return {
              kind: "error",
              text: `执行 ${config.command} 失败: ${error?.message ?? String(error)}`,
            };
          }
        },
      }),
      "bugx-dsh-plugin: /bugx-cli command",
    );
  });
}

export const name = "bugx-dsh-plugin";
export const inject = [];
export const Config = z.object({
  command: z.string().default("bugx"),
  commandTimeoutMs: z.number().default(60000),
});

export function apply(ctx, config) {
  ensureBugxBinOnPath();
  registerBundledSkills(ctx);
  registerCliCommand(ctx, config);
}
