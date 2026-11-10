/*
 * DSH's stock MCP client owns one process-level cwd.  BugX cannot use that
 * contract because DSH Web assigns cwd per session.  This bridge keeps the
 * tool schemas process-wide, but opens one BugX MCP process per session cwd.
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { scrubbedParentEnv } from "@deepseek-ai/dsh-subprocess";
import z from "@deepseek-ai/schemastery";

// Resilient loader supporting @modelcontextprotocol/client, @modelcontextprotocol/sdk, and host bundles
let Client, StdioClientTransport, zod;

try {
  const m = await import("zod");
  zod = m.z || m.default || m;
} catch {
  try {
    const desktopReq = createRequire("/Applications/DSH Desktop.app/Contents/Resources/app.asar.unpacked/package.json");
    zod = desktopReq("zod");
  } catch {
    zod = { record: () => ({ parse: (x) => x }) };
  }
}

try {
  const clientMod = await import("@modelcontextprotocol/client");
  const stdioMod = await import("@modelcontextprotocol/client/stdio");
  Client = clientMod.Client;
  StdioClientTransport = stdioMod.StdioClientTransport;
} catch {
  try {
    const clientMod = await import("@modelcontextprotocol/sdk/client/index.js");
    const stdioMod = await import("@modelcontextprotocol/sdk/client/stdio.js");
    Client = clientMod.Client;
    StdioClientTransport = stdioMod.StdioClientTransport;
  } catch {
    try {
      const desktopReq = createRequire("/Applications/DSH Desktop.app/Contents/Resources/app.asar.unpacked/package.json");
      const clientPkg = desktopReq("@modelcontextprotocol/client");
      const stdioPkg = desktopReq("@modelcontextprotocol/client/stdio");
      Client = clientPkg.Client;
      StdioClientTransport = stdioPkg.StdioClientTransport;
    } catch (e) {
      console.error("[bugx-dsh-mcp-client] Failed to load MCP client library:", e);
    }
  }
}

const MAX_NAME = 64;
const INVALID_NAME = /[^A-Za-z0-9_-]/g;
const HASH_LENGTH = 12;
const RAW_RESULT = zod?.record && zod?.string && zod?.unknown
  ? zod.record(zod.string(), zod.unknown())
  : { parse: (value) => value };

function publicName(server, raw) {
  const joined = `mcp__${server}__${raw}`;
  const normalized = joined.replace(INVALID_NAME, "_");
  if (normalized === joined && normalized.length <= MAX_NAME) return normalized;
  const hash = createHash("sha256").update(`${server}\0${raw}`).digest("hex").slice(0, HASH_LENGTH);
  return `${normalized.slice(0, MAX_NAME - HASH_LENGTH - 1)}_${hash}`;
}

function textFrom(content, name) {
  const text = Array.isArray(content)
    ? content.filter((item) => item && item.type === "text").map((item) => item.text ?? "").join("\n")
    : "";
  return text || `(${name} returned no model-visible content)`;
}

function output(rawName) {
  return {
    schema: {
      type: "object",
      properties: { content: { type: "array", items: {} }, structuredContent: {} },
      required: ["content"],
      additionalProperties: false,
    },
    render(_args, value) {
      return [{ type: "text", text: textFrom(value?.content, rawName) }];
    },
  };
}

function runJson(command, args, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { env: { ...scrubbedParentEnv() }, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`BugX MCP schema command timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new Error(stderr.trim() || `BugX MCP schema command exited with ${code}`));
        return;
      }
      try { resolve(JSON.parse(stdout)); }
      catch (error) { reject(new Error(`BugX MCP schema output is invalid: ${error.message}`)); }
    });
  });
}

function sessionCwd(exec, workspaceRegistry) {
  const cwd = exec?.agent?.session?.header?.cwd;
  if (typeof cwd !== "string" || !cwd.trim() || !existsSync(cwd)) {
    throw new Error("未选中 DSH Web 项目目录。请先在 Web 工作区选择一个项目，再启动 BugX Agent；BugX 不会猜测目录，也不会创建临时审计目录。");
  }
  const sessionId = exec?.agent?.session?.id;
  const selected = typeof sessionId === "string" && workspaceRegistry?.list?.().some(
    (workspace) => workspace.sessionIds?.some((id) => String(id) === sessionId),
  );
  // The compatibility launcher intentionally supplies DSH_CWD for the first
  // session. Direct dsh web sessions must be attached to a real workspace.
  if (!selected && process.env.DSH_CWD !== cwd) {
    throw new Error("未选中 DSH Web 项目目录。请先在 Web 工作区选择一个项目，再启动 BugX Agent；BugX 不会猜测目录，也不会创建临时审计目录。");
  }
  return cwd;
}

export const name = "bugx-session-mcp-client";
export const inject = ["tools"];
export const Config = z.object({
  serverName: z.string().default("bugx-supervisor"),
  command: z.string().default("bugx"),
  args: z.array(String).default(["mcp", "serve"]),
  role: z.string().default("host"),
  schemaCommand: z.string().default("bugx"),
  schemaArgs: z.array(String).default(["mcp", "schema"]),
  toolCallTimeoutMs: z.number().default(60000),
  failOnStartupError: z.boolean().default(false),
});

export async function apply(ctx, config) {
  let schema;
  try {
    schema = await runJson(config.schemaCommand, [...config.schemaArgs, "--role", config.role]);
  } catch (error) {
    if (config.failOnStartupError) throw error;
    ctx.logger.error(`bugx-session-mcp-client: ${String(error)}`);
    return;
  }
  const tools = new Map();
  const connections = new Map();
  const workspaceRegistry = ctx.get?.("workspaceRegistry");

  async function connectionFor(cwd) {
    let connection = connections.get(cwd);
    if (connection) return connection;
    const client = new Client({ name: "bugx-dsh-session-client", version: "0.18.0" }, { capabilities: {} });
    const transport = new StdioClientTransport({
      command: config.command,
      args: [...config.args, "--root", cwd, "--role", config.role],
      env: { ...scrubbedParentEnv() },
      cwd,
    });
    connection = { client, transport };
    connections.set(cwd, connection);
    try {
      await client.connect(transport);
      return connection;
    } catch (error) {
      connections.delete(cwd);
      try { await client.close(); } catch {}
      throw error;
    }
  }

  for (const tool of schema.tools ?? []) {
    const rawName = tool.name;
    const modelName = publicName(schema.server ?? config.serverName, rawName);
    const definition = {
      name: modelName,
      description: tool.description ?? "",
      parameters: tool.inputSchema,
      output: output(rawName),
      timeoutMs: config.toolCallTimeoutMs,
      execute: async (args, exec) => {
        const cwd = sessionCwd(exec, workspaceRegistry);
        const { client } = await connectionFor(cwd);
        let result;
        if (typeof client.callTool === "function") {
          result = await client.callTool(
            { name: rawName, arguments: args && typeof args === "object" ? args : {} },
            { signal: exec.signal, timeout: config.toolCallTimeoutMs }
          );
        } else {
          result = await client.request(
            { method: "tools/call", params: { name: rawName, arguments: args && typeof args === "object" ? args : {} } },
            RAW_RESULT,
            { signal: exec.signal, timeout: config.toolCallTimeoutMs }
          );
        }
        if (result.isError === true) throw new Error(textFrom(result.content, rawName));
        return {
          content: Array.isArray(result.content) ? result.content : [{ type: "text", text: textFrom([], rawName) }],
          ...(result.structuredContent === undefined ? {} : { structuredContent: result.structuredContent }),
        };
      },
    };
    tools.set(modelName, ctx.tools.register(definition));
  }

  ctx.effect(() => async () => {
    for (const dispose of tools.values()) dispose();
    for (const { client } of connections.values()) {
      try { await client.close(); } catch {}
    }
    connections.clear();
  }, "bugx-session-mcp-client");
}
