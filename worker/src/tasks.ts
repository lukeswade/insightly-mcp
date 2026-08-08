/**
 * Background tasks on a Durable Object — the one piece of the local server that could
 * not stay in-process (a stateless Worker forgets everything between requests).
 *
 * Isolation: the DO instance is addressed by a hash of (api key + pod), so each key sees
 * only its own tasks — necessary on a shared endpoint.
 *
 * Key custody: the API key is stored in the DO ONLY while a task is running (the alarm
 * needs it to keep paging with nobody connected) and is deleted the moment the task
 * reaches a terminal state. Task data itself is swept one hour after completion, matching
 * the local server's TTL.
 *
 * Budget: each alarm tick spends at most ~36 upstream calls (6-wide × 6 rounds), safely
 * inside even the free-plan 50-subrequest ceiling, then re-arms itself one second out.
 */
import { Insightly, PAGE_MAX, PK, briefStrip, fit, pooled } from "./insightly";

const TASK_POLL_MS = 500;
const TASK_TTL_MS = 3600_000;
const EXPORT_SAFETY_CAP = 250_000;
const CHUNK = PAGE_MAX;            // one storage chunk per API page
const PAGES_PER_TICK = 6;          // rounds of 6-wide fetches per alarm
const CREATES_PER_TICK = 30;

interface Meta {
  task_id: string; kind: string; detail: string;
  status: "working" | "completed" | "failed" | "cancelled";
  status_message: string;
  created_at: string; last_updated_at: string; done_at: string | null;
  progress: number; total: number | null;
  error: string | null; summary: any;
  cancel: boolean; chunks: number; count: number;
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

export class TaskDO {
  constructor(private state: DurableObjectState, private env: unknown) {}

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

  async fetch(request: Request): Promise<Response> {
    const body: any = await request.json();
    const op = body.op as string;
    const respond = (x: unknown) => Response.json(x as any);

    if (op === "start_export" || op === "start_bulk") {
      const tid = crypto.randomUUID().replace(/-/g, "").slice(0, 12);
      const m: Meta = {
        task_id: tid, kind: op === "start_export" ? "export" : "bulk_create",
        detail: body.detail, status: "working", status_message: "queued",
        created_at: now(), last_updated_at: now(), done_at: null,
        progress: 0, total: null, error: null, summary: null,
        cancel: false, chunks: 0, count: 0, params: body.params,
        session: body.session,
      };
      if (op === "start_bulk") {
        const records: any[] = body.records ?? [];
        m.total = records.length;
        for (let i = 0; i * CHUNK < records.length; i++) {
          await this.state.storage.put(`bulkin:${tid}:${i}`, records.slice(i * CHUNK, (i + 1) * CHUNK));
        }
      }
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
      // Read only the chunks the window touches — a 167k-record export never fully loads.
      const c0 = Math.floor(start / CHUNK), c1 = Math.floor((start + page - 1) / CHUNK);
      const rows: any[] = [];
      for (let c = c0; c <= c1 && c < m.chunks; c++) {
        rows.push(...(await this.state.storage.get<any[]>(`chunk:${m.task_id}:${c}`) ?? []));
      }
      const window = rows.slice(start - c0 * CHUNK, start - c0 * CHUNK + page);
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

    if (op === "list") {
      const metas = await this.state.storage.list<Meta>({ prefix: "meta:" });
      const tasks = [...metas.values()].map((m) => pub(m, true))
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

    if (m.kind === "export") {
      const { o, brief, updatedAfterUtc, cap } = m.params;
      if (m.total === null) {
        const [, hdrs] = await ins.request("GET", `/${o}`, {
          params: { top: 1, brief: "true", count_total: "true" }, wantHeaders: true });
        const t = parseInt(hdrs?.["x-total-count"] ?? "", 10);
        m.total = Number.isNaN(t) ? null : Math.min(t, cap);
      }
      for (let round = 0; round < PAGES_PER_TICK; round++) {
        if (m.cancel) { await this.finish(m, "cancelled", `cancelled after ${m.count} records`); return; }
        const remaining = Math.min(cap, m.total ?? cap) - m.count;
        if (remaining <= 0) break;
        const lanes = Math.min(6, Math.ceil(remaining / PAGE_MAX));
        const offsets = Array.from({ length: lanes }, (_, i) => m.count + i * PAGE_MAX);
        const pages = await pooled(offsets.map((off) => () => ins.request("GET", `/${o}`, {
          params: { top: PAGE_MAX, skip: off, brief: String(brief),
                    ...(updatedAfterUtc ? { updated_after_utc: updatedAfterUtc } : {}) } })));
        let short = false;
        for (const page of pages) {
          if (page && !Array.isArray(page) && page.error) {
            m.error = page.error;
            await this.finish(m, "failed", `API error after ${m.count} records`);
            return;
          }
          const rows = brief ? briefStrip(Array.isArray(page) ? page : []) : (Array.isArray(page) ? page : []);
          if (rows.length) {
            await this.state.storage.put(`chunk:${m.task_id}:${m.chunks}`, rows.slice(0, CHUNK));
            m.chunks++; m.count += rows.length; m.progress = m.count;
          }
          if (rows.length < PAGE_MAX) short = true;
        }
        m.status_message = `exported ${m.count}${m.total ? ` of ${m.total}` : ""}`;
        await this.putMeta(m);
        if (short || m.count >= cap) {
          m.summary = { object: o, exported: m.count, truncated: m.count >= cap };
          await this.finish(m, "completed", `exported ${m.count} records`);
          return;
        }
      }
      return;   // more work next tick
    }

    if (m.kind === "bulk_create") {
      const { o } = m.params;
      const pk = PK[o];
      const done: number = m.params.done ?? 0;
      const total = m.total ?? 0;
      const batchEnd = Math.min(done + CREATES_PER_TICK, total);
      const chunkIdx = Math.floor(done / CHUNK);
      const chunk = await this.state.storage.get<any[]>(`bulkin:${m.task_id}:${chunkIdx}`) ?? [];
      const slice = chunk.slice(done - chunkIdx * CHUNK, batchEnd - chunkIdx * CHUNK);
      const ids: any[] = m.params.ids ?? [];
      const errors: any[] = m.params.errors ?? [];
      // Writes go 4-wide: quick, but leaves headroom under the write endpoints' pacing.
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
        await this.state.storage.put(`chunk:${m.task_id}:0`, ids.slice(0, CHUNK));
        m.chunks = 1; m.count = ids.length;
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
