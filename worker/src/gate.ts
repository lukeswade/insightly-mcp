/**
 * Failure throttling for the public endpoint.
 *
 * /mcp is on the open internet. The bridge secret keeps anonymous callers from using it
 * as a free anonymizing proxy to api.insightly.com, but a secret alone still lets someone
 * grind at it — either guessing the secret, or (holding it) guessing Insightly keys. This
 * DO counts failures per IP and hands out a cooldown.
 *
 * Only failures touch it. A request with a valid secret and a working key never pays the
 * round trip, so the happy path is unaffected.
 *
 * The two kinds of failure are counted SEPARATELY and enforced differently:
 *   "secret" — wrong or missing bridge credential. Blocking it costs a caller who already
 *              cannot get in, so it never affects a request that presents a valid secret.
 *   "key"    — Insightly rejected the API key (401). That is key probing by someone who
 *              already holds the bridge secret, so a block here applies to every request
 *              from that address, valid credential or not.
 */
const WINDOW_MS = 600_000;      // 10 minutes to accumulate
const MAX_FAILS = 20;           // then
const BLOCK_MS = 900_000;       // 15 minutes of 429s

interface Row { n: number; first: number; until: number; }

export class GateDO {
  constructor(private state: DurableObjectState) {}

  async fetch(request: Request): Promise<Response> {
    const body: any = await request.json();
    const ip = String(body.ip ?? "unknown").slice(0, 64);
    const kind = body.kind === "key" ? "key" : "secret";
    const k = `ip:${kind}:${ip}`;
    const now = Date.now();
    let row = (await this.state.storage.get<Row>(k)) ?? { n: 0, first: now, until: 0 };

    if (row.until > now) {
      return Response.json({ blocked: true, retry_after_s: Math.ceil((row.until - now) / 1000) });
    }
    if (body.op === "check") return Response.json({ blocked: false, retry_after_s: 0 });

    if (now - row.first > WINDOW_MS) row = { n: 0, first: now, until: 0 };
    row.n++;
    if (row.n >= MAX_FAILS) row.until = now + BLOCK_MS;
    await this.state.storage.put(k, row);
    // One sweep alarm keeps the counter table from growing without bound.
    if (!(await this.state.storage.getAlarm())) {
      await this.state.storage.setAlarm(now + WINDOW_MS + BLOCK_MS);
    }
    return row.until > now
      ? Response.json({ blocked: true, retry_after_s: Math.ceil(BLOCK_MS / 1000) })
      : Response.json({ blocked: false, retry_after_s: 0, fails: row.n });
  }

  async alarm(): Promise<void> {
    const now = Date.now();
    const rows = await this.state.storage.list<Row>({ prefix: "ip:" });
    let live = 0;
    for (const [k, r] of rows) {
      if (r.until <= now && now - r.first > WINDOW_MS) await this.state.storage.delete(k);
      else live++;
    }
    if (live) await this.state.storage.setAlarm(now + WINDOW_MS);
  }
}

export type FailKind = "secret" | "key";

async function call(env: any, op: string, ip: string, kind: FailKind):
    Promise<{ blocked: boolean; retry_after_s: number }> {
  try {
    const stub = env.GATE.get(env.GATE.idFromName("gate"));
    const r = await stub.fetch("https://gate/", {
      method: "POST", body: JSON.stringify({ op, ip, kind }),
      headers: { "Content-Type": "application/json" },
    });
    return (await r.json()) as any;
  } catch {
    return { blocked: false, retry_after_s: 0 };     // fail open — never lock out on our own bug
  }
}

/** Record a failed attempt and report whether this IP is now cooling off. */
export const gateFail = (env: any, ip: string, kind: FailKind) =>
  env?.GATE ? call(env, "fail", ip, kind) : Promise.resolve({ blocked: false, retry_after_s: 0 });

/** Is this IP already cooling off? Consulted only for addresses already seen failing. */
export const gateCheck = (env: any, ip: string, kind: FailKind) =>
  env?.GATE ? call(env, "check", ip, kind) : Promise.resolve({ blocked: false, retry_after_s: 0 });

export function tooMany(retryAfterS: number): Response {
  return new Response("Too many failed attempts. Try again later.\n", {
    status: 429, headers: { "Retry-After": String(Math.max(1, retryAfterS)) },
  });
}
