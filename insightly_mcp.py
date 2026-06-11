#!/usr/bin/env python3
"""
Insightly CRM MCP server — interactive auth, full read/write.

On first use the server PROMPTS you for an Insightly API key (MCP elicitation) —
no env vars or config files to hand-edit. You may optionally save the key under a
friendly name so you can reuse it next time; saving is handled by the server, never
a manual file edit. If your orchestration injects INSIGHTLY_API_KEY in the env, that
is used automatically and you won't be prompted.

Generic backbone otherwise: full CRUD on every object + a raw_request escape hatch,
429 backoff. Keys live in process memory for the session (and, only if you choose to
save, in ~/.insightly-mcp/keys.json, chmod 600) — never passed as tool arguments.

Optional env vars:
  INSIGHTLY_API_KEY    pre-inject a key (skips the prompt)
  INSIGHTLY_POD        pod for an injected key, default "na1"
  INSIGHTLY_READONLY   "1"/"true" disables ALL writes (safe mode)
  INSIGHTLY_KEYS_FILE  saved-keys path, default ~/.insightly-mcp/keys.json
"""
import os
import json
import asyncio
import pathlib
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context, FastMCP

READONLY = os.environ.get("INSIGHTLY_READONLY", "").lower() in ("1", "true", "yes")
KEYS_FILE = os.environ.get("INSIGHTLY_KEYS_FILE", os.path.expanduser("~/.insightly-mcp/keys.json"))

mcp = FastMCP("insightly")

# Live credentials for THIS connection (in memory only).
SESSION: dict = {"api_key": None, "pod": "na1", "name": None}

PK = {
    "Contacts": "CONTACT_ID", "Organisations": "ORGANISATION_ID", "Leads": "LEAD_ID",
    "Opportunities": "OPPORTUNITY_ID", "Projects": "PROJECT_ID", "Tasks": "TASK_ID",
    "Events": "EVENT_ID", "Notes": "NOTE_ID", "Products": "PRODUCT_ID",
    "Emails": "EMAIL_ID", "Quotations": "QUOTATION_ID", "Milestones": "MILESTONE_ID",
    "Pricebooks": "PRICEBOOK_ID", "Tickets": "TICKET_ID", "Knowledge": "KNOWLEDGE_ARTICLE_ID",
}
COMMON_OBJECTS = sorted(PK.keys()) + [
    "Pipelines", "PipelineStages", "Relationships", "Tags", "Categories",
    "Currencies", "CustomFields", "CustomObjects", "TeamMembers", "Users", "ActivitySets",
]


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
    n = (name or "").strip().strip("/")
    return "Organisations" if n.lower() in ("organizations", "organization") else n

async def _request(method: str, path: str, params: Optional[dict] = None, json_body: Any = None) -> Any:
    method = method.upper()
    key = SESSION.get("api_key")
    pod = SESSION.get("pod") or "na1"
    if not key:
        return {"error": "not connected — run connect() (you'll be prompted) or set_api_key(...)."}
    if READONLY and method != "GET":
        return {"error": "read-only mode is on (INSIGHTLY_READONLY); writes are disabled."}
    base = f"https://api.{pod}.insightly.com/v3.1"
    last: Optional[httpx.Response] = None
    async with httpx.AsyncClient(base_url=base, auth=(key, ""), timeout=30.0,
                                 headers={"Accept": "application/json"}) as c:
        for attempt in range(4):
            try:
                r = await c.request(method, path, params=params, json=json_body)
            except Exception as e:
                return {"error": f"request failed: {e}"}
            if r.status_code == 429:
                last = r
                await asyncio.sleep(min(float(r.headers.get("Retry-After", 1.5)) * (attempt + 1), 12.0))
                continue
            if r.status_code == 401:
                return {"error": "unauthorized (401) — the API key was rejected."}
            if not r.is_success:
                return {"error": f"HTTP {r.status_code}", "body": _safe(r)}
            if r.status_code == 204 or not r.content:
                return {"ok": True}
            return r.json()
    return {"error": "rate limited (429) after retries", "body": _safe(last)}


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
    """Show whether this connection is authenticated and which org/pod it points at."""
    return {"connected": bool(SESSION.get("api_key")), "as": SESSION.get("name"),
            "pod": SESSION.get("pod"), "read_only": READONLY}

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
    """Common Insightly object endpoint names usable as `object`. Note British spelling
    'Organisations'. Anything else is reachable via raw_request."""
    return {"objects": COMMON_OBJECTS, "read_only": READONLY}

@mcp.tool()
async def list_records(object: str, ctx: Context, top: int = 20, skip: int = 0, brief: bool = False,
                       order_by: Optional[str] = None, updated_after_utc: Optional[str] = None,
                       count_total: bool = False) -> Any:
    """List records for an object (e.g. 'Contacts'). top<=500 (default 20), skip for
    paging, order_by like 'DATE_UPDATED_UTC desc', updated_after_utc like
    '2026-01-01T00:00:00Z'. Prompts for a key on first use."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    params: dict = {"top": min(max(top, 1), 500), "skip": max(skip, 0),
                    "brief": str(brief).lower(), "count_total": str(count_total).lower()}
    if order_by:
        params["order_by"] = order_by
    if updated_after_utc:
        params["updated_after_utc"] = updated_after_utc
    return await _request("GET", f"/{_obj(object)}", params=params)

@mcp.tool()
async def search_records(object: str, field_name: str, field_value: str, ctx: Context,
                         top: int = 20, skip: int = 0) -> Any:
    """Exact-match field search, e.g.
    search_records('Contacts', 'EMAIL_ADDRESS', 'jane@example.com')."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    return await _request("GET", f"/{_obj(object)}/Search",
                          params={"field_name": field_name, "field_value": field_value,
                                  "top": min(max(top, 1), 500), "skip": max(skip, 0)})

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
    return await _request("POST", f"/{_obj(object)}", json_body=fields)

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
    return await _request("PUT", f"/{o}", json_body=body)

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
