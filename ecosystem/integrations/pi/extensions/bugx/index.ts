import { existsSync, realpathSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";

const root = (() => {
  try { return realpathSync.native(process.cwd()); } catch { return resolve(process.cwd()); }
})();
const homedir = process.env.HOME || process.env.USERPROFILE || "";
const agentDir = resolve(
  process.env.BUGX_GLOBAL_ENGINE_ROOT?.trim() || resolve(homedir, ".bugx", "core"),
);
const launcher = resolve(agentDir, "launch_bugx.py");

function pythonCommand(): string {
  const configured = process.env.BUGX_PYTHON?.trim();
  if (configured) return configured;

  const virtualEnv = process.env.VIRTUAL_ENV?.trim();
  if (virtualEnv) {
    const candidate = resolve(
      virtualEnv,
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    );
    if (existsSync(candidate)) return candidate;
  }

  const homedir = process.env.HOME || process.env.USERPROFILE || "";
  const globalStandalone = process.platform === "win32"
    ? resolve(homedir, ".bugx", "python", "python.exe")
    : resolve(homedir, ".bugx", "python", "bin", "python3");
  if (existsSync(globalStandalone)) return globalStandalone;

  const candidates = process.platform === "win32"
    ? ["py -3.14", "py -3.13", "py -3.12", "py -3.11", "py -3", "python", "python3"]
    : [
        "python3.14", "python3.13", "python3.12", "python3.11",
        "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
        "python3", "python",
      ];
  for (const cand of candidates) {
    if (existsSync(cand)) return cand;
  }
  return process.platform === "win32" ? "python" : "python3";
}

const protectedScripts = new Set([
  "verify_life.py",
  "launch_bugx.py",
  "audit_supervisor.py",
  "module_coverage.py",
  "prep_validate.py",
  "finding_report.py",
  "finding_confirmation.py",
  "coverage_intelligence.py",
  "behavior_graph.py",
  "project_graph.py",
  "lesson_ledger.py",
]);

const allowedLauncherActions = new Set([
  "pre",
  "post",
  "status",
  "polling",
  "heartbeat",
  "pause",
  "resume",
  "abort",
]);

function shortOutput(stdout: string, stderr: string): string {
  const output = (stdout || stderr || "无输出").trim();
  return output.length > 3500 ? `${output.slice(-3500)}\n…（输出已截断）` : output;
}

type PiSelectChoice = { value: string; label: string; description?: string };
type PiAskOptions = { title: string; options: Array<{ label: string; description?: string }>; multiple?: false };
type PiUI = {
  notify(message: string, type?: "info" | "warning" | "error"): void;
  setStatus(key: string, value: string): void;
  select?(opts: PiAskOptions): Promise<{ label: string } | null>;
  ask?(opts: PiAskOptions): Promise<{ label: string } | null>;
};

async function askPiSingleSelect(
  ctx: { ui: PiUI },
  title: string,
  options: PiSelectChoice[],
): Promise<string | null> {
  const ui: PiUI = ctx.ui as PiUI;
  const select = ui.select ?? ui.ask;
  if (typeof select === "function") {
    try {
      const picked = await select.call(ui, {
        title,
        options: options.map(({ label, description }) => ({ label, description })),
        multiple: false,
      });
      if (picked && typeof picked.label === "string") {
        // Pi returns the label. Resolve it only against the exact option object
        return options.find((option) => option.label === picked.label)?.value ?? null;
      }
    } catch {}
  }
  return null;
}

async function runBugx(args: string[], ctx: { ui: PiUI }): Promise<void> {
  if (!existsSync(launcher)) {
    ctx.ui.notify("BugX 全局引擎缺失，请先运行 bugx install --agent pi", "error");
    return;
  }
  const result = await pi.exec(pythonCommand(), [launcher, ...args], {
    cwd: root,
    timeout: 120_000,
  });
  const output = shortOutput(result.stdout, result.stderr);
  // 若核心返回统一交互请求，优先用 Pi 原生单选呈现，而非纯文本 notify。
  const requestLine = output.split("\n").find((l) => l.startsWith("BUGX_INTERACTION_REQUEST "));
  if (requestLine) {
    try {
      const payload = JSON.parse(requestLine.slice(requestLine.indexOf("{")));
      const gate = payload.question_id ?? "选择";
      const choices: PiSelectChoice[] = (payload.options ?? payload.choices ?? []).map((c: any) =>
        typeof c === "string"
          ? { value: c, label: c }
          : { value: String(c.value ?? ""), label: String(c.label ?? c.value ?? ""), description: c.description },
      );
      if (choices.length > 0 && payload.multiple === false) {
        const picked = await askPiSingleSelect(ctx, String(gate), choices);
        if (picked) {
          const retry = retryArgs(args, payload, picked);
          if (retry) {
            ctx.ui.notify(`已选择：${picked}，正在重新检查当前问题`, "info");
            return runBugx(retry, ctx);
          }
          ctx.ui.notify(`已选择：${picked}，但当前请求需要外层控制器处理`, "warning");
          return;
        }
      }
    } catch {}
  }
  if (result.code === 0) {
    ctx.ui.notify(output, "info");
  } else {
    ctx.ui.notify(`BugX 命令失败（退出码 ${result.code}）：\n${output}`, "error");
  }
}

function retryArgs(args: string[], payload: any, value: string): string[] | null {
  const questionId = String(payload.question_id ?? "");
  const next = [...args];

  // 1. coverage_bootstrap
  if (questionId === "coverage_bootstrap") {
    if (value === "bootstrap" || value === "1") {
      if (!next.includes("--bootstrap-coverage")) next.push("--bootstrap-coverage");
      return next;
    }
    return null; // 选了下次再说/停止，不重试
  }

  // 2. aborted_session / session_recovery
  if (questionId === "aborted_session" || questionId === "session_recovery") {
    const recoveryValue = (value === "1" || value === "resume") ? "resume" : "new";
    const flag = "--session-recovery";
    const index = next.indexOf(flag);
    if (index >= 0 && index + 1 < next.length) next[index + 1] = recoveryValue;
    else next.push(flag, recoveryValue);
    return next;
  }

  // 3. strategy_plan
  if (questionId === "strategy_plan") {
    if (value === "approve" || value === "1") {
      const flag = "--strategy-plan";
      const index = next.indexOf(flag);
      if (index >= 0 && index + 1 < next.length) next[index + 1] = "approve";
      else next.push(flag, "approve");
      return next;
    } else if (value === "customize" || value === "2") {
      const flag = "--strategy-plan";
      const index = next.indexOf(flag);
      if (index >= 0 && index + 1 < next.length) next[index + 1] = "customized";
      else next.push(flag, "customized");
      return next;
    }
    return null; // 选了暂存/停止
  }

  // 4. 通用 key-value flags
  const flags: Record<string, string> = {
    review_mode: "--review-mode",
    polling_policy: "--polling",
    fix_mode: "--fix-mode",
    commit_policy: "--commit-policy",
    startup_action: "--startup-action",
    report_policy: "--report-policy",
    startup_interaction: "--startup-interaction",
  };
  const flag = flags[questionId];
  if (!flag) return null;
  const index = next.indexOf(flag);
  if (index >= 0 && index + 1 < next.length) next[index + 1] = value;
  else next.push(flag, value);
  return next;
}

function pathFromInput(value: unknown): string | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  return isAbsolute(value) ? resolve(value) : resolve(root, value);
}

function isProtectedScript(path: string): boolean {
  const relativePath = relative(agentDir, path);
  return !relativePath.startsWith("..") && protectedScripts.has(relativePath);
}

function isAllowedLauncherCommand(command: string): boolean {
  // Pi receives a shell command string.  Substring matching is unsafe because
  // ``python launch_bugx.py pre; sed ...`` used to bypass the protected-file
  // guard.  Only accept a shell-free argv-shaped command whose launcher path
  // is the canonical global-engine path and whose action is allowlisted.
  if (!command.trim() || /[;&|`$()<>{}\n\r]/.test(command)) return false;
  const tokens = command.trim().split(/\s+/).map((item) => item.replace(/^['"]|['"]$/g, ""));
  const launcherIndex = tokens.findIndex((item) => item.endsWith("launch_bugx.py"));
  if (launcherIndex < 1 || launcherIndex + 1 >= tokens.length) return false;
  try {
    if (resolve(tokens[launcherIndex]) !== launcher) return false;
  } catch { return false; }
  const python = tokens[launcherIndex - 1];
  if (!/(^|\/)(python|python3|python3\.[0-9]+|py)(\.exe)?$/.test(python)) return false;
  return allowedLauncherActions.has(tokens[launcherIndex + 1]);
}

function touchesProtectedRuntime(command: string): boolean {
  if (isAllowedLauncherCommand(command)) return false;
  return [...protectedScripts].some((name) => {
    const candidate = resolve(agentDir, name);
    return command.includes(candidate) || command.includes(candidate.replaceAll("\\", "/"));
  });
}

export default function registerBugxExtension(pi: {
  exec(command: string, args: string[], options?: { cwd?: string; timeout?: number }): Promise<{ stdout: string; stderr: string; code: number }>;
  on(event: string, handler: (event: any, ctx: any) => Promise<{ block: boolean; reason: string } | void> | void): void;
  registerCommand(name: string, options: { description: string; handler: (args: string, ctx: any) => Promise<void> }): void;
  ui?: PiUI;
}) {
  pi.registerCommand("bugx-status", {
    description: "显示 BugX 当前生命周期、轮询和 Supervisor 状态",
    handler: async (_args, ctx) => runBugx(["status", "--json"], ctx),
  });

  pi.registerCommand("bugx-auto", {
    description: "设置 Round 报告后的自动续轮",
    handler: async (_args, ctx) => runBugx(["polling", "auto"], ctx),
  });

  pi.registerCommand("bugx-manual", {
    description: "设置 Round 报告后的手动停止",
    handler: async (_args, ctx) => runBugx(["polling", "manual"], ctx),
  });

  pi.registerCommand("bugx-pause", {
    description: "暂停当前 BugX 运行",
    handler: async (_args, ctx) => runBugx(["pause"], ctx),
  });

  pi.registerCommand("bugx-resume", {
    description: "恢复当前 BugX 运行",
    handler: async (_args, ctx) => runBugx(["resume"], ctx),
  });

  pi.registerCommand("bugx-stop", {
    description: "停止当前 BugX 运行",
    handler: async (args, ctx) => runBugx(["abort", "--reason", args.trim() || "Pi 用户请求"], ctx),
  });

  pi.on("session_start", async (_event, ctx) => {
    if (existsSync(launcher)) {
      ctx.ui.setStatus("bugx", "BugX ready");
    } else {
      ctx.ui.setStatus("bugx", "BugX incomplete");
    }
  });

  pi.on("tool_call", async (event) => {
    if (event.toolName === "write" || event.toolName === "edit") {
      const path = pathFromInput(event.input?.path);
      if (path && isProtectedScript(path)) {
        return { block: true, reason: "BugX 协议脚本受保护，必须通过共享运行时调用，不能直接修改。" };
      }
    }
    if (event.toolName === "bash" && typeof event.input?.command === "string"
        && touchesProtectedRuntime(event.input.command)) {
      return { block: true, reason: "BugX 受保护运行时不能直接修改；请使用 BugX 协议命令。" };
    }
  });
}
