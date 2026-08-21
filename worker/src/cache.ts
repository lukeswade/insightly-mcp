/**
 * Metadata cache (Workers KV), one namespace per tenant hash.
 *
 * Field definitions, custom-object lists, pipelines, users and categories are
 * CONFIGURATION: they change when an admin changes them, not when a record moves. The
 * laptop server re-fetched them in every new chat, and describe_object — the tool the
 * model calls before it writes anything — paid two API round trips every time. An hour of
 * KV is ~10ms instead of ~400ms, and it takes the calls off the key's rate budget.
 *
 * Two rules keep this honest:
 *   - An ALLOWLIST decides what may be cached. Record reads can never land here by
 *     accident, however a call site is written.
 *   - Every failure is silent and falls through to the live API. A cache must not be able
 *     to break an answer.
 * The cache key contains the tenant HASH, never the API key.
 */
const TTL_S = 3600;

const CACHEABLE: RegExp[] = [
  /^\/CustomFields\/[A-Za-z0-9_]+$/, /^\/CustomObjects$/, /^\/Users$/, /^\/Teams$/,
  /^\/Pipelines$/, /^\/PipelineStages$/, /^\/Relationships$/, /^\/Instance$/,
  /^\/TaskCategories$/, /^\/OpportunityCategories$/, /^\/OpportunityStateReasons$/,
  /^\/ProjectCategories$/, /^\/FileCategories$/, /^\/LeadSources$/, /^\/LeadStatuses$/,
  /^\/Currencies$/, /^\/Countries$/, /^\/ActivitySets$/, /^\/Tags$/,
];

/** Is this Insightly path configuration rather than data? */
export const cacheable = (path: string) => CACHEABLE.some((r) => r.test(path));

export interface Cached<T> { value: T; hit: boolean; }

/**
 * Read-through cache. `slot` names the entry within the tenant; `refresh` forces a live
 * read and rewrites the entry.
 */
export async function cached<T>(env: any, tenant: string, slot: string, refresh: boolean,
                                live: () => Promise<T>): Promise<Cached<T>> {
  const kv = env?.META;
  const k = `md:v1:${tenant}:${slot}`;
  if (kv && !refresh) {
    try {
      const hit = await kv.get(k, "json");
      if (hit !== null && hit !== undefined) return { value: hit as T, hit: true };
    } catch { /* fall through to live */ }
  }
  const value = await live();
  // Never cache an error envelope — that would pin a transient failure for an hour.
  const bad = value && typeof value === "object" && !Array.isArray(value) && (value as any).error;
  if (kv && !bad) {
    try { await kv.put(k, JSON.stringify(value), { expirationTtl: TTL_S }); } catch { /* best effort */ }
  }
  return { value, hit: false };
}
