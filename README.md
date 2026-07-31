# Insightly SE MCP (internal) — interactive auth, full read/write

> **Sharing kit:** [OVERVIEW.md](OVERVIEW.md) — what it is & how it works ·
> [ONBOARDING.md](ONBOARDING.md) — 2-minute setup for colleagues ·
> [demo.html](demo.html) — animated visual demo (open in any browser)

Generic read/write plumbing for the [Insightly v3.1 REST API](https://api.na1.insightly.com/v3.1/).
You **don't edit any files or env vars to authenticate** — the first time you use a
tool, the server **prompts you for an API key** (MCP elicitation). You can reuse a
previously saved key or enter a new one, and optionally save it under a friendly
name for next time. Saving is handled by the server; it's never a manual file edit.

> Requires a client that supports MCP elicitation (Claude Code does). If yours
> doesn't, use the `set_api_key` fallback or inject `INSIGHTLY_API_KEY` in the env.

## How auth works
1. Call any tool (or `connect`). If no key is active for this session:
2. If `INSIGHTLY_API_KEY` is set in the env, it's used automatically (no prompt).
3. Otherwise you're **prompted**: pick a saved key by name, or enter a new
   `api_key` (+ `pod`, optional `friendly_name`, and a **Save?** checkbox).
4. The key lives in memory for the session. If you ticked Save, it's written to
   `~/.insightly-mcp/keys.json` (`chmod 600`) so you can reuse it later.

Switch orgs anytime with `connect` (re-prompts) — handy across your many demo envs.

## Tools
**Session:** `connect` (prompt), `set_api_key` (non-interactive fallback),
`connection_info`, `disconnect`, `list_saved`, `forget_saved`

**CRM (act on the connected org):**
| Tool | Does |
|------|------|
| `env_summary()` | One-call env overview: real record counts across the core objects — the perfect first call after connecting |
| `describe_object(object)` | Field reference: standard fields + compact custom fields (types, dropdown options, lookup targets). Call before writing to an unfamiliar object |
| `list_supported_objects` | Common object names |
| `list_records(object, …)` | List → `{items, returned, skip, top, has_more, next_skip}` envelope. `brief` defaults **true**; `top` default 100 (max 500). `count_total=true` adds the real `total`. `fetch_all=true` pages everything (to `max_records`, cap 5000). `order_by` sorts returned records client-side. `updated_after_utc` for incremental pulls. |
| `search_records(object, field_name, field_value, …)` | **Exact-match** single-field search (paged envelope) |
| `find_by_email(object, email)` | Convenience exact search on `EMAIL_ADDRESS` |
| `filter_records(object, contains, [field_name], …)` | **Contains** filter, client-side (scans up to `max_scan`) since the API is exact-match only. Omit `field_name` to match ANY field |
| `get_record(object, record_id)` | One record (shows field names) |
| `create_record(object, fields)` | Create |
| `create_records(object, records)` | **Batch** create (≤50/call, rate-paced) — demo seeding in one call |
| `update_record(object, record_id, fields)` | Partial update |
| `delete_record(object, record_id, confirm)` | **Permanent** delete — needs `confirm=true` |
| `add_note(parent_object, parent_id, title, body)` | Attach a note |
| `raw_request(method, path, query, body)` | Any other endpoint |

**Background jobs (v3.x, SDK 2.x branch)** — long work no longer has to finish inside one call:
| Tool | Does |
|------|------|
| `start_export(object, …)` | Export an entire object in the background — **no 5,000 cap** |
| `start_bulk_create(object, records)` | Create any number of records — **no 50-per-call cap** |
| `task_status(task_id)` | Progress: status, progress/total, summary |
| `task_result(task_id, top, skip)` | Read a finished job's records, paged |
| `list_tasks()` / `cancel_task(task_id)` | Inventory / stop a running job |

Also exposed as the cacheable resource **`insightly://{object}/fields`** (same data as
`describe_object`), plus the spec's `tasks/*` methods for task-aware clients.

Built for record-heavy envs: a pooled keep-alive connection (not a fresh client per
call), client-side rate pacing under the API's 10 req/s limit, and brief-by-default
listing so responses stay small. The API has **no server-side sort** — `order_by` is
applied client-side over what was fetched (pair with `fetch_all` for a global sort).

## Setup
No credentials needed at install — you'll be prompted on first use:

```bash
claude mcp add insightly \
  -- uv run --with 'mcp==1.29.0' --with 'httpx<1' --with 'pydantic<3' python /Users/luke.wade/Documents/Claude/insightly-mcp/insightly_mcp.py
```

Then ask Claude e.g. *"search Insightly contacts for jane@example.com"* — it'll pop
the key prompt the first time. Or call `connect` explicitly to authenticate / switch.

Optional env: `INSIGHTLY_API_KEY` + `INSIGHTLY_POD` (skip the prompt — for
orchestration), `INSIGHTLY_READONLY=1` (safe read-only), `INSIGHTLY_KEYS_FILE`
(saved-keys path).

## Safety
- **Full write access.** `delete_record` needs `confirm=true`; `INSIGHTLY_READONLY=1`
  blocks all writes.
- **Keys** live in memory by default; only saved to disk if you choose, and never
  passed as tool arguments (so they don't enter the model/transcript) — except the
  `set_api_key` fallback, where the key is necessarily in the call.
- **Rate limits:** ~10 req/sec + a daily cap by plan; the server paces itself under
  that limit and retries on 429. `top` defaults to 100 (max 500).

## Notes
- Object names are normalised automatically — case, singular/plural, and US
  `Organizations` all resolve to the API's real endpoints. (The API itself is
  inconsistent: `Ticket`, `Product`, `Quotation`, `Pricebook` are singular; the
  rest plural.)
- `update_record` sends partial fields; for objects outside the built-in primary-key
  map, include the `*_ID` field yourself.
- Requires Python 3.10+. `uv` runs it with deps inline, or `pip install -r requirements.txt`.
