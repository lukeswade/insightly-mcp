/**
 * The complete tool surface, ported from insightly_mcp.py v3.7.0 — same names, same
 * descriptions (they encode usage guidance the model relies on), same result shapes.
 *
 * Environment-key management (connect / set_api_key / use_saved / list_saved /
 * rename_saved / forget_saved / disconnect and the app_* env tools) is executed by the
 * BRIDGE on the user's machine, where the keystore lives — keys never reach this worker's
 * storage. The worker still ADVERTISES those tools (the host and the dashboard widget
 * need them listed) and answers with guidance if one is ever called directly.
 */
import { McpServer, ResourceTemplate } from "@modelcontextprotocol/server";
import { z } from "zod";
import {
  COMMON_OBJECTS, HYDRATE_MAX, Insightly, LINKABLE, NAME_FIELDS, PAGE_MAX, PK, SCAN_CAP,
  applySort, briefStrip, fetchAll, fit, forwardDated, hydrate, mask, newestByField,
  newestRecords, obj, pageEnvelope, pooled, project, projectAll, rankTopBy, recordContains,
  sortNewest,
  sortNewestBasis,
} from "./insightly";
import { WIDGET_HTML } from "./widget";
import { cached } from "./cache";
import { DEFAULT_TTL_MIN, MAX_TTL_MIN, signedUrl } from "./links";
import { makePacer } from "./pacer";
import { listSnapshots, querySnapshot } from "./snapshots";
import { tenantHash } from "./tenant";
import { Metric, WhereClause, accumulate, containsAnywhere, finishGroups, matches, referencedFields } from "./query";

export const SERVER_VERSION = "4.6.0-cf";
const UI_URI = "ui://insightly/env-dashboard.html";
const SUMMARY_OBJECTS = ["Contacts", "Organisations", "Leads", "Opportunities", "Projects",
  "Tasks", "Events", "Notes", "Emails", "Ticket", "Product", "KnowledgeArticle", "Users"];

export interface WorkerSession { key: string | null; pod: string; envName: string | null; }

const T = (payload: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(payload) }] });
const uiMeta = (visibility: string[]) => ({
  "ui/resourceUri": UI_URI, ui: { resourceUri: UI_URI, visibility } });

const NOT_CONNECTED = {
  error: "Not connected — no API key arrived with this request.",
  hint: "Through the bridge: say \"switch to <env name>\" (use_saved) or set an API key in " +
        "the extension settings. The key stays on your machine; it is sent per request and " +
        "never stored by the server.",
};
const BRIDGE_MANAGED = (tool: string) => ({
  error: `${tool} manages environment keys, which live on YOUR machine — the bridge ` +
         "executes this tool locally and it should never reach the server.",
  hint: "If you see this, the bridge version is too old for this server. Re-download the " +
        "bridge .mcpb and reinstall.",
});

function writeHint(res: any, o: string): any {
  if (res && typeof res === "object" && String(res.error ?? "").startsWith("HTTP 4")) {
    res.hint = res.hint ?? `check field names, required fields and option values via describe_object('${o}')`;
  }
  return res;
}

const DESCRIBE_SAMPLE = 5;

/**
 * Field reference for an object, and an explicit statement of what it rests on.
 *
 * Standard fields are not published by any Insightly metadata endpoint, so they can only
 * be read off actual records. This used to infer them from ONE record — which is exactly
 * the failure mode we flag in other people's tools: if the API ever omitted null-valued
 * fields, the list would be silently short. Verified today (2026-08: every record of an
 * object returns an identical key set, nulls included), but verified is not guaranteed, so
 * now it takes the UNION of a sample from both ends of the object, reports how many
 * records that was, and names any field that appeared in some records but not all.
 * Custom fields come from /CustomFields/{object}, which IS authoritative.
 */
async function describeLive(ins: Insightly, o: string): Promise<Record<string, any>> {
  const out: Record<string, any> = { object: o, pk: PK[o] ?? null };
  const [head, cfs] = await Promise.all([
    ins.request("GET", `/${o}`, { params: { top: DESCRIBE_SAMPLE, brief: "false",
                                            count_total: "true" }, wantHeaders: true }),
    ins.request("GET", `/CustomFields/${o}`),
  ]);
  const [body, hdrs] = head as [any, Record<string, string>];
  let sample: any[] = Array.isArray(body) ? body.filter((r) => r && typeof r === "object") : [];
  const total = parseInt(hdrs?.["x-total-count"] ?? "", 10);
  const oldest = sample.length;
  let newest = 0;
  if (!Number.isNaN(total) && total > DESCRIBE_SAMPLE) {
    // The newest end too: a field added by an admin last week can only show up there.
    const tail = await ins.request("GET", `/${o}`, {
      params: { top: DESCRIBE_SAMPLE, skip: Math.max(total - DESCRIBE_SAMPLE, 0), brief: "false" } });
    if (Array.isArray(tail)) {
      const rows = tail.filter((r) => r && typeof r === "object");
      newest = rows.length;
      sample = sample.concat(rows);
    }
  }

  if (body && !Array.isArray(body) && body.error) {
    out.standard_fields_error = body.error;
    out.basis = "standard fields unavailable — the record read failed";
  } else if (!sample.length) {
    out.standard_fields = [];
    out.basis = "no records yet — standard fields cannot be read from data";
    out.note = "create one record, or consult Insightly's API docs, for the standard field list.";
  } else {
    const seen = new Map<string, number>();
    for (const rec of sample) {
      for (const k of Object.keys(rec)) {
        if (k === "CUSTOMFIELDS" || k === "ETag") continue;
        seen.set(k, (seen.get(k) ?? 0) + 1);          // insertion order = API order
      }
    }
    out.standard_fields = [...seen.keys()];
    const partial = [...seen.entries()].filter(([, n]) => n < sample.length).map(([k]) => k);
    out.basis = `union of ${sample.length} records (${oldest} oldest + ${newest} newest` +
      `${Number.isNaN(total) ? "" : ` of ${total}`}) — Insightly publishes no standard-field metadata`;
    out.sampled = sample.length;
    if (!Number.isNaN(total)) out.total_records = total;
    if (partial.length) {
      out.fields_partial = partial;
      out.warning = "these fields were absent from some sampled records, so this object's " +
        "field set varies by record and standard_fields may still be incomplete — " +
        "confirm against a record you care about before relying on it.";
    }
  }

  if (Array.isArray(cfs)) {
    out.custom_fields = cfs.filter((f) => f && typeof f === "object").map((f) => {
      const c: Record<string, any> = { name: f.FIELD_NAME, label: f.FIELD_LABEL,
                                       type: f.FIELD_TYPE, editable: f.EDITABLE };
      const opts = (f.CUSTOM_FIELD_OPTIONS ?? []).map((op: any) => op?.OPTION_VALUE).filter(Boolean);
      if (opts.length) c.options = opts;
      if (f.JOIN_OBJECT) c.links_to = f.JOIN_OBJECT;
      return c;
    });
    out.custom_fields_basis = `/CustomFields/${o} (authoritative)`;
  } else {
    out.custom_fields = [];
    if (cfs && cfs.error) out.custom_fields_error = cfs.error;
  }
  return out;
}

/** Cached describe: field definitions are configuration, so an hour of KV is safe. */
async function describe(ins: Insightly, o: string, env?: any,
                        tenant?: () => Promise<string>, refresh = false):
    Promise<Record<string, any>> {
  if (!env?.META || !tenant) return describeLive(ins, o);
  const { value, hit } = await cached(env, await tenant(), `desc:${o}`, refresh,
                                     () => describeLive(ins, o));
  return hit ? { ...(value as any), cached: true } : (value as any);
}

async function snapshot(ins: Insightly, s: WorkerSession): Promise<Record<string, any>> {
  const counts: Record<string, any> = {};
  const failed: Record<string, string> = {};
  // 13 count probes in parallel — the laptop walks them one by one.
  const results = await pooled(SUMMARY_OBJECTS.map((o) => () =>
    ins.request("GET", `/${o}`, { params: { top: 1, brief: "true", count_total: "true" },
                                  wantHeaders: true })));
  SUMMARY_OBJECTS.forEach((o, i) => {
    const [body, hdrs] = results[i] as [any, Record<string, string>];
    if (body && !Array.isArray(body) && body.error) { failed[o] = body.error; return; }
    const t = parseInt(hdrs?.["x-total-count"] ?? "", 10);
    counts[o] = Number.isNaN(t) ? null : t;
  });
  const out: Record<string, any> = { connected_as: s.envName, pod: s.pod,
                                     version: SERVER_VERSION, counts };
  if (ins.quota.remaining !== null) {
    out.daily_quota = { limit: ins.quota.limit, remaining: ins.quota.remaining };
  }
  if (Object.keys(failed).length) out.failed = failed;
  return out;
}

export interface BuildOpts {
  /** Called when Insightly rejects the key (401) — feeds per-IP failure throttling. */
  onAuthFail?: () => void;
  /** This worker's own origin, for signing download links. */
  origin?: string;
}

export function buildServer(s: WorkerSession, era: string, env: any, taskCall:
    (env: any, sess: { key: string; pod: string }, payload: Record<string, unknown>) => Promise<any>,
    opts: BuildOpts = {}): McpServer {
  const server = new McpServer({ name: "insightly-se-mcp", version: SERVER_VERSION }, {
    // 2026-era cache hints: 5 minutes, not an hour — this server iterates fast, and a
    // long hint is exactly how clients end up reasoning against last week's tool list.
    // field metadata differs per env key, so the resource template hints private below.
    cacheHints: { "tools/list": { ttlMs: 300_000, cacheScope: "public" } },
  } as any);
  // One tenant hash per request, computed at most once, and never the key itself.
  let tenantP: Promise<string> | null = null;
  const tenant = () => (tenantP ??= tenantHash({ key: s.key, pod: s.pod }));
  const ins = new Insightly({ key: s.key, pod: s.pod }, makePacer(env, tenant),
                            { onAuthFail: opts.onAuthFail });
  const ensure = () => (s.key ? null : NOT_CONNECTED);
  const sess = () => ({ key: s.key as string, pod: s.pod });
  /** Custom-object definitions change when an admin changes them: cache for an hour.
   *  The dashboard asks for these on every render. */
  const customObjects = async (): Promise<any> => {
    const live = () => ins.request("GET", "/CustomObjects", { params: { top: 200 } });
    if (!env?.META) return live();
    return (await cached(env, await tenant(), "p:/CustomObjects?top=200", false, live)).value;
  };

  // ------------------------------------------------------------------------ resources
  server.registerResource("Insightly environment dashboard", UI_URI, {
    title: "Insightly environment",
    description: "Record counts across the connected Insightly demo environment.",
    mimeType: "text/html;profile=mcp-app",
    _meta: { ui: { prefersBorder: true } },
  }, async () => ({
    contents: [{ uri: UI_URI, mimeType: "text/html;profile=mcp-app", text: WIDGET_HTML }],
  }));

  server.registerResource("Insightly object fields",
    new ResourceTemplate("insightly://{object}/fields", { list: undefined }), {
      description: "Standard + custom field reference for one Insightly object " +
        "(cacheable; per-connection, since custom fields differ per env).",
      mimeType: "application/json",
      cacheHint: { ttlMs: 300_000, cacheScope: "private" },
    }, async (_uri: URL, vars: any) => {
      const o = obj(String(vars?.object ?? _uri.hostname ?? ""));
      const payload = ensure() ? { error: "not connected — the bridge supplies the key; " +
        "switch to a saved env first, then read this resource again." } : await describe(ins, o);
      return { contents: [{ uri: `insightly://${o}/fields`, mimeType: "application/json",
                            text: JSON.stringify(payload) }] };
    });

  // ---------------------------------------------------------------- dashboard + counts
  server.registerTool("env_dashboard", {
    description: "Interactive dashboard of what's in the connected Insightly environment — " +
      "record counts per object, with the day's remaining API quota. Same data as " +
      "env_summary, rendered inline.",
    inputSchema: z.object({}),
    _meta: uiMeta(["model", "app"]),
  }, async () => {
    const e = ensure(); if (e) return T(e);
    const snap = await snapshot(ins, s);
    // Same graceful degradation as the local server: on a legacy handshake the
    // extensions map is never negotiated, so name the host-version gap instead of
    // returning bare numbers. (Hosts that render key on the tool _meta regardless.)
    snap.ui = era === "modern" ? UI_URI
      : "inline dashboard unavailable: this host negotiated a pre-2026-07-28 revision, " +
        "and UI extensions are only advertised on MCP 2026-07-28 (stateless). These are " +
        "exactly the numbers env_summary returns — nothing is broken.";
    return T(snap);
  });

  server.registerTool("env_summary", {
    description: "One-call overview of the connected environment: real record counts for the " +
      "core objects. The perfect first call after connecting — \"what's in this env?\". For " +
      "the same thing as an interactive dashboard, use env_dashboard.",
    inputSchema: z.object({}),
  }, async () => {
    const e = ensure(); if (e) return T(e);
    return T(await snapshot(ins, s));
  });

  server.registerTool("describe_object", {
    description: "Field reference for an object: standard + custom fields with types, labels " +
      "and valid dropdown options. Use before creating/updating records so values are valid.",
    inputSchema: z.object({ object: z.string(), refresh: z.boolean().optional() }),
  }, async ({ object, refresh }: any) => {
    const e = ensure(); if (e) return T(e);
    return T(await describe(ins, obj(object), env, tenant, !!refresh));
  });

  // ------------------------------------------------------------------------- reads
  server.registerTool("list_records", {
    description: "List records for an object (e.g. 'Contacts'). Returns a paginated envelope: " +
      "{items, returned, skip, top, has_more, next_skip[, total]}.\n" +
      "- brief defaults True (drops bulky fields — far smaller). Pass brief=false for every field.\n" +
      "- Paging: default top=100 (max 500). If has_more, call again with next_skip — OR pass " +
      "fetch_all=true (default 500 records, hard cap 5000, pages fetched in parallel).\n" +
      "- count_total=true adds the real `total` (X-Total-Count).\n" +
      "- updated_after_utc: NOTE the list endpoint ignores this filter; search_records applies it for real.\n" +
      "- order_by like 'DATE_UPDATED_UTC desc' sorts the RETURNED records CLIENT-SIDE.\n" +
      "For the newest records use newest_records — records come back in ascending id order, " +
      "so page 1 is the OLDEST. For finding records prefer search_records / filter_records." +
      "\n`fields`: optional list of field names to return INSTEAD of whole records \u2014 matched top-level or inside CUSTOMFIELDS (flattened), pk always included. Use it whenever you are building a table: big records (Organisations carry 300+ custom fields and a LINKS array) are 100KB+ each, and projection trims them server-side before they ever reach the conversation. A field present with an empty value returns null; a field ABSENT from that record's layout is omitted entirely — Insightly layouts differ per pipeline/record type, and that distinction is often the answer.",
    inputSchema: z.object({
      object: z.string(), top: z.number().int().optional(), skip: z.number().int().optional(),
      brief: z.boolean().optional(), order_by: z.string().optional(),
      updated_after_utc: z.string().optional(), count_total: z.boolean().optional(),
      fetch_all: z.boolean().optional(), max_records: z.number().int().optional(),
      fields: z.array(z.string()).optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const wantFields: string[] | undefined = a.fields?.length ? a.fields : undefined;
    // Projection needs the full record on the wire (custom fields live in CUSTOMFIELDS,
    // which brief strips); the trim happens here instead, and is far bigger.
    const brief = wantFields ? false : (a.brief ?? true);
    if (a.fetch_all) {
      const res = await fetchAll(ins, o, { brief,
        updatedAfterUtc: a.updated_after_utc, maxRecords: a.max_records ?? 500 });
      let items = res.items;
      delete res.items;
      if (a.order_by) items = applySort(items, a.order_by);
      items = projectAll(items, wantFields, o);
      return T(fit(items, res));
    }
    const page = Math.min(Math.max(a.top ?? 100, 1), PAGE_MAX);
    const params: Record<string, unknown> = { top: page, skip: Math.max(a.skip ?? 0, 0),
      brief: String(brief) };
    if (a.updated_after_utc) params.updated_after_utc = a.updated_after_utc;
    if (a.count_total) params.count_total = "true";
    const [body, hdrs] = await ins.request("GET", `/${o}`, { params, wantHeaders: true });
    if (body && !Array.isArray(body) && body.error) {
      if (String(body.error).startsWith("HTTP 4")) {
        body.hint = "check the object name via list_supported_objects; some objects aren't listable via GET.";
      }
      return T(body);
    }
    let items = Array.isArray(body) ? body : [];
    if (brief) briefStrip(items);
    if (a.order_by) items = applySort(items, a.order_by);
    items = projectAll(items, wantFields, o);
    const envl = pageEnvelope(items, Math.max(a.skip ?? 0, 0), page);
    if (a.count_total) {
      const t = parseInt(hdrs?.["x-total-count"] ?? "", 10);
      if (!Number.isNaN(t)) envl.total = t;
    }
    const its = envl.items; delete envl.items;
    return T(fit(its, envl));
  });

  server.registerTool("newest_records", {
    description: "The most recently created or updated records for an object, newest first.\n" +
      "Use this for \"latest\", \"recent\" or \"newest\" questions. list_records cannot answer " +
      "them: the API returns records in ascending id order and has no sort parameter, so its " +
      "first page is the OLDEST records. This walks /{Object}/Search — which does honour a " +
      "date filter — or reads the whole object when it is small enough, then ranks by the " +
      "later of DATE_CREATED_UTC and DATE_UPDATED_UTC. The `basis` field says which." +
      "\n`fields`: optional list of field names to return INSTEAD of whole records \u2014 matched top-level or inside CUSTOMFIELDS (flattened), pk always included. Use it whenever you are building a table: big records (Organisations carry 300+ custom fields and a LINKS array) are 100KB+ each, and projection trims them server-side before they ever reach the conversation. A field present with an empty value returns null; a field ABSENT from that record's layout is omitted entirely — Insightly layouts differ per pipeline/record type, and that distinction is often the answer.",
    inputSchema: z.object({ object: z.string(), top: z.number().int().optional(),
                            fields: z.array(z.string()).optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const want = Math.min(Math.max(a.top ?? 25, 1), SCAN_CAP);
    const [items, total, basis] = await newestRecords(ins, o, want);
    if (items && !Array.isArray(items) && items.error) return T(items);
    const [full, note] = await hydrate(ins, o, items as any[]);
    const rows = projectAll(full, a.fields?.length ? a.fields : undefined, o);
    const out: Record<string, any> = { returned: rows.length,
      detail_level: a.fields?.length ? `projected to ${a.fields.length} fields` : note,
      sorted_by: sortNewestBasis(rows), basis };
    if (total !== null) out.total = total;
    return T(fit(rows, out));
  });

  server.registerTool("newest_by", {
    description: "Latest records by ANY date field — \"the 50 most recently closed opportunities\".\n" +
      "Use this whenever the ranking field is not the created/updated stamp, e.g. " +
      "newest_by('Opportunities', 'ACTUAL_CLOSE_DATE'). It does NOT scan the object: it prices " +
      "a change window with one-record count probes, fetches only that window, ranks inside it, " +
      "and reports the cost. `complete: true` means the answer is provably the true top N.\n" +
      "Only works for fields that cannot postdate a record's last update. Forecast/due/renewal " +
      "dates are rejected — export instead. Returned records are hydrated to full detail." +
      "\n`fields`: optional list of field names to return INSTEAD of whole records \u2014 matched top-level or inside CUSTOMFIELDS (flattened), pk always included. Use it whenever you are building a table: big records (Organisations carry 300+ custom fields and a LINKS array) are 100KB+ each, and projection trims them server-side before they ever reach the conversation. A field present with an empty value returns null; a field ABSENT from that record's layout is omitted entirely — Insightly layouts differ per pipeline/record type, and that distinction is often the answer.",
    inputSchema: z.object({ object: z.string(), date_field: z.string(),
                            top: z.number().int().optional(),
                            fields: z.array(z.string()).optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const field = String(a.date_field ?? "").trim().toUpperCase();
    if (!field) return T({ error: "date_field is required, e.g. 'ACTUAL_CLOSE_DATE'." });
    if (forwardDated(field)) {
      return T({ error: `${field} can hold future dates, so it cannot be bounded by a change ` +
        "window — the ranking would be silently incomplete.",
        hint: "Rank on a field that is written when the event happens (ACTUAL_CLOSE_DATE, " +
          "DATE_CREATED_UTC), or run start_export and rank the exported records." });
    }
    const res = await newestByField(ins, obj(a.object), field,
                                    Math.min(Math.max(a.top ?? 50, 1), HYDRATE_MAX));
    if (a.fields?.length && Array.isArray(res.items)) {
      res.items = projectAll(res.items, [field, ...a.fields], obj(a.object));
      res.detail_level = `projected to ${a.fields.length + 1} fields`;
    }
    return T(res);
  });

  server.registerTool("search_records", {
    description: "EXACT-match search on a single field (the API does not do partial match here), " +
      "e.g. search_records('Contacts', 'EMAIL_ADDRESS', 'jane@example.com'). Works on standard " +
      "AND custom fields (use the custom FIELD_NAME, e.g. 'Intake_Status__c'). For substring " +
      "matching use filter_records. Supports count_total and updated_after_utc." +
      "\n`fields`: optional list of field names to return INSTEAD of whole records \u2014 matched top-level or inside CUSTOMFIELDS (flattened), pk always included. Use it whenever you are building a table: big records (Organisations carry 300+ custom fields and a LINKS array) are 100KB+ each, and projection trims them server-side before they ever reach the conversation. A field present with an empty value returns null; a field ABSENT from that record's layout is omitted entirely — Insightly layouts differ per pipeline/record type, and that distinction is often the answer.",
    inputSchema: z.object({
      object: z.string(), field_name: z.string(), field_value: z.string(),
      top: z.number().int().optional(), skip: z.number().int().optional(),
      count_total: z.boolean().optional(), updated_after_utc: z.string().optional(),
      brief: z.boolean().optional(), fields: z.array(z.string()).optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const page = Math.min(Math.max(a.top ?? 20, 1), PAGE_MAX);
    const wantFields: string[] | undefined = a.fields?.length ? a.fields : undefined;
    const brief = wantFields ? false : (a.brief ?? true);
    const params: Record<string, unknown> = { field_name: a.field_name, field_value: a.field_value,
      top: page, skip: Math.max(a.skip ?? 0, 0), brief: String(brief) };
    if (a.updated_after_utc) params.updated_after_utc = a.updated_after_utc;
    if (a.count_total) params.count_total = "true";
    const [body, hdrs] = await ins.request("GET", `/${obj(a.object)}/Search`,
                                           { params, wantHeaders: true });
    if (body && !Array.isArray(body) && body.error) return T(body);
    let items = Array.isArray(body) ? body : [];
    if (brief) briefStrip(items);
    items = projectAll(items, wantFields, obj(a.object));
    const envl = pageEnvelope(items, Math.max(a.skip ?? 0, 0), page);
    if (a.count_total) {
      const t = parseInt(hdrs?.["x-total-count"] ?? "", 10);
      if (!Number.isNaN(t)) envl.total = t;
    }
    const its = envl.items; delete envl.items;
    return T(fit(its, envl));
  });

  server.registerTool("find_by_email", {
    description: "Convenience: find records by exact email address (e.g. Contacts, Leads). " +
      "Shortcut for search_records on EMAIL_ADDRESS.",
    inputSchema: z.object({ object: z.string(), email: z.string() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const body = await ins.request("GET", `/${obj(a.object)}/Search`,
      { params: { field_name: "EMAIL_ADDRESS", field_value: a.email, top: 20 } });
    if (body && !Array.isArray(body) && body.error) return T(body);
    return T(pageEnvelope(Array.isArray(body) ? body : [], 0, 20));
  });

  server.registerTool("filter_records", {
    description: "CONTAINS filter, done CLIENT-SIDE because Insightly's search is exact-match " +
      "only. Scans up to max_scan records (default 1000, hard cap 5000) and returns those " +
      "matching `contains` (case-insensitive) — in `field_name` if given, otherwise in ANY " +
      "top-level field. If the object holds more than max_scan records the scan covers the " +
      "NEWEST max_scan of them. brief defaults FALSE here on purpose: brief strips DETAILS and " +
      "CUSTOMFIELDS, exactly where a stray mention tends to hide." +
      "\n`fields`: optional list of field names to return INSTEAD of whole records \u2014 matched top-level or inside CUSTOMFIELDS (flattened), pk always included. Use it whenever you are building a table: big records (Organisations carry 300+ custom fields and a LINKS array) are 100KB+ each, and projection trims them server-side before they ever reach the conversation. A field present with an empty value returns null; a field ABSENT from that record's layout is omitted entirely — Insightly layouts differ per pipeline/record type, and that distinction is often the answer.",
    inputSchema: z.object({
      object: z.string(), contains: z.string(), field_name: z.string().optional(),
      brief: z.boolean().optional(), max_scan: z.number().int().optional(),
      fields: z.array(z.string()).optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const res = await fetchAll(ins, obj(a.object), { brief: a.brief ?? false,
      maxRecords: a.max_scan ?? 1000, newestFirst: true });
    if (res.error && !(res.items as any[])?.length) return T(res);
    const needle = String(a.contains ?? "").toLowerCase();
    const hits = projectAll(
      sortNewest((res.items as any[]).filter((r) => recordContains(r, needle, a.field_name)), obj(a.object)),
      a.fields?.length ? a.fields : undefined, obj(a.object));
    return T(fit(hits, { matched: hits.length, scanned: res.total_fetched,
      scanned_from: "newest",
      searched_fields: (a.brief ?? false)
        ? "top-level fields only (brief=true skips DETAILS/CUSTOMFIELDS)" : "every field",
      truncated: res.truncated }));
  });

  server.registerTool("get_record", {
    description: "Fetch one record by id, e.g. get_record('Contacts', 12345). Shows field names." +
      "\n`fields`: optional list of field names to return INSTEAD of whole records \u2014 matched top-level or inside CUSTOMFIELDS (flattened), pk always included. Use it whenever you are building a table: big records (Organisations carry 300+ custom fields and a LINKS array) are 100KB+ each, and projection trims them server-side before they ever reach the conversation. A field present with an empty value returns null; a field ABSENT from that record's layout is omitted entirely — Insightly layouts differ per pipeline/record type, and that distinction is often the answer.",
    inputSchema: z.object({ object: z.string(), record_id: z.number().int(),
                            fields: z.array(z.string()).optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const rec = await ins.request("GET", `/${o}/${a.record_id}`);
    if (a.fields?.length && rec && typeof rec === "object" && !rec.error) {
      return T(project(rec, a.fields, PK[o] ?? null));
    }
    return T(rec);
  });

  server.registerTool("resolve_lookups", {
    description: "Turn a list of record ids into {id: name} — for the ORGANISATION_ID / " +
      "CONTACT_ID style lookup fields that come back as bare numbers. One tool call returns " +
      "just the names instead of dozens of full records. Unknown ids come back under `missing`.\n" +
      "`fields`: also return those fields per id (top-level or custom, flattened) under " +
      "`values` — THE way to read one column off many big records, e.g. account ARR off 35 " +
      "linked Organisations in one call, a few bytes per org instead of 100KB.",
    inputSchema: z.object({ object: z.string(), ids: z.array(z.number().int()),
                            fields: z.array(z.string()).optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const wanted = [...new Set((a.ids ?? []).map((i: any) => Math.trunc(i)))].slice(0, HYDRATE_MAX);
    if (!wanted.length) return T({ error: "ids is required (a list of record ids)." });
    const names: Record<string, any> = {};
    const values: Record<string, any> = {};
    const missing: number[] = [];
    // Parallel: the whole point of this tool is turning N round-trips into one call.
    const recs = await pooled(wanted.map((rid) => () => ins.request("GET", `/${o}/${rid}`)));
    wanted.forEach((rid, i) => {
      const rec = recs[i];
      if (!rec || typeof rec !== "object" || rec.error) { missing.push(rid as number); return; }
      let label = NAME_FIELDS.map((f) => rec[f]).find(Boolean) ?? null;
      if (label === null) {
        label = ["FIRST_NAME", "LAST_NAME"].map((f) => rec[f]).filter(Boolean).join(" ") || null;
      }
      names[String(rid)] = label;
      if (a.fields?.length) values[String(rid)] = project(rec, a.fields);
    });
    const out: Record<string, any> = { object: o, pk: PK[o] ?? null, names,
      resolved: Object.keys(names).length, requested: wanted.length };
    if (a.fields?.length) out.values = values;
    if (missing.length) out.missing = missing;
    if ((a.ids ?? []).length > HYDRATE_MAX) {
      out.note = `Only the first ${HYDRATE_MAX} ids were resolved — each one costs an API ` +
        "call. Call again with the rest if you need them.";
    }
    return T(out);
  });

  // ------------------------------------------------------------------------- writes
  server.registerTool("create_record", {
    description: "Create a record. `fields` = API field names → values, e.g. " +
      "create_record('Contacts', {'FIRST_NAME':'Jane','LAST_NAME':'Doe'}).\n" +
      "FOR TASKS, PREFER create_task. Setting OPPORTUNITY_ID / PROJECT_ID here fills the " +
      "\"Linked Opportunity/Project\" field but does NOT put the task on that record's " +
      "Activity tab — Insightly needs a separate Link too. create_task does both; otherwise " +
      "follow this call with link_records(object='Tasks', record_id=<new task>, " +
      "link_object_name='Opportunity'|'Project', link_object_id=<the record>).",
    inputSchema: z.object({ object: z.string(), fields: z.record(z.string(), z.any()) }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    return T(writeHint(await ins.request("POST", `/${o}`, { body: a.fields }), o));
  });

  server.registerTool("create_records", {
    description: "Batch-create up to 50 records in one call (rate-paced) — ideal for demo " +
      "seeding. `records` is a list of `fields` dicts as in create_record. Continues past " +
      "individual failures. Returns {created, failed, ids, errors}. For bigger batches use " +
      "start_bulk_create.",
    inputSchema: z.object({ object: z.string(), records: z.array(z.record(z.string(), z.any())) }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    if (!Array.isArray(a.records) || !a.records.length) {
      return T({ error: "pass a non-empty list of field dicts." });
    }
    const o = obj(a.object);
    const batch = a.records.slice(0, 50);
    const pk = PK[o];
    const ids: any[] = [];
    const errors: any[] = [];
    const results: any[] = await pooled(batch.map((fields: any) => () =>
      ins.request("POST", `/${o}`, { body: fields })), 4);
    results.forEach((res: any, i: number) => {
      if (res && res.error) errors.push({ index: i, error: res.error });
      else ids.push(pk && res && typeof res === "object" ? res[pk] : null);
    });
    const out: Record<string, any> = { created: ids.length, failed: errors.length, ids, errors };
    if (a.records.length > 50) out.note = "only the first 50 were created — use start_bulk_create for more.";
    return T(out);
  });

  server.registerTool("update_record", {
    description: "Partial update (send only changed fields). PK is filled in for common " +
      "objects. Optimistic concurrency: pass the record's `ETag` as `if_match`, or safe=true " +
      "to fetch the current ETag first. On a stale ETag Insightly answers **400**, not the " +
      "documented 412 (verified live).",
    inputSchema: z.object({
      object: z.string(), record_id: z.number().int(), fields: z.record(z.string(), z.any()),
      if_match: z.string().optional(), safe: z.boolean().optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const body: Record<string, any> = { ...a.fields };
    const pk = PK[o];
    if (pk) body[pk] = body[pk] ?? a.record_id;
    else if (!Object.keys(body).some((k) => k.toUpperCase().endsWith("_ID"))) {
      return T({ error: `unknown primary key for '${o}' — include its *_ID field in \`fields\`.` });
    }
    let tag = a.if_match;
    if (a.safe && !tag) {
      const cur = await ins.request("GET", `/${o}/${a.record_id}`);
      if (cur && cur.error) return T({ error: `couldn't read the current record to get its ETag: ${cur.error}` });
      tag = cur?.ETag;
      if (!tag) return T({ error: "safe=true but this record has no ETag to compare against." });
    }
    return T(writeHint(await ins.request("PUT", `/${o}`, { body,
      headers: tag ? { "If-Match": tag } : undefined }), o));
  });

  server.registerTool("delete_record", {
    description: "PERMANENTLY delete a record. Must pass confirm=true.",
    inputSchema: z.object({ object: z.string(), record_id: z.number().int(),
                            confirm: z.boolean().optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    if (!a.confirm) return T({ error: "destructive — pass confirm=true to actually delete this record." });
    return T(await ins.request("DELETE", `/${obj(a.object)}/${a.record_id}`) ?? { ok: true });
  });

  server.registerTool("create_task", {
    description: "Create follow-up tasks against Opportunities or Projects — and make them " +
      "actually appear on the record's Activity tab.\n" +
      "USE THIS INSTEAD OF create_record FOR TASKS. Setting OPPORTUNITY_ID / PROJECT_ID on a " +
      "Task fills Insightly's \"Linked Opportunity/Project\" field, but that alone does NOT " +
      "put the task on that record's Activity tab — Insightly needs a separate true Link as " +
      "well. This tool always does both, so a task created here is both associated and " +
      "visible where people look for it.\n" +
      "Pass link_ids to create one task per record in a single call — \"a follow-up task on " +
      "every open opportunity\" is one call, not one per deal. Give due_in_days (e.g. 7) or " +
      "an explicit due_date (YYYY-MM-DD).",
    inputSchema: z.object({
      title: z.string(),
      link_object: z.string().optional(),
      link_ids: z.array(z.number().int()).optional(),
      due_in_days: z.number().int().optional(),
      due_date: z.string().optional(),
      details: z.string().optional(),
      responsible_user_id: z.number().int().optional(),
      priority: z.number().int().optional(),
      status: z.string().optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const ids: number[] = (a.link_ids ?? []).slice(0, 100);
    const lo = a.link_object ? obj(a.link_object) : null;
    if (ids.length && !lo) {
      return T({ error: "link_object is required when link_ids is given (Opportunities or Projects)." });
    }
    if (lo && !["Opportunities", "Projects"].includes(lo)) {
      return T({ error: `tasks link to Opportunities or Projects, not '${lo}'.`,
                 hint: "for other objects create the task then call link_records yourself." });
    }
    let due = a.due_date;
    if (!due && a.due_in_days !== undefined) {
      due = new Date(Date.now() + a.due_in_days * 864e5).toISOString().slice(0, 10);
    }
    const idField = lo === "Projects" ? "PROJECT_ID" : "OPPORTUNITY_ID";
    const singular = lo === "Projects" ? "Project" : "Opportunity";
    const baseFields: Record<string, any> = { TITLE: a.title, COMPLETED: false };
    if (due) baseFields.DUE_DATE = due;
    if (a.details) baseFields.DETAILS = a.details;
    if (a.responsible_user_id) baseFields.RESPONSIBLE_USER_ID = a.responsible_user_id;
    if (a.priority !== undefined) baseFields.PRIORITY = a.priority;
    if (a.status) baseFields.STATUS = a.status;

    const targets = ids.length ? ids : [null];
    const created: any[] = [];
    const failed: any[] = [];
    // Four at a time: each target is two dependent writes, and write endpoints deserve
    // more headroom under the per-second cap than reads do.
    await pooled(targets.map((rid) => async () => {
      const fields = { ...baseFields, ...(rid !== null ? { [idField]: rid } : {}) };
      const task = await ins.request("POST", `/${"Tasks"}`, { body: fields });
      if (!task || task.error) {
        failed.push({ link_id: rid, error: task?.error ?? "create failed",
                      body: String(task?.body ?? "").slice(0, 120) });
        return;
      }
      const row: any = { task_id: task.TASK_ID, title: task.TITLE, due_date: task.DUE_DATE };
      if (rid !== null) {
        row[idField] = rid;
        const link = await ins.request("POST", `/Tasks/${task.TASK_ID}/Links`,
          { body: { LINK_OBJECT_NAME: singular, LINK_OBJECT_ID: rid } });
        if (link && link.error) {
          row.linked = false;
          row.link_error = link.error;
          row.warning = "the task was created and associated, but the Activity-tab link " +
                        "failed — call link_records to finish it.";
        } else {
          row.linked = true;
          row.link_id = link?.LINK_ID ?? null;
        }
      }
      created.push(row);
    }), 4);

    const linked = created.filter((r) => r.linked).length;
    return T(fit(created, {
      created: created.length, failed: failed.length, errors: failed.slice(0, 10),
      linked_to: lo, linked_count: linked,
      note: lo ? `each task carries ${idField} AND a true Link, so it shows on the ` +
                 `${singular} Activity tab` : "standalone task (no record to link to)",
    }, "tasks"));
  });

  server.registerTool("add_note", {
    description: "Attach a note to a record (Contacts, Organisations, Opportunities, Projects, Leads).",
    inputSchema: z.object({ parent_object: z.string(), parent_id: z.number().int(),
                            title: z.string(), body: z.string().optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    return T(await ins.request("POST", `/${obj(a.parent_object)}/${a.parent_id}/Notes`,
      { body: { TITLE: a.title, BODY: a.body ?? "" } }));
  });

  // -------------------------------------------------------------------------- links
  server.registerTool("list_links", {
    description: "Show what a record is linked to (linked contacts, organisations, opportunities…).",
    inputSchema: z.object({ object: z.string(), record_id: z.number().int() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    if (!LINKABLE.includes(o)) {
      return T({ error: `'${o}' has no Links endpoint. Linkable objects: ${LINKABLE.join(", ")}.` });
    }
    return T(await ins.request("GET", `/${o}/${a.record_id}/Links`));
  });

  server.registerTool("link_records", {
    description: "Link two records, e.g. put a contact into an organisation: " +
      "link_records('Contacts', 123, 'Organisation', 456). `link_object_name` is the SINGULAR " +
      "object name. Both ids must already exist.",
    inputSchema: z.object({
      object: z.string(), record_id: z.number().int(), link_object_name: z.string(),
      link_object_id: z.number().int(), role: z.string().optional(), details: z.string().optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    if (!LINKABLE.includes(o)) {
      return T({ error: `'${o}' has no Links endpoint. Linkable objects: ${LINKABLE.join(", ")}.` });
    }
    const body: Record<string, any> = { LINK_OBJECT_NAME: a.link_object_name.trim(),
                                        LINK_OBJECT_ID: a.link_object_id };
    if (a.role) body.ROLE = a.role;
    if (a.details) body.DETAILS = a.details;
    const res = await ins.request("POST", `/${o}/${a.record_id}/Links`, { body });
    if (res && typeof res === "object" && String(res.error ?? "").startsWith("HTTP 4")) {
      res.hint = res.hint ?? "check both ids exist and that LINK_OBJECT_NAME is the SINGULAR " +
        "object name (e.g. 'Organisation', not 'Organisations').";
    }
    return T(res);
  });

  server.registerTool("unlink_records", {
    description: "Remove a link (get link_id from list_links). Must pass confirm=true.",
    inputSchema: z.object({ object: z.string(), record_id: z.number().int(),
                            link_id: z.number().int(), confirm: z.boolean().optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    if (!a.confirm) return T({ error: "pass confirm=true to remove this link." });
    const o = obj(a.object);
    if (!LINKABLE.includes(o)) {
      return T({ error: `'${o}' has no Links endpoint. Linkable objects: ${LINKABLE.join(", ")}.` });
    }
    return T(await ins.request("DELETE", `/${o}/${a.record_id}/Links/${a.link_id}`) ?? { ok: true });
  });

  // ---------------------------------------------------------------- background tasks
  server.registerTool("start_export", {
    description: "Export an ENTIRE object in the background — no 5,000-record cap. Returns a " +
      "task_id immediately; poll with task_status(task_id) and read pages with " +
      "task_result(task_id). Use this instead of list_records(fetch_all=true) for big environments.",
    inputSchema: z.object({ object: z.string(), brief: z.boolean().optional(),
      updated_after_utc: z.string().optional(), max_records: z.number().int().optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const r = await taskCall(env, sess(), { op: "start_export", detail: o,
      params: { o, brief: a.brief ?? true, updatedAfterUtc: a.updated_after_utc ?? null,
                cap: Math.min(Math.max(a.max_records ?? 100_000, 1), 250_000) } });
    if (r.error) return T(r);
    return T({ task_id: r.task_id, status: r.status, poll_interval_ms: 500,
      next: `task_status('${r.task_id}') until status=completed, then task_result('${r.task_id}')` });
  });

  server.registerTool("start_bulk_create", {
    description: "Create ANY number of records in the background — no 50-per-call cap. Returns " +
      "a task_id immediately; poll with task_status(task_id). Use create_records for small batches.",
    inputSchema: z.object({ object: z.string(), records: z.array(z.record(z.string(), z.any())) }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    if (!Array.isArray(a.records) || !a.records.length) {
      return T({ error: "pass a non-empty list of field dicts." });
    }
    const o = obj(a.object);
    const r = await taskCall(env, sess(), { op: "start_bulk", detail: `${o} × ${a.records.length}`,
      params: { o }, records: a.records });
    if (r.error) return T(r);
    return T({ task_id: r.task_id, status: r.status, queued: a.records.length,
      poll_interval_ms: 500, next: `task_status('${r.task_id}')` });
  });

  server.registerTool("task_status", {
    description: "Progress of a background task: status (working/completed/failed/cancelled), " +
      "progress, total, and a summary once finished.",
    inputSchema: z.object({ task_id: z.string() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    return T(await taskCall(env, sess(), { op: "status", task_id: a.task_id }));
  });

  server.registerTool("task_result", {
    description: "Read a finished task's records, PAGED (default 100 at a time). Returns " +
      "{items, returned, skip, top, has_more, next_skip, count}.",
    inputSchema: z.object({ task_id: z.string(), top: z.number().int().optional(),
                            skip: z.number().int().optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    return T(await taskCall(env, sess(), { op: "result", task_id: a.task_id,
                                           top: a.top ?? 100, skip: a.skip ?? 0 }));
  });

  server.registerTool("list_tasks", {
    description: "All background tasks for this environment's key, newest first.",
    inputSchema: z.object({}),
  }, async () => {
    const e = ensure(); if (e) return T(e);
    return T(await taskCall(env, sess(), { op: "list" }));
  });

  server.registerTool("cancel_task", {
    description: "Stop a running background task.",
    inputSchema: z.object({ task_id: z.string() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    return T(await taskCall(env, sess(), { op: "cancel", task_id: a.task_id }));
  });

  // -------------------------------------------------------------- session / meta tools
  server.registerTool("connection_info", {
    description: "Show whether this connection is authenticated, which org/pod it points at, " +
      "the server version, and the LIVE remaining daily API quota (read from a one-record probe).",
    inputSchema: z.object({}),
  }, async () => {
    const out: Record<string, any> = { connected: !!s.key, as: s.envName, pod: s.pod,
      read_only: false, version: SERVER_VERSION, served_from: "cloudflare-worker" };
    if (s.key) {
      await ins.request("GET", "/Instance", { params: { top: 1 } });
      if (ins.quota.limit !== null) {
        out.daily_quota = { limit: ins.quota.limit, remaining: ins.quota.remaining,
                            as_of: new Date().toISOString() };
      }
    }
    return T(out);
  });

  server.registerTool("list_supported_objects", {
    description: "Insightly object names this server knows. Names are normalised automatically " +
      "(the API is inconsistent: Ticket/Product/Quotation/Pricebook are singular, the rest " +
      "plural; 'Organizations' US spelling also accepted). Anything else via raw_request.",
    inputSchema: z.object({}),
  }, async () => T({ objects: COMMON_OBJECTS, read_only: false, version: SERVER_VERSION }));

  server.registerTool("raw_request", {
    description: "Escape hatch — any endpoint. `path` is relative to the v3.1 base, e.g. " +
      "'/Opportunities/123/Tasks'.",
    inputSchema: z.object({ method: z.string(), path: z.string(),
      query: z.record(z.string(), z.any()).optional(),
      body: z.record(z.string(), z.any()).optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    return T(await ins.request(a.method, "/" + String(a.path).replace(/^\/+/, ""),
                               { params: a.query, body: a.body }));
  });


  // ------------------------------------------------------- query engine + deliverables
  const whereSchema = z.array(z.object({
    field: z.string().optional(), contains: z.string().optional(),
    equals: z.union([z.string(), z.number(), z.boolean()]).optional(),
    not_empty: z.boolean().optional(),
    gte: z.union([z.string(), z.number()]).optional(),
    lte: z.union([z.string(), z.number()]).optional(),
  })).optional();
  const metricsSchema = z.array(z.object({
    op: z.enum(["count", "sum", "avg", "min", "max"]), field: z.string().optional(),
  })).optional();
  const WHERE_DOC = "`where`: AND-ed conditions [{field?, contains?|equals?|not_empty?|gte?|lte?}] — " +
    "contains without field searches every field incl. custom; gte/lte compare numerically " +
    "when possible, else lexicographically (ISO dates work).";

  server.registerTool("aggregate", {
    description: "Group-by / sum / count / avg / min / max over an object — the aggregation " +
      "Insightly's API doesn't have. \"Total opportunity value by pipeline stage\" is one call " +
      "returning a small table instead of an export plus math in the conversation.\n" +
      "Small objects compute inline; big ones automatically become a background task (poll " +
      "task_status, read the grouped table with task_result). Fields (group_by, metric fields, " +
      "where fields) resolve against standard AND custom fields, flattened. " + WHERE_DOC,
    inputSchema: z.object({
      object: z.string(), group_by: z.string().optional(), metrics: metricsSchema,
      where: whereSchema, updated_after_utc: z.string().optional(),
      max_inline: z.number().int().optional(), max_records: z.number().int().optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const metrics: Metric[] = a.metrics?.length ? a.metrics : [{ op: "count" }];
    const where: WhereClause[] | undefined = a.where?.length ? a.where : undefined;
    const inlineCap = Math.min(Math.max(a.max_inline ?? 1000, 100), 2000);
    const [, hdrs] = await ins.request("GET", `/${o}`, {
      params: { top: 1, brief: "true", count_total: "true" }, wantHeaders: true });
    const total = parseInt(hdrs?.["x-total-count"] ?? "", 10);
    const needFull = referencedFields(a.group_by, metrics, where) === null
      || (referencedFields(a.group_by, metrics, where) ?? []).length > 0;
    if (!Number.isNaN(total) && total <= inlineCap) {
      const res = await fetchAll(ins, o, { brief: !needFull, maxRecords: inlineCap,
                                           updatedAfterUtc: a.updated_after_utc });
      if (res.error) return T(res);
      const state: Record<string, any> = {};
      let matched = 0;
      for (const rec of res.items as any[]) {
        if (!matches(rec, where)) continue;
        matched++;
        accumulate(state, rec, a.group_by, metrics);
      }
      const rows = finishGroups(state, a.group_by, metrics);
      return T(fit(rows, { basis: "inline", scanned: res.total_fetched, matched,
                           groups: rows.length, group_by: a.group_by ?? null }));
    }
    const r = await taskCall(env, sess(), { op: "start_aggregate",
      detail: `${o}${a.group_by ? ` by ${a.group_by}` : ""}`,
      params: { o, brief: false, updatedAfterUtc: a.updated_after_utc ?? null,
                cap: Math.min(Math.max(a.max_records ?? 250_000, 1), 250_000),
                groupBy: a.group_by, metrics, where } });
    if (r.error) return T(r);
    return T({ task_id: r.task_id, status: r.status, total_to_scan: Number.isNaN(total) ? null : total,
      poll_interval_ms: 500,
      next: `big object — aggregating in the background at the API's rate ceiling. ` +
            `task_status('${r.task_id}') until completed, then task_result('${r.task_id}') ` +
            `returns the grouped table.` });
  });

  server.registerTool("top_by", {
    description: "Top N records ranked by ANY field, ascending or descending — the ranking " +
      "Insightly's API cannot do. This is the tool for \"top customers by annual revenue\", " +
      "\"longest-tenured accounts\", \"biggest open deals\", \"oldest unresolved tickets\".\n" +
      "ALWAYS narrow first with filter_field/filter_value when you can: Insightly filters " +
      "those EXACTLY and server-side, including custom fields, which is what makes huge " +
      "objects tractable — 362k organisations become 14k with PayingStatus__c=paying before " +
      "a single record is fetched. Use describe_object to find the field and its valid " +
      "values.\n" +
      "direction: 'desc' (default) for biggest/latest, 'asc' for smallest/earliest — " +
      "longest tenure is an ASCENDING sort on the start date. Ranking reads full records so " +
      "custom fields are visible; pass `fields` to get back only the columns you want. " +
      "Small jobs answer inline; large ones return a task_id to poll (task_status, then " +
      "task_result) instead of stalling. Records with no value in `field` are excluded " +
      "rather than ranked as zero.",
    inputSchema: z.object({
      object: z.string(), field: z.string(),
      direction: z.enum(["asc", "desc"]).optional(),
      filter_field: z.string().optional(), filter_value: z.string().optional(),
      where: whereSchema, top: z.number().int().optional(),
      fields: z.array(z.string()).optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const opts = { field: a.field, direction: a.direction ?? "desc",
                   filterField: a.filter_field, filterValue: a.filter_value,
                   where: a.where, top: a.top ?? 25, fields: a.fields };
    const inline = await rankTopBy(ins, o, { ...opts, budget: 8 });
    if (inline) {
      if (inline.items.length === 0 && inline.scanned > 0) {
        return T({ error: `no records carry a value in ${a.field}.`,
                   scanned: inline.scanned, candidates: inline.candidates,
                   hint: `confirm the field name with describe_object('${o}') — custom ` +
                         "fields end in __c and are case-sensitive." });
      }
      return T(fit(inline.items, {
        object: o, ranked_by: `${a.field} ${opts.direction}`,
        returned: inline.items.length, scanned: inline.scanned,
        candidates: inline.candidates,
        filtered_by: a.filter_field ? `${a.filter_field} = ${a.filter_value}` : null,
        complete: inline.exhausted, basis: "inline" }));
    }
    const r = await taskCall(env, sess(), { op: "start_rank",
      detail: `${o} by ${a.field} ${opts.direction}`, params: { o, opts } });
    if (r.error) return T(r);
    return T({ task_id: r.task_id, status: r.status, poll_interval_ms: 500,
      next: `too many candidates to rank in one turn — running in the background. ` +
            `task_status('${r.task_id}') until completed, then task_result('${r.task_id}') ` +
            `returns the ranked rows.`,
      hint: a.filter_field ? undefined
        : "a filter_field/filter_value would narrow this server-side and often make it inline." });
  });

  server.registerTool("task_query", {
    description: "Query a COMPLETED export like a table — the API can't sort/filter/aggregate, " +
      "but the export snapshot can. Sort by ANY field (order_by), filter (where), group and " +
      "aggregate (group_by/metrics), project (fields) — all server-side over the task's stored " +
      "records; only the answer reaches the conversation. Pattern for big orgs: start_export " +
      "once, then ask this snapshot as many questions as you like. " + WHERE_DOC,
    inputSchema: z.object({
      task_id: z.string(), where: whereSchema, order_by: z.string().optional(),
      group_by: z.string().optional(), metrics: metricsSchema,
      fields: z.array(z.string()).optional(), top: z.number().int().optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const r = await taskCall(env, sess(), { op: "query", task_id: a.task_id, where: a.where,
      order_by: a.order_by, group_by: a.group_by, metrics: a.metrics, fields: a.fields,
      top: a.top });
    // The live task expires after an hour, the snapshot lasts a week — so an id that
    // worked this morning keeps working this evening instead of erroring.
    if (r?.expired && env?.EXPORTS) {
      const snap = await querySnapshot(env, await tenant(), String(a.task_id), a);
      if (snap) return T({ ...snap, note: "answered from the 7-day snapshot — the live task expired." });
    }
    return T(r);
  });

  server.registerTool("export_csv", {
    description: "Turn a completed export task into a downloadable CSV file and return the " +
      "link — the deliverable escapes the chat's size ceiling entirely. Columns are the union " +
      "of standard fields plus flattened custom fields. The link is SIGNED and EXPIRES " +
      "(default 60 minutes, max 24 hours); anyone holding it can download until it expires, " +
      "so treat links from real-data environments accordingly. Files are deleted after 7 days.",
    inputSchema: z.object({ task_id: z.string(), ttl_minutes: z.number().int().optional() }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const secret = env?.EXPORT_SIGNING_KEY;
    if (!secret || !opts.origin) {
      return T({ error: "downloads are not configured on this worker (no signing key).",
                 hint: "task_result still returns the rows page by page." });
    }
    const r = await taskCall(env, sess(), { op: "csv", task_id: a.task_id });
    if (r.error) return T(r);
    const link = await signedUrl(secret, opts.origin, r.key,
                                 a.ttl_minutes ?? DEFAULT_TTL_MIN);
    return T({ url: link.url, expires_at: link.expires_at, ttl_minutes: link.ttl_minutes,
               rows: r.rows, columns: r.columns, bytes: r.bytes,
               note: `signed link, valid for ${link.ttl_minutes} minutes, then dead. ` +
                     `Re-issue with export_csv. Max ttl_minutes ${MAX_TTL_MIN}.` });
  });

  server.registerTool("snapshot_list", {
    description: "Every stored export snapshot for this environment, newest first. A " +
      "completed export is kept for 7 DAYS as a queryable snapshot (the live task itself " +
      "expires after an hour), so yesterday's expensive export can answer today's question " +
      "in a new chat — no re-export. Use snapshot_query with the snapshot_id.",
    inputSchema: z.object({}),
  }, async () => {
    const e = ensure(); if (e) return T(e);
    if (!env?.EXPORTS) return T({ error: "no snapshot store bound to this worker." });
    const snaps = await listSnapshots(env, await tenant());
    return T(fit(snaps, { count: snaps.length,
      note: snaps.length ? undefined : "no snapshots yet — run start_export first." }));
  });

  server.registerTool("snapshot_query", {
    description: "Ask a stored snapshot anything: filter, sort, group, aggregate, project — " +
      "the same query surface as task_query, but against a snapshot that survives for 7 days " +
      "and streams from storage, so repeat questions cost nothing extra. This is how to do " +
      "'top customers by tenure AND by revenue' properly: export once, then ask twice.\n" +
      WHERE_DOC,
    inputSchema: z.object({
      snapshot_id: z.string(), where: whereSchema, order_by: z.string().optional(),
      group_by: z.string().optional(), metrics: metricsSchema,
      fields: z.array(z.string()).optional(), top: z.number().int().optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    if (!env?.EXPORTS) return T({ error: "no snapshot store bound to this worker." });
    const r = await querySnapshot(env, await tenant(), String(a.snapshot_id), a);
    if (!r) {
      return T({ error: `no snapshot '${a.snapshot_id}' — snapshots are kept 7 days.`,
                 hint: "list what exists with snapshot_list, or re-run start_export." });
    }
    return T(r);
  });

  server.registerTool("search_everywhere", {
    description: "One sweep across the WHOLE environment — every core object plus custom " +
      "objects — for a term, case-insensitive, standard and custom fields alike. Returns hits " +
      "grouped by object with compact projected rows. Scans the NEWEST max_scan_per_object " +
      "records of each object (default 300), so on big orgs say so if the term may be old.",
    inputSchema: z.object({
      term: z.string(), objects: z.array(z.string()).optional(),
      max_scan_per_object: z.number().int().optional(),
      fields: z.array(z.string()).optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const needle = String(a.term ?? "").toLowerCase();
    if (!needle) return T({ error: "term is required." });
    const cap = Math.min(Math.max(a.max_scan_per_object ?? 300, 50), 1000);
    let objects: string[];
    if (a.objects?.length) objects = a.objects.map(obj);
    else {
      const defs = await customObjects();
      const custom = (Array.isArray(defs) ? defs : []).map((d: any) => d?.OBJECT_NAME).filter(Boolean);
      objects = [...SUMMARY_OBJECTS.filter((o) => o !== "Users"), ...custom];
    }
    const scanned: Record<string, number> = {};
    const hits: Record<string, any[]> = {};
    const errors: Record<string, string> = {};
    const results = await pooled(objects.map((o) => async () => {
      const res = await fetchAll(ins, o, { brief: false, maxRecords: cap, newestFirst: true });
      return { o, res };
    }), 4);
    for (const { o, res } of results) {
      if (res.error && !(res.items as any[])?.length) { errors[o] = res.error; continue; }
      scanned[o] = res.total_fetched;
      const found = (res.items as any[]).filter((r) => containsAnywhere(r, needle)).slice(0, 20);
      if (found.length) {
        const defaults = [...NAME_FIELDS, "FIRST_NAME", "LAST_NAME", "EMAIL_ADDRESS", "DATE_UPDATED_UTC"];
        hits[o] = projectAll(found, a.fields?.length ? a.fields : defaults, o);
      }
    }
    const out: Record<string, any> = { term: a.term, objects_scanned: Object.keys(scanned).length,
      total_hits: Object.values(hits).reduce((n, v) => n + v.length, 0), hits, scanned,
      note: `each object scanned its NEWEST ${cap} records — raise max_scan_per_object or ` +
            "search a specific object with filter_records for deeper coverage." };
    if (Object.keys(errors).length) out.not_searchable = errors;
    return T(out);
  });

  server.registerTool("join_related", {
    description: "Records + fields from their LINKED records, merged into one table in one " +
      "call — the join Insightly can't do. Example: the 50 newest-closed Opportunities with " +
      "each linked Organisation's account-ARR field: join_related('Opportunities', " +
      "relation_field='ORGANISATION_ID', related_object='Organisations', " +
      "related_fields=['Total_Account_ARR_USD__c'], date_field='ACTUAL_CLOSE_DATE', top=50). " +
      "Left rows come from `ids` if given, else the newest records (by date_field when set).",
    inputSchema: z.object({
      object: z.string(), relation_field: z.string(), related_object: z.string(),
      related_fields: z.array(z.string()), fields: z.array(z.string()).optional(),
      ids: z.array(z.number().int()).optional(), date_field: z.string().optional(),
      top: z.number().int().optional(),
    }),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const ro = obj(a.related_object);
    const top = Math.min(Math.max(a.top ?? 25, 1), HYDRATE_MAX);
    let left: any[];
    if (a.ids?.length) {
      left = (await pooled(a.ids.slice(0, HYDRATE_MAX).map((rid: number) => () =>
        ins.request("GET", `/${o}/${rid}`)))).filter((r: any) => r && !r.error);
    } else if (a.date_field) {
      const res = await newestByField(ins, o, String(a.date_field).toUpperCase(), top);
      if (res.error) return T(res);
      left = res.items ?? [];
    } else {
      const [items] = await newestRecords(ins, o, top);
      if (items && !Array.isArray(items)) return T(items);
      const [full] = await hydrate(ins, o, items as any[]);
      left = full;
    }
    const leftFields = a.fields?.length ? a.fields : NAME_FIELDS;
    const relIds = [...new Set(left.map((r) => r?.[a.relation_field]).filter((v) => v != null))]
      .slice(0, HYDRATE_MAX);
    const relRecs = await pooled(relIds.map((rid) => () => ins.request("GET", `/${ro}/${rid}`)));
    const relById: Record<string, any> = {};
    relIds.forEach((rid, i) => {
      const rec = relRecs[i];
      if (rec && typeof rec === "object" && !rec.error) {
        relById[String(rid)] = project(rec, a.related_fields, PK[ro] ?? null);
      }
    });
    const rows = left.map((r) => ({
      ...project(r, [...leftFields, a.relation_field], PK[o] ?? null),
      related: relById[String(r?.[a.relation_field])] ?? null,
    }));
    const missing = relIds.filter((rid) => !(String(rid) in relById));
    return T(fit(rows, { joined: rows.filter((r) => r.related).length,
      returned: rows.length, relation: `${o}.${a.relation_field} -> ${ro}`,
      ...(missing.length ? { missing_related: missing } : {}) }));
  });

  // Environment-key tools: executed by the bridge on the user's machine. Advertised here
  // (the host needs them in tools/list) but never legitimately executed server-side.
  const bridgeTools: Array<[string, string, object]> = [
    ["connect", "Authenticate / switch Insightly org. (Executed locally by the bridge — keys stay on your machine.)", z.object({})],
    ["set_api_key", "Set the API key without a prompt (non-interactive clients). Executed locally by the bridge.", z.object({ api_key: z.string(), pod: z.string().optional(), save_as: z.string().optional() })],
    ["disconnect", "Clear the in-memory key for this session (does not delete saved keys). Executed locally by the bridge.", z.object({})],
    ["use_saved", "Switch to a saved environment by name (key never enters the chat). Executed locally by the bridge.", z.object({ name: z.string() })],
    ["list_saved", "List locally-saved keys (masked). Executed locally by the bridge.", z.object({})],
    ["rename_saved", "Rename a saved environment (key untouched). Executed locally by the bridge.", z.object({ name: z.string(), new_name: z.string() })],
    ["forget_saved", "Delete a saved key by name. Executed locally by the bridge.", z.object({ name: z.string() })],
  ];
  for (const [name, description, schema] of bridgeTools) {
    server.registerTool(name, { description, inputSchema: schema as any },
      async () => T(BRIDGE_MANAGED(name)));
  }

  // ------------------------------------------------------------------ app-only tools
  server.registerTool("app_records", {
    description: "(dashboard) newest records for one object, for the drill-in panel.",
    inputSchema: z.object({ object: z.string(), top: z.number().int().optional(),
                            order_by: z.string().optional() }),
    _meta: uiMeta(["app"]),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const o = obj(a.object);
    const page = Math.min(Math.max(a.top ?? 25, 1), SCAN_CAP);
    const [items, total, basis] = await newestRecords(ins, o, page);
    if (items && !Array.isArray(items) && items.error) return T(items);
    let rows = items as any[];
    if (a.order_by) rows = applySort(rows, a.order_by);
    const out: Record<string, any> = { returned: rows.length,
      sorted_by: a.order_by ?? sortNewestBasis(rows),
      basis, top: page, skip: 0 };
    if (total !== null) out.total = total;
    out.has_more = total !== null && total > rows.length;
    return T(fit(rows, out));
  });

  server.registerTool("app_fields", {
    description: "(dashboard) field list for one object.",
    inputSchema: z.object({ object: z.string() }),
    _meta: uiMeta(["app"]),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    return T(await describe(ins, obj(a.object)));
  });

  server.registerTool("app_custom_objects", {
    description: "(dashboard) custom object definitions + how many records each holds.",
    inputSchema: z.object({ with_counts: z.boolean().optional() }),
    _meta: uiMeta(["app"]),
  }, async (a: any) => {
    const e = ensure(); if (e) return T(e);
    const defs = await customObjects();
    if (defs && !Array.isArray(defs) && defs.error) return T(defs);
    const rows = (Array.isArray(defs) ? defs : []).filter((d) => d && typeof d === "object");
    const counts = (a.with_counts ?? true)
      ? await pooled(rows.map((d) => async () => {
          if (!d.OBJECT_NAME) return null;
          const [, hdrs] = await ins.request("GET", `/${d.OBJECT_NAME}`, {
            params: { top: 1, brief: "true", count_total: "true" }, wantHeaders: true });
          const t = parseInt(hdrs?.["x-total-count"] ?? "", 10);
          return Number.isNaN(t) ? null : t;
        }))
      : rows.map(() => null);
    return T({ total: rows.length, custom_objects: rows.map((d, i) => ({
      name: d.OBJECT_NAME,
      label: d.PLURAL_LABEL || d.SINGULAR_LABEL || d.OBJECT_NAME,
      singular: d.SINGULAR_LABEL, in_navbar: d.ENABLE_NAVBAR,
      ...(a.with_counts ?? true ? { count: counts[i] } : {}),
    })) });
  });

  const appEnvTools: Array<[string, string, object]> = [
    ["app_envs", "(dashboard) saved environments and which one is active. Executed locally by the bridge.", z.object({})],
    ["app_use_env", "(dashboard) switch to a saved environment by name. Executed locally by the bridge.", z.object({ name: z.string() })],
    ["app_add_env", "(dashboard) verify + save a new environment. Executed locally by the bridge.", z.object({ name: z.string(), api_key: z.string(), pod: z.string().optional() })],
    ["app_rename_env", "(dashboard) rename a saved environment. Executed locally by the bridge.", z.object({ name: z.string(), new_name: z.string() })],
    ["app_remove_env", "(dashboard) remove a saved environment from this machine. Executed locally by the bridge.", z.object({ name: z.string() })],
  ];
  for (const [name, description, schema] of appEnvTools) {
    server.registerTool(name, { description, inputSchema: schema as any, _meta: uiMeta(["app"]) },
      async () => T(BRIDGE_MANAGED(name)));
  }

  return server;
}

export { mask };
