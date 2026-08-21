/**
 * Signed, expiring download links for CSV exports.
 *
 * The bucket used to be served straight off its public r2.dev URL: unguessable keys, but
 * no auth and no expiry beyond the 7-day lifecycle rule. Fine for QBO demo rows; not fine
 * once someone points the tool at a production environment and pastes the link into
 * Slack. Now the Worker is the only way in, and the link carries an HMAC over
 * (tenant, file, expiry): it cannot be replayed after it expires, cannot be edited to
 * reach another environment's file, and cannot be forged without the Worker secret.
 *
 * The link is still a bearer capability while it lives — anyone holding it can download —
 * so the TTL is short by default and the tool says so.
 */
import { hmacHex, safeEqual } from "./tenant";

export const DEFAULT_TTL_MIN = 60;
export const MAX_TTL_MIN = 1440;

export const exportKey = (tenant: string, taskId: string) =>
  `exports/${tenant}/${taskId}-${crypto.randomUUID().replace(/-/g, "")}.csv`;

/** Turn an R2 key into a signed URL on this worker's own origin. */
export async function signedUrl(secret: string, origin: string, r2key: string, ttlMin: number):
    Promise<{ url: string; expires_at: string; ttl_minutes: number }> {
  const ttl = Math.min(Math.max(Math.trunc(ttlMin || DEFAULT_TTL_MIN), 1), MAX_TTL_MIN);
  const parts = r2key.split("/");                       // exports/<tenant>/<file>
  const tenant = parts[1] ?? "", file = parts.slice(2).join("/");
  const exp = Math.floor(Date.now() / 1000) + ttl * 60;
  const sig = await hmacHex(secret, `${tenant}|${file}|${exp}`);
  return {
    url: `${origin}/d/${tenant}/${file}?e=${exp}&t=${sig.slice(0, 32)}`,
    expires_at: new Date(exp * 1000).toISOString(),
    ttl_minutes: ttl,
  };
}

/** Serve GET /d/<tenant>/<file> — signature and expiry first, R2 second. */
export async function serveDownload(env: any, url: URL): Promise<Response> {
  const deny = (msg: string, status = 403) =>
    new Response(msg + "\n", { status, headers: { "content-type": "text/plain" } });
  const secret = env?.EXPORT_SIGNING_KEY;
  if (!secret) return deny("Downloads are not configured on this worker.", 503);

  const seg = url.pathname.split("/").filter(Boolean);        // ["d", tenant, ...file]
  if (seg.length < 3) return deny("Malformed download link.", 400);
  const tenant = seg[1], file = seg.slice(2).join("/");
  const exp = parseInt(url.searchParams.get("e") ?? "", 10);
  const got = url.searchParams.get("t") ?? "";
  if (!Number.isFinite(exp) || !got) return deny("Malformed download link.", 400);
  if (exp * 1000 < Date.now()) {
    return deny("This download link has expired. Re-issue it with export_csv.", 410);
  }
  const want = (await hmacHex(secret, `${tenant}|${file}|${exp}`)).slice(0, 32);
  if (!safeEqual(got, want)) return deny("Invalid download link.");

  const obj = await env.EXPORTS?.get(`exports/${tenant}/${file}`);
  if (!obj) return deny("That export is gone — files are deleted after 7 days.", 404);
  return new Response(obj.body, {
    headers: {
      "content-type": obj.httpMetadata?.contentType ?? "text/csv",
      "content-disposition": obj.httpMetadata?.contentDisposition ?? "attachment",
      "cache-control": "private, no-store",
    },
  });
}

/**
 * Serve the current bridge bundle.
 *
 * The bundle carries the endpoint credential, so it cannot live in the GitHub repo — that
 * repo is public, and a published bundle is a published secret. It lives in R2 instead and
 * is downloaded through this route, whose token is the thing the install guide protects.
 * That keeps the credential off every public surface while leaving the install itself at
 * two fields: an access token pasted by hand is friction paid by every tester forever, to
 * solve a problem that is really about where the file is hosted.
 *
 * A plain GET with the token in the query string, because a browser download button cannot
 * set headers. The token has its own secret (INSTALL_TOKEN) so rotating it does not
 * invalidate outstanding CSV links.
 */
export async function serveInstall(env: any, url: URL): Promise<Response> {
  const deny = (msg: string, status = 403) =>
    new Response(msg + "\n", { status, headers: { "content-type": "text/plain" } });
  if (!env?.INSTALL_TOKEN) return deny("Downloads are not configured on this worker.", 503);
  if (!safeEqual(url.searchParams.get("t") ?? "", env.INSTALL_TOKEN)) {
    return deny("This download link is not valid. Get the current one from the install guide.");
  }
  const obj = await env.INSTALL?.get(INSTALL_KEY);
  if (!obj) return deny("No bundle has been published yet.", 404);
  return new Response(obj.body, {
    headers: {
      "content-type": "application/octet-stream",
      "content-disposition": 'attachment; filename="insightly-se-mcp-bridge.mcpb"',
      "cache-control": "no-store",
    },
  });
}

export const INSTALL_KEY = "bridge/insightly-se-mcp-bridge-latest.mcpb";
