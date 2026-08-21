/**
 * Insightly SE MCP — the full server on Cloudflare Workers (stateless).
 *
 * The MCP core needs NO Durable Objects: createMcpHandler builds a fresh server per
 * request (the 2026-07-28 stateless model), and the API key/pod arrive as per-request
 * headers injected by the bridge — nothing is stored server-side. The DO bindings exist
 * for work that must outlive a request (TaskDO), for state that must be shared ACROSS
 * requests (PacerDO — one rate budget per key, however many isolates are live), and for
 * abuse counters (GateDO).
 *
 * This endpoint is on the public internet, so two things gate it:
 *   1. A shared bridge secret. Without it, /mcp is a free anonymizing proxy to
 *      api.insightly.com for anyone who finds the URL — and an oracle for testing stolen
 *      Insightly keys. The secret ships inside the .mcpb (not in the public repo), which
 *      makes it a team credential, not a user one: it stops the internet, not a colleague.
 *      Cloudflare Access in front is the upgrade when the audience grows past the team.
 *   2. Per-IP failure throttling (GateDO), so neither the secret nor a key can be ground
 *      down by repetition.
 * Downloads (/d/...) carry their own signature instead, since a browser has no secret.
 *
 * Headers (set by the bridge; never logged):
 *   X-Bridge-Auth     the shared bridge secret
 *   X-Insightly-Key   the active env's API key
 *   X-Insightly-Pod   na1 / eu1 / ap1
 *   X-Insightly-Env   the display name of the active env (for connected_as)
 */
import { createMcpHandler } from "@modelcontextprotocol/server";
import { buildServer, SERVER_VERSION, WorkerSession } from "./tools";
import { taskCall, TaskDO } from "./tasks";
import { PacerDO } from "./pacer";
import { GateDO, gateCheck, gateFail, tooMany } from "./gate";
import { serveDownload, serveInstall } from "./links";
import { safeEqual } from "./tenant";

export { TaskDO, PacerDO, GateDO };

interface Env {
  TASKS: DurableObjectNamespace;
  PACER?: DurableObjectNamespace;
  GATE?: DurableObjectNamespace;
  META?: KVNamespace;
  EXPORTS?: R2Bucket;
  INSTALL?: R2Bucket;
  BRIDGE_SECRET?: string;
  EXPORT_SIGNING_KEY?: string;
  INSTALL_TOKEN?: string;
}

const OUT_OF_DATE =
  "Bridge out of date or unauthorized. Reinstall the current Insightly SE MCP (Cloudflare) " +
  "bundle — this endpoint requires bridge 0.4.0 or later.";

/**
 * One MCP handler per env object rather than a module-global that every request
 * overwrites. There is exactly one env per isolate, so this is the same object each time;
 * keying on it means the bindings the factory closes over are provably the ones that
 * arrived with the request.
 */
const handlers = new WeakMap<object, { fetch(r: Request, c?: any): Promise<Response> }>();

/**
 * IPs whose Insightly KEY was rejected in THIS isolate. Consulting the gate on every
 * request would put a DO round trip on the happy path; consulting it only for addresses
 * already seen probing keeps the cost on the suspicious traffic. Best-effort by
 * construction (one isolate's view), with the durable count in GateDO. Bad-secret
 * attempts are deliberately NOT tracked here: they never reach Insightly anyway, so
 * counting them against a valid-secret request would only punish the wrong caller.
 */
const suspect = new Map<string, number>();
const SUSPECT_MS = 900_000;

function markSuspect(ip: string): void {
  if (suspect.size > 500) suspect.clear();
  suspect.set(ip, Date.now());
}

function handlerFor(env: Env, ip: string, ctx: ExecutionContext) {
  let h = handlers.get(env as object);
  if (!h) {
    h = createMcpHandler(
      (c: any) => {
        const auth = c?.authInfo;
        const session: WorkerSession = {
          key: auth?.token || null,
          pod: auth?.extra?.pod || "na1",
          envName: auth?.extra?.envName || null,
        };
        // The hook is per-isolate, not per-request: it reports to the gate for whichever
        // IP is currently being served, which is what the counter needs.
        return buildServer(session, c?.era ?? "legacy", env, taskCall, {
          origin: c?.authInfo?.extra?.origin,
          onAuthFail: () => {
            const who = c?.authInfo?.extra?.ip || ip;
            markSuspect(who);
            ctx.waitUntil(gateFail(env, who, "key").then(() => undefined));
          },
        });
      },
      { onerror: (e: Error) => console.error("[mcp]", e.message) },
    );
    handlers.set(env as object, h);
  }
  return h;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";

    if (url.pathname === "/") {
      return new Response(
        `insightly-se-mcp ${SERVER_VERSION}: MCP endpoint at /mcp (Streamable HTTP). ` +
        "Requires the bridge secret. Keys arrive per request and are never stored.\n",
        { headers: { "content-type": "text/plain" } });
    }
    if (url.pathname.startsWith("/d/")) return serveDownload(env, url);
    // The installer itself: token-gated, because the bundle contains the endpoint
    // credential. Deliberately BEFORE the bridge-secret gate — a browser has neither.
    if (url.pathname.startsWith("/install")) return serveInstall(env, url);

    if (!env.BRIDGE_SECRET) {
      return new Response("Worker misconfigured: BRIDGE_SECRET is not set.\n", { status: 503 });
    }
    if (!safeEqual(request.headers.get("X-Bridge-Auth") ?? "", env.BRIDGE_SECRET)) {
      const g = await gateFail(env, ip, "secret");
      if (g.blocked) return tooMany(g.retry_after_s);
      return new Response(JSON.stringify({
        jsonrpc: "2.0", id: null, error: { code: -32001, message: OUT_OF_DATE },
      }), { status: 401, headers: { "content-type": "application/json" } });
    }
    const seen = suspect.get(ip);
    if (seen && Date.now() - seen < SUSPECT_MS) {
      const g = await gateCheck(env, ip, "key");
      if (g.blocked) return tooMany(g.retry_after_s);
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
    return handlerFor(env, ip, ctx).fetch(request, {
      authInfo: {
        token: key, clientId: "insightly-bridge", scopes: [],
        extra: { pod, envName, ip, origin: url.origin },
      } as any,
    });
  },
};
