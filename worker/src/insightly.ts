/**
 * Insightly v3.1 client + the workaround algorithms, ported line-for-line in behavior
 * from insightly_mcp.py (v3.7.0) — the 88-check suite is the contract.
 *
 * Differences from the Python server are performance-only:
 *   - _hydrate, resolve_lookups, env_summary counts, and fetch_all pages run in PARALLEL
 *     (up to 6 concurrent — the Workers per-invocation connection cap), paced under
 *     Insightly's 10 req/s per key. The laptop server did all of these sequentially.
 *   - No process state: the key/pod arrive per request; quota is read live.
 */

import { getField } from "./query";
import { Pacer } from "./pacer";

// ------------------------------------------------------------------ constants (ported)
export const PAGE_MAX = 500;
export const FETCH_ALL_HARD_CAP = 5000;
export const SCAN_CAP = 500;
export const WINDOW_BOUND = 3000;
export const HYDRATE_MAX = 100;
export const RESULT_BUDGET = 900_000;
export const RECENT_WINDOWS = [1, 7, 30, 90, 365, 1825];
const CONCURRENCY = 6;                 // Workers cap simultaneous outbound connections
const RATE_PER_SEC = 9;                // rolling-window budget < Insightly's 10 req/s per key
const RESERVE_BATCH = 6;               // slots bought per PacerDO round trip

export const PK: Record<string, string> = {
  Contacts: "CONTACT_ID", Organisations: "ORGANISATION_ID", Leads: "LEAD_ID",
  Opportunities: "OPPORTUNITY_ID", Projects: "PROJECT_ID", Tasks: "TASK_ID",
  Events: "EVENT_ID", Notes: "NOTE_ID", Product: "PRODUCT_ID",
  Emails: "EMAIL_ID", Quotation: "QUOTE_ID", Milestones: "MILESTONE_ID",
  Pricebook: "PRICEBOOK_ID", Ticket: "TICKET_ID", KnowledgeArticle: "ARTICLE_ID",
};

export const COMMON_OBJECTS: string[] = [...Object.keys(PK).sort(), ...[
  "Pipelines", "PipelineStages", "Relationships", "Tags", "Teams",
  "LeadSources", "LeadStatuses", "Currencies", "CustomObjects",
  "TeamMembers", "Users", "ActivitySets",
  "Instance", "Countries", "Permissions", "Prospect", "DocumentTemplates",
  "OpportunityCategories", "OpportunityStateReasons", "OpportunityLineItem",
  "QuotationLineItem", "PricebookEntry", "ProjectCategories", "TaskCategories",
  "FileCategories", "KnowledgeArticleCategory", "KnowledgeArticleFolder",
  "MarketingVisits", "Follows",
]];

export const LINKABLE = ["Contacts", "Organisations", "Opportunities", "Projects",
                         "Tasks", "Events", "Notes", "Emails"];

const ALIASES: Record<string, string> = {};
for (const c of COMMON_OBJECTS) ALIASES[c.toLowerCase()] = c;
for (const c of COMMON_OBJECTS) {
  if (c.endsWith("s")) { const k = c.slice(0, -1).toLowerCase(); if (!(k in ALIASES)) ALIASES[k] = c; }
  else { const k = c.toLowerCase() + "s"; if (!(k in ALIASES)) ALIASES[k] = c; }
}
ALIASES["organizations"] = ALIASES["organization"] = "Organisations";
ALIASES["quote"] = ALIASES["quotes"] = "Quotation";              // docs: "Quote" is rejected
ALIASES["knowledgearticles"] = ALIASES["knowledge"] = "KnowledgeArticle";

export function obj(name: string): string {
  const n = String(name ?? "").trim().replace(/^\/+|\/+$/g, "");
  return ALIASES[n.toLowerCase()] ?? n;
}

const BRIEF_DROP = new Set(["body", "details", "customfields", "image_url", "etag"]);

export const NAME_FIELDS = ["ORGANISATION_NAME", "OPPORTUNITY_NAME", "PROJECT_NAME",
  "QUOTATION_NAME", "PRODUCT_NAME", "RECORD_NAME", "TASK_NAME", "MILESTONE_NAME",
  "TICKET_TITLE", "SUBJECT", "TITLE", "NAME"];

const FORWARD_DATED = ["FORECAST", "DUE", "TARGET", "START", "END", "EXPIR", "RENEW", "NEXT"];

// ------------------------------------------------------------------------ request layer
export interface Session { key: string | null; pod: string; }

/** Out-of-band notifications for the request layer (abuse counting, telemetry). */
export interface Hooks { onAuthFail?: () => void; }
export interface Quota { limit: number | null; remaining: number | null; }

export class NoKeyError extends Error {}

/** Per-request Insightly client: paced, 429-aware, quota-observing. */
export class Insightly {
  quota: Quota = { limit: null, remaining: null };
  private starts: number[] = [];
  private inFlight = 0;
  private waiters: Array<() => void> = [];
  /** Slots already bought from the global pacer and not yet spent. */
  private credits = 0;

  constructor(public session: Session, private pacer?: Pacer, private hooks?: Hooks) {}

  private base(): string {
    return `https://api.${this.session.pod || "na1"}.insightly.com/v3.1`;
  }

  private auth(): string {
    if (!this.session.key) throw new NoKeyError("Not connected — no API key on this request.");
    return "Basic " + btoa(`${this.session.key}:`);
  }

  /** Pacing inside one invocation: <=6 concurrent, <=9 starts per rolling second.
   * A rolling window (not a fixed inter-request gap) is what lets a burst of parallel
   * probes actually run in parallel while sustained loads stay under Insightly's
   * 10 req/s per key. */
  private async slot(): Promise<void> {
    while (this.inFlight >= CONCURRENCY) {
      await new Promise<void>((res) => this.waiters.push(res));
    }
    this.inFlight++;
    await this.globalSlot();
    for (;;) {
      const now = Date.now();
      this.starts = this.starts.filter((t) => now - t < 1000);
      if (this.starts.length < RATE_PER_SEC) { this.starts.push(now); return; }
      const wait = 1000 - (now - this.starts[0]) + 5;
      await new Promise((r) => setTimeout(r, wait));
    }
  }

  /**
   * Reserve capacity from the ONE bucket for this key (PacerDO), not just from this
   * isolate. Slots are bought RESERVE_BATCH at a time so the round trip is amortised over
   * a whole wave of pages rather than paid per request. If the pacer is absent or errors,
   * this is a no-op and local pacing stands — a pacing outage must never fail an answer.
   */
  private async globalSlot(): Promise<void> {
    if (!this.pacer) return;
    if (this.credits > 0) { this.credits--; return; }
    const wait = await this.pacer.reserve(RESERVE_BATCH);
    this.credits = RESERVE_BATCH - 1;
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
  }

  private release(): void {
    this.inFlight--;
    const w = this.waiters.shift();
    if (w) w();
  }

  async request(method: string, path: string, opts: {
    params?: Record<string, unknown>; body?: unknown; headers?: Record<string, string>;
    wantHeaders?: boolean;
  } = {}): Promise<any> {
    const url = new URL(this.base() + path);
    for (const [k, v] of Object.entries(opts.params ?? {})) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
    for (let attempt = 1; attempt <= 3; attempt++) {
      await this.slot();
      let r: Response;
      try {
        r = await fetch(url.toString(), {
          method: method.toUpperCase(),
          headers: {
            Authorization: this.auth(),
            Accept: "application/json",
            ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
            ...(opts.headers ?? {}),
          },
          body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        });
      } finally {
        this.release();
      }
      // 401 means the key itself was rejected. Reported so repeated rejections from one
      // address can be throttled; 403 (valid key, no permission) is NOT an auth failure.
      if (r.status === 401) this.hooks?.onAuthFail?.();
      const lim = r.headers.get("X-RateLimit-Limit"), rem = r.headers.get("X-RateLimit-Remaining");
      if (lim !== null) this.quota.limit = parseInt(lim, 10);
      if (rem !== null) this.quota.remaining = parseInt(rem, 10);

      if (r.status === 429) {
        // remaining==0 means the DAILY quota — never retry that one.
        if (this.quota.remaining === 0) {
          return { error: "HTTP 429", body: "Daily API quota exhausted for this key.",
                   hint: "The quota resets at midnight UTC. Check connection_info for the limit." };
        }
        if (attempt < 3) { await new Promise((res) => setTimeout(res, 400 * attempt)); continue; }
      }
      const text = await r.text();
      const lower: Record<string, string> = {};
      r.headers.forEach((v, k) => { lower[k.toLowerCase()] = v; });
      if (!r.ok) {
        const body = { error: `HTTP ${r.status}`, body: text.slice(0, 300) } as any;
        if (r.status === 400 && text.includes("If-Match")) {
          body.hint = "the record changed since you read it — re-fetch and retry with the fresh ETag";
        }
        return opts.wantHeaders ? [body, lower] : body;
      }
      let parsed: any = null;
      if (text.trim()) { try { parsed = JSON.parse(text); } catch { parsed = { raw: text.slice(0, 500) }; } }
      return opts.wantHeaders ? [parsed, lower] : parsed;
    }
    return { error: "HTTP 429", body: "rate-limited after retries" };
  }
}

/** Run thunks with bounded concurrency, preserving order of results. */
export async function pooled<T>(thunks: Array<() => Promise<T>>, width = CONCURRENCY): Promise<T[]> {
  const out: T[] = new Array(thunks.length);
  let next = 0;
  const lanes = Array.from({ length: Math.min(width, thunks.length) }, async () => {
    while (next < thunks.length) {
      const i = next++;
      out[i] = await thunks[i]();
    }
  });
  await Promise.all(lanes);
  return out;
}

// ---------------------------------------------------------------------- ported helpers
export function briefStrip(data: any): any {
  if (Array.isArray(data)) {
    for (const item of data) {
      if (item && typeof item === "object") {
        for (const k of Object.keys(item)) if (BRIEF_DROP.has(k.toLowerCase())) delete item[k];
      }
    }
  }
  return data;
}

export function recency(rec: any): string {
  if (!rec || typeof rec !== "object") return "";
  // Resolve through the same flattening as projection: a custom object can carry its
  // change stamps inside CUSTOMFIELDS rather than at the top level.
  let best = "";
  for (const f of ["DATE_UPDATED_UTC", "DATE_CREATED_UTC"]) {
    const v = getField(rec, f);
    if (v !== undefined && v !== null && v !== "") {
      const str = String(v);
      if (str > best) best = str;
    }
  }
  return best;
}

/** Numeric primary key, for ordering when dates cannot decide it. */
export function recordId(rec: any, o?: string): number {
  if (!rec || typeof rec !== "object") return -1;
  const pk = o ? PK[o] : undefined;
  const raw = pk !== undefined && rec[pk] !== undefined
    ? rec[pk]
    : rec[Object.keys(rec).find((k) => k.toUpperCase().endsWith("_ID")) ?? ""];
  const n = typeof raw === "number" ? raw : Number(String(raw));
  return Number.isNaN(n) ? -1 : n;
}

/**
 * Newest first — and never silently a no-op.
 *
 * The old version compared only recency, so records with no usable date (or with equal
 * dates) kept the API's ascending-id order: the OLDEST first, under a label promising the
 * newest. Insightly ids increase with creation, so the id is a sound decider both as a
 * tie-break and as a whole-list fallback. `sortNewestBasis` reports which was used so
 * callers can say so out loud instead of guessing.
 */
export function sortNewest(items: any[], o?: string): any[] {
  return [...items].sort((a, b) => {
    const ra = recency(a), rb = recency(b);
    if (ra !== rb) return ra < rb ? 1 : -1;
    return recordId(b, o) - recordId(a, o);
  });
}

export function sortNewestBasis(items: any[]): string {
  if (!items.length) return "most recently created or updated, newest first";
  const dated = items.filter((r) => recency(r) !== "").length;
  if (dated === 0) {
    return "id descending — these records carry no created/updated date, so newest is " +
           "inferred from the record id";
  }
  if (dated < items.length) {
    return `most recently created or updated, newest first (${items.length - dated} of ` +
           `${items.length} records carry no date and sort last by id)`;
  }
  return "most recently created or updated, newest first";
}

export function applySort(items: any, orderBy?: string | null): any {
  if (!orderBy || !Array.isArray(items)) return items;
  const parts = String(orderBy).split(/\s+/);
  const field = parts[0];
  const desc = parts.length > 1 && parts[1].toLowerCase().startsWith("desc");
  const present = items.filter((r) => r && typeof r === "object" && r[field] !== undefined && r[field] !== null);
  const missing = items.filter((r) => !(r && typeof r === "object" && r[field] !== undefined && r[field] !== null));
  present.sort((a, b) => {
    const x = a[field], y = b[field];
    const cmp = typeof x === "number" && typeof y === "number" ? x - y
      : String(x) < String(y) ? -1 : String(x) > String(y) ? 1 : 0;
    return desc ? -cmp : cmp;
  });
  return [...present, ...missing];
}

export function fit(items: any[], envelope: Record<string, any>, key = "items"): Record<string, any> {
  envelope[key] = items;
  if (JSON.stringify(envelope).length <= RESULT_BUDGET) return envelope;
  let lo = 0, hi = items.length;
  while (lo < hi) {
    const mid = Math.floor((lo + hi + 1) / 2);
    envelope[key] = items.slice(0, mid);
    if (JSON.stringify(envelope).length <= RESULT_BUDGET) lo = mid; else hi = mid - 1;
  }
  envelope[key] = items.slice(0, lo);
  envelope.capped = true;
  envelope.capped_note =
    `Result trimmed to ${lo} of ${items.length} records to stay under the host's 1MB ` +
    "limit. Ask for a smaller top, add brief=true to drop bulky fields, narrow the " +
    "object/date range, or use start_export for the whole object (it pages through " +
    "task_result without a size ceiling).";
  return envelope;
}

export function pageEnvelope(items: any[], skip: number, top: number): Record<string, any> {
  const returned = Array.isArray(items) ? items.length : 0;
  return { items, returned, skip, top, has_more: returned === top, next_skip: skip + returned };
}

export function recordContains(rec: any, needle: string, field?: string | null): boolean {
  if (!rec || typeof rec !== "object") return false;
  if (field) return String(rec[field] ?? "").toLowerCase().includes(needle);
  for (const v of Object.values(rec)) {
    if (v !== null && typeof v !== "object" && String(v).toLowerCase().includes(needle)) return true;
  }
  return false;
}

// ------------------------------------------------------------------ ported algorithms
export async function fetchAll(ins: Insightly, o: string, opts: {
  brief?: boolean; updatedAfterUtc?: string | null; maxRecords?: number; newestFirst?: boolean;
} = {}): Promise<Record<string, any>> {
  const brief = opts.brief ?? true;
  const cap = Math.min(Math.max(Math.trunc(opts.maxRecords ?? 500), 1), FETCH_ALL_HARD_CAP);
  const extra = opts.updatedAfterUtc ? { updated_after_utc: opts.updatedAfterUtc } : {};

  // First page also learns the real total, which is what lets the REMAINING pages go out
  // in parallel — their skip offsets are then known, and pages are independent reads.
  const start0 = opts.newestFirst ? await (async () => {
    const [, hdrs] = await ins.request("GET", `/${o}`, {
      params: { top: 1, brief: "true", count_total: "true" }, wantHeaders: true });
    const t = parseInt(hdrs?.["x-total-count"] ?? "", 10);
    return Number.isNaN(t) ? 0 : Math.max(0, t - cap);
  })() : 0;

  const [first, hdrs] = await ins.request("GET", `/${o}`, {
    params: { top: Math.min(PAGE_MAX, cap), skip: start0, brief: String(brief),
              count_total: "true", ...extra }, wantHeaders: true });
  if (first && !Array.isArray(first) && first.error) {
    return { items: [], total_fetched: 0, truncated: true, partial: true, error: first.error };
  }
  const out: any[] = Array.isArray(first) ? first : [];
  const total = parseInt(hdrs?.["x-total-count"] ?? "", 10);
  const totalKnown = !Number.isNaN(total);

  if (out.length === Math.min(PAGE_MAX, cap) && out.length < cap) {
    if (totalKnown) {
      const wantMore = Math.min(cap, total - start0) - out.length;
      const offsets: number[] = [];
      for (let got = 0; got < wantMore; got += PAGE_MAX) offsets.push(start0 + out.length + got);
      const pages = await pooled(offsets.map((off) => () => ins.request("GET", `/${o}`, {
        params: { top: PAGE_MAX, skip: off, brief: String(brief), ...extra } })));
      for (const page of pages) {
        if (page && !Array.isArray(page) && page.error) {
          return { items: brief ? briefStrip(out) : out, total_fetched: out.length,
                   truncated: true, partial: true, error: page.error };
        }
        out.push(...(Array.isArray(page) ? page : []));
      }
    } else {
      // No total header — degrade to the sequential walk the laptop server does.
      while (out.length < cap) {
        const page = await ins.request("GET", `/${o}`, {
          params: { top: Math.min(PAGE_MAX, cap - out.length), skip: start0 + out.length,
                    brief: String(brief), ...extra } });
        if (page && !Array.isArray(page) && page.error) {
          return { items: brief ? briefStrip(out) : out, total_fetched: out.length,
                   truncated: true, partial: true, error: page.error };
        }
        const rows = Array.isArray(page) ? page : [];
        out.push(...rows);
        if (rows.length < Math.min(PAGE_MAX, cap - (out.length - rows.length))) break;
      }
    }
  }
  const items = out.slice(0, cap);
  if (brief) briefStrip(items);
  const truncated = start0 > 0
    || (totalKnown ? start0 + items.length < total : items.length === cap);
  return { items, total_fetched: items.length, truncated };
}

export async function hydrate(ins: Insightly, o: string, items: any[],
                              limit = HYDRATE_MAX): Promise<[any[], string]> {
  const pk = PK[o];
  if (!pk || !items.length) return [items, "not hydrated (no primary key known for this object)"];
  if (items.length > limit) return [items, `not hydrated (${items.length} records exceeds the ${limit}-call budget)`];
  // The laptop server did this sequentially; 6-wide is the visible speedup.
  const out = await pooled(items.map((rec) => async () => {
    const rid = rec && typeof rec === "object" ? rec[pk] : null;
    if (rid === null || rid === undefined) return rec;
    const full = await ins.request("GET", `/${o}/${rid}`);
    return full && typeof full === "object" && !full.error ? full : rec;
  }));
  return [out, `hydrated (${items.length} full records incl. custom fields)`];
}

async function searchWindow(ins: Insightly, o: string, since: string): Promise<[any[] | null, boolean]> {
  const out: any[] = [];
  while (out.length < WINDOW_BOUND) {
    const page = await ins.request("GET", `/${o}/Search`, {
      params: { top: SCAN_CAP, skip: out.length, brief: "true", updated_after_utc: since } });
    if (page && !Array.isArray(page) && page.error) return out.length ? [out, false] : [null, false];
    const rows = Array.isArray(page) ? briefStrip(page) : [];
    out.push(...rows);
    if (rows.length < SCAN_CAP) return [out, true];
  }
  return [out, false];
}

function daysAgo(d: number): string {
  return new Date(Date.now() - d * 864e5).toISOString().slice(0, 19).replace("T", " ");
}

/** Coarse ladder in MINUTES, used only to bracket the search before bisecting. */
const LADDER_MIN = [60, 360, 1440, 10080, 43200, 129600, 525600, 2628000];

function minutesAgo(m: number): string {
  return new Date(Date.now() - m * 60_000).toISOString().slice(0, 19).replace("T", " ");
}

function ago(m: number): string {
  if (m < 90) return `${m}m`;
  if (m < 2880) return `${Math.round(m / 60)}h`;
  return `${Math.round(m / 1440)}d`;
}

/** How many records changed since `since` — one record on the wire, count in the header. */
async function windowCount(ins: Insightly, o: string, since: string): Promise<number> {
  const [, hdrs] = await ins.request("GET", `/${o}/Search`, { wantHeaders: true,
    params: { top: 1, brief: "true", count_total: "true", updated_after_utc: since } });
  const n = parseInt(hdrs?.["x-total-count"] ?? "", 10);
  return Number.isNaN(n) ? -1 : n;
}

/**
 * The `want` most recently created-or-updated records — correctly, on any size of object.
 *
 * Insightly cannot sort, so the only way to rank by recency is to hold the candidates in
 * memory. The trick is to shrink the candidate set until it fits one page, using the fact
 * that /{Object}/Search DOES filter on updated_after_utc and DOES report a count for a
 * single-record request. So: bracket a change window on a coarse ladder, then BISECT the
 * cutoff in time until the window holds between `want` and one page of records. Every
 * record newer than that cutoff is inside the window, so ranking the window ranks the
 * world — the answer is exact, not a sample.
 *
 * Two shapes fall out of real data:
 *   - a steady stream (Payments, Cases) brackets in a couple of probes;
 *   - a nightly sync that stamps thousands of rows within the same minute cannot be
 *     bisected below that cluster. Those records genuinely tie on recency, so the
 *     documented (recency, id) order makes the highest ids the answer, and reading the
 *     window's tail by id is exact rather than approximate. `basis` says which happened.
 *
 * Cost is a handful of one-record probes plus a single page fetch: measured 20.2s -> ~4s
 * on a 190k-record object, which matters because the dashboard widget abandons any tool
 * call that takes longer than 20s and then silently shows the raw (oldest-first) list.
 */
export async function newestRecords(ins: Insightly, o: string, want: number):
    Promise<[any, number | null, string]> {
  const [, hdrs0] = await ins.request("GET", `/${o}`, {
    params: { top: 1, brief: "true", count_total: "true" }, wantHeaders: true });
  const t0 = parseInt(hdrs0?.["x-total-count"] ?? "", 10);
  const total = Number.isNaN(t0) ? null : t0;

  const rank = async (params: Record<string, unknown>, path: string, basis: string):
      Promise<[any, number | null, string]> => {
    const body = await ins.request("GET", path, { params });
    if (body && !Array.isArray(body) && body.error) return [body, total, "error"];
    const items = briefStrip(Array.isArray(body) ? body : []);
    return [sortNewest(items, o).slice(0, want), total, basis];
  };

  // Small enough to hold whole — and the fallback when the count header is missing.
  if (total === null || total <= SCAN_CAP) {
    return rank({ top: SCAN_CAP, skip: 0, brief: "true" }, `/${o}`, "exact");
  }

  // Bracket: the narrowest ladder rung holding at least `want`.
  let lo = 0, loCount = 0;            // lo is always known to hold < want
  let hi = -1, hiCount = -1;
  for (const m of LADDER_MIN) {
    const n = await windowCount(ins, o, minutesAgo(m));
    if (n < 0) break;                 // this object has no searchable change date
    if (n >= want) { hi = m; hiCount = n; break; }
    lo = m; loCount = n;
  }

  if (hi < 0) {
    // No Search, or nothing recent enough anywhere on the ladder: ascending ids mean the
    // newest-created sit on the last page, which is the cheapest sound answer available.
    return rank({ top: want, skip: Math.max(0, total - want), brief: "true" }, `/${o}`,
                hiCount < 0 && loCount === 0
                  ? "newest by id (this object has no searchable change date)"
                  : "newest by id (too few recent changes to bracket a window)");
  }

  // Bisect the cutoff in time until the window fits one page.
  let guard = 14;
  while (hiCount > SCAN_CAP && hi - lo > 1 && guard-- > 0) {
    const mid = Math.floor((lo + hi) / 2);
    const n = await windowCount(ins, o, minutesAgo(mid));
    if (n < 0) break;
    if (n >= want) { hi = mid; hiCount = n; } else { lo = mid; loCount = n; }
  }

  const since = minutesAgo(hi);
  if (hiCount <= SCAN_CAP) {
    return rank({ top: SCAN_CAP, skip: 0, brief: "true", updated_after_utc: since },
                `/${o}/Search`, `exact — ranked every record changed in the last ${ago(hi)}`);
  }

  // Irreducible cluster: this many records share (near enough) one timestamp, so recency
  // cannot separate them and the id tie-break decides. Their newest end is the last page.
  return rank({ top: SCAN_CAP, skip: Math.max(0, hiCount - SCAN_CAP), brief: "true",
                updated_after_utc: since }, `/${o}/Search`,
              `exact by tie-break — ${hiCount} records share the newest change time ` +
              `(within ${ago(hi)}), so the highest ids win`);
}

/**
 * Top N records by ANY field, in either direction — the ranking Insightly cannot do.
 *
 * Two facts make this affordable on objects far too large to export. First,
 * /{Object}/Search?field_name=&field_value= filters EXACTLY, server-side, including on
 * custom fields: on a 362k-organisation org, PayingStatus__c=paying is 14,276 records, a
 * 25x reduction before a single record is fetched. Second, a one-record request reports
 * X-Total-Count, so the job can be priced before it is run.
 *
 * From there the scan keeps only a bounded heap, never the records, so memory is O(top)
 * regardless of scale — the thing that made the export path impossible (362k full records
 * is ~36GB, because custom-field values live in CUSTOMFIELDS and brief strips them).
 *
 * Returns null when the work exceeds `budget` pages so the caller can hand it to a
 * background task instead of stalling a chat turn.
 */
export async function rankTopBy(ins: Insightly, o: string, opts: {
  field: string; direction?: "asc" | "desc"; filterField?: string; filterValue?: string;
  where?: any[]; top?: number; fields?: string[]; budget?: number;
  onProgress?: (scanned: number) => void; startPage?: number;
  /** Inline callers want null when the job is too big to finish now; the background
   *  ranker wants to walk `budget` pages per tick and be told whether more remain. */
  bailIfOverBudget?: boolean;
}): Promise<{ items: any[]; scanned: number; candidates: number | null;
              pages: number; exhausted: boolean } | null> {
  const want = Math.min(Math.max(opts.top ?? 25, 1), HYDRATE_MAX);
  const desc = (opts.direction ?? "desc") === "desc";
  const filtering = !!(opts.filterField && opts.filterValue !== undefined);
  const path = filtering ? `/${o}/Search` : `/${o}`;
  const base: Record<string, unknown> = filtering
    ? { field_name: opts.filterField, field_value: opts.filterValue } : {};

  // Full records: the interesting columns are nearly always custom, and brief drops them.
  const perPage = 200;
  const [, hdrs] = await ins.request("GET", path, { wantHeaders: true,
    params: { ...base, top: 1, brief: "true", count_total: "true" } });
  const c = parseInt(hdrs?.["x-total-count"] ?? "", 10);
  const candidates = Number.isNaN(c) ? null : c;
  const pagesNeeded = candidates === null ? null : Math.ceil(candidates / perPage);
  const budget = Math.max(opts.budget ?? 8, 1);
  if ((opts.bailIfOverBudget ?? true) && pagesNeeded !== null && pagesNeeded > budget) {
    return null;                                   // caller should hand this to a task
  }

  const heap: any[] = [];
  const keyOf = (r: any) => getField(r, opts.field);

  const startPage = opts.startPage ?? 0;
  let scanned = 0, pages = 0, reachedEnd = false, failed = false;

  // Pages go out three at a time. Full records are ~100KB each, so a 200-record page is
  // ~20MB — six-wide (the norm elsewhere here) would put 120MB in flight against a 128MB
  // isolate. Three keeps peak memory sane while still cutting an inline rank from ~16s to
  // ~6s, which matters because the dashboard abandons any call over 20s. Ordering is
  // irrelevant: every page feeds the same heap.
  const LANES = 3;
  while (pages < budget && !reachedEnd && !failed) {
    const lanes = Math.min(LANES, budget - pages);
    const offsets = Array.from({ length: lanes }, (_, i) => (startPage + pages + i) * perPage);
    const results = await pooled(offsets.map((off) => () => ins.request("GET", path, {
      params: { ...base, top: perPage, skip: off, brief: "false" } })), LANES);
    for (const rows of results) {
      if (rows && !Array.isArray(rows) && rows.error) { failed = true; break; }
      const batch = Array.isArray(rows) ? rows : [];
      pages++; scanned += batch.length;
      for (const r of batch) {
        if (opts.where?.length && !whereOk(r, opts.where)) continue;
        const k = keyOf(r);
        if (k === undefined || k === null || k === "") continue;   // unranked, not "lowest"
        heap.push(r);
        if (heap.length > want * 4) { rankSort(heap, o, opts.field, opts.direction); heap.length = want; }
      }
      if (batch.length < perPage) { reachedEnd = true; break; }
    }
    opts.onProgress?.(scanned);
  }
  if (failed) return { items: finish(), scanned, candidates, pages, exhausted: false };
  const exhausted = reachedEnd
    || (pagesNeeded !== null && startPage + pages >= pagesNeeded);
  function finish(): any[] {
    rankSort(heap, o, opts.field, opts.direction);
    const top = heap.slice(0, want);
    return opts.fields?.length ? projectAll(top, [opts.field, ...opts.fields], o) : top;
  }
  return { items: finish(), scanned, candidates, pages, exhausted };
}

/**
 * Rank in place, best-first, by an arbitrary field. Numbers compare numerically, anything
 * else lexicographically (ISO dates sort correctly either way), and the record id breaks
 * ties so a page boundary never reshuffles equal values. Exported because the background
 * ranker re-ranks the union of each tick's leaders and must use the identical order.
 */
export function rankSort(items: any[], o: string, field: string,
                         direction: "asc" | "desc" = "desc"): any[] {
  const desc = direction === "desc";
  items.sort((a, b) => {
    const ka = getField(a, field), kb = getField(b, field);
    const sa = String(ka ?? "").trim(), sb = String(kb ?? "").trim();
    const na = typeof ka === "number" ? ka : Number(sa);
    const nb = typeof kb === "number" ? kb : Number(sb);
    let c: number;
    if (sa !== "" && sb !== "" && !Number.isNaN(na) && !Number.isNaN(nb)) c = na - nb;
    else c = sa < sb ? -1 : sa > sb ? 1 : 0;
    if (c === 0) return recordId(b, o) - recordId(a, o);
    return desc ? -c : c;
  });
  return items;
}

/** where-clause evaluation, kept here so the ranker needs no import cycle. */
function whereOk(rec: any, where: any[]): boolean {
  for (const w of where) {
    const v = w.field ? getField(rec, w.field) : undefined;
    if (w.contains !== undefined) {
      const needle = String(w.contains).toLowerCase();
      if (w.field) {
        if (v === undefined || v === null || !String(v).toLowerCase().includes(needle)) return false;
      }
      continue;
    }
    if (w.not_empty && (v === undefined || v === null || String(v).trim() === "" || v === 0)) return false;
    if (w.equals !== undefined && String(v) !== String(w.equals)) return false;
    if (w.gte !== undefined && (v === undefined || v === null
        || String(v) < String(w.gte))) return false;
    if (w.lte !== undefined && (v === undefined || v === null
        || String(v) > String(w.lte))) return false;
  }
  return true;
}

export function forwardDated(field: string): boolean {
  return FORWARD_DATED.some((w) => field.includes(w));
}

export async function newestByField(ins: Insightly, o: string, field: string, want: number):
    Promise<Record<string, any>> {
  let probes = 0;
  const priced: Array<[number, string, number]> = [];
  const over: Array<[number, string, number]> = [];
  // Probe windows in PARALLEL — six one-record requests instead of a sequential walk.
  const windows = [...RECENT_WINDOWS.slice(1), 3650];
  const results = await pooled(windows.map((days) => async () => {
    const since = daysAgo(days);
    const [, hdrs] = await ins.request("GET", `/${o}/Search`, { wantHeaders: true,
      params: { top: 1, brief: "true", count_total: "true", updated_after_utc: since } });
    return [days, since, parseInt(hdrs?.["x-total-count"] ?? "", 10)] as [number, string, number];
  }));
  probes = windows.length;
  for (const [days, since, count] of results) {
    if (Number.isNaN(count)) {
      return { error: `/${o}/Search did not report a count, so a window cannot be priced.`,
               hint: "Use start_export for this object — it pages without a cap." };
    }
    if (count > WINDOW_BOUND) { over.push([days, since, count]); break; }
    priced.push([days, since, count]);
  }
  if (!priced.length && !over.length) {
    return { error: `no records in ${o} carry a change date, so no window can be built.`,
             hint: "Use start_export and rank the exported records." };
  }
  const plan = [...(priced.length ? [priced[priced.length - 1]] : []), ...over.slice(0, 1)];
  let fetched = 0;
  let attempt: any = null;
  for (const [days, since, count] of plan) {
    const [rows, whole] = await searchWindow(ins, o, since);
    if (rows === null) {
      return { error: `/${o}/Search is not available for '${o}'.`,
               hint: "Use start_export and rank the exported records instead." };
    }
    fetched += rows.length;
    const have = rows.filter((r) => r && typeof r === "object" && r[field])
      .sort((a, b) => (String(a[field]) < String(b[field]) ? 1 : -1));
    const top = have.slice(0, want);
    const edge = top.length ? String(top[top.length - 1][field]).slice(0, 10) : "";
    const proven = top.length >= want && whole && edge > since.slice(0, 10);
    attempt = { days, since, count, have, top, proven, whole };
    if (proven) break;
  }
  const { days, since, count, have, top, proven, whole } = attempt;
  const out: Record<string, any> = {
    object: o, date_field: field, sorted_by: `${field}, newest first`,
    returned: top.length, window_days: days, window_start: since.slice(0, 10),
    records_updated_in_window: count, records_with_a_value_in_window: have.length,
    cost: { count_probes: probes, records_fetched: fetched },
    complete: proven,
  };
  if (!proven) {
    if (top.length < want) {
      out.caveat = `Only ${top.length} records carry ${field} within the last ${days} days — ` +
        "that is everything available in the widest window that could be fetched.";
    } else if (!whole) {
      out.caveat = `The ${days}-day window exceeded the ${WINDOW_BOUND}-record fetch bound, ` +
        "so records inside it were not all ranked.";
    } else {
      out.caveat = `The ${top.length}th value (${String(top[top.length - 1][field]).slice(0, 10)}) falls ` +
        `outside the ${days}-day change window, so an older record could rank higher. These are ` +
        "the best available without scanning the object — use start_export to be certain.";
    }
  }
  const [hydrated, note] = await hydrate(ins, o, top);
  out.detail_level = note;
  return fit(hydrated, out);
}

export function mask(k?: string | null): string {
  return k && k.length >= 4 ? "…" + k.slice(-4) : (k ? "set" : "");
}

/**
 * Server-side field projection — the workaround for two Insightly API gaps at once:
 * no field selection (every read returns the whole record) and no batch-get. Big
 * Organisation records run 100KB+ (300+ custom fields, a LINKS array with 100+ entries);
 * reading one number off 35 of them used to mean 3.5MB into the conversation. Projection
 * fetches the full record HERE and returns only what was asked for.
 *
 * Fields resolve against the record's top-level keys first, then inside the CUSTOMFIELDS
 * array (v3.1 nests custom values as {FIELD_NAME, FIELD_VALUE}), flattened to plain
 * `name: value` — case-insensitive on both. The primary key always rides along so results
 * stay joinable. Missing fields are simply absent, never invented.
 */
export function project(rec: any, fields: string[], pk?: string | null): Record<string, any> {
  if (!rec || typeof rec !== "object") return {};
  const out: Record<string, any> = {};
  if (pk && rec[pk] !== undefined) out[pk] = rec[pk];
  const topByLower: Record<string, string> = {};
  for (const k of Object.keys(rec)) topByLower[k.toLowerCase()] = k;
  const cf = Array.isArray(rec.CUSTOMFIELDS) ? rec.CUSTOMFIELDS : [];
  for (const f of fields) {
    const want = String(f);
    const topKey = topByLower[want.toLowerCase()];
    if (topKey !== undefined && topKey !== "CUSTOMFIELDS") {
      out[want] = rec[topKey];
      continue;
    }
    const hit = cf.find((c: any) =>
      String(c?.FIELD_NAME ?? "").toLowerCase() === want.toLowerCase() ||
      String(c?.CUSTOM_FIELD_ID ?? "").toLowerCase() === want.toLowerCase());
    if (hit) out[want] = hit.FIELD_VALUE;
  }
  return out;
}

export function projectAll(items: any[], fields: string[] | undefined, o: string): any[] {
  if (!fields?.length || !Array.isArray(items)) return items;
  const pk = PK[o] ?? null;
  return items.map((r) => project(r, fields, pk));
}
