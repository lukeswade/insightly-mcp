/**
 * Global rate pacing, one bucket per API key.
 *
 * The in-isolate pacer in Insightly.slot() can only see its own requests. Two chats on
 * the same environment — or a background export running while someone asks a question —
 * are two isolates, each believing it owns the whole 10 req/s, which is how a key earns
 * 429s. This Durable Object is the one place that knows the truth.
 *
 * Virtual-clock scheduling, not a token bucket: a reservation for n requests takes the
 * next free slot and pushes the clock forward n/RATE seconds, so callers queue instead of
 * bursting. Idle time does NOT accrue credit (nextAt is clamped forward to now), which is
 * what keeps a long-idle key from being allowed a 50-request stampede.
 *
 * Fail-open by design: if the DO is unreachable the caller keeps its local pacing. A
 * pacing outage must never turn into a failed answer.
 */
const RATE_PER_SEC = 9;              // Insightly allows 10/s per key; leave one in hand
const MAX_WAIT_MS = 10_000;          // beyond this the caller proceeds and lets 429-retry cope

export class PacerDO {
  private nextAt = 0;

  constructor(private state: DurableObjectState) {}

  async fetch(request: Request): Promise<Response> {
    const body: any = await request.json();
    const n = Math.max(1, Math.min(Math.trunc(body.n ?? 1), 60));
    if (!this.nextAt) this.nextAt = (await this.state.storage.get<number>("nextAt")) ?? 0;
    const now = Date.now();
    const start = Math.max(now, this.nextAt);
    this.nextAt = start + Math.ceil((n / RATE_PER_SEC) * 1000);
    await this.state.storage.put("nextAt", this.nextAt);
    return Response.json({ wait_ms: Math.min(start - now, MAX_WAIT_MS), granted: n });
  }
}

export interface Pacer { reserve(n: number): Promise<number>; }

/** A pacer bound to one tenant, or null when the binding is absent (local dev, tests). */
export function makePacer(env: any, tenant: () => Promise<string>): Pacer | undefined {
  if (!env?.PACER) return undefined;
  return {
    async reserve(n: number): Promise<number> {
      try {
        const id = env.PACER.idFromName(await tenant());
        const r = await env.PACER.get(id).fetch("https://pacer/", {
          method: "POST", body: JSON.stringify({ n }),
          headers: { "Content-Type": "application/json" },
        });
        const j: any = await r.json();
        return Math.max(0, Math.trunc(j?.wait_ms ?? 0));
      } catch {
        return 0;                    // fail open: local pacing still applies
      }
    },
  };
}
