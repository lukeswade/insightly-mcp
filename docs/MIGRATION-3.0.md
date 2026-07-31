# v3.0 — migrating to MCP SDK 2.x (spec 2026-07-28)

**Status:** Spikes 1 & 2 done; port + caching + tasks + **Apps UI** + a full swagger/API-doc
audit implemented and validated on this branch (**46/46** checks — `spike/validate_v31.py`,
plus 10 unit tests), **not merged**. See also [API-AUDIT.md](API-AUDIT.md). `main` stays on the pinned
SDK 1.29.0 (v2.1.4) until the bundle is repinned and install-tested.

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
| Import | `from mcp.server.fastmcp import Context, FastMCP` | `from mcp.server import MCPServer` + `from mcp.server.mcpserver import Context` ✅ (**not** `mcp.server.context` — see §3) | 2 lines |
| Construction | `FastMCP("Insightly SE MCP (internal)")` | `MCPServer(name=..., version=SERVER_VERSION, title=..., instructions=...)` ✅ | 1 line (+ bonus: `version` finally reports our version in `serverInfo`) |
| Tool registration | `@mcp.tool()` | `@mcp.tool()` — **unchanged** ✅ | none |
| Transport | `mcp.run()` | `mcp.run()`, default `transport="stdio"` ✅ | none |
| **Elicitation** | `await ctx.elicit(message=, schema=)` | **unchanged** — same method, same signature, on the right Context ✅ | none |
| Everything else (httpx layer, pacing, pagination, all 16 tools' bodies) | — | untouched | none |

Roughly: **3 lines**. `_prompt()` and the 500-odd lines of Insightly logic don't move at all.
(Already applied on this branch — see §3.)

### Deprecations: we are clean
Roots, Sampling, and Logging are deprecated — **we use none of them** (our stderr output is
not MCP logging). Legacy HTTP+SSE transport is deprecated — we're stdio, unaffected. Nothing
in the deprecation list requires work.

## 3. Spike 1 — RESOLVED ✅ (elicitation works unchanged)

**Outcome: the risk was a false alarm caused by two same-named classes, and the port is done.**
Reproduce with `uv run --with 'mcp==2.0.0' --with 'pydantic<3' python spike/spike_client.py`.

SDK 2.x ships **two `Context` classes** and only one belongs in a tool signature:

| Class | `.elicit`? | Use |
|---|---|---|
| `mcp.server.context.Context` | **no** | middleware/dispatch context. Annotating a tool with it **crashes at registration** — pydantic can't generate a schema for it (this is what the first spike run hit). |
| `mcp.server.mcpserver.Context` | **yes** | the FastMCP-equivalent tool context. `elicit(message, schema)` — *identical signature to 1.x* — plus `.session`, `.elicit_url`, `.request_state`, `.input_responses` (the MRTR surface), `.protocol_version`. |

My first pass introspected the wrong one and concluded `Context.elicit` was gone. It isn't:
**`_prompt()`'s body needs no changes at all.** Verified end-to-end over stdio — the server
issued `elicitation/create`, the harness answered, and the reply validated into the pydantic
model (`r.data.api_key`, `r.data.pod`), exactly as in 1.x. The old `initialize` handshake also
still works; SDK 2.0.0 negotiated `2025-06-18` with an unmodified client.

**Port applied on this branch and verified against the live `demo1` env under `mcp==2.0.0`:**
- 3 lines changed: the two imports, `MCPServer(name=…, version=SERVER_VERSION)`, `SERVER_VERSION="3.0.0"`.
- `serverInfo` now reports **our** version (3.0.0) instead of the SDK's — free win from the `version` kwarg.
- All 10 unit tests pass; `env_summary` returns all 13 objects (Contacts 81 … Ticket 20, Product 9,
  zero failures); `list_records` pagination envelope and `total` intact; `tools/list` returns all tools.

### Testing gotcha worth remembering
An async tool returns `{"code":-32000,"message":"Connection closed"}` **if the harness closes
stdin right after writing.** Sync tools (`connection_info`) still answer, so it looks like a
selective failure. Keep stdin open until the response arrives — the 1.x scripts got away with it
by appending `sleep`. This is a harness artifact, not a server bug.

### Spike 2 — DONE (implementation), with one thing only Luke can confirm
Implemented capability-aware prompting in `_prompt()` via `_elicit_support(ctx)`:
- client declares **no** elicitation → return actionable guidance immediately
  (`set_api_key(...)` / `INSIGHTLY_API_KEY`) instead of firing a doomed request and
  catching the failure. Validated: a no-capability client gets the message, not a crash.
- client declares **form** (or plain) elicitation → prompt exactly as before. Validated.
- client declares **url only** → we deliberately refuse. `ctx.elicit_url` would mean hosting a
  key-entry page and pushing an API key through a web form we serve. Not worth it for credentials.

❓ **Unresolved and untestable from here:** whether claude.ai web chat now satisfies form
elicitation. The code is ready either way and degrades cleanly. To find out, ask web chat
*"connect to Insightly"* with no key injected: a prompt means we can drop the `env` workaround;
the guidance message means keep it.

## 4. Superseded — original risk analysis (kept for history)

> ⚠️ **This section is WRONG and kept only to record how the mistake happened.** Its
> "`Context.elicit` is gone" claim came from introspecting `mcp.server.context.Context`
> instead of `mcp.server.mcpserver.Context`. §3 is the correct account. Do not act on
> anything below.

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

## 5. The payoff — all three DONE ✅

**a. Apps extension — DONE ✅ (interactive env dashboard).** Built against the real ext-apps
contract (SEP-1865) rather than guesswork: JSON-RPC 2.0 over `postMessage`, with
`ui/initialize` → `ui/notifications/initialized` handshake, data arriving as
`ui/notifications/tool-result`, buttons calling `tools/call`, and "Ask in chat" using
`ui/message`. HTML lives in [`app_ui.py`](../app_ui.py).

What's in the UI:
- **Header**: env name, pod, remaining daily API quota, and a **Refresh** button.
- **Stat tiles** per object (tabular numerals, proportional bars) — each one **clickable**,
  opening a drill-in panel with the newest 25 records and a **Fields** button.
- **Fields view**: custom fields as a table (label, type, and the *accepted values* for
  dropdowns / the lookup target) plus standard fields as chips — this is the thing that stops
  the model inventing field names.
- **Explore row**: Custom objects · Pipelines · Stages · Users · Tags · Currencies.
- **Ask in chat** on every panel, which pushes a matching prompt into the conversation.
- Host theming adopted from `hostContext.theme` and `styles.variables`, skeleton loading
  states, keyboard focus rings, `prefers-reduced-motion` respected.

**Two design decisions worth keeping:**
- **App-only tools.** A host may only let an app call *its own* tools, so the buttons are backed
  by `app_records` / `app_fields` registered with `visibility=["app"]` — callable from the UI,
  invisible to the model, so they add nothing to its tool list. Each call still falls back to the
  public tool, then to asking in chat, so no button is ever a dead end.
- **Everything degrades.** No host bridge → a clear waiting state. Refused `tools/call` → the
  error plus an "Ask in chat" button. No Apps support at all → `env_dashboard` returns identical
  numbers with an explanatory note (SEP-2133 requires this).

**Still unverified:** the rendered appearance and the live button round-trip need a real
Apps-capable host. Everything checkable from outside one is verified — handshake/bridge code
present, tool `_meta.ui` correct, resource served as `text/html;profile=mcp-app`, app-only
visibility, and the backing tools returning real data (81 contacts, 72 custom fields on
Opportunities).

**b. Cacheable results — DONE ✅.** `MCPServer` takes a declarative `cache_hints=` mapping, so
no middleware is needed (the SDK marks middleware "provisional"; avoid it). Implemented:
- `describe_object`'s payload is now also the resource **`insightly://{object}/fields`**
  (`tools/call` is NOT cacheable, so the tool alone could never benefit). Both surfaces share
  one `_describe()`; the tool still prompts for auth, the resource can't prompt and says so.
- Hints: `tools/list` + `server/discover` 1 h, `resources/list`/`templates/list` 10 min,
  `resources/read` 5 min — long enough to kill repeat lookups, short enough that an SE who just
  added a custom field sees it next question. **`scope="private"` always**: custom fields differ
  per demo env, so a shared cache would leak one env's schema into another.

> ⚠️ **Cache hints only reach stateless clients.** They're a 2026-07-28 feature, and 2026-07-28
> *is* the stateless protocol — no `initialize`. A legacy handshake client negotiates 2025-11-25
> at best and sees no hints (verified both ways). Claude Desktop today still handshakes, so this
> is **forward-looking**: correct now, beneficial when Claude adopts the stateless transport.
> To exercise it, a request's `params._meta` needs `io.modelcontextprotocol/protocolVersion`,
> `…/clientInfo` **and** `…/clientCapabilities` — omit any and you get a clear -32602.

**c. Tasks — DONE ✅ (caps gone).** There is no batteries-included server-side task manager in
the SDK, so this is one in-process registry with **two surfaces over it**:
- **Tools that work with every client today:** `start_export` (whole object, no 5,000 cap —
  safety backstop 100,000), `start_bulk_create` (no 50-per-call cap), `task_status`,
  `task_result` (**paged** — a full export is far too big to return at once), `list_tasks`,
  `cancel_task`.
- **The spec surface** via `_TasksExtension(Extension)` with `identifier =
  "io.modelcontextprotocol/tasks"`, serving `tasks/get` / `tasks/result` / `tasks/cancel` /
  `tasks/list` from the same registry using real `mcp_types` params. Verified `tasks/get` returns
  a proper task record, so a task-aware client can poll natively.
Jobs report progress against a real total (from `X-Total-Count`), survive exceptions without
taking the server down, and stop at the next record boundary when cancelled. `list_records`
keeps `fetch_all` for small jobs; big work should use `start_export`.

## 6. Hosting: the strategic unlock (separate project)

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

## 7. Non-goals

- **Do not** publish to Claude's public connector directory (it's the gate for the new
  observability dashboard). This is an internal tool wired to demo-env credentials.
- **Do not** migrate and add features in one commit. §3 ships as v3.0 with zero behaviour
  change; §4 items ship individually after.
- **Do not** unpin dependencies again. v3.0 pins `mcp==2.x.y` exactly, same as today.

## 8. Sequence & acceptance criteria

1. ~~**Spike 1** — elicitation works in 2.x.~~ **DONE ✅** (see §3; `spike/`).
2. **v3.0 port** — **code done on this branch ✅** (10 unit tests pass; live `env_summary` matches;
   `connection_info` reports 3.0.0). *Remaining gate before merge: repin the bundle to `mcp==2.0.0`,
   rebuild the `.mcpb`, and confirm a real double-click install works on macOS **and** Windows.*
3. **Spike 2** — web-chat elicitation. *Gate: documented yes/no; if yes, drop `env` injection
   from the recommended install.*
4. **Apps UI** — env picker first, then `env_summary` dashboard. *Gate: degrades cleanly when
   `client_supports_apps` is false.*
5. **Cacheable metadata resource**, then **Tasks**. *Gate: measured latency improvement.*

Rollout is unchanged and non-breaking for colleagues: bump the `.mcpb`, they double-click.
Because v3.0 pins its own SDK, a v2.1.4 install keeps working untouched until they update.
