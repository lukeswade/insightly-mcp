/**
 * The query core: where / sort / group-aggregate / contains over Insightly records,
 * with every field reference resolving through the same flattening rules as projection
 * (top-level keys first, then CUSTOMFIELDS entries, case-insensitive).
 *
 * This is the layer Insightly's API never shipped — it has no filtering, no sorting, no
 * aggregation, no "field is not empty". Everything here runs server-side over records the
 * worker (or the task Durable Object) already holds, so the conversation only ever sees
 * the answer.
 */

export interface WhereClause {
  field?: string;
  contains?: string;    // case-insensitive substring; without `field`, searches ANY field
  equals?: unknown;     // loose equality (string/number tolerant)
  not_empty?: boolean;  // field has a non-null, non-empty value
  gte?: unknown;        // >=, numeric when both sides parse, else lexicographic (dates ok)
  lte?: unknown;
}

export interface Metric { op: "count" | "sum" | "avg" | "min" | "max"; field?: string; }

/** Field lookup with CUSTOMFIELDS flattening — the one resolver every feature shares. */
export function getField(rec: any, name: string): unknown {
  if (!rec || typeof rec !== "object" || !name) return undefined;
  const want = String(name).toLowerCase();
  for (const k of Object.keys(rec)) {
    if (k.toLowerCase() === want && k !== "CUSTOMFIELDS") return rec[k];
  }
  const cf = Array.isArray(rec.CUSTOMFIELDS) ? rec.CUSTOMFIELDS : [];
  const hit = cf.find((c: any) =>
    String(c?.FIELD_NAME ?? "").toLowerCase() === want ||
    String(c?.CUSTOM_FIELD_ID ?? "").toLowerCase() === want);
  return hit ? hit.FIELD_VALUE : undefined;
}

export function containsAnywhere(rec: any, needle: string): boolean {
  if (!rec || typeof rec !== "object") return false;
  for (const [k, v] of Object.entries(rec)) {
    if (k === "CUSTOMFIELDS" || k === "LINKS") continue;
    if (v !== null && typeof v !== "object" &&
        String(v).toLowerCase().includes(needle)) return true;
  }
  for (const c of (Array.isArray(rec.CUSTOMFIELDS) ? rec.CUSTOMFIELDS : [])) {
    const v = c?.FIELD_VALUE;
    if (v !== null && v !== undefined && typeof v !== "object" &&
        String(v).toLowerCase().includes(needle)) return true;
  }
  return false;
}

function cmp(a: unknown, b: unknown): number {
  // Number(), not parseFloat(): parseFloat reads "2026-03-11 16:26" as 2026, which made
  // every ISO date compare equal and turned date sorts into silent no-ops.
  const na = typeof a === "number" ? a : (String(a).trim() === "" ? NaN : Number(String(a).trim()));
  const nb = typeof b === "number" ? b : (String(b).trim() === "" ? NaN : Number(String(b).trim()));
  if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
  const sa = String(a), sb = String(b);
  return sa < sb ? -1 : sa > sb ? 1 : 0;
}

export function matches(rec: any, where: WhereClause[] | undefined): boolean {
  if (!where?.length) return true;
  for (const w of where) {
    if (w.contains !== undefined) {
      const needle = String(w.contains).toLowerCase();
      if (w.field) {
        const v = getField(rec, w.field);
        if (v === undefined || v === null ||
            !String(v).toLowerCase().includes(needle)) return false;
      } else if (!containsAnywhere(rec, needle)) return false;
      continue;
    }
    const v = w.field ? getField(rec, w.field) : undefined;
    if (w.not_empty) {
      if (v === undefined || v === null || String(v).trim() === "" || v === 0) return false;
    }
    if (w.equals !== undefined) {
      if (String(v) !== String(w.equals)) return false;
    }
    if (w.gte !== undefined) {
      if (v === undefined || v === null || cmp(v, w.gte) < 0) return false;
    }
    if (w.lte !== undefined) {
      if (v === undefined || v === null || cmp(v, w.lte) > 0) return false;
    }
  }
  return true;
}

export function sortByField(items: any[], orderBy: string): any[] {
  const parts = String(orderBy).trim().split(/\s+/);
  const field = parts[0];
  const desc = parts.length > 1 && parts[1].toLowerCase().startsWith("desc");
  const present = items.filter((r) => { const v = getField(r, field); return v !== undefined && v !== null; });
  const missing = items.filter((r) => { const v = getField(r, field); return v === undefined || v === null; });
  present.sort((a, b) => { const c = cmp(getField(a, field), getField(b, field)); return desc ? -c : c; });
  return [...present, ...missing];
}

/** Streaming top-N under an order_by — bounded memory however many records flow through. */
export class TopN {
  private rows: any[] = [];
  constructor(private orderBy: string, private n: number) {}
  push(rec: any): void {
    this.rows.push(rec);
    if (this.rows.length >= this.n * 3) this.trim();
  }
  private trim(): void { this.rows = sortByField(this.rows, this.orderBy).slice(0, this.n); }
  result(): any[] { this.trim(); return this.rows; }
}

/** Incremental group accumulator — safe to persist between Durable Object ticks. */
export interface GroupState {
  [groupKey: string]: { count: number; sums: Record<string, number>;
                        mins: Record<string, any>; maxs: Record<string, any>;
                        nums: Record<string, number> };
}

export function accumulate(state: GroupState, rec: any, groupBy: string | undefined,
                           metrics: Metric[]): void {
  const key = groupBy === undefined ? "ALL" : String(getField(rec, groupBy) ?? "(none)");
  const g = state[key] ?? (state[key] = { count: 0, sums: {}, mins: {}, maxs: {}, nums: {} });
  g.count++;
  for (const m of metrics) {
    if (m.op === "count" || !m.field) continue;
    const raw = getField(rec, m.field);
    if (raw === undefined || raw === null || raw === "") continue;
    if (m.op === "min" || m.op === "max") {
      const cur = m.op === "min" ? g.mins[m.field] : g.maxs[m.field];
      const better = cur === undefined || (m.op === "min" ? cmp(raw, cur) < 0 : cmp(raw, cur) > 0);
      if (better) (m.op === "min" ? g.mins : g.maxs)[m.field] = raw;
      continue;
    }
    const n = typeof raw === "number" ? raw : parseFloat(String(raw));
    if (Number.isNaN(n)) continue;
    g.sums[m.field] = (g.sums[m.field] ?? 0) + n;
    g.nums[m.field] = (g.nums[m.field] ?? 0) + 1;
  }
}

export function finishGroups(state: GroupState, groupBy: string | undefined,
                             metrics: Metric[]): Record<string, any>[] {
  const rows = Object.entries(state).map(([key, g]) => {
    const row: Record<string, any> = groupBy === undefined ? {} : { [groupBy]: key };
    for (const m of metrics) {
      if (m.op === "count") { row.count = g.count; continue; }
      if (!m.field) continue;
      const label = `${m.op}_${m.field}`;
      if (m.op === "sum") row[label] = g.sums[m.field] ?? 0;
      else if (m.op === "avg") {
        row[label] = g.nums[m.field] ? +(g.sums[m.field]! / g.nums[m.field]!).toFixed(4) : null;
      } else if (m.op === "min") row[label] = g.mins[m.field] ?? null;
      else if (m.op === "max") row[label] = g.maxs[m.field] ?? null;
    }
    if (!metrics.length) row.count = g.count;
    return row;
  });
  // biggest groups first, by the first numeric column after the group key
  const sortKey = Object.keys(rows[0] ?? {}).find((k) => k !== groupBy) ?? "count";
  rows.sort((a, b) => cmp(b[sortKey], a[sortKey]));
  return rows;
}

/** The columns a query touches — used to project pages down BEFORE accumulating. */
export function referencedFields(groupBy: string | undefined, metrics: Metric[],
                                 where: WhereClause[] | undefined): string[] | null {
  const out = new Set<string>();
  if (groupBy) out.add(groupBy);
  for (const m of metrics) if (m.field) out.add(m.field);
  for (const w of where ?? []) {
    if (w.contains !== undefined && !w.field) return null;   // any-field scan: keep whole records
    if (w.field) out.add(w.field);
  }
  return [...out];
}
