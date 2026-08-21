/**
 * Durable snapshots in R2 — the answer to "export it again, I have another question".
 *
 * An export used to live only in its Durable Object, swept an hour after it finished. So
 * every follow-up ("now group by owner", "now the bottom ten", "same list, as CSV") meant
 * paying for the whole export again, and a question asked in a NEW chat couldn't reach
 * yesterday's work at all. That is exactly what made "top customers by tenure and ARR"
 * feel like it quit: the expensive part had already been done and then thrown away.
 *
 * Every completed export now also lands in R2 as NDJSON with its description in R2 custom
 * metadata, so one list call describes every snapshot a tenant owns. Queries stream the
 * object line by line through the same engine the DO uses (runQuery below is the single
 * implementation both call), so memory stays flat no matter how large the snapshot is.
 *
 * Snapshots inherit the bucket's 7-day lifecycle rule, and are namespaced by tenant hash.
 */
import { PAGE_MAX, fit, projectAll } from "./insightly";
import { GroupState, Metric, TopN, WhereClause, accumulate, finishGroups,
         matches } from "./query";

const PART_BYTES = 8_000_000;         // R2 multipart minimum is 5MB; 8 keeps parts few
const QUERY_TOP_CAP = 500;

export const snapshotKey = (tenant: string, id: string) => `snap/${tenant}/${id}.ndjson`;

function concat(parts: Uint8Array[]): Uint8Array {
  const n = parts.reduce((a, p) => a + p.length, 0);
  const out = new Uint8Array(n);
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}

/**
 * Write rows as NDJSON, single-shot below 8MB and multipart above it, so an export of any
 * size streams out of the DO without ever being held whole in memory.
 */
export async function writeSnapshot(env: any, tenant: string, id: string,
                                    customMetadata: Record<string, string>,
                                    chunks: AsyncIterable<any[]>): Promise<{ bytes: number }> {
  const key = snapshotKey(tenant, id);
  const enc = new TextEncoder();
  const opts = { customMetadata, httpMetadata: { contentType: "application/x-ndjson" } };
  let buf: Uint8Array[] = [], buffered = 0, total = 0;
  let mp: any = null;
  const parts: any[] = [];

  for await (const rows of chunks) {
    if (!rows.length) continue;
    const b = enc.encode(rows.map((r) => JSON.stringify(r)).join("\n") + "\n");
    buf.push(b); buffered += b.length; total += b.length;
    if (buffered >= PART_BYTES) {
      if (!mp) mp = await env.EXPORTS.createMultipartUpload(key, opts);
      parts.push(await mp.uploadPart(parts.length + 1, concat(buf)));
      buf = []; buffered = 0;
    }
  }
  if (mp) {
    if (buffered) parts.push(await mp.uploadPart(parts.length + 1, concat(buf)));
    await mp.complete(parts);
  } else {
    await env.EXPORTS.put(key, concat(buf), opts);
  }
  return { bytes: total };
}

/** Stream a snapshot line by line. Bounded memory: one partial line is ever held. */
export async function scanSnapshot(env: any, tenant: string, id: string,
                                   visit: (rec: any) => void | boolean): Promise<number> {
  const obj = await env.EXPORTS?.get(snapshotKey(tenant, id));
  if (!obj) return -1;
  const reader = obj.body.pipeThrough(new TextDecoderStream()).getReader();
  let tail = "", n = 0, stop = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    tail += value;
    let nl: number;
    while ((nl = tail.indexOf("\n")) >= 0) {
      const line = tail.slice(0, nl); tail = tail.slice(nl + 1);
      if (!line) continue;
      n++;
      if (visit(JSON.parse(line)) === false) { stop = true; break; }
    }
    if (stop) { try { await reader.cancel(); } catch { /* already closed */ } break; }
  }
  if (!stop && tail.trim()) { n++; visit(JSON.parse(tail)); }
  return n;
}

export interface QueryParams {
  where?: WhereClause[]; metrics?: Metric[]; group_by?: string; order_by?: string;
  fields?: string[]; top?: number;
}

/**
 * The one query implementation, shared by the live Durable Object and R2 snapshots.
 * Whoever calls it supplies a scan; everything about filtering, grouping, ranking and
 * projection lives here so the two paths cannot answer the same question differently.
 */
export async function runQuery(scan: (visit: (rec: any) => void | boolean) => Promise<void>,
                               p: QueryParams, count: number, object: string,
                               source: Record<string, any>): Promise<Record<string, any>> {
  const top = Math.min(Math.max(Math.trunc(p.top ?? 100), 1), QUERY_TOP_CAP);
  const metrics: Metric[] = p.metrics ?? [];
  const where = p.where;

  if (p.group_by !== undefined || metrics.length) {
    const state: GroupState = {};
    const mm = metrics.length ? metrics : [{ op: "count" } as Metric];
    let matched = 0;
    await scan((rec) => {
      if (!matches(rec, where)) return;
      matched++;
      accumulate(state, rec, p.group_by, mm);
    });
    const rows = finishGroups(state, p.group_by, mm);
    return fit(rows.slice(0, top), { matched, scanned: count, groups: rows.length,
      group_by: p.group_by ?? null, ...source });
  }

  let matched = 0;
  let rows: any[];
  if (p.order_by) {
    const topn = new TopN(p.order_by, top);
    await scan((rec) => { if (matches(rec, where)) { matched++; topn.push(rec); } });
    rows = topn.result();
  } else {
    rows = [];
    await scan((rec) => {
      if (!matches(rec, where)) return;
      matched++;
      if (rows.length < top) rows.push(rec);
    });
  }
  if (p.fields?.length) rows = projectAll(rows, p.fields, object);
  return fit(rows, { matched, scanned: count, returned: rows.length,
    order_by: p.order_by ?? null, ...source,
    note: matched > rows.length ? `showing ${rows.length} of ${matched} matches — ` +
      "narrow with `where`, or raise `top` (cap 500)." : undefined });
}

/** Every snapshot this tenant owns, newest first, described from R2 custom metadata. */
export async function listSnapshots(env: any, tenant: string): Promise<any[]> {
  if (!env?.EXPORTS) return [];
  const out: any[] = [];
  let cursor: string | undefined;
  do {
    const page: any = await env.EXPORTS.list({ prefix: `snap/${tenant}/`, cursor,
                                               include: ["customMetadata"] });
    for (const o of page.objects ?? []) {
      const md = o.customMetadata ?? {};
      out.push({
        snapshot_id: o.key.split("/").pop()?.replace(/\.ndjson$/, ""),
        object: md.object ?? null, rows: md.rows ? Number(md.rows) : null,
        detail: md.detail ?? null, captured_at: md.captured_at ?? o.uploaded,
        bytes: o.size,
        expires_at: md.captured_at
          ? new Date(Date.parse(md.captured_at) + 7 * 86400_000).toISOString() : null,
      });
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return out.sort((a, b) => (String(a.captured_at) < String(b.captured_at) ? 1 : -1)).slice(0, PAGE_MAX);
}

/** Run a query against a stored snapshot. Returns null when the snapshot is gone. */
export async function querySnapshot(env: any, tenant: string, id: string, p: QueryParams):
    Promise<Record<string, any> | null> {
  const head = await env.EXPORTS?.head(snapshotKey(tenant, id));
  if (!head) return null;
  const md = head.customMetadata ?? {};
  const rows = md.rows ? Number(md.rows) : 0;
  let scanned = 0;
  const scan = async (visit: (rec: any) => void | boolean) => {
    const n = await scanSnapshot(env, tenant, id, visit);
    if (n >= 0) scanned = n;
  };
  const res = await runQuery(scan, p, rows, String(md.object ?? ""),
    { snapshot_id: id, object: md.object ?? null, captured_at: md.captured_at ?? null,
      source: "r2 snapshot" });
  if (scanned && !res.scanned) res.scanned = scanned;
  return res;
}
