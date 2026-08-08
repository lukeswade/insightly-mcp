/**
 * Spike 1 (go/no-go): does Claude render our MCP Apps dashboard when it arrives from a
 * CUSTOM REMOTE CONNECTOR instead of a local .mcpb?
 *
 * Serves the REAL ENV_DASHBOARD_HTML (generated verbatim from app_ui.py — regenerate with
 * extract-widget.py) plus fake, clearly-labeled data behind the same tool names the
 * widget calls. No auth, no Insightly, no secrets: synthetic data only, because this is a
 * public endpoint. The wire shapes replicate what the working local Python server emits
 * byte-for-byte (captured 2026-08-08):
 *
 *   tool._meta   = { "ui/resourceUri": U, "ui": { resourceUri: U, visibility: [...] } }
 *   resource     = mimeType "text/html;profile=mcp-app", _meta { ui: { prefersBorder } }
 *   tool result  = content[0].text JSON (no structuredContent on the wire)
 *
 * Spike 2 rides along at GET /egress: unauthenticated latency probes of Insightly's API
 * (401s measure the network path without any key leaving anywhere).
 */
import { McpServer, createMcpHandler } from "@modelcontextprotocol/server";
import { WIDGET_HTML } from "./widget";

const UI_URI = "ui://insightly/env-dashboard.html";
const ui = (visibility: string[]) => ({
  "ui/resourceUri": UI_URI,
  ui: { resourceUri: UI_URI, visibility },
});

// --- synthetic environment data (public endpoint: nothing real) ----------------------
const FAKE = {
  dashboard: {
    connected_as: "spike-demo",
    pod: "na1",
    version: "cf-spike-0.1",
    daily_quota: { limit: 100000, remaining: 99999 },
    counts: {
      Contacts: 81, Organisations: 52, Leads: 55, Opportunities: 120, Projects: 27,
      Tasks: 418, Events: 16, Notes: 275, Emails: 313, Ticket: 12, Product: 9,
      KnowledgeArticle: 5, Users: 7,
    },
    ui: UI_URI,
  },
  envs: {
    active: "spike-demo",
    count: 2,
    envs: [
      { name: "spike-demo", pod: "na1", masked: "…d3m0", active: true },
      { name: "spike-alt", pod: "na1", masked: "…a1t0", active: false },
    ],
  },
  customObjects: {
    custom_objects: [
      { name: "Widget__c", label: "Widgets (Spike)", singular: "Widget", in_navbar: true, count: 7 },
      { name: "Gadget__c", label: "Gadgets (Spike)", singular: "Gadget", in_navbar: true, count: 3 },
    ],
  },
};

function fakeRecords(object: string, top: number) {
  const now = Date.UTC(2026, 7, 8, 12, 0, 0); // fixed stamp: synthetic data, stable output
  const n = Math.min(top, 25);
  const items = Array.from({ length: n }, (_, i) => ({
    [`${object.replace(/s$/, "").toUpperCase()}_ID`]: 900000 + i,
    RECORD_NAME: `Spike ${object} ${i + 1}`,
    DATE_CREATED_UTC: new Date(now - i * 864e5).toISOString().slice(0, 19).replace("T", " "),
    DATE_UPDATED_UTC: new Date(now - i * 432e5).toISOString().slice(0, 19).replace("T", " "),
  }));
  return { items, returned: items.length, total: 500 + n, skip: 0, top,
           sorted_by: "most recently created or updated, newest first", basis: "synthetic" };
}

const text = (payload: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(payload) }] });

function buildServer(): McpServer {
  const server = new McpServer({ name: "insightly-se-mcp-spike", version: "0.1.0" });

  server.registerResource(
    "Insightly environment dashboard",
    UI_URI,
    {
      title: "Insightly environment",
      description: "Record counts across the connected Insightly demo environment.",
      mimeType: "text/html;profile=mcp-app",
      _meta: { ui: { prefersBorder: true } },
    },
    async () => ({
      contents: [{ uri: UI_URI, mimeType: "text/html;profile=mcp-app", text: WIDGET_HTML }],
    }),
  );

  server.registerTool(
    "env_dashboard",
    {
      description:
        "Interactive dashboard of what's in the connected Insightly environment — record " +
        "counts per object. SPIKE BUILD: synthetic data.",
      _meta: ui(["model", "app"]),
    },
    async () => text(FAKE.dashboard),
  );

  server.registerTool(
    "ping",
    { description: "Liveness check for the spike connector." },
    async () => text({ ok: true, server: "insightly-se-mcp-spike", at: "cloudflare-worker" }),
  );

  // App-only tools the widget calls after mounting. visibility ["app"] mirrors the local
  // server; a host that ignores visibility just sees harmless fake-data tools.
  const appTools: Array<[string, string, (args: any) => unknown]> = [
    ["app_records", "(dashboard) newest records for one object, for the drill-in panel.",
      (a) => fakeRecords(String(a?.object ?? "Records"), Number(a?.top ?? 25))],
    ["app_envs", "(dashboard) saved environments and which one is active.", () => FAKE.envs],
    ["app_custom_objects", "(dashboard) custom object definitions + record counts.",
      () => FAKE.customObjects],
    ["app_fields", "(dashboard) field list for one object.",
      (a) => ({ object: a?.object, pk: "SPIKE_ID", fields: [
        { name: "RECORD_NAME", type: "TEXT" }, { name: "DATE_CREATED_UTC", type: "DATE" }],
        custom_fields: [] })],
  ];
  for (const [name, description, impl] of appTools) {
    server.registerTool(name, { description, _meta: ui(["app"]) },
      async (args: unknown) => text(impl(args)));
  }

  return server;
}

const mcp = createMcpHandler(() => buildServer());

// ---- Spike 2: network-path timing to Insightly, no credentials involved -------------
async function egress(url: URL): Promise<Response> {
  const pod = url.searchParams.get("pod") ?? "na1";
  const runs = Math.min(Number(url.searchParams.get("runs") ?? 5), 10);
  const target = `https://api.${pod}.insightly.com/v3.1/Instance`;
  const timings: number[] = [];
  let status = 0;
  for (let i = 0; i < runs; i++) {
    const t0 = Date.now();
    const r = await fetch(target, { headers: { Accept: "application/json" } });
    await r.arrayBuffer(); // drain
    timings.push(Date.now() - t0);
    status = r.status; // expect 401: proves reachability + full TLS/HTTP path, no key
  }
  const sorted = [...timings].sort((a, b) => a - b);
  return Response.json({
    target, status_seen: status, note: "401 expected — path timing without credentials",
    runs, ms: timings, min: sorted[0], median: sorted[Math.floor(sorted.length / 2)],
    colo: (globalThis as any).__colo ?? undefined,
  });
}

export default {
  async fetch(request: Request, env: unknown, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/egress") return egress(url);
    if (url.pathname === "/" ) {
      return new Response(
        "insightly-se-mcp-spike: MCP endpoint at /mcp (Streamable HTTP). Spike build, synthetic data.\n",
        { headers: { "content-type": "text/plain" } });
    }
    return mcp.fetch(request);
  },
};
