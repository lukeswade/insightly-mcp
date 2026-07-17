#!/usr/bin/env python3
"""
Insightly SE MCP (internal) — Insightly CRM, full read/write.

On first use the server PROMPTS you for an Insightly API key (MCP elicitation) —
no env vars or config files to hand-edit. You may optionally save the key under a
friendly name so you can reuse it next time; saving is handled by the server, never
a manual file edit. If your orchestration injects INSIGHTLY_API_KEY in the env, that
is used automatically and you won't be prompted.

Generic backbone otherwise: full CRUD on every object + a raw_request escape hatch.
Built for record-heavy demo envs: a pooled HTTP connection, client-side rate pacing
(the API allows 10 req/s), paginated list results with a has_more/next_skip envelope,
an opt-in fetch_all, brief-by-default listing, and a client-side contains filter.
Keys live in process memory for the session (and, only if you choose to save, in
~/.insightly-mcp/keys.json, chmod 600) — never passed as tool arguments.

Optional env vars:
  INSIGHTLY_API_KEY    pre-inject a key (skips the prompt)
  INSIGHTLY_POD        pod for an injected key, default "na1"
  INSIGHTLY_READONLY   "1"/"true" disables ALL writes (safe mode)
  INSIGHTLY_KEYS_FILE  saved-keys path, default ~/.insightly-mcp/keys.json
"""
import os
import json
import time
import asyncio
import pathlib
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context, FastMCP

SERVER_VERSION = "2.1.2"
READONLY = os.environ.get("INSIGHTLY_READONLY", "").lower() in ("1", "true", "yes")
KEYS_FILE = os.environ.get("INSIGHTLY_KEYS_FILE", os.path.expanduser("~/.insightly-mcp/keys.json"))

# Insightly limits: max 500 records/request; 10 requests/second (all plans).
PAGE_MAX = 500
FETCH_ALL_HARD_CAP = 5000
_MIN_INTERVAL = 0.12  # ~8.3 req/s ceiling, comfortably under the API's 10/s

# Display name shown in Claude's UI. The registration key stays `insightly`
# (mcpServers key / `claude mcp add insightly`), so tool names are unchanged.
mcp = FastMCP("Insightly SE MCP (internal)")

# Live credentials for THIS connection (in memory only).
SESSION: dict = {"api_key": None, "pod": "na1", "name": None}

# Endpoint names exactly as the v3.1 API exposes them (verified against swagger).
# NOTE the API is inconsistent: most objects are plural, but Ticket, Product,
# Quotation and Pricebook are SINGULAR (their plural forms 405). _obj() aliases
# both forms so callers never have to care.
PK = {
    "Contacts": "CONTACT_ID", "Organisations": "ORGANISATION_ID", "Leads": "LEAD_ID",
    "Opportunities": "OPPORTUNITY_ID", "Projects": "PROJECT_ID", "Tasks": "TASK_ID",
    "Events": "EVENT_ID", "Notes": "NOTE_ID", "Product": "PRODUCT_ID",
    "Emails": "EMAIL_ID", "Quotation": "QUOTE_ID", "Milestones": "MILESTONE_ID",
    "Pricebook": "PRICEBOOK_ID", "Ticket": "TICKET_ID", "KnowledgeArticle": "ARTICLE_ID",
}
COMMON_OBJECTS = sorted(PK.keys()) + [
    "Pipelines", "PipelineStages", "Relationships", "Tags", "Teams",
    "LeadSources", "LeadStatuses", "Currencies", "CustomObjects",
    "TeamMembers", "Users", "ActivitySets",
]

# name (any case, singular or plural) → canonical endpoint. Built from the list
# above; e.g. "tickets" → "Ticket", "contact" → "Contacts". US spellings included.
_ALIASES: dict = {}
for _c in COMMON_OBJECTS:
    _ALIASES[_c.lower()] = _c
for _c in COMMON_OBJECTS:
    if _c.endswith("s"):
        _ALIASES.setdefault(_c[:-1].lower(), _c)     # singular → canonical plural
    else:
        _ALIASES.setdefault(_c.lower() + "s", _c)    # plural → canonical singular
_ALIASES["organizations"] = _ALIASES["organization"] = "Organisations"


# ----------------------------------------------------------------- saved-keys store
def _load_saved() -> dict:
    try:
        with open(KEYS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_saved(d: dict) -> None:
    p = pathlib.Path(KEYS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(d, f, indent=2)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass

def _mask(k: Optional[str]) -> str:
    return ("…" + k[-4:]) if k and len(k) >= 4 else ("set" if k else "")


# --------------------------------------------------------------------- elicitation
class KeyChoice(BaseModel):
    selection: str = Field(
        description="Type a saved key's name to reuse it, or 'new' to enter a new key.")

class NewKey(BaseModel):
    api_key: str = Field(description="Your Insightly API key (User Settings → API).")
    pod: str = Field(default="na1", description="Region pod: na1, eu1, ap1, …")
    friendly_name: str = Field(default="", description="Optional label to remember this key by.")
    save: bool = Field(default=False, description="Save this key locally for next time?")


async def _prompt(ctx: Context) -> Optional[str]:
    """Interactively obtain credentials. Returns an error string, or None on success."""
    saved = _load_saved()
    try:
        if saved:
            names = ", ".join(saved.keys())
            r = await ctx.elicit(
                message=f"Connect to Insightly. Saved keys: {names}. "
                        f"Type one of those names to reuse it, or 'new' to enter a new key.",
                schema=KeyChoice)
            if r.action != "accept":
                return "connection cancelled."
            sel = (r.data.selection or "").strip()
            if sel and sel.lower() != "new" and sel in saved:
                SESSION.update(api_key=saved[sel]["api_key"], pod=saved[sel].get("pod", "na1"), name=sel)
                return None
        r = await ctx.elicit(message="Enter your Insightly API key.", schema=NewKey)
        if r.action != "accept":
            return "connection cancelled."
        key = (r.data.api_key or "").strip()
        if not key:
            return "no API key entered."
        pod = (r.data.pod or "na1").strip()
        name = (r.data.friendly_name or "").strip()
        SESSION.update(api_key=key, pod=pod, name=name or "session")
        if r.data.save:
            label = name or "default"
            saved[label] = {"api_key": key, "pod": pod}
            _save_saved(saved)
            SESSION["name"] = label
        return None
    except Exception as e:
        return (f"couldn't prompt for a key (client may not support elicitation: {e}). "
                f"Use set_api_key(...), or set INSIGHTLY_API_KEY in the environment.")


async def _ensure(ctx: Context) -> Optional[str]:
    """Make sure we have a key: use the session, else an injected env key, else prompt."""
    if SESSION.get("api_key"):
        return None
    env_key = os.environ.get("INSIGHTLY_API_KEY")
    if env_key:
        SESSION.update(api_key=env_key, pod=(os.environ.get("INSIGHTLY_POD") or "na1"), name="env")
        return None
    return await _prompt(ctx)


# ----------------------------------------------------------------------------- http
def _safe(resp: Optional[httpx.Response]) -> Any:
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return (resp.text or "")[:500]

def _obj(name: str) -> str:
    """Normalise an object name to the API's canonical endpoint (case/plural-proof).
    Unknown names pass through unchanged so raw endpoints still work."""
    n = (name or "").strip().strip("/")
    return _ALIASES.get(n.lower(), n)

# Heavy fields Insightly's own `brief` mode fails to strip — full HTML/long free text
# that bloats list results (e.g. a KnowledgeArticle Body can be tens of KB). We drop
# them client-side when brief is requested.
_BRIEF_DROP = ("Body",)

def _brief_strip(data: Any) -> Any:
    """Drop heavy fields from each record in a brief list result; pass other shapes through."""
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                for k in _BRIEF_DROP:
                    item.pop(k, None)
    return data

def _apply_sort(items: Any, order_by: Optional[str]) -> Any:
    """CLIENT-SIDE sort of already-fetched records (the API has no sort param).
    order_by like 'DATE_UPDATED_UTC desc'. Records missing the field sort last.
    Returns a new list; never mutates the input (a failed mixed-type sort could
    otherwise leave it partially ordered)."""
    if not order_by or not isinstance(items, list):
        return items
    parts = str(order_by).split()
    field = parts[0]
    desc = len(parts) > 1 and parts[1].lower().startswith("desc")
    present = [r for r in items if isinstance(r, dict) and r.get(field) is not None]
    missing = [r for r in items if not (isinstance(r, dict) and r.get(field) is not None)]
    try:
        ordered = sorted(present, key=lambda r: r.get(field), reverse=desc)
    except TypeError:
        ordered = sorted(present, key=lambda r: str(r.get(field)), reverse=desc)
    return ordered + missing


def _record_contains(rec: Any, needle: str, field: Optional[str] = None) -> bool:
    """Case-insensitive contains-match on one field, or (field=None) on every
    scalar top-level value of the record."""
    if not isinstance(rec, dict):
        return False
    if field:
        return needle in str(rec.get(field, "")).lower()
    return any(needle in str(v).lower()
               for v in rec.values() if isinstance(v, (str, int, float)))


def _cf_compact(f: dict) -> dict:
    """Compact one /CustomFields entry to what the model needs to write records."""
    out = {"name": f.get("FIELD_NAME"), "label": f.get("FIELD_LABEL"),
           "type": f.get("FIELD_TYPE"), "editable": f.get("EDITABLE")}
    opts = [o.get("OPTION_VALUE") for o in (f.get("CUSTOM_FIELD_OPTIONS") or []) if o.get("OPTION_VALUE")]
    if opts:
        out["options"] = opts
    if f.get("JOIN_OBJECT"):
        out["links_to"] = f["JOIN_OBJECT"]
    return out


def _write_hint(res: Any, o: str) -> Any:
    """Attach a next-step hint to 4xx create/update failures (usually bad field names/values)."""
    if isinstance(res, dict) and str(res.get("error", "")).startswith("HTTP 4"):
        res.setdefault("hint", f"field names or option values may be wrong — call "
                               f"describe_object('{o}') to see valid standard + custom fields.")
    return res

def _page_envelope(items: list, skip: int, top: int) -> dict:
    """Wrap a page of records so the model can tell there's more and where to resume."""
    returned = len(items) if isinstance(items, list) else 0
    return {"items": items, "returned": returned, "skip": skip, "top": top,
            "has_more": returned == top, "next_skip": skip + returned}


# One pooled client per (pod, key) so we reuse the TLS/keep-alive connection across
# calls instead of paying a fresh handshake every request (the old behaviour).
_CLIENT: Optional[httpx.AsyncClient] = None
_CLIENT_ID: tuple = (None, None)

async def _client() -> httpx.AsyncClient:
    global _CLIENT, _CLIENT_ID
    key = SESSION.get("api_key")
    pod = SESSION.get("pod") or "na1"
    ident = (pod, key)
    if _CLIENT is None or _CLIENT_ID != ident:
        if _CLIENT is not None:
            try:
                await _CLIENT.aclose()
            except Exception:
                pass
        _CLIENT = httpx.AsyncClient(
            base_url=f"https://api.{pod}.insightly.com/v3.1",
            auth=(key or "", ""), timeout=30.0,
            headers={"Accept": "application/json"},
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20))
        _CLIENT_ID = ident
    return _CLIENT

# Simple client-side pacer so bulk paging never trips the API's 10 req/s limit.
_RATE_LOCK = asyncio.Lock()
_NEXT_OK = 0.0

async def _pace() -> None:
    global _NEXT_OK
    async with _RATE_LOCK:
        now = time.monotonic()
        wait = _NEXT_OK - now
        if wait > 0:
            await asyncio.sleep(wait)
        _NEXT_OK = max(now, _NEXT_OK) + _MIN_INTERVAL


async def _request(method: str, path: str, params: Optional[dict] = None,
                   json_body: Any = None, want_headers: bool = False) -> Any:
    """Perform one API call. Returns the parsed body, or (body, headers_lower) when
    want_headers=True (headers keyed lowercase). On failure returns an {'error': ...} body."""
    method = method.upper()
    key = SESSION.get("api_key")

    def wrap(body: Any, headers: Optional[dict] = None) -> Any:
        return (body, headers or {}) if want_headers else body

    if not key:
        return wrap({"error": "not connected — run connect() (you'll be prompted) or set_api_key(...)."})
    if READONLY and method != "GET":
        return wrap({"error": "read-only mode is on (INSIGHTLY_READONLY); writes are disabled."})

    client = await _client()
    last: Optional[httpx.Response] = None
    for attempt in range(4):
        await _pace()
        try:
            r = await client.request(method, path, params=params, json=json_body)
        except Exception as e:
            return wrap({"error": f"request failed: {e}"})
        hdr = {k.lower(): v for k, v in r.headers.items()}
        if r.status_code == 429:
            last = r
            await asyncio.sleep(min(float(r.headers.get("Retry-After", 1.5)) * (attempt + 1), 12.0))
            continue
        if r.status_code == 401:
            SESSION["api_key"] = None  # force a fresh connect; the key was rejected
            return wrap({"error": "unauthorized (401) — the API key was rejected; "
                                  "reconnect() or set_api_key(...) with a valid key/pod."}, hdr)
        if not r.is_success:
            return wrap({"error": f"HTTP {r.status_code}", "body": _safe(r)}, hdr)
        if r.status_code == 204 or not r.content:
            return wrap({"ok": True}, hdr)
        return wrap(r.json(), hdr)
    return wrap({"error": "rate limited (429) after retries — the API allows ~10 req/s; slow down bulk asks.",
                 "body": _safe(last)}, {k.lower(): v for k, v in last.headers.items()} if last else {})


async def _fetch_all(o: str, brief: bool = True, updated_after_utc: Optional[str] = None,
                     max_records: int = 1000) -> dict:
    """Page through an object up to max_records (hard cap FETCH_ALL_HARD_CAP), rate-paced."""
    cap = min(max(int(max_records), 1), FETCH_ALL_HARD_CAP)
    out: list = []
    skip = 0
    truncated = False
    while len(out) < cap:
        page = min(PAGE_MAX, cap - len(out))
        params = {"top": page, "skip": skip, "brief": str(brief).lower()}
        if updated_after_utc:
            params["updated_after_utc"] = updated_after_utc
        body = await _request("GET", f"/{o}", params=params)
        if isinstance(body, dict) and body.get("error"):
            return {"items": _brief_strip(out) if brief else out, "total_fetched": len(out),
                    "truncated": True, "partial": True, "error": body["error"]}
        batch = body if isinstance(body, list) else []
        out.extend(batch)
        if len(batch) < page:
            break  # short page → reached the end
        skip += len(batch)
    else:
        truncated = True  # exited on the cap; more may remain
    if brief:
        _brief_strip(out)
    return {"items": out, "total_fetched": len(out), "truncated": truncated}


# -------------------------------------------------------------------- session tools
@mcp.tool()
async def connect(ctx: Context) -> dict:
    """Connect to Insightly. Prompts you for an API key (reuse a saved one or enter a
    new one, optionally saving it). Use this to (re)authenticate or switch orgs."""
    err = await _prompt(ctx)
    if err:
        return {"connected": False, "error": err}
    chk = await _request("GET", "/Contacts", params={"top": 1, "brief": "true"})
    ok = not (isinstance(chk, dict) and chk.get("error"))
    return {"connected": ok, "as": SESSION.get("name"), "pod": SESSION.get("pod"),
            **({} if ok else {"detail": chk})}

@mcp.tool()
def set_api_key(api_key: str, pod: str = "na1", friendly_name: str = "", save: bool = False) -> dict:
    """Non-interactive fallback if your client can't show prompts: set the key
    directly (NOTE: the key appears in this tool call). Optionally save under a name."""
    SESSION.update(api_key=api_key.strip(), pod=(pod or "na1").strip(), name=(friendly_name.strip() or "session"))
    if save and friendly_name.strip():
        s = _load_saved(); s[friendly_name.strip()] = {"api_key": api_key.strip(), "pod": (pod or "na1").strip()}
        _save_saved(s); SESSION["name"] = friendly_name.strip()
    return {"connected": True, "as": SESSION.get("name"), "pod": SESSION.get("pod")}

@mcp.tool()
def connection_info() -> dict:
    """Show whether this connection is authenticated, which org/pod it points at, and the server version."""
    env_key = os.environ.get("INSIGHTLY_API_KEY")
    key = SESSION.get("api_key") or env_key
    name = SESSION.get("name") or ("env" if env_key else None)
    pod = SESSION.get("pod") or os.environ.get("INSIGHTLY_POD") or "na1"
    return {"connected": bool(key), "as": name, "pod": pod,
            "read_only": READONLY, "version": SERVER_VERSION}

@mcp.tool()
def disconnect() -> dict:
    """Clear the in-memory key for this session (does not delete saved keys)."""
    SESSION.update(api_key=None, name=None)
    return {"ok": True}

@mcp.tool()
def list_saved() -> dict:
    """List locally-saved keys (names + pod + masked key). Keys are never shown in full."""
    s = _load_saved()
    return {"active": SESSION.get("name"),
            "saved": [{"name": n, "pod": v.get("pod", "na1"), "key": _mask(v.get("api_key"))}
                      for n, v in s.items()]}

@mcp.tool()
def forget_saved(name: str) -> dict:
    """Delete a saved key by name."""
    s = _load_saved(); existed = s.pop(name, None) is not None; _save_saved(s)
    return {"ok": True, "removed": name if existed else None}


# ------------------------------------------------------------------------ CRM tools
@mcp.tool()
def list_supported_objects() -> dict:
    """Common Insightly object endpoint names usable as `object`. Case and plural are
    normalised automatically (the API itself is inconsistent: Ticket/Product/Quotation/
    Pricebook are singular, the rest plural; 'Organizations' US spelling also accepted).
    Anything else is reachable via raw_request."""
    return {"objects": COMMON_OBJECTS, "read_only": READONLY, "version": SERVER_VERSION}

@mcp.tool()
async def list_records(object: str, ctx: Context, top: int = 100, skip: int = 0, brief: bool = True,
                       order_by: Optional[str] = None, updated_after_utc: Optional[str] = None,
                       count_total: bool = False, fetch_all: bool = False, max_records: int = 1000) -> Any:
    """List records for an object (e.g. 'Contacts'). Returns a paginated envelope:
    {items, returned, skip, top, has_more, next_skip[, total]}.

    - brief defaults True (top-level fields only — far smaller). Pass brief=false for
      every field incl. linked/custom fields.
    - Paging: default top=100 (max 500). If has_more is true, call again with the
      returned next_skip — OR pass fetch_all=true to get everything at once.
    - fetch_all=true pages through the whole object up to max_records (default 1000,
      hard cap 5000), rate-paced under the API limit; returns
      {items, total_fetched, truncated}. truncated=true means the cap was hit and more remain.
    - count_total=true adds the real `total` (from Insightly's X-Total-Count header).
    - updated_after_utc like '2026-01-01T00:00:00Z' for incremental pulls.
    - order_by like 'DATE_UPDATED_UTC desc' sorts the RETURNED records CLIENT-SIDE
      (the API has no sort param); combine with fetch_all for a global sort.

    For finding specific records prefer search_records (exact) or filter_records (contains)."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    if fetch_all:
        res = await _fetch_all(o, brief=brief, updated_after_utc=updated_after_utc, max_records=max_records)
        if order_by and isinstance(res.get("items"), list):
            res["items"] = _apply_sort(res["items"], order_by)
        return res
    page = min(max(top, 1), PAGE_MAX)
    params: dict = {"top": page, "skip": max(skip, 0), "brief": str(brief).lower()}
    if updated_after_utc:
        params["updated_after_utc"] = updated_after_utc
    if count_total:
        params["count_total"] = "true"
    body, headers = await _request("GET", f"/{o}", params=params, want_headers=True)
    if isinstance(body, dict) and body.get("error"):
        if str(body.get("error", "")).startswith("HTTP 4"):
            body["hint"] = "check the object name via list_supported_objects; some objects aren't listable via GET."
        return body
    items = body if isinstance(body, list) else []
    if brief:
        _brief_strip(items)
    if order_by:
        items = _apply_sort(items, order_by)
    env = _page_envelope(items, max(skip, 0), page)
    if count_total:
        tot = headers.get("x-total-count")
        if tot is not None:
            try:
                env["total"] = int(tot)
            except Exception:
                env["total"] = tot
    return env

@mcp.tool()
async def search_records(object: str, field_name: str, field_value: str, ctx: Context,
                         top: int = 20, skip: int = 0) -> Any:
    """EXACT-match search on a single field (the API does not do partial match here), e.g.
    search_records('Contacts', 'EMAIL_ADDRESS', 'jane@example.com'). For substring matching
    use filter_records. Returns a paginated envelope like list_records."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    page = min(max(top, 1), PAGE_MAX)
    body = await _request("GET", f"/{_obj(object)}/Search",
                          params={"field_name": field_name, "field_value": field_value,
                                  "top": page, "skip": max(skip, 0), "brief": "true"})
    if isinstance(body, dict) and body.get("error"):
        return body
    items = _brief_strip(body if isinstance(body, list) else [])
    return _page_envelope(items, max(skip, 0), page)

@mcp.tool()
async def find_by_email(object: str, email: str, ctx: Context) -> Any:
    """Convenience: find records by exact email address (e.g. Contacts, Leads).
    Shortcut for search_records on EMAIL_ADDRESS."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    body = await _request("GET", f"/{_obj(object)}/Search",
                          params={"field_name": "EMAIL_ADDRESS", "field_value": email, "top": 20})
    if isinstance(body, dict) and body.get("error"):
        return body
    return _page_envelope(body if isinstance(body, list) else [], 0, 20)

@mcp.tool()
async def filter_records(object: str, contains: str, ctx: Context, field_name: Optional[str] = None,
                         brief: bool = True, max_scan: int = 1000) -> Any:
    """CONTAINS filter, done CLIENT-SIDE because Insightly's search is exact-match only.
    Scans up to max_scan records (default 1000, hard cap 5000, rate-paced) and returns
    those matching `contains` (case-insensitive) — in `field_name` if given, otherwise
    in ANY top-level field ("find anything mentioning X"). For exact match prefer
    search_records. Returns {items, matched, scanned, truncated}."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    res = await _fetch_all(_obj(object), brief=brief, max_records=max_scan)
    if res.get("error") and not res.get("items"):
        return res
    needle = (contains or "").lower()
    hits = [r for r in res["items"] if _record_contains(r, needle, field_name)]
    return {"items": hits, "matched": len(hits), "scanned": res.get("total_fetched", 0),
            "truncated": res.get("truncated", False)}

_SUMMARY_OBJECTS = ["Contacts", "Organisations", "Leads", "Opportunities", "Projects",
                    "Tasks", "Events", "Notes", "Emails", "Ticket", "Product",
                    "KnowledgeArticle", "Users"]

@mcp.tool()
async def env_summary(ctx: Context) -> Any:
    """One-call overview of the connected environment: real record counts for the core
    objects (Contacts, Organisations, Leads, Opportunities, Projects, Tasks, Events,
    Notes, Emails, Tickets, Products, KnowledgeArticle, Users). The perfect first call
    after connecting — \"what's in this env?\"."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    counts: dict = {}
    failed: dict = {}
    for o in _SUMMARY_OBJECTS:
        body, headers = await _request("GET", f"/{o}",
                                       params={"top": 1, "brief": "true", "count_total": "true"},
                                       want_headers=True)
        if isinstance(body, dict) and body.get("error"):
            failed[o] = body["error"]
            continue
        tot = headers.get("x-total-count")
        try:
            counts[o] = int(tot) if tot is not None else None
        except Exception:
            counts[o] = tot
    out = {"connected_as": SESSION.get("name"), "pod": SESSION.get("pod"),
           "version": SERVER_VERSION, "counts": counts}
    if failed:
        out["failed"] = failed
    return out

@mcp.tool()
async def describe_object(object: str, ctx: Context) -> Any:
    """Field reference for an object — call this BEFORE creating/updating records you
    haven't touched yet. Returns `standard_fields` (from a sample record) and compact
    `custom_fields` (name, label, type, dropdown options, lookup target) so payloads
    use real field names and valid option values. Custom values go in CUSTOMFIELDS:
    [{"FIELD_NAME": "...__c", "FIELD_VALUE": ...}]."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    out: dict = {"object": o, "pk": PK.get(o)}
    sample = await _request("GET", f"/{o}", params={"top": 1, "brief": "false"})
    if isinstance(sample, list) and sample and isinstance(sample[0], dict):
        out["standard_fields"] = [k for k in sample[0].keys() if k not in ("CUSTOMFIELDS", "ETag")]
    elif isinstance(sample, dict) and sample.get("error"):
        out["standard_fields_error"] = sample["error"]
    else:
        out["standard_fields"] = []
        out["note"] = "no records yet — standard fields unavailable from a sample."
    cfs = await _request("GET", f"/CustomFields/{o}")
    if isinstance(cfs, list):
        out["custom_fields"] = [_cf_compact(f) for f in cfs if isinstance(f, dict)]
    else:
        out["custom_fields"] = []
    return out

@mcp.tool()
async def create_records(object: str, records: list, ctx: Context) -> Any:
    """Batch-create up to 50 records in one call (rate-paced) — ideal for demo seeding,
    e.g. \"create 20 sample contacts\". `records` is a list of `fields` dicts as in
    create_record. Continues past individual failures. Returns
    {created, failed, ids, errors: [{index, error}]}."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    if not isinstance(records, list) or not records:
        return {"error": "pass a non-empty list of field dicts."}
    if len(records) > 50:
        return {"error": f"max 50 records per call (got {len(records)}) — split into batches."}
    o = _obj(object)
    pk = PK.get(o)
    ids: list = []
    errors: list = []
    for i, fields in enumerate(records):
        if not isinstance(fields, dict):
            errors.append({"index": i, "error": "not a field dict"})
            continue
        res = _write_hint(await _request("POST", f"/{o}", json_body=fields), o)
        if isinstance(res, dict) and res.get("error"):
            errors.append({"index": i, "error": res.get("error"), **({"body": res["body"]} if res.get("body") else {})})
        else:
            ids.append(res.get(pk) if pk and isinstance(res, dict) else None)
    return {"created": len(ids), "failed": len(errors), "ids": ids, "errors": errors}

@mcp.tool()
async def get_record(object: str, record_id: int, ctx: Context) -> Any:
    """Fetch one record by id, e.g. get_record('Contacts', 12345). Shows field names."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    return await _request("GET", f"/{_obj(object)}/{record_id}")

@mcp.tool()
async def create_record(object: str, fields: dict, ctx: Context) -> Any:
    """Create a record. `fields` = API field names → values, e.g.
    create_record('Contacts', {'FIRST_NAME':'Jane','LAST_NAME':'Doe'})."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    return _write_hint(await _request("POST", f"/{o}", json_body=fields), o)

@mcp.tool()
async def update_record(object: str, record_id: int, fields: dict, ctx: Context) -> Any:
    """Partial update (send only changed fields). PK is filled in for common objects;
    otherwise include the *_ID field in `fields`. e.g.
    update_record('Contacts', 12345, {'PHONE':'555-1212'})."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    body = dict(fields)
    pk = PK.get(o)
    if pk:
        body.setdefault(pk, record_id)
    elif not any(k.upper().endswith("_ID") for k in body):
        return {"error": f"unknown primary key for '{o}' — include its *_ID field in `fields`."}
    return _write_hint(await _request("PUT", f"/{o}", json_body=body), o)

@mcp.tool()
async def delete_record(object: str, record_id: int, ctx: Context, confirm: bool = False) -> Any:
    """PERMANENTLY delete a record. Must pass confirm=true."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    if not confirm:
        return {"error": "destructive — pass confirm=true to actually delete this record."}
    return await _request("DELETE", f"/{_obj(object)}/{record_id}")

@mcp.tool()
async def add_note(parent_object: str, parent_id: int, title: str, ctx: Context, body: str = "") -> Any:
    """Attach a note to a record (Contacts, Organisations, Opportunities, Projects, Leads)."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    return await _request("POST", f"/{_obj(parent_object)}/{parent_id}/Notes",
                          json_body={"TITLE": title, "BODY": body})

@mcp.tool()
async def raw_request(method: str, path: str, ctx: Context,
                      query: Optional[dict] = None, body: Optional[dict] = None) -> Any:
    """Escape hatch — any endpoint. `path` is relative to the v3.1 base, e.g.
    '/Opportunities/123/Tasks'. Honors read-only mode for non-GET."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    return await _request(method, "/" + path.lstrip("/"), params=query, json_body=body)


if __name__ == "__main__":
    mcp.run()
