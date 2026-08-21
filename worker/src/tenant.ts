/**
 * One stable, non-reversible id per (api key, pod).
 *
 * This is the tenant boundary for everything the worker keeps outside a single request:
 * task Durable Objects, cached metadata, R2 snapshots, CSV keys, and rate pacing. The
 * formula must stay byte-identical to the one taskCall has always used, or existing task
 * DOs become unaddressable.
 */
export async function tenantHash(session: { key: string | null; pod: string }): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256",
    new TextEncoder().encode(`${session.key}|${session.pod}`));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Constant-time compare for secrets and signatures (never leak position via early exit). */
export function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function hmacHex(secret: string, msg: string): Promise<string> {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
