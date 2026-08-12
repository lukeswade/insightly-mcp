/**
 * Background tasks on a Durable Object — exports, bulk-creates, streaming aggregations,
 * and the query/CSV surface over completed exports.
 *
 * Isolation: the DO instance is addressed by a hash of (api key + pod), so each key sees
 * only its own tasks — necessary on a shared endpoint.
 *
 * Key custody: the API key is stored ONLY while a task is running (the alarm needs it to
 * keep paging with nobody connected) and is deleted the moment the task reaches a
 * terminal state. Task data sweeps one hour after completion.
 *
 * Storage shape: records live in BYTE-BOUNDED chunks (~700KB serialized) with per-chunk
 * row counts in meta.chunk_rows — a page of full Organisation records (100KB+ each) would
 * blow a per-value limit if chunked by count. Aggregation tasks store no records at all:
 * they stream pages through a group accumulator and persist only the running totals.
 */
import { Insightly, PAGE_MAX, PK, briefStrip, fit, pooled, projectAll, rankSort,
         rankTopBy } from "./insightly";
import { GroupState, Metric, TopN, WhereClause, accumulate, finishGroups, getField,
         matches, referencedFields, sortByField } from "./query";

const TASK_POLL_MS = 500;
const TASK_TTL_MS = 3600_000;
const EXPORT_SAFETY_CAP = 250_000;
const CHUNK_BYTES = 700_000;
const ROUNDS_PER_TICK = 6;         // waves of parallel pages per alarm
const CREATES_PER_TICK = 30;
const QUERY_TOP_CAP = 500;

interface Meta {
  task_id: string; kind: string; detail: string;
  status: "working" | "completed" | "failed" | "cancelled";
  status_message: string;
  created_at: string; last_updated_at: string; done_at: string | null;
  progress: number; total: number | null;
  error: string | null; summary: any;
  cancel: boolean; chunks: number; count: number;
  chunk_rows: number[];
  params: any;
  session?: { key: string; pod: string };   // present ONLY while status === "working"
}

function pub(m: Meta, includeResult = false): Record<string, any> {
  const out: Record<string, any> = {
    task_id: m.task_id, status: m.status, status_message: m.status_message,
    created_at: m.created_at, last_updated_at: m.last_updated_at,
    kind: m.kind, detail: m.detail, progress: m.progress, total: m.total,
    poll_interval_ms: TASK_POLL_MS,
  };
  if (m.error) out.error = m.error;
  if (m.summary) out.summary = m.summary;
  if (includeResult && m.status !== "working") out.result_count = m.count;
  return out;
}

const now = () => new Date().toISOString();

function csvEscape(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = typeof v === "object" ? JSON.stringify(v) : String(v);
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

export class TaskDO {
  constructor(private state: DurableObjectState, private env: any) {}

  private async meta(tid: string): Promise<Meta | undefined> {
    return await this.state.storage.get<Meta>(`meta:${tid}`);
  }

  private async putMeta(m: Meta): Promise<void> {
    m.last_updated_at = now();
    await this.state.storage.put(`meta:${m.task_id}`, m);
  }

  private async finish(m: Meta, status: Meta["status"], msg: string): Promise<void> {
    m.status = status;
    m.status_message = msg;
    m.done_at = now();
    delete m.session;                               // the key leaves storage immediately
    await this.putMeta(m);
  }

  /** Append rows as byte-bounded chunks. */
  private async appendRows(m: Meta, rows: any[]): Promise<void> {
    let buf: any[] = [];
    let bytes = 2;
    const flush = async () => {
      if (!buf.length) return;
      await this.state.storage.put(`chunk:${m.task_id}:${m.chunks}`, buf);
      m.chunk_rows.push(buf.length);
      m.chunks++; m.count += buf.length;
      buf = []; bytes = 2;
    };
    for (const r of rows) {
      const sz = JSON.stringify(r).length + 1;
      if (buf.length && bytes + sz > CHUNK_BYTES) await flush();
      buf.push(r); bytes += sz;
    }
    await flush();
  }

  /** Stream every stored row through a visitor, chunk by chunk. */
  private async scan(m: Meta, visit: (rec: any) => void | boolean): Promise<void> {
    for (let c = 0; c < m.chunks; c++) {
      const rows = await this.state.storage.get<any[]>(`chunk:${m.task_id}:${c}`) ?? [];
      for (const r of rows) if (visit(r) === false) return;
    }
  }

  async fetch(request: Request): Promise<Response> {
    const body: any = await request.json();
    const op = body.op as string;
    const respond = (x: unknown) => Response.json(x as any);

    if (op === "start_export" || op === "start_bulk" || op === "start_aggregate"
        || op === "start_rank") {
      const tid = crypto.randomUUID().replace(/-/g, "").slice(0, 12);
      const kind = op === "start_export" ? "export"
        : op === "start_bulk" ? "bulk_create"
        : op === "start_rank" ? "rank" : "aggregate";
      const m: Meta = {
        task_id: tid, kind, detail: body.detail, status: "working", status_message: "queued",
        created_at: now(), last_updated_at: now(), done_at: null,
        progress: 0, total: null, error: null, summary: null,
        cancel: false, chunks: 0, count: 0, chunk_rows: [], params: body.params,
        session: body.session,
      };
      if (op === "start_bulk") {
        const records: any[] = body.records ?? [];
        m.total = records.length;
        for (let i = 0; i * PAGE_MAX < records.length; i++) {
          await this.state.storage.put(`bulkin:${tid}:${i}`, records.slice(i * PAGE_MAX, (i + 1) * PAGE_MAX));
        }
      }
      if (op === "start_aggregate") m.params.groups = {};
      await this.putMeta(m);
      await this.state.storage.setAlarm(Date.now() + 25);
      return respond({ task_id: tid, status: m.status });
    }

    if (op === "status") {
      const m = await this.meta(body.task_id);
      if (!m) return respond({ error: `unknown task_id '${body.task_id}' (finished tasks are kept for 60 minutes).` });
      return respond(pub(m, true));
    }

    if (op === "result") {
      const m = await this.meta(body.task_id);
      if (!m) return respond({ error: `unknown task_id '${body.task_id}'.` });
      if (m.status === "working") {
        return respond({ error: `no result yet — status is '${m.status}'.`,
                         status: m.status, progress: m.progress });
      }
      const page = Math.min(Math.max(Math.trunc(body.top ?? 100), 1), PAGE_MAX);
      const start = Math.max(Math.trunc(body.skip ?? 0), 0);
      // Walk chunk_rows to the window — chunks are byte-bounded, not fixed-count.
      const window: any[] = [];
      let seen = 0;
      for (let c = 0; c < m.chunks && window.length < page; c++) {
        const rows = m.chunk_rows[c] ?? 0;
        if (seen + rows <= start) { seen += rows; continue; }
        const chunk = await this.state.storage.get<any[]>(`chunk:${m.task_id}:${c}`) ?? [];
        for (const r of chunk) {
          if (seen >= start && window.length < page) window.push(r);
          seen++;
          if (window.length >= page) break;
        }
      }
      const env = fit(window, { returned: window.length, skip: start, top: page,
                                has_more: start + window.length < m.count,
                                next_skip: start + window.length, count: m.count,
                                status: m.status, summary: m.summary });
      if (env.capped) {
        env.returned = (env.items as any[]).length;
        env.next_skip = start + (env.items as any[]).length;
        env.has_more = env.next_skip < m.count;
      }
      return respond(env);
    }

    if (op === "query") {
      const m = await this.meta(body.task_id);
      if (!m) return respond({ error: `unknown task_id '${body.task_id}'.` });
      if (m.status === "working") {
        return respond({ error: `the export is still running — status '${m.status}', ` +
                                `progress ${m.progress}. Query it when completed.` });
      }
      const where: WhereClause[] | undefined = body.where;
      const metrics: Metric[] = body.metrics ?? [];
      const groupBy: string | undefined = body.group_by;
      const fields: string[] | undefined = body.fields?.length ? body.fields : undefined;
      const top = Math.min(Math.max(Math.trunc(body.top ?? 100), 1), QUERY_TOP_CAP);

      if (groupBy !== undefined || metrics.length) {
        const state: GroupState = {};
        let matched = 0;
        await this.scan(m, (rec) => {
          if (!matches(rec, where)) return;
          matched++;
          accumulate(state, rec, groupBy, metrics.length ? metrics : [{ op: "count" }]);
        });
        const rows = finishGroups(state, groupBy, metrics.length ? metrics : [{ op: "count" }]);
        return respond(fit(rows.slice(0, top), {
          matched, scanned: m.count, groups: rows.length,
          group_by: groupBy ?? null, source_task: m.task_id }));
      }

      let matched = 0;
      let rows: any[];
      if (body.order_by) {
        const topn = new TopN(body.order_by, top);
        await this.scan(m, (rec) => { if (matches(rec, where)) { matched++; topn.push(rec); } });
        rows = topn.result();
      } else {
        rows = [];
        await this.scan(m, (rec) => {
          if (!matches(rec, where)) return;
          matched++;
          if (rows.length < top) rows.push(rec);
        });
      }
      const o = String(m.params?.o ?? "");
      if (fields) rows = projectAll(rows, fields, o);
      return respond(fit(rows, { matched, scanned: m.count, returned: rows.length,
        order_by: body.order_by ?? null, source_task: m.task_id,
        note: matched > rows.length ? `showing ${rows.length} of ${matched} matches — ` +
          "narrow with `where`, or raise `top` (cap 500)." : undefined }));
    }

    if (op === "csv") {
      const m = await this.meta(body.task_id);
      if (!m) return respond({ error: `unknown task_id '${body.task_id}'.` });
      if (m.status === "working") {
        return respond({ error: `the export is still running — progress ${m.progress}.` });
      }
      if (!this.env?.EXPORTS) return respond({ error: "no R2 bucket bound." });
      // pass 1: the column set (standard keys + flattened custom names, bounded)
      const cols: string[] = [];
      const seen = new Set<string>();
      await this.scan(m, (rec) => {
        for (const k of Object.keys(rec)) {
          if (k === "CUSTOMFIELDS" || k === "LINKS" || k === "ETag") continue;
          if (!seen.has(k) && cols.length < 400) { seen.add(k); cols.push(k); }
        }
        for (const c of (Array.isArray(rec.CUSTOMFIELDS) ? rec.CUSTOMFIELDS : [])) {
          const n = c?.FIELD_NAME;
          if (n && !seen.has(n) && cols.length < 400) { seen.add(n); cols.push(n); }
        }
      });
      // pass 2: the rows
      const lines: string[] = [cols.map(csvEscape).join(",")];
      await this.scan(m, (rec) => {
        lines.push(cols.map((c) => csvEscape(getField(rec, c))).join(","));
      });
      const csv = lines.join("\r\n");
      const key = `exports/${m.task_id}-${crypto.randomUUID().replace(/-/g, "")}.csv`;
      await this.env.EXPORTS.put(key, csv, {
        httpMetadata: { contentType: "text/csv",
                        contentDisposition: `attachment; filename="${m.detail || "export"}-${m.task_id}.csv"` } });
      return respond({ key, rows: m.count, columns: cols.length, bytes: csv.length });
    }

    if (op === "list") {
      const metas = await this.state.storage.list<Meta>({ prefix: "meta:" });
      const tasks = [...metas.values()].map((mm) => pub(mm, true))
        .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      return respond({ tasks });
    }

    if (op === "cancel") {
      const m = await this.meta(body.task_id);
      if (!m) return respond({ error: `unknown task_id '${body.task_id}'.` });
      if (m.status !== "working") return respond({ ...pub(m), note: "already finished" });
      m.cancel = true;
      await this.putMeta(m);
      return respond({ task_id: m.task_id, status: "cancelling",
                       note: "the task stops at its next checkpoint (within ~1s)." });
    }

    return respond({ error: `unknown op '${op}'` });
  }

  async alarm(): Promise<void> {
    const metas = await this.state.storage.list<Meta>({ prefix: "meta:" });
    let anyWorking = false;
    for (const m of metas.values()) {
      if (m.status === "working") {
        try {
          await this.tick(m);
        } catch (e) {
          m.error = String(e).slice(0, 300);
          await this.finish(m, "failed", "unexpected error — see error field");
        }
        if (m.status === "working") anyWorking = true;
      } else if (m.done_at && Date.parse(m.done_at) < Date.now() - TASK_TTL_MS) {
        for (let c = 0; c < m.chunks; c++) await this.state.storage.delete(`chunk:${m.task_id}:${c}`);
        await this.state.storage.delete(`meta:${m.task_id}`);
      }
    }
    if (anyWorking) await this.state.storage.setAlarm(Date.now() + 1000);
    else if ([...metas.values()].some((m) => m.done_at)) {
      await this.state.storage.setAlarm(Date.now() + TASK_TTL_MS + 5000);   // the sweep
    }
  }

  private async tick(m: Meta): Promise<void> {
    if (!m.session) { await this.finish(m, "failed", "session lost"); return; }
    const ins = new Insightly({ key: m.session.key, pod: m.session.pod });

    if (m.kind === "export" || m.kind === "aggregate") {
      const { o, brief, updatedAfterUtc, cap } = m.params;
      // Full records of heavy objects (Organisations!) run 100KB+ each — smaller pages
      // keep each parse and each storage write inside sane bounds.
      const pageSize = brief ? PAGE_MAX : 200;
      if (m.total === null) {
        const [, hdrs] = await ins.request("GET", `/${o}`, {
          params: { top: 1, brief: "true", count_total: "true" }, wantHeaders: true });
        const t = parseInt(hdrs?.["x-total-count"] ?? "", 10);
        m.total = Number.isNaN(t) ? null : Math.min(t, cap);
      }
      const fetched = () => m.params.fetched ?? 0;
      for (let round = 0; round < ROUNDS_PER_TICK; round++) {
        if (m.cancel) { await this.finish(m, "cancelled", `cancelled after ${fetched()} records`); return; }
        const remaining = Math.min(cap, m.total ?? cap) - fetched();
        if (remaining <= 0) break;
        const lanes = Math.min(6, Math.ceil(remaining / pageSize));
        const offsets = Array.from({ length: lanes }, (_, i) => fetched() + i * pageSize);
        const pages = await pooled(offsets.map((off) => () => ins.request("GET", `/${o}`, {
          params: { top: pageSize, skip: off, brief: String(brief),
                    ...(updatedAfterUtc ? { updated_after_utc: updatedAfterUtc } : {}) } })));
        let short = false;
        for (const page of pages) {
          if (page && !Array.isArray(page) && page.error) {
            m.error = page.error;
            await this.finish(m, "failed", `API error after ${fetched()} records`);
            return;
          }
          let rows = Array.isArray(page) ? page : [];
          m.params.fetched = fetched() + rows.length;
          if (m.kind === "aggregate") {
            const { groupBy, metrics, where } = m.params;
            for (const rec of rows) {
              if (matches(rec, where)) {
                m.params.matched = (m.params.matched ?? 0) + 1;
                accumulate(m.params.groups, rec, groupBy, metrics);
              }
            }
          } else {
            if (brief) rows = briefStrip(rows);
            if (m.params.fields?.length) rows = projectAll(rows, m.params.fields, o);
            if (rows.length) await this.appendRows(m, rows);
          }
          if (rows.length < pageSize) short = true;
        }
        m.progress = fetched();
        m.status_message = `${m.kind === "aggregate" ? "scanned" : "exported"} ` +
          `${fetched()}${m.total ? ` of ${m.total}` : ""}`;
        await this.putMeta(m);
        if (short || fetched() >= cap) {
          if (m.kind === "aggregate") {
            const { groupBy, metrics } = m.params;
            const rows = finishGroups(m.params.groups, groupBy, metrics);
            await this.appendRows(m, rows);
            m.summary = { object: o, scanned: fetched(), matched: m.params.matched ?? 0,
                          groups: rows.length, group_by: groupBy ?? null };
            delete m.params.groups;
            await this.finish(m, "completed", `aggregated ${fetched()} records into ${rows.length} groups`);
          } else {
            m.summary = { object: o, exported: fetched(), truncated: fetched() >= cap };
            await this.finish(m, "completed", `exported ${fetched()} records`);
          }
          return;
        }
      }
      return;   // more work next tick
    }

    if (m.kind === "rank") {
      // Resume where the last tick stopped and keep only the heap, so a 14k-record rank
      // costs O(top) storage no matter how many pages it walks.
      const { o, opts } = m.params;
      const startPage = m.params.page ?? 0;
      const carried: any[] = m.params.heap ?? [];
      const res = await rankTopBy(ins, o, { ...opts, budget: 12, startPage,
        bailIfOverBudget: false, top: Math.max(opts.top ?? 25, 25) });
      if (res === null) { await this.finish(m, "failed", "could not price the rank"); return; }
      // Merge the previous leaders with this tick's and re-rank locally — no extra calls.
      const union = rankSort([...carried, ...res.items], o, opts.field, opts.direction)
        .slice(0, Math.max((opts.top ?? 25) * 4, 100));
      m.params.heap = union;
      m.params.page = startPage + res.pages;
      m.count = union.length;
      m.progress = (m.params.scanned ?? 0) + res.scanned;
      m.params.scanned = m.progress;
      m.total = res.candidates;
      m.status_message = `ranked ${m.progress}${m.total ? ` of ${m.total}` : ""}`;
      if (m.cancel) { await this.finish(m, "cancelled", `cancelled after ${m.progress}`); return; }
      if (res.exhausted || res.pages === 0) {
        const wanted = opts.top ?? 25;
        const ranked = opts.fields?.length
          ? projectAll(union.slice(0, wanted), [opts.field, ...opts.fields], o)
          : union.slice(0, wanted);
        m.count = 0; m.chunks = 0; m.chunk_rows = [];
        await this.appendRows(m, ranked);
        m.summary = { object: o, field: opts.field, direction: opts.direction ?? "desc",
                      scanned: m.progress, candidates: res.candidates,
                      filtered_by: opts.filterField
                        ? `${opts.filterField} = ${opts.filterValue}` : null };
        await this.finish(m, "completed", `ranked ${m.progress} records`);
        return;
      }
      await this.putMeta(m);
      return;
    }

    if (m.kind === "bulk_create") {
      const { o } = m.params;
      const pk = PK[o];
      const done: number = m.params.done ?? 0;
      const total = m.total ?? 0;
      const batchEnd = Math.min(done + CREATES_PER_TICK, total);
      const chunkIdx = Math.floor(done / PAGE_MAX);
      const chunk = await this.state.storage.get<any[]>(`bulkin:${m.task_id}:${chunkIdx}`) ?? [];
      const slice = chunk.slice(done - chunkIdx * PAGE_MAX, batchEnd - chunkIdx * PAGE_MAX);
      const ids: any[] = m.params.ids ?? [];
      const errors: any[] = m.params.errors ?? [];
      const results = await pooled(slice.map((fields, j) => async () => {
        if (m.cancel || !fields || typeof fields !== "object") {
          return { index: done + j, error: fields ? "cancelled" : "not a field dict" };
        }
        const res = await ins.request("POST", `/${o}`, { body: fields });
        if (res && res.error) return { index: done + j, error: res.error };
        return { id: pk && res && typeof res === "object" ? res[pk] : null };
      }), 4);
      for (const r of results) {
        if ("error" in r && r.error !== "cancelled") errors.push(r);
        else if ("id" in r) ids.push(r.id);
      }
      m.params.done = batchEnd; m.params.ids = ids; m.params.errors = errors;
      m.progress = batchEnd;
      m.status_message = `created ${ids.length} of ${total}`;
      if (m.cancel) { await this.finish(m, "cancelled", `cancelled after ${ids.length} created`); return; }
      if (batchEnd >= total) {
        await this.appendRows(m, ids);
        m.summary = { object: o, created: ids.length, failed: errors.length, errors: errors.slice(0, 20) };
        await this.finish(m, "completed", `created ${ids.length}, ${errors.length} failed`);
        return;
      }
      await this.putMeta(m);
    }
  }
}

/** Route a task op to the per-key DO. */
export async function taskCall(env: any, session: { key: string; pod: string },
                               payload: Record<string, unknown>): Promise<any> {
  const buf = await crypto.subtle.digest("SHA-256",
    new TextEncoder().encode(`${session.key}|${session.pod}`));
  const name = [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  const stub = env.TASKS.get(env.TASKS.idFromName(name));
  const r = await stub.fetch("https://task-do/", {
    method: "POST",
    body: JSON.stringify({ ...payload, session }),
    headers: { "Content-Type": "application/json" },
  });
  return await r.json();
}
