/**
 * Insightly SE MCP — the full server on Cloudflare Workers (stateless).
 *
 * The MCP core needs NO Durable Objects: createMcpHandler builds a fresh server per
 * request (the 2026-07-28 stateless model), and the API key/pod arrive as per-request
 * headers injected by the bridge — nothing is stored server-side. The single DO binding
 * (TaskDO) exists only for background exports/bulk-creates, which must keep running after
 * the request that started them returns.
 *
 * Headers (set by the bridge; never logged):
 *   X-Insightly-Key   the active env's API key
 *   X-Insightly-Pod   na1 / eu1 / ap1
 *   X-Insightly-Env   the display name of the active env (for connected_as)
 */
import { createMcpHandler } from "@modelcontextprotocol/server";
import { buildServer, SERVER_VERSION, WorkerSession } from "./tools";
import { taskCall, TaskDO } from "./tasks";

export { TaskDO };

interface Env { TASKS: DurableObjectNamespace; }

let ENV: Env | null = null;

const mcp = createMcpHandler(
  (ctx: any) => {
    const auth = ctx?.authInfo;
    const session: WorkerSession = {
      key: auth?.token || null,
      pod: auth?.extra?.pod || "na1",
      envName: auth?.extra?.envName || null,
    };
    return buildServer(session, ctx?.era ?? "legacy", ENV, taskCall);
  },
  { onerror: (e: Error) => console.error("[mcp]", e.message) },
);

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    ENV = env;
    const url = new URL(request.url);
    if (url.pathname === "/") {
      return new Response(
        `insightly-se-mcp ${SERVER_VERSION}: MCP endpoint at /mcp (Streamable HTTP). ` +
        "Keys arrive per request from the bridge and are never stored.\n",
        { headers: { "content-type": "text/plain" } });
    }
    const key = request.headers.get("X-Insightly-Key") ?? "";
    const pod = request.headers.get("X-Insightly-Pod") ?? "na1";
    const envName = request.headers.get("X-Insightly-Env") ?? "";

    // The 2026-11-25-era tasks/* spec methods are not in the TS SDK's typed surface, so
    // serve the read-only ones here, mapped onto the same Durable Object registry the
    // task tools use. Body is consumed to peek, so rebuild the request either way.
    if (request.method === "POST" && key) {
      const raw = await request.text();
      let msg: any = null;
      try { msg = JSON.parse(raw); } catch { /* not JSON-RPC — let the SDK answer */ }
      const m = msg?.method;
      if (m === "tasks/get" || m === "tasks/cancel") {
        const tid = msg?.params?.taskId ?? msg?.params?.task_id;
        const r = await taskCall(env, { key, pod },
          { op: m === "tasks/get" ? "status" : "cancel", task_id: tid });
        const result = r.error ? { taskId: tid, status: "failed", error: r.error }
          : { taskId: r.task_id, status: r.status, statusMessage: r.status_message,
              createdAt: r.created_at, lastUpdatedAt: r.last_updated_at,
              pollInterval: r.poll_interval_ms };
        return Response.json({ jsonrpc: "2.0", id: msg.id, result });
      }
      request = new Request(request.url, { method: "POST", headers: request.headers, body: raw });
    }
    return mcp.fetch(request, {
      authInfo: {
        token: key, clientId: "insightly-bridge", scopes: [],
        extra: { pod, envName },
      } as any,
    });
  },
};
