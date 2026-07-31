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
import uuid
import asyncio
import pathlib
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import mcp_types as types
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.caching import CacheHint
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import Context  # NOT mcp.server.context — that one has no .elicit

from app_ui import ENV_DASHBOARD_HTML

SERVER_VERSION = "3.1.1"
READONLY = os.environ.get("INSIGHTLY_READONLY", "").lower() in ("1", "true", "yes")
KEYS_FILE = os.environ.get("INSIGHTLY_KEYS_FILE", os.path.expanduser("~/.insightly-mcp/keys.json"))

# Insightly limits: max 500 records/request; 10 requests/second (all plans).
PAGE_MAX = 500
FETCH_ALL_HARD_CAP = 5000
_MIN_INTERVAL = 0.12  # ~8.3 req/s ceiling, comfortably under the API's 10/s

# Daily quota, learned from every response's X-RateLimit-* headers (verified live:
# limit 100000 / remaining 99986 on a demo pod). The per-second cap is handled by the
# pacer; the DAILY cap can't be waited out, so a 429 with remaining == 0 must fail fast
# instead of burning retries.
RATE: dict = {"limit": None, "remaining": None, "seen_at": None}


# ------------------------------------------------------------------ background tasks
# Long jobs (full-object exports, bulk creates) used to be capped so they could
# finish inside one blocking tool call. They now run as background tasks the client
# polls, so the caps are gone. Two surfaces over ONE registry:
#   * plain tools (task_status/task_result/…) — work with every client today;
#   * the spec's `tasks/*` methods via _TasksExtension — for task-aware clients.
TASK_TTL_S = 3600           # forget finished tasks after an hour
TASK_POLL_MS = 1000         # advertised poll interval
EXPORT_SAFETY_CAP = 100_000  # backstop so a runaway export can't eat all memory

_TASKS: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_prune() -> None:
    cutoff = time.time() - TASK_TTL_S
    for tid in [t for t, v in _TASKS.items() if v["done_at"] and v["done_at"] < cutoff]:
        _TASKS.pop(tid, None)


def _task_new(kind: str, detail: str) -> dict:
    _task_prune()
    tid = uuid.uuid4().hex[:12]
    rec = {"task_id": tid, "status": "working", "status_message": f"{kind}: starting",
           "created_at": _now(), "last_updated_at": _now(), "done_at": None,
           "kind": kind, "detail": detail, "progress": 0, "total": None,
           "items": None, "summary": None, "error": None,
           "cancel": False, "_runner": None}
    _TASKS[tid] = rec
    return rec


def _task_touch(rec: dict, msg: Optional[str] = None, **kw: Any) -> None:
    rec.update(kw)
    if msg:
        rec["status_message"] = msg
    rec["last_updated_at"] = _now()


def _task_finish(rec: dict, status: str, msg: str) -> None:
    _task_touch(rec, msg, status=status)
    rec["done_at"] = time.time()


def _task_public(rec: dict, include_result: bool = False) -> dict:
    """The client-facing view (never leaks internals like the runner handle)."""
    out = {k: rec[k] for k in ("task_id", "status", "status_message", "created_at",
                               "last_updated_at", "kind", "detail", "progress", "total")}
    out["poll_interval_ms"] = TASK_POLL_MS
    if rec["error"]:
        out["error"] = rec["error"]
    if rec["summary"]:
        out["summary"] = rec["summary"]
    if include_result and rec["items"] is not None:
        out["result_count"] = len(rec["items"])
    return out


async def _job_export(rec: dict, o: str, brief: bool, updated_after_utc: Optional[str],
                      max_records: int) -> None:
    """Page through an entire object with no per-call cap, reporting progress."""
    cap = min(max(int(max_records), 1), EXPORT_SAFETY_CAP)
    out: list = []
    skip = 0
    # Best-effort total up front so progress means something.
    _, headers = await _request("GET", f"/{o}", params={"top": 1, "brief": "true",
                                                        "count_total": "true"}, want_headers=True)
    try:
        rec["total"] = int(headers.get("x-total-count")) if headers.get("x-total-count") else None
    except Exception:
        rec["total"] = None
    while len(out) < cap:
        if rec["cancel"]:
            _task_finish(rec, "cancelled", f"cancelled after {len(out)} records")
            rec["items"] = out
            return
        page = min(PAGE_MAX, cap - len(out))
        params: dict = {"top": page, "skip": skip, "brief": str(brief).lower()}
        if updated_after_utc:
            params["updated_after_utc"] = updated_after_utc
        body = await _request("GET", f"/{o}", params=params)
        if isinstance(body, dict) and body.get("error"):
            rec["items"] = out
            rec["error"] = body["error"]
            _task_finish(rec, "failed", f"API error after {len(out)} records")
            return
        batch = body if isinstance(body, list) else []
        out.extend(batch)
        _task_touch(rec, f"fetched {len(out)}" + (f" of {rec['total']}" if rec["total"] else ""),
                    progress=len(out))
        if len(batch) < page:
            break
        skip += len(batch)
    if brief:
        _brief_strip(out)
    rec["items"] = out
    truncated = len(out) >= cap
    rec["summary"] = {"object": o, "fetched": len(out), "truncated": truncated}
    _task_finish(rec, "completed", f"exported {len(out)} records"
                 + (" (hit safety cap)" if truncated else ""))


async def _job_bulk_create(rec: dict, o: str, records: list) -> None:
    """Create any number of records (no 50-per-call cap), reporting progress."""
    pk = PK.get(o)
    ids: list = []
    errors: list = []
    rec["total"] = len(records)
    for i, fields in enumerate(records):
        if rec["cancel"]:
            _task_finish(rec, "cancelled", f"cancelled after {len(ids)} created")
            break
        if not isinstance(fields, dict):
            errors.append({"index": i, "error": "not a field dict"})
            continue
        res = _write_hint(await _request("POST", f"/{o}", json_body=fields), o)
        if isinstance(res, dict) and res.get("error"):
            errors.append({"index": i, "error": res.get("error")})
        else:
            ids.append(res.get(pk) if pk and isinstance(res, dict) else None)
        _task_touch(rec, f"created {len(ids)} of {len(records)}", progress=i + 1)
    rec["items"] = ids
    rec["summary"] = {"object": o, "created": len(ids), "failed": len(errors), "errors": errors[:20]}
    if rec["status"] != "cancelled":
        _task_finish(rec, "completed" if not errors else "completed",
                     f"created {len(ids)}, {len(errors)} failed")


def _spawn(rec: dict, coro: Any) -> None:
    """Run a job in the background, keeping a reference so it isn't garbage collected."""
    async def _guard() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            _task_finish(rec, "cancelled", "cancelled")
            raise
        except Exception as e:  # never let a job take the server down
            rec["error"] = f"{type(e).__name__}: {e}"
            _task_finish(rec, "failed", "job raised an exception")
    rec["_runner"] = asyncio.create_task(_guard())


class _TasksExtension(Extension):
    """Serves the spec's `tasks/*` methods over the same registry the tools use.

    Task-aware clients can poll natively; everyone else uses the task_* tools.
    """

    identifier = "io.modelcontextprotocol/tasks"

    def methods(self) -> list[MethodBinding]:
        async def get(ctx: Any, params: Any) -> dict:
            rec = _TASKS.get(getattr(params, "task_id", None) or "")
            if not rec:
                return {"error": "unknown task_id"}
            return {"taskId": rec["task_id"], "status": rec["status"],
                    "statusMessage": rec["status_message"], "createdAt": rec["created_at"],
                    "lastUpdatedAt": rec["last_updated_at"], "pollInterval": TASK_POLL_MS}

        async def result(ctx: Any, params: Any) -> dict:
            rec = _TASKS.get(getattr(params, "task_id", None) or "")
            if not rec:
                return {"error": "unknown task_id"}
            if rec["status"] == "working":
                return {"error": "still working", "status": rec["status"]}
            return {"status": rec["status"], "summary": rec["summary"],
                    "count": len(rec["items"]) if rec["items"] is not None else 0}

        async def cancel(ctx: Any, params: Any) -> dict:
            rec = _TASKS.get(getattr(params, "task_id", None) or "")
            if not rec:
                return {"error": "unknown task_id"}
            rec["cancel"] = True
            return {"taskId": rec["task_id"], "status": "cancelled"}

        async def listing(ctx: Any, params: Any) -> dict:
            return {"tasks": [_task_public(r) for r in _TASKS.values()]}

        return [
            MethodBinding("tasks/get", types.GetTaskRequestParams, get),
            MethodBinding("tasks/result", types.GetTaskPayloadRequestParams, result),
            MethodBinding("tasks/cancel", types.CancelTaskRequestParams, cancel),
            MethodBinding("tasks/list", types.ListTasksRequest, listing),
        ]


# --------------------------------------------------------------------------- apps UI
# MCP Apps (io.modelcontextprotocol/ui): a tool can point at a `ui://` HTML resource the
# host renders inline. Per SEP-2133 an extension MUST degrade gracefully, so the tool
# below returns the same numbers as data no matter what — the UI is a bonus, never a
# requirement.
apps = Apps()
# prefers_border/csp/permissions are what populate the resource's `_meta.ui` block. With
# none of them passed, `_meta` is omitted entirely — and a host looking for `_meta.ui` to
# decide "is this an app view?" would skip it. Declare an explicit (empty-domain) CSP so
# the block always exists: we load nothing external, so no origins are allowed.
apps.add_html_resource(
    "ui://insightly/env-dashboard.html", ENV_DASHBOARD_HTML,
    name="Insightly environment dashboard",
    title="Insightly environment",
    description="Record counts across the connected Insightly demo environment.",
    csp=ResourceCsp(connect_domains=[], resource_domains=[],
                    frame_domains=[], base_uri_domains=[]),
    prefers_border=True,
)


@apps.tool(resource_uri="ui://insightly/env-dashboard.html",
           visibility=["model", "app"],
           name="env_dashboard",
           description="Interactive dashboard of what's in the connected Insightly "
                       "environment — record counts per object, with the day's remaining "
                       "API quota. Same data as env_summary, rendered inline.")
async def env_dashboard(ctx: Context) -> Any:
    """Render the environment overview as an inline dashboard."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    snap = await _env_snapshot()
    # Degrade gracefully, and say WHY rather than returning bare numbers. Apps rendering
    # needs the `extensions` capability map, which this SDK only exchanges on the stateless
    # 2026-07-28 protocol (via `server/discover`). On the legacy `initialize` handshake
    # extensions are never negotiated at all — client_supports_apps() is False there even
    # when the client declares UI support — so nothing can render however capable the host
    # is. That is a host-version gap, not a misconfiguration, so name it.
    if not client_supports_apps(ctx):
        proto = str(getattr(ctx, "protocol_version", "") or "") or "a pre-2026-07-28 revision"
        snap["ui"] = (f"inline dashboard unavailable: this host negotiated {proto}, and UI "
                      f"extensions are only advertised on MCP 2026-07-28 (stateless). These "
                      f"are exactly the numbers env_summary returns — nothing is broken.")
    return snap


# App-only tools (visibility=["app"]): the dashboard's buttons call these, so the UI never
# depends on a host being willing to proxy arbitrary model-facing tools. They are hidden
# from the model, so they add nothing to its tool list.
@apps.tool(resource_uri="ui://insightly/env-dashboard.html", visibility=["app"],
           name="app_records",
           description="(dashboard) newest records for one object, for the drill-in panel.")
async def app_records(object: str, ctx: Context, top: int = 25,
                      order_by: Optional[str] = "DATE_UPDATED_UTC desc") -> Any:
    """Newest records for one object — powers the dashboard drill-in."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    page = min(max(int(top), 1), 100)
    body, hdrs = await _request("GET", f"/{o}",
                               params={"top": page, "skip": 0, "brief": "true",
                                       "count_total": "true"}, want_headers=True)
    if isinstance(body, dict) and body.get("error"):
        return body
    items = _brief_strip(body if isinstance(body, list) else [])
    out = _page_envelope(_apply_sort(items, order_by), 0, page)
    if hdrs.get("x-total-count") is not None:
        try:
            out["total"] = int(hdrs["x-total-count"])
        except ValueError:
            pass
    return out


@apps.tool(resource_uri="ui://insightly/env-dashboard.html", visibility=["app"],
           name="app_fields",
           description="(dashboard) field reference for one object, for the drill-in panel.")
async def app_fields(object: str, ctx: Context) -> Any:
    """Standard + custom fields for one object — powers the dashboard's Fields view."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    return await _describe(_obj(object))


# Display name shown in Claude's UI. The registration key stays `insightly`
# (mcpServers key / `claude mcp add insightly`), so tool names are unchanged.
#
# cache_hints: object field metadata is re-fetched constantly and changes rarely, so
# it is worth caching — but it is per-org (custom fields differ per demo env), hence
# scope="private", never "public". The tool list only changes when this file does.
mcp = MCPServer(
    name="Insightly SE MCP (internal)",
    version=SERVER_VERSION,
    extensions=[_TasksExtension(), apps],
    cache_hints={
        # Tool/resource inventories only change when this file does.
        "server/discover": CacheHint(ttl_ms=3_600_000, scope="private"),
        "tools/list": CacheHint(ttl_ms=3_600_000, scope="private"),
        "resources/list": CacheHint(ttl_ms=600_000, scope="private"),
        "resources/templates/list": CacheHint(ttl_ms=600_000, scope="private"),
        # Field metadata: long enough to kill repeat lookups in a session, short enough
        # that an SE who just added a custom field sees it on their next question.
        "resources/read": CacheHint(ttl_ms=300_000, scope="private"),
    },
)

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
# Read-only / reference collections that exist in the v3.1 swagger. Listed so they are
# discoverable and alias-resolvable; anything else still works via raw_request.
COMMON_OBJECTS = sorted(PK.keys()) + [
    "Pipelines", "PipelineStages", "Relationships", "Tags", "Teams",
    "LeadSources", "LeadStatuses", "Currencies", "CustomObjects",
    "TeamMembers", "Users", "ActivitySets",
    # added after auditing the swagger (all GET-able)
    "Instance", "Countries", "Permissions", "Prospect", "DocumentTemplates",
    "OpportunityCategories", "OpportunityStateReasons", "OpportunityLineItem",
    "QuotationLineItem", "PricebookEntry", "ProjectCategories", "TaskCategories",
    "FileCategories", "KnowledgeArticleCategory", "KnowledgeArticleFolder",
    "MarketingVisits", "Follows",
]

# Objects whose records can be linked to other records (swagger: /{obj}/{id}/Links).
LINKABLE = ("Contacts", "Organisations", "Opportunities", "Projects",
            "Tasks", "Events", "Notes", "Emails")

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
# The docs are explicit: the Quote endpoints use "Quotation"; "Quote" is rejected.
_ALIASES["quote"] = _ALIASES["quotes"] = "Quotation"
_ALIASES["knowledgearticles"] = _ALIASES["knowledge"] = "KnowledgeArticle"


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


def _have_key() -> bool:
    """True if a key is already available without prompting (session or environment)."""
    if SESSION.get("api_key"):
        return True
    env_key = os.environ.get("INSIGHTLY_API_KEY")
    if env_key:
        SESSION.update(api_key=env_key, pod=(os.environ.get("INSIGHTLY_POD") or "na1"), name="env")
        return True
    return False


_NO_PROMPT_HELP = ("this client can't show input prompts. Set the key without a prompt: "
                   "set_api_key('<key>', pod='na1'), or add INSIGHTLY_API_KEY to the server's "
                   "env (the .mcpb extension does this for you).")


def _elicit_support(ctx: Context) -> dict:
    """What kind of prompting this client accepts.

    SDK 2.x advertises elicitation sub-capabilities (form / url) instead of one flag,
    so we can pick a mode — or bail out with a useful message — instead of firing a
    request into the void and catching the failure.
    """
    caps = getattr(ctx, "client_capabilities", None)
    el = getattr(caps, "elicitation", None) if caps is not None else None
    if caps is None:
        return {"known": False, "any": True, "form": True, "url": False}  # assume yes, try it
    if el is None:
        return {"known": True, "any": False, "form": False, "url": False}
    form = getattr(el, "form", None)
    url = getattr(el, "url", None)
    # Sub-capabilities unset means the client declared plain elicitation: treat as form.
    return {"known": True, "any": True,
            "form": form is not None or url is None,
            "url": url is not None}


async def _prompt(ctx: Context) -> Optional[str]:
    """Interactively obtain credentials. Returns an error string, or None on success."""
    support = _elicit_support(ctx)
    if not support["any"]:
        return _NO_PROMPT_HELP
    if not support["form"] and support["url"]:
        # URL-mode only. We deliberately don't implement it: it would mean hosting a
        # key-entry page, and an API key should not travel through a web form we serve.
        return ("this client only supports URL prompts, which this server doesn't use for "
                "credentials. " + _NO_PROMPT_HELP)
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
    if _have_key():
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

def _note_rate(headers: dict) -> None:
    """Remember the daily-quota counters the API returns on every response."""
    lim, rem = headers.get("x-ratelimit-limit"), headers.get("x-ratelimit-remaining")
    if lim is None and rem is None:
        return
    try:
        RATE["limit"] = int(lim) if lim is not None else RATE["limit"]
        RATE["remaining"] = int(rem) if rem is not None else RATE["remaining"]
        RATE["seen_at"] = _now()
    except (TypeError, ValueError):
        pass


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
                   json_body: Any = None, want_headers: bool = False,
                   headers: Optional[dict] = None) -> Any:
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
            r = await client.request(method, path, params=params, json=json_body,
                                     headers=headers or None)
        except Exception as e:
            return wrap({"error": f"request failed: {e}"})
        hdr = {k.lower(): v for k, v in r.headers.items()}
        _note_rate(hdr)
        if r.status_code == 429:
            last = r
            # Two different 429s: per-second burst (retryable) vs. daily quota
            # exhausted (NOT retryable — the docs say no more requests until the next
            # day). Tell them apart by the remaining counter.
            if RATE.get("remaining") == 0:
                return wrap({"error": "daily API quota exhausted (429). X-RateLimit-Remaining is 0 — "
                                      f"the limit is {RATE.get('limit')} requests/day and resets "
                                      "tomorrow. Retrying now won't help.",
                             "rate_limit": dict(RATE)}, hdr)
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
    """Show whether this connection is authenticated, which org/pod it points at, and the
    server version. `daily_quota` (from Insightly's X-RateLimit-* headers) appears once at
    least one API call has been made in this session — it is read from responses, not polled."""
    env_key = os.environ.get("INSIGHTLY_API_KEY")
    key = SESSION.get("api_key") or env_key
    name = SESSION.get("name") or ("env" if env_key else None)
    pod = SESSION.get("pod") or os.environ.get("INSIGHTLY_POD") or "na1"
    out = {"connected": bool(key), "as": name, "pod": pod,
           "read_only": READONLY, "version": SERVER_VERSION}
    if RATE.get("limit") is not None:
        out["daily_quota"] = {"limit": RATE["limit"], "remaining": RATE["remaining"],
                              "as_of": RATE["seen_at"]}
    return out

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
                         top: int = 20, skip: int = 0, count_total: bool = False,
                         updated_after_utc: Optional[str] = None, brief: bool = True) -> Any:
    """EXACT-match search on a single field (the API does not do partial match here), e.g.
    search_records('Contacts', 'EMAIL_ADDRESS', 'jane@example.com'). Works on standard AND
    custom fields (use the custom FIELD_NAME, e.g. 'Intake_Status__c'). For substring
    matching use filter_records.

    Supports the same paging extras as list_records: count_total=true adds the real
    `total` from X-Total-Count, and updated_after_utc ('2026-01-01T00:00:00Z') filters by
    change time. Returns a paginated envelope."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    page = min(max(top, 1), PAGE_MAX)
    params: dict = {"field_name": field_name, "field_value": field_value,
                    "top": page, "skip": max(skip, 0), "brief": str(brief).lower()}
    if updated_after_utc:
        params["updated_after_utc"] = updated_after_utc
    if count_total:
        params["count_total"] = "true"
    body, hdrs = await _request("GET", f"/{_obj(object)}/Search", params=params, want_headers=True)
    if isinstance(body, dict) and body.get("error"):
        return body
    items = body if isinstance(body, list) else []
    if brief:
        _brief_strip(items)
    env = _page_envelope(items, max(skip, 0), page)
    if count_total and hdrs.get("x-total-count") is not None:
        try:
            env["total"] = int(hdrs["x-total-count"])
        except ValueError:
            env["total"] = hdrs["x-total-count"]
    return env

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
    after connecting — "what's in this env?". For the same thing as an interactive
    dashboard, use env_dashboard."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    return await _env_snapshot()


async def _env_snapshot() -> dict:
    """Count the core objects. Shared by env_summary and the Apps dashboard."""
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
        except (TypeError, ValueError):
            counts[o] = tot
    out: dict = {"connected_as": SESSION.get("name"), "pod": SESSION.get("pod"),
                 "version": SERVER_VERSION, "counts": counts}
    if RATE.get("remaining") is not None:
        out["daily_quota"] = {"limit": RATE["limit"], "remaining": RATE["remaining"]}
    if failed:
        out["failed"] = failed
    return out


@mcp.tool()
async def describe_object(object: str, ctx: Context) -> Any:
    """Field reference for an object — call this BEFORE creating/updating records you
    haven't touched yet. Returns `standard_fields` (from a sample record) and compact
    `custom_fields` (name, label, type, dropdown options, lookup target) so payloads
    use real field names and valid option values. Custom values go in CUSTOMFIELDS:
    [{"FIELD_NAME": "...__c", "FIELD_VALUE": ...}].

    The same data is also served as a cacheable resource, `insightly://{object}/fields`,
    so repeat lookups in a long session don't re-hit the API."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    return await _describe(_obj(object))


async def _describe(o: str) -> dict:
    """Build the field reference for an object. Shared by the tool and the resource."""
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


@mcp.resource("insightly://{object}/fields", mime_type="application/json",
              name="Insightly object fields",
              description="Standard + custom field reference for one Insightly object "
                          "(cacheable; per-connection, since custom fields differ per env).")
async def object_fields_resource(object: str) -> str:
    """Cacheable view of describe_object.

    Resources can't prompt for credentials, so this uses whatever key the session or
    environment already has and says so plainly if there is none.
    """
    if not _have_key():
        return json.dumps({"error": "not connected — run connect() or set_api_key(...) first, "
                                    "then read this resource again."})
    return json.dumps(await _describe(_obj(object)), default=str)

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
async def update_record(object: str, record_id: int, fields: dict, ctx: Context,
                        if_match: Optional[str] = None, safe: bool = False) -> Any:
    """Partial update (send only changed fields). PK is filled in for common objects;
    otherwise include the *_ID field in `fields`. e.g.
    update_record('Contacts', 12345, {'PHONE':'555-1212'}).

    Optimistic concurrency (avoid clobbering someone else's edit): pass the record's
    `ETag` as `if_match`, or `safe=true` to have the server fetch the current ETag first.
    On a stale ETag Insightly rejects the write — note it answers **400**, not the 412 the
    docs advertise (verified live)."""
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
    tag = if_match
    if safe and not tag:
        cur = await _request("GET", f"/{o}/{record_id}")
        if isinstance(cur, dict) and cur.get("error"):
            return {"error": f"couldn't read the current record to get its ETag: {cur['error']}"}
        tag = cur.get("ETag") if isinstance(cur, dict) else None
        if not tag:
            return {"error": "safe=true but this record has no ETag — retry without safe."}
    hdrs = {"If-Match": tag} if tag else None
    res = _write_hint(await _request("PUT", f"/{o}", json_body=body, headers=hdrs), o)
    if tag and isinstance(res, dict) and str(res.get("error", "")).startswith("HTTP 4"):
        res["hint"] = ("the record changed since you read that ETag (Insightly returns 400 here, "
                       "not 412). Re-read it with get_record and retry.")
    return res

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


# ------------------------------------------------------------------------ link tools
# Insightly models cross-object relationships as Links on the record
# (/{Object}/{id}/Links). Note LINK_OBJECT_NAME is SINGULAR — "Organisation", "Contact",
# "Opportunity" — even though the endpoints are plural.
@mcp.tool()
async def list_links(object: str, record_id: int, ctx: Context) -> Any:
    """Show what a record is linked to (linked contacts, organisations, opportunities…)."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    if o not in LINKABLE:
        return {"error": f"'{o}' has no Links endpoint. Linkable objects: {', '.join(LINKABLE)}."}
    return await _request("GET", f"/{o}/{record_id}/Links")

@mcp.tool()
async def link_records(object: str, record_id: int, link_object_name: str,
                       link_object_id: int, ctx: Context,
                       role: Optional[str] = None, details: Optional[str] = None) -> Any:
    """Link two records, e.g. put a contact into an organisation:
    link_records('Contacts', 123, 'Organisation', 456).

    `link_object_name` is the SINGULAR object name ('Organisation', 'Contact',
    'Opportunity', 'Project', 'Lead'). Both ids must already exist — look them up first
    (search_records / find_by_email) rather than guessing."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    if o not in LINKABLE:
        return {"error": f"'{o}' has no Links endpoint. Linkable objects: {', '.join(LINKABLE)}."}
    body: dict = {"LINK_OBJECT_NAME": link_object_name.strip(), "LINK_OBJECT_ID": link_object_id}
    if role:
        body["ROLE"] = role
    if details:
        body["DETAILS"] = details
    res = await _request("POST", f"/{o}/{record_id}/Links", json_body=body)
    if isinstance(res, dict) and str(res.get("error", "")).startswith("HTTP 4"):
        res.setdefault("hint", "check both ids exist and that LINK_OBJECT_NAME is the SINGULAR "
                               "object name (e.g. 'Organisation', not 'Organisations').")
    return res

@mcp.tool()
async def unlink_records(object: str, record_id: int, link_id: int, ctx: Context,
                         confirm: bool = False) -> Any:
    """Remove a link (get link_id from list_links). Must pass confirm=true."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    if not confirm:
        return {"error": "pass confirm=true to remove this link."}
    o = _obj(object)
    if o not in LINKABLE:
        return {"error": f"'{o}' has no Links endpoint. Linkable objects: {', '.join(LINKABLE)}."}
    return await _request("DELETE", f"/{o}/{record_id}/Links/{link_id}")


# ------------------------------------------------------------------------ task tools
@mcp.tool()
async def start_export(object: str, ctx: Context, brief: bool = True,
                       updated_after_utc: Optional[str] = None,
                       max_records: int = 100000) -> Any:
    """Export an ENTIRE object in the background — no 5,000-record cap. Returns a
    task_id immediately; poll with task_status(task_id) and read pages of the result
    with task_result(task_id). Use this instead of list_records(fetch_all=true) for
    big environments ("export all 40k contacts")."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    rec = _task_new("export", o)
    _spawn(rec, _job_export(rec, o, brief, updated_after_utc, max_records))
    return {"task_id": rec["task_id"], "status": rec["status"],
            "poll_interval_ms": TASK_POLL_MS,
            "next": f"task_status('{rec['task_id']}') until status=completed, "
                    f"then task_result('{rec['task_id']}')"}

@mcp.tool()
async def start_bulk_create(object: str, records: list, ctx: Context) -> Any:
    """Create ANY number of records in the background — no 50-per-call cap. Returns a
    task_id immediately; poll with task_status(task_id). Use create_records for small
    batches you want confirmed inline."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    if not isinstance(records, list) or not records:
        return {"error": "pass a non-empty list of field dicts."}
    o = _obj(object)
    rec = _task_new("bulk_create", f"{o} × {len(records)}")
    _spawn(rec, _job_bulk_create(rec, o, records))
    return {"task_id": rec["task_id"], "status": rec["status"], "queued": len(records),
            "poll_interval_ms": TASK_POLL_MS,
            "next": f"task_status('{rec['task_id']}')"}

@mcp.tool()
def task_status(task_id: str) -> Any:
    """Progress of a background task: status (working/completed/failed/cancelled),
    progress, total, and a summary once finished."""
    rec = _TASKS.get(task_id)
    if not rec:
        return {"error": f"unknown task_id '{task_id}' (finished tasks are kept for "
                         f"{TASK_TTL_S // 60} minutes)."}
    return _task_public(rec, include_result=True)

@mcp.tool()
def task_result(task_id: str, top: int = 100, skip: int = 0) -> Any:
    """Read a finished task's records, PAGED (default 100 at a time) — a full export is
    far too large to return at once. Returns {items, returned, skip, top, has_more,
    next_skip, count}."""
    rec = _TASKS.get(task_id)
    if not rec:
        return {"error": f"unknown task_id '{task_id}'."}
    if rec["items"] is None:
        return {"error": f"no result yet — status is '{rec['status']}'.",
                "status": rec["status"], "progress": rec["progress"]}
    items = rec["items"]
    page = min(max(int(top), 1), PAGE_MAX)
    start = max(int(skip), 0)
    window = items[start:start + page]
    return {"items": window, "returned": len(window), "skip": start, "top": page,
            "has_more": start + len(window) < len(items),
            "next_skip": start + len(window), "count": len(items),
            "status": rec["status"], "summary": rec["summary"]}

@mcp.tool()
def list_tasks() -> Any:
    """All background tasks this session knows about, newest first."""
    _task_prune()
    return {"tasks": sorted((_task_public(r, include_result=True) for r in _TASKS.values()),
                            key=lambda r: r["created_at"], reverse=True)}

@mcp.tool()
def cancel_task(task_id: str) -> Any:
    """Ask a running task to stop. It stops at the next page/record boundary and keeps
    whatever it already collected."""
    rec = _TASKS.get(task_id)
    if not rec:
        return {"error": f"unknown task_id '{task_id}'."}
    if rec["status"] != "working":
        return {"ok": False, "status": rec["status"], "note": "task is not running."}
    rec["cancel"] = True
    return {"ok": True, "status": "cancelling", "task_id": task_id}


if __name__ == "__main__":
    mcp.run()
