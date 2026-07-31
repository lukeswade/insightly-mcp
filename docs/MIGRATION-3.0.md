# v3.0 — migrating to MCP SDK 2.x (spec 2026-07-28)

**Status:** scoped, not started. `main` stays on the pinned SDK 1.29.0 until this lands.

Everything below marked ✅ was verified by introspecting `mcp==2.0.0` locally on
2026-07-31, not read from docs. Items marked ❓ are open questions with a spike attached.

---

## 1. Why bother

The pin we shipped in v2.1.4 (`mcp==1.29.0`) is a **tourniquet, not a plan**:

- SDK 2.0.0 implements spec revision **2026-07-28** and **deleted `mcp.server.fastmcp`**,
  the module this server imports. Unpinned installs died on import; that's what the pin fixes.
- 1.x will not get the new capabilities, and the spec's deprecated features carry
  **12-month support windows**. Staying on 1.x is fine for months, not forever.
- The three things we actually want (Apps UI, cacheable metadata, Tasks for long jobs)
  exist **only** in 2.x.

So: migrate deliberately now, while 1.x still works and there is no outage pressure.

## 2. What actually changes in our code

Smaller than feared. `MCPServer` is close to a drop-in for `FastMCP`.

| Area | 1.x (today) | 2.x | Effort |
|---|---|---|---|
| Import | `from mcp.server.fastmcp import Context, FastMCP` | `from mcp.server import MCPServer` + `from mcp.server.context import Context` ✅ | 2 lines |
| Construction | `FastMCP("Insightly SE MCP (internal)")` | `MCPServer(name=..., version=SERVER_VERSION, title=..., instructions=...)` ✅ | 1 line (+ bonus: `version` finally reports our version in `serverInfo`) |
| Tool registration | `@mcp.tool()` | `@mcp.tool()` — **unchanged** ✅ | none |
| Transport | `mcp.run()` | `mcp.run()`, default `transport="stdio"` ✅ | none |
| **Elicitation** | `await ctx.elicit(message=, schema=)` | **`Context.elicit` no longer exists** ✅ — see §3 | the only real work |
| Everything else (httpx layer, pacing, pagination, all 16 tools' bodies) | — | untouched | none |

Roughly: **~6 lines of scaffolding plus one function (`_prompt`)**. The 500-odd lines of
Insightly logic are transport-agnostic and do not move.

### Deprecations: we are clean
Roots, Sampling, and Logging are deprecated — **we use none of them** (our stderr output is
not MCP logging). Legacy HTTP+SSE transport is deprecated — we're stdio, unaffected. Nothing
in the deprecation list requires work.

## 3. The one real risk: elicitation → MRTR

Our `connect` / `_prompt` flow is the only code that depends on server-initiated requests.
The spec replaces held-open-stream server requests with **Multi Round-Trip Requests**
(tool returns `resultType: "input_required"`; client retries with `inputResponses`).

Verified ✅:
- `Context.elicit` is **gone**. New `Context` exposes: `can_send_request`, `cancel_requested`,
  `connection`, `headers`, `lifespan`, `log`, `meta`, `notify`, `report_progress`,
  `send_raw_request`, `session_id`, `transport`.
- `mcp.server.elicitation` provides `elicit_with_validation(session, message, schema, related_request_id)`,
  plus **`elicit_url`** (URL-mode), and result types `AcceptedElicitation` / `DeclinedElicitation` /
  `CancelledElicitation` / `AcceptedUrlElicitation`.
- `Context` has no `.session`, but `ctx.connection` exposes `send_request`, `check_capability`,
  `client_params`, `has_standalone_channel`.

❓ **Spike 1 (half a day, do this first):** wire `_prompt` to elicitation in 2.x — determine
whether the session comes off `ctx.connection` or a `ServerRequestContext`, and confirm the
prompt still renders in Claude Code. **Do not start the rest of the migration until this works.**

❓ **Spike 2 (the interesting one):** our key prompt has never worked in **claude.ai web chat**,
which is why we inject the key via `env`. Because MRTR is plain request/response rather than a
held stream, web chat may now be able to satisfy it. Two things to try:
- feature-detect with `ctx.can_send_request` and fall back gracefully (this is strictly better
  than today's try/except around `ctx.elicit`);
- `elicit_url` mode as a browser-based key entry path.
If either works, the env-injection workaround becomes optional rather than mandatory.

## 4. The payoff (only worth doing after §3 succeeds)

**a. Apps extension — interactive UI inline.** `mcp.server.apps` provides `Apps`, `Extension`,
`ToolBinding`, `ResourceBinding`, `ResourceCsp`, `ResourcePermissions`, `Visibility`,
`APP_MIME_TYPE`, and — importantly — **`client_supports_apps(ctx)`** for graceful degradation ✅.
Highest-value targets, in order:
1. **Demo-env picker** — replaces pasting keys into chat when switching envs.
2. **`env_summary` as a dashboard** instead of a markdown table (the demo money shot).
3. **Secure key entry form** — an alternative solution to the web-chat gap in Spike 2.
4. Record tables / seeding confirmation.

**b. Cacheable results.** `CacheHint(ttl_ms, scope)` + `apply_cache_hint` ✅. Note the
cacheable method set is `{server/discover, resources/read, tools/list, prompts/list,
resources/list, resources/templates/list}` — **`tools/call` is not cacheable**. Consequence:
to cache `describe_object` (re-fetched constantly, near-static) we must **re-expose object
metadata as a resource**, not just a tool. That's a small refactor with a real latency win in
record-heavy envs.

**c. Tasks extension** (`io.modelcontextprotocol/tasks`, poll-based `tasks/get`). Our
`FETCH_ALL_HARD_CAP = 5000` and `create_records`' 50-per-call limit exist *because* everything
must finish inside one blocking call. Tasks removes that constraint: "export all 40k contacts"
becomes a polled job. Do this last — it changes tool contracts.

## 5. Hosting: the strategic unlock (separate project)

The spec's **stateless core** (no `initialize` handshake, no `Mcp-Session-Id`; per-request
identity in `_meta`) exists specifically so servers can run serverless/edge behind a plain load
balancer. `MCPServer` ships `run(transport="streamable-http")` and `streamable_http_app` ✅ —
the Cloudflare Workers plan we shelved is now the blessed path, on the *non*-deprecated transport.

Two consequences when/if we go remote:
- **Our `SESSION` global must go.** A module-level dict holding `api_key`/`pod` is exactly the
  anti-pattern the stateless core forbids. The spec's own guidance is the fix: have `connect`
  **mint an opaque env handle** that later calls pass back as an argument. Harmless locally
  (one process per user), fatal when shared.
- **Enterprise-managed auth** (Entra/Okta; admin authorizes once, users inherit via IdP groups)
  solves per-user authentication — previously the hardest part of the org-connector idea. The
  only remaining bespoke piece is mapping identity → Insightly API key, since Insightly is
  API-key based rather than OAuth.

**MCP Tunnels** (research preview) could expose an internally-hosted server to Claude without
public internet exposure — worth watching, too early to plan on.

## 6. Non-goals

- **Do not** publish to Claude's public connector directory (it's the gate for the new
  observability dashboard). This is an internal tool wired to demo-env credentials.
- **Do not** migrate and add features in one commit. §3 ships as v3.0 with zero behaviour
  change; §4 items ship individually after.
- **Do not** unpin dependencies again. v3.0 pins `mcp==2.x.y` exactly, same as today.

## 7. Sequence & acceptance criteria

1. **Spike 1** — elicitation works in 2.x on a scratch server. *Gate: prompt renders in Claude Code.*
2. **v3.0 port** — imports, `MCPServer`, `_prompt`. *Gate: all 9 unit tests pass; a live
   `env_summary` against `demo1` matches today's counts; `.mcpb` installs and starts on both
   platforms; `connection_info` reports 3.0.0.*
3. **Spike 2** — web-chat elicitation. *Gate: documented yes/no; if yes, drop `env` injection
   from the recommended install.*
4. **Apps UI** — env picker first, then `env_summary` dashboard. *Gate: degrades cleanly when
   `client_supports_apps` is false.*
5. **Cacheable metadata resource**, then **Tasks**. *Gate: measured latency improvement.*

Rollout is unchanged and non-breaking for colleagues: bump the `.mcpb`, they double-click.
Because v3.0 pins its own SDK, a v2.1.4 install keeps working untouched until they update.
