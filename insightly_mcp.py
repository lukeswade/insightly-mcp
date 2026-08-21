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
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
from typing import Any, Optional

import httpx
import mcp_types as types
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.apps import Apps, client_supports_apps
from mcp.server.caching import CacheHint
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import Context  # NOT mcp.server.context — that one has no .elicit

from app_ui import ENV_DASHBOARD_HTML

SERVER_VERSION = "3.11.0"
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
    # NOTE: do NOT declare an empty-domain csp here. It reads as "allow nothing" and can
    # block this page's own inline <style>/<script>, which renders the widget blank.
    # prefers_border alone is enough to make the resource's _meta.ui block exist.
    prefers_border=True,
)


@apps.tool(resource_uri="ui://insightly/env-dashboard.html",
           visibility=["model", "app"],
           # Compatibility shim: the SDK stamps only the current nested
           # _meta.ui.resourceUri, but some shipped hosts read ONLY the deprecated flat
           # key. Emitting both is the documented field workaround and is harmless — a
           # host that doesn't recognise the flat key ignores it.
           meta={"ui/resourceUri": "ui://insightly/env-dashboard.html"},
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
                      order_by: Optional[str] = None) -> Any:
    """Newest records for one object — powers the dashboard drill-in.

    "Newest" means most recently created OR updated, newest first. Pass order_by to sort
    on some other field instead."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    # The Explore lists ask for 200-500 (every stage, every user); clamping to 100 was
    # silently truncating them.
    page = min(max(int(top), 1), _SCAN_CAP)
    items, total, basis = await _newest_records(o, page)
    if isinstance(items, dict) and items.get("error"):
        return items
    if order_by:
        items = _apply_sort(items, order_by)
    out = _page_envelope(items, 0, page)
    # A recency-ranked top-N has no "next page" — the API pages in id order, so resuming
    # at skip=25 would hand back older records, not the next-newest ones.
    out.pop("next_skip", None)
    out["has_more"] = bool(total is not None and total > len(items))
    out = _fit(out.pop("items", []), out)
    out["sorted_by"] = order_by or _sort_newest_basis(items)
    out["basis"] = basis
    if total is not None:
        out["total"] = total
    return out


@apps.tool(resource_uri="ui://insightly/env-dashboard.html", visibility=["app"],
           name="app_envs",
           description="(dashboard) saved environments and which one is active.")
async def app_envs(ctx: Context) -> Any:
    """Saved environments for the picker. Keys are masked and never leave this machine."""
    saved = _load_saved()
    active = SESSION.get("name")
    return {"active": active,
            "envs": [{"name": n, "pod": saved[n].get("pod", "na1"),
                      "masked": _mask(saved[n].get("api_key")), "active": n == active}
                     for n in sorted(saved)],
            "count": len(saved)}


@apps.tool(resource_uri="ui://insightly/env-dashboard.html", visibility=["app"],
           name="app_use_env",
           description="(dashboard) switch to a saved environment by name.")
async def app_use_env(name: str, ctx: Context) -> Any:
    """Switch environments from the picker — the key is read from the local key store."""
    return await use_saved(name=name, ctx=ctx)


@apps.tool(resource_uri="ui://insightly/env-dashboard.html", visibility=["app"],
           name="app_add_env",
           description="(dashboard) save a new environment and switch to it.")
async def app_add_env(name: str, api_key: str, ctx: Context, pod: str = "na1") -> Any:
    """Save a new environment under a friendly name, verify the key, and make it active.

    The key is written to the local key store (chmod 600) and is usable by name from then
    on. Verifies before saving so a typo cannot leave a dead entry in the list.
    """
    name = (name or "").strip()
    api_key = (api_key or "").strip()
    pod = (pod or "na1").strip() or "na1"
    if not name:
        return {"saved": False, "error": "give the environment a name so you can switch to it later."}
    if not api_key:
        return {"saved": False, "error": "the API key is required (Insightly: User Settings then API)."}
    saved = _load_saved()
    replacing = name in saved

    # Verify against the API BEFORE writing, so a bad key never enters the list.
    prev = dict(SESSION)
    SESSION.update(api_key=api_key, pod=pod, name=name)
    _CLIENT_ID_RESET()
    chk = await _request("GET", "/Contacts", params={"top": 1, "brief": "true"})
    if isinstance(chk, dict) and chk.get("error"):
        SESSION.update(prev)
        _CLIENT_ID_RESET()
        return {"saved": False,
                "error": f"that key did not work on pod '{pod}': {chk['error']}",
                "hint": "check the key was copied whole, and that the pod matches your API URL."}

    saved[name] = {"api_key": api_key, "pod": pod}
    _save_saved(saved)
    return {"saved": True, "connected": True, "as": name, "pod": pod,
            "replaced": replacing,
            "note": f"'{name}' is saved on this machine and active. Switch back to it any time "
                    f"with the picker or use_saved('{name}')."}


@apps.tool(resource_uri="ui://insightly/env-dashboard.html", visibility=["app"],
           name="app_rename_env",
           description="(dashboard) rename a saved environment.")
async def app_rename_env(name: str, new_name: str, ctx: Context) -> Any:
    """Rename an environment from the picker."""
    return rename_saved(name=name, new_name=new_name)


@apps.tool(resource_uri="ui://insightly/env-dashboard.html", visibility=["app"],
           name="app_remove_env",
           description="(dashboard) remove a saved environment.")
async def app_remove_env(name: str, ctx: Context) -> Any:
    """Forget a saved environment. Only the stored key is dropped; nothing in Insightly
    changes. If it was the active one the session stays connected until you switch."""
    name = (name or "").strip()
    saved = _load_saved()
    if name not in saved:
        return {"ok": False, "error": f"no saved environment called '{name}'.",
                "available": sorted(saved)}
    res = forget_saved(name=name)
    res["was_active"] = SESSION.get("name") == name
    res["remaining"] = sorted(_load_saved())
    return res


@apps.tool(resource_uri="ui://insightly/env-dashboard.html", visibility=["app"],
           name="app_custom_objects",
           description="(dashboard) this environment's custom objects with record counts.")
async def app_custom_objects(ctx: Context, with_counts: bool = True) -> Any:
    """Custom object definitions + how many records each holds.

    /CustomObjects returns definitions (OBJECT_NAME, SINGULAR_LABEL, PLURAL_LABEL), not
    records — so counts come from the generic /{objectName} endpoint, one cheap
    count_total call each.
    """
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    defs = await _request("GET", "/CustomObjects", params={"top": 200})
    if isinstance(defs, dict) and defs.get("error"):
        return defs
    out: list = []
    for d in (defs if isinstance(defs, list) else []):
        if not isinstance(d, dict):
            continue
        api = d.get("OBJECT_NAME")
        row = {"name": api,
               "label": d.get("PLURAL_LABEL") or d.get("SINGULAR_LABEL") or api,
               "singular": d.get("SINGULAR_LABEL"),
               "in_navbar": d.get("ENABLE_NAVBAR")}
        if with_counts and api:
            _, hdrs = await _request("GET", f"/{api}",
                                     params={"top": 1, "brief": "true", "count_total": "true"},
                                     want_headers=True)
            tot = hdrs.get("x-total-count")
            try:
                row["count"] = int(tot) if tot is not None else None
            except (TypeError, ValueError):
                row["count"] = None
        out.append(row)
    out.sort(key=lambda r: (r.get("count") is None, -(r.get("count") or 0)))
    return {"custom_objects": out, "total": len(out)}


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
# Where a human-readable label lives, per object. Insightly is not consistent about this,
# so it is a search order rather than a map.
_NAME_FIELDS = ("ORGANISATION_NAME", "OPPORTUNITY_NAME", "PROJECT_NAME", "QUOTATION_NAME",
                "PRODUCT_NAME", "RECORD_NAME", "TASK_NAME", "MILESTONE_NAME",
                "TICKET_TITLE", "SUBJECT", "TITLE", "NAME")

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
        SESSION.update(api_key=env_key, pod=(os.environ.get("INSIGHTLY_POD") or "na1"),
                       name=_env_key_label(env_key))
        return True
    return False


def _env_key_label(env_key: str) -> str:
    """What to call the key the desktop extension injects.

    If it is one we already have saved, use that name — then the header and the picker
    agree, and the active row gets its tick. Otherwise say where it came from rather than
    the literal "env", which reads as a placeholder in the UI.
    """
    for name, rec in _load_saved().items():
        if rec.get("api_key") == env_key:
            return name
    return "Extension key"


def _no_prompt_help() -> str:
    """Guidance for clients that cannot show the interactive picker (e.g. desktop chat).

    If environments are already saved, switching needs no key at all — say so, because
    otherwise the only apparent option is pasting a secret into the conversation.
    """
    names = sorted(_load_saved().keys())
    if names:
        return ("this client can't show the interactive picker, but you don't need it: switch "
                "with use_saved('<name>') and the key is read from the local key store, never "
                f"typed into the chat. Saved environments: {', '.join(names)}.")
    return ("this client can't show input prompts, and there are no saved environments yet. "
            "Use set_api_key('<key>', pod='na1', friendly_name='demo1', save=true) once; after "
            "that you can switch with use_saved('demo1').")


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
        return _no_prompt_help()
    if not support["form"] and support["url"]:
        # URL-mode only. We deliberately don't implement it: it would mean hosting a
        # key-entry page, and an API key should not travel through a web form we serve.
        return ("this client only supports URL prompts, which this server doesn't use for "
                "credentials. " + _no_prompt_help())
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
# brief is the SCAN mode: strip everything bulky so a wide sweep stays cheap. It used to
# be the single name "Body", which matches nothing Insightly actually returns — BODY is
# upper-case and the real weight is DETAILS/CUSTOMFIELDS. Matched case-insensitively for
# that reason. Anything returned to the user as a final answer gets hydrated back to whole
# records by _hydrate, because CUSTOMFIELDS is often where the interesting data lives.
_BRIEF_DROP = frozenset(("body", "details", "customfields", "image_url", "etag"))
# Records sampled from EACH END of an object to derive its standard field list.
_DESCRIBE_SAMPLE = 5

def _brief_strip(data: Any) -> Any:
    """Drop heavy fields from each record in a brief list result; pass other shapes through."""
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                for k in [k for k in item if k.lower() in _BRIEF_DROP]:
                    item.pop(k, None)
    return data

_RESULT_BUDGET = 900_000     # hosts reject a tool result over 1MB; leave room for the envelope


def _fit(items: list, envelope: dict, key: str = "items") -> dict:
    """Put `items` into `envelope` under `key`, dropping from the tail until it fits.

    A result over the host's 1MB ceiling is thrown away before the model ever sees it — the
    user just gets "Tool result is too large" and Claude gets nothing. Returning fewer
    records with an explicit note is strictly better than returning nothing, and saying so
    out loud is what stops it looking like a bug.
    """
    envelope[key] = items
    if len(json.dumps(envelope, default=str)) <= _RESULT_BUDGET:
        return envelope
    lo, hi = 0, len(items)                       # largest prefix that fits
    while lo < hi:
        mid = (lo + hi + 1) // 2
        envelope[key] = items[:mid]
        if len(json.dumps(envelope, default=str)) <= _RESULT_BUDGET:
            lo = mid
        else:
            hi = mid - 1
    envelope[key] = items[:lo]
    envelope["capped"] = True
    envelope["capped_note"] = (
        f"Result trimmed to {lo} of {len(items)} records to stay under the host's 1MB "
        "limit. Ask for a smaller top, add brief=true to drop bulky fields, narrow the "
        "object/date range, or use start_export for the whole object (it pages through "
        "task_result without a size ceiling).")
    return envelope


_SCAN_CAP = 500                          # the most records the API will hand over at once
_RECENT_WINDOWS = (1, 7, 30, 90, 365, 1825)   # days back to try on /Search, narrowest first


_WINDOW_BOUND = 3000                     # most records we will pull to rank one window
_HYDRATE_MAX = 100                        # per-record GETs we are willing to spend on a list


async def _hydrate(o: str, items: list, limit: int = _HYDRATE_MAX) -> tuple:
    """Refetch each record in full so the answer carries custom fields.

    brief is how we scan cheaply, but CUSTOMFIELDS is frequently where the meaningful data
    sits, so a list handed back as a final answer must not be the stripped version.
    Insightly has no batch endpoint and no field projection, so this costs one GET per
    record — hence the cap. Records that fail to refetch keep their brief form rather than
    disappearing.
    """
    pk = PK.get(o)
    if not pk or not items:
        return items, "not hydrated (no primary key known for this object)"
    if len(items) > limit:
        return items, f"not hydrated ({len(items)} records exceeds the {limit}-call budget)"
    out = []
    for rec in items:
        rid = rec.get(pk) if isinstance(rec, dict) else None
        if rid is None:
            out.append(rec)
            continue
        full = await _request("GET", f"/{o}/{rid}")
        out.append(full if isinstance(full, dict) and not full.get("error") else rec)
    return out, f"hydrated ({len(items)} full records incl. custom fields)"


# A window built from updated_after_utc only bounds fields that cannot postdate the last
# update. Recording a close writes the record, so ACTUAL_CLOSE_DATE <= DATE_UPDATED_UTC
# always holds; a forecast or due date is free to sit in the future, so the same window
# would silently omit rows. Refuse those rather than answer them wrongly.
_FORWARD_DATED = ("FORECAST", "DUE", "TARGET", "START", "END", "EXPIR", "RENEW", "NEXT")


async def _newest_by_field(o: str, field: str, want: int) -> dict:
    """The `want` records with the latest `field`, without scanning the whole object.

    Insightly cannot sort, and cannot range-filter an arbitrary field — but /Search does
    honour updated_after_utc and count_total, so a one-record request prices a window
    before we commit to fetching it. Because the field can never postdate the record's last
    update, "updated since T" is a superset of "field >= T": rank inside it and the answer
    is exact, not approximate.

    The stop condition proves itself: if the want-th result is NEWER than the window's
    cutoff, no better record can exist outside the window. So pick the WIDEST window we can
    afford to fetch (a wider one strictly dominates — it contains every narrower one), and
    widen again if the proof still fails.
    """
    probes = 0
    priced: list = []                    # (days, since, count) that we could afford
    over: list = []                      # windows too big for the record bound
    for days in _RECENT_WINDOWS[1:] + (3650,):
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        _, hdrs = await _request("GET", f"/{o}/Search", want_headers=True,
                                 params={"top": 1, "brief": "true", "count_total": "true",
                                         "updated_after_utc": since})
        probes += 1
        try:
            count = int(hdrs.get("x-total-count"))
        except (TypeError, ValueError):
            return {"error": f"/{o}/Search did not report a count, so a window cannot be priced.",
                    "hint": "Use start_export for this object — it pages without a cap."}
        if count > _WINDOW_BOUND:
            over.append((days, since, count))
            break                        # everything wider is bigger still
        priced.append((days, since, count))

    if not priced and not over:
        return {"error": f"no records in {o} carry a change date, so no window can be built.",
                "hint": "Use start_export and rank the exported records."}
    # Widest affordable window first, then one deliberate overshoot if the proof needs it.
    plan = ([priced[-1]] if priced else []) + over[:1]
    fetched, attempt = 0, None
    for days, since, count in plan:
        rows, whole = await _search_window(o, since)
        if rows is None:
            return {"error": f"/{o}/Search is not available for '{o}'.",
                    "hint": "Use start_export and rank the exported records instead."}
        fetched += len(rows)
        have = sorted((r for r in rows if isinstance(r, dict) and r.get(field)),
                      key=lambda r: str(r.get(field)), reverse=True)
        top = have[:want]
        edge = str(top[-1].get(field))[:10] if top else ""
        proven = len(top) >= want and whole and edge > since[:10]
        attempt = (days, since, count, have, top, proven, whole)
        if proven:
            break

    days, since, count, have, top, proven, whole = attempt
    out = {"object": o, "date_field": field, "sorted_by": f"{field}, newest first",
           "returned": len(top), "window_days": days, "window_start": since[:10],
           "records_updated_in_window": count,
           "records_with_a_value_in_window": len(have),
           "cost": {"count_probes": probes, "records_fetched": fetched},
           "complete": proven}
    if not proven:
        if len(top) < want:
            out["caveat"] = (f"Only {len(top)} records carry {field} within the last {days} "
                             f"days — that is everything available in the widest window "
                             f"that could be fetched.")
        elif not whole:
            out["caveat"] = (f"The {days}-day window exceeded the {_WINDOW_BOUND}-record "
                             "fetch bound, so records inside it were not all ranked.")
        else:
            out["caveat"] = (f"The {len(top)}th value ({str(top[-1].get(field))[:10]}) falls "
                             f"outside the {days}-day change window, so an older record "
                             f"could rank higher. These are the best available without "
                             f"scanning the object — use start_export to be certain.")
    top, note = await _hydrate(o, top)
    out["detail_level"] = note
    return _fit(top, out)


async def _search_window(o: str, since: str) -> tuple:
    """Every record changed since `since`, paged out of /{Object}/Search.

    Returns (records, complete) — or (None, False) if the object has no Search endpoint.
    Paging matters: taking only the first page would leave us ranking an arbitrary slice,
    which is the very mistake this whole path exists to avoid.
    """
    out: list = []
    while len(out) < _WINDOW_BOUND:
        page = await _request("GET", f"/{o}/Search",
                              params={"top": _SCAN_CAP, "skip": len(out), "brief": "true",
                                      "updated_after_utc": since})
        if isinstance(page, dict) and page.get("error"):
            return (None, False) if not out else (out, False)
        page = _brief_strip(page if isinstance(page, list) else [])
        out.extend(page)
        if len(page) < _SCAN_CAP:
            return out, True             # short page: the window is exhausted
    return out, False                    # hit the bound; there may be more in this window


_LADDER_MIN = (60, 360, 1440, 10080, 43200, 129600, 525600, 2628000)


def _minutes_ago(m: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")


def _ago(m: int) -> str:
    if m < 90:
        return f"{m}m"
    if m < 2880:
        return f"{round(m / 60)}h"
    return f"{round(m / 1440)}d"


async def _window_count(o: str, since: str) -> int:
    """How many records changed since `since` — one record on the wire, count in the header."""
    _, hdrs = await _request("GET", f"/{o}/Search", want_headers=True,
                            params={"top": 1, "brief": "true", "count_total": "true",
                                    "updated_after_utc": since})
    try:
        return int(hdrs.get("x-total-count"))
    except (TypeError, ValueError):
        return -1


async def _newest_records(o: str, want: int) -> tuple:
    """The `want` most recently created-or-updated records — correctly, at any object size.

    Insightly cannot sort, so ranking by recency means holding the candidates in memory.
    The trick is shrinking the candidate set until it fits one page: /{Object}/Search does
    filter on updated_after_utc and does report a count for a single-record request, so
    bracket a change window on a coarse ladder and then BISECT the cutoff in time until the
    window holds between `want` and one page. Every record newer than that cutoff is inside
    the window, so ranking the window ranks the whole object — exact, not a sample.

    Two real shapes: a steady stream brackets in a couple of probes; a nightly sync that
    stamps thousands of rows in one minute cannot be bisected below that cluster, but those
    records genuinely tie on recency, so the documented (recency, id) order makes the
    highest ids the answer and the window's tail is exact rather than approximate.
    """
    _, hdrs0 = await _request("GET", f"/{o}", want_headers=True,
                              params={"top": 1, "brief": "true", "count_total": "true"})
    try:
        total = int(hdrs0.get("x-total-count"))
    except (TypeError, ValueError):
        total = None

    async def rank(path: str, params: dict, basis: str) -> tuple:
        body = await _request("GET", path, params=params)
        if isinstance(body, dict) and body.get("error"):
            return body, total, "error"
        items = _brief_strip(body if isinstance(body, list) else [])
        return _sort_newest(items, o)[:want], total, basis

    # Small enough to hold whole — also the fallback when no count header comes back.
    if total is None or total <= _SCAN_CAP:
        return await rank(f"/{o}", {"top": _SCAN_CAP, "skip": 0, "brief": "true"}, "exact")

    lo, lo_count, hi, hi_count = 0, 0, -1, -1
    for m in _LADDER_MIN:
        n = await _window_count(o, _minutes_ago(m))
        if n < 0:
            break
        if n >= want:
            hi, hi_count = m, n
            break
        lo, lo_count = m, n

    if hi < 0:
        why = ("newest by id (this object has no searchable change date)"
               if hi_count < 0 and lo_count == 0
               else "newest by id (too few recent changes to bracket a window)")
        return await rank(f"/{o}", {"top": want, "skip": max(0, total - want),
                                    "brief": "true"}, why)

    guard = 14
    while hi_count > _SCAN_CAP and hi - lo > 1 and guard > 0:
        guard -= 1
        mid = (lo + hi) // 2
        n = await _window_count(o, _minutes_ago(mid))
        if n < 0:
            break
        if n >= want:
            hi, hi_count = mid, n
        else:
            lo, lo_count = mid, n

    since = _minutes_ago(hi)
    if hi_count <= _SCAN_CAP:
        return await rank(f"/{o}/Search",
                          {"top": _SCAN_CAP, "skip": 0, "brief": "true",
                           "updated_after_utc": since},
                          f"exact — ranked every record changed in the last {_ago(hi)}")

    return await rank(f"/{o}/Search",
                      {"top": _SCAN_CAP, "skip": max(0, hi_count - _SCAN_CAP),
                       "brief": "true", "updated_after_utc": since},
                      f"exact by tie-break — {hi_count} records share the newest change "
                      f"time (within {_ago(hi)}), so the highest ids win")


def _field(rec: Any, name: str) -> Any:
    """Field lookup that also sees CUSTOMFIELDS — a custom object can carry its change
    stamps there rather than at the top level."""
    if not isinstance(rec, dict):
        return None
    want = name.lower()
    for k, v in rec.items():
        if k.lower() == want and k != "CUSTOMFIELDS":
            return v
    for c in (rec.get("CUSTOMFIELDS") or []):
        if isinstance(c, dict) and str(c.get("FIELD_NAME", "")).lower() == want:
            return c.get("FIELD_VALUE")
    return None


def _project(rec: Any, fields: list, pk: Optional[str] = None) -> dict:
    """Return only the named fields (top-level or flattened out of CUSTOMFIELDS), with the
    primary key always along for joining. A field absent from the record's layout is
    omitted rather than invented — that distinction is often the answer."""
    if not isinstance(rec, dict):
        return {}
    out: dict = {}
    if pk and rec.get(pk) is not None:
        out[pk] = rec[pk]
    for f in fields:
        v = _field(rec, f)
        if v is not None:
            out[str(f)] = v
    return out


def _recency(rec: Any) -> str:
    """Sort key for 'newest': the later of created and updated.

    Insightly stamps DATE_UPDATED_UTC on create, so in practice updated >= created and
    the max is just updated — but a record that is only ever created must not sort below
    one that was merely touched, and custom objects are not guaranteed to follow the
    convention. Taking the max is correct either way. Both are ISO-ish strings that sort
    lexicographically; a missing pair sorts last (empty string)."""
    if not isinstance(rec, dict):
        return ""
    best = ""
    for f in ("DATE_UPDATED_UTC", "DATE_CREATED_UTC"):
        v = _field(rec, f)
        if v not in (None, ""):
            best = max(best, str(v))
    return best


def _record_id(rec: Any, o: Optional[str] = None) -> int:
    """Numeric primary key, for ordering when the dates cannot decide it."""
    if not isinstance(rec, dict):
        return -1
    pk = PK.get(o or "")
    raw = rec.get(pk) if pk and pk in rec else next(
        (rec[k] for k in rec if k.upper().endswith("_ID")), None)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _sort_newest(items: list, o: Optional[str] = None) -> list:
    """Newest first — and never silently a no-op.

    Comparing recency alone left records with no usable date (or with equal dates) in the
    API's ascending-id order: the OLDEST first, under a label promising the newest.
    Insightly ids increase with creation, so the id both breaks ties and stands in when no
    record carries a date."""
    return sorted(items, key=lambda r: (_recency(r), _record_id(r, o)), reverse=True)


def _sort_newest_basis(items: list) -> str:
    """What the ordering actually rests on, so callers can say so rather than assume."""
    if not items:
        return "most recently created or updated, newest first"
    dated = sum(1 for r in items if _recency(r))
    if dated == 0:
        return ("id descending — these records carry no created/updated date, so newest is "
                "inferred from the record id")
    if dated < len(items):
        return (f"most recently created or updated, newest first ({len(items) - dated} of "
                f"{len(items)} records carry no date and sort last by id)")
    return "most recently created or updated, newest first"


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


def _CLIENT_ID_RESET() -> None:
    """Invalidate the pooled client so the next call rebuilds it for the new key/pod."""
    global _CLIENT_ID
    _CLIENT_ID = (None, None)


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
                     max_records: int = 500, newest_first: bool = False) -> dict:
    """Page through an object up to max_records (hard cap FETCH_ALL_HARD_CAP), rate-paced.

    newest_first only changes WHICH records a truncated scan keeps. Paging always runs in
    the API's ascending-id order, so a capped scan normally covers the OLDEST slice of a
    big object; starting at the tail instead covers the newest, which is what a caller
    looking for "anything mentioning X" actually wants."""
    cap = min(max(int(max_records), 1), FETCH_ALL_HARD_CAP)
    out: list = []
    skip = 0
    truncated = False
    if newest_first:
        _, hdrs = await _request("GET", f"/{o}", want_headers=True,
                                 params={"top": 1, "brief": "true", "count_total": "true"})
        try:
            skip = max(0, int(hdrs.get("x-total-count")) - cap)
        except (TypeError, ValueError):
            skip = 0
        truncated = skip > 0
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
    name = SESSION.get("name") or (_env_key_label(env_key) if env_key else None)
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
async def use_saved(name: str, ctx: Context) -> dict:
    """Switch to a saved environment BY NAME — the way to change envs in any client.

    The key is read from the local key store, so it never appears in the conversation.
    Use list_saved() to see the names. Example: use_saved('demo1').
    """
    saved = _load_saved()
    if not saved:
        return {"connected": False,
                "error": "no saved environments yet. Connect once with set_api_key(key, pod, "
                         "friendly_name='demo1', save=true) and it will be reusable by name."}
    entry = saved.get(name)
    if entry is None:
        # Be forgiving about case/spacing before giving up.
        match = [k for k in saved if k.strip().lower() == (name or "").strip().lower()]
        if match:
            entry = saved[match[0]]
            name = match[0]
    if entry is None:
        return {"connected": False,
                "error": f"no saved environment called '{name}'.",
                "available": sorted(saved.keys())}
    SESSION.update(api_key=entry["api_key"], pod=entry.get("pod", "na1"), name=name)
    _CLIENT_ID_RESET()
    chk = await _request("GET", "/Contacts", params={"top": 1, "brief": "true"})
    if isinstance(chk, dict) and chk.get("error"):
        return {"connected": False, "as": name, "pod": SESSION.get("pod"),
                "error": f"switched to '{name}' but the key was rejected: {chk['error']}"}
    return {"connected": True, "as": name, "pod": SESSION.get("pod"),
            "note": f"now using the '{name}' environment."}


@mcp.tool()
def list_saved() -> dict:
    """List locally-saved environments (names + pod + masked key). Keys are never shown in
    full — switch to one with use_saved('<name>'), which never puts the key in the chat."""
    s = _load_saved()
    return {"active": SESSION.get("name"),
            "saved": [{"name": n, "pod": v.get("pod", "na1"), "key": _mask(v.get("api_key"))}
                      for n, v in s.items()],
            "switch_with": "use_saved('<name>')"}

@mcp.tool()
def rename_saved(name: str, new_name: str) -> dict:
    """Rename a saved environment. The key is untouched — only the label you switch by."""
    name, new_name = (name or "").strip(), (new_name or "").strip()
    saved = _load_saved()
    if name not in saved:
        return {"ok": False, "error": f"no saved environment called '{name}'.",
                "available": sorted(saved)}
    if not new_name:
        return {"ok": False, "error": "the new name cannot be empty."}
    if new_name != name and new_name in saved:
        return {"ok": False, "error": f"'{new_name}' already exists — pick another name."}
    saved[new_name] = saved.pop(name)
    _save_saved(saved)
    if SESSION.get("name") == name:
        SESSION["name"] = new_name          # keep the active label in step
    return {"ok": True, "renamed": {"from": name, "to": new_name}}


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


_RANK_PER_PAGE = 200          # full records are heavy; 200 keeps each page parseable
_RANK_INLINE_PAGES = 8        # what a chat turn can absorb before it should background


def _rank_key(rec: Any, field: str) -> Any:
    return _field(rec, field)


def _rank_sort(items: list, o: str, field: str, direction: str = "desc") -> list:
    """Rank in place, best-first, by an arbitrary field.

    Deliberately a comparator rather than a sort key: the value follows `direction` but ties
    ALWAYS break by record id descending (newer first), matching the (recency, id) order the
    newest-record tools use. A key-plus-reverse implementation flips the tie-break along with
    the values, which made ascending ranks disagree with the Worker's — caught by parity
    tests, so both now run this identical comparison.
    """
    desc = direction == "desc"

    def compare(a: Any, b: Any) -> int:
        ka, kb = _rank_key(a, field), _rank_key(b, field)
        sa = "" if ka is None else str(ka).strip()
        sb = "" if kb is None else str(kb).strip()
        na = nb = None
        if sa != "":
            try:
                na = float(sa)
            except ValueError:
                na = None
        if sb != "":
            try:
                nb = float(sb)
            except ValueError:
                nb = None
        if na is not None and nb is not None:
            c = (na > nb) - (na < nb)
        else:
            c = (sa > sb) - (sa < sb)
        if c == 0:
            return _record_id(b, o) - _record_id(a, o)
        return -c if desc else c

    items.sort(key=cmp_to_key(compare))
    return items


async def _rank_top_by(o: str, field: str, direction: str, filter_field: Optional[str],
                       filter_value: Optional[str], where: Optional[list], top: int,
                       fields: Optional[list], budget: int,
                       start_page: int = 0, rec: Optional[dict] = None) -> Optional[dict]:
    """Top `top` records by any field, in either direction.

    Two facts make this affordable on objects far too large to export. /{Object}/Search
    filters EXACTLY and server-side, custom fields included — PayingStatus__c=paying turns
    362k organisations into 14k before a record is fetched — and a one-record request
    reports X-Total-Count, so the job can be priced before it runs. From there only a
    bounded heap is kept, never the records, so memory is O(top) at any scale.

    Returns None when the work exceeds `budget` pages, so the caller can background it.
    """
    filtering = bool(filter_field and filter_value is not None)
    path = f"/{o}/Search" if filtering else f"/{o}"
    base: dict = {"field_name": filter_field, "field_value": filter_value} if filtering else {}

    _, hdrs = await _request("GET", path, want_headers=True,
                             params={**base, "top": 1, "brief": "true", "count_total": "true"})
    try:
        candidates = int(hdrs.get("x-total-count"))
    except (TypeError, ValueError):
        candidates = None
    pages_needed = None if candidates is None else -(-candidates // _RANK_PER_PAGE)
    if rec is None and pages_needed is not None and pages_needed > budget:
        return None

    heap: list = []
    scanned = pages = 0
    reached_end = False
    while pages < budget:
        if rec is not None and rec.get("cancel"):
            break
        body = await _request("GET", path, params={**base, "top": _RANK_PER_PAGE,
                                                  "skip": (start_page + pages) * _RANK_PER_PAGE,
                                                  "brief": "false"})
        if isinstance(body, dict) and body.get("error"):
            break
        batch = body if isinstance(body, list) else []
        pages += 1
        scanned += len(batch)
        for r in batch:
            if where and not _rank_where(r, where):
                continue
            k = _rank_key(r, field)
            if k is None or k == "":
                continue          # no value in this field: unranked, not "lowest"
            heap.append(r)
            if len(heap) > max(top * 4, 100):
                _rank_sort(heap, o, field, direction)
                del heap[top:]
        if rec is not None:
            _task_touch(rec, f"ranked {(start_page + pages) * _RANK_PER_PAGE}", progress=scanned)
        if len(batch) < _RANK_PER_PAGE:
            reached_end = True
            break

    _rank_sort(heap, o, field, direction)
    rows = heap[:top]
    if fields:
        rows = [_project(r, [field] + list(fields), PK.get(o)) for r in rows]
    exhausted = reached_end or (pages_needed is not None
                                and start_page + pages >= pages_needed)
    return {"items": rows, "scanned": scanned, "candidates": candidates,
            "pages": pages, "exhausted": exhausted, "heap": heap}


def _rank_where(rec: Any, where: list) -> bool:
    for w in where or []:
        v = _field(rec, w.get("field")) if w.get("field") else None
        if w.get("contains") is not None and w.get("field"):
            if v is None or str(w["contains"]).lower() not in str(v).lower():
                return False
            continue
        if w.get("not_empty") and (v is None or str(v).strip() == "" or v == 0):
            return False
        if w.get("equals") is not None and str(v) != str(w["equals"]):
            return False
        if w.get("gte") is not None and (v is None or str(v) < str(w["gte"])):
            return False
        if w.get("lte") is not None and (v is None or str(v) > str(w["lte"])):
            return False
    return True


async def _job_rank(rec: dict, o: str, field: str, direction: str,
                    filter_field: Optional[str], filter_value: Optional[str],
                    where: Optional[list], top: int, fields: Optional[list]) -> None:
    """Walk the whole narrowed set in the background, carrying only the leaders forward."""
    carried: list = []
    page = 0
    while True:
        res = await _rank_top_by(o, field, direction, filter_field, filter_value, where,
                                 max(top, 25), None, budget=12, start_page=page, rec=rec)
        if res is None:
            _task_finish(rec, "failed", "could not price the rank")
            rec["items"] = []
            return
        carried = _rank_sort(carried + res["heap"], o, field, direction)[:max(top * 4, 100)]
        page += res["pages"]
        rec["total"] = res["candidates"]
        if rec["cancel"]:
            _task_finish(rec, "cancelled", f"cancelled after {page * _RANK_PER_PAGE} records")
            rec["items"] = carried[:top]
            return
        if res["exhausted"] or res["pages"] == 0:
            rows = carried[:top]
            if fields:
                rows = [_project(r, [field] + list(fields), PK.get(o)) for r in rows]
            rec["items"] = rows
            rec["summary"] = {"object": o, "field": field, "direction": direction,
                              "scanned": rec.get("progress"), "candidates": res["candidates"],
                              "filtered_by": (f"{filter_field} = {filter_value}"
                                              if filter_field else None)}
            _task_finish(rec, "completed", f"ranked {res['candidates']} records")
            return


@mcp.tool()
async def top_by(object: str, field: str, ctx: Context, direction: str = "desc",
                 filter_field: Optional[str] = None, filter_value: Optional[str] = None,
                 where: Optional[list] = None, top: int = 25,
                 fields: Optional[list] = None) -> Any:
    """Top N records ranked by ANY field, ascending or descending — the ranking Insightly's
    API cannot do. This is the tool for "top customers by annual revenue", "longest-tenured
    accounts", "biggest open deals", "oldest unresolved tickets".

    ALWAYS narrow first with filter_field/filter_value when you can: Insightly filters those
    EXACTLY and server-side, custom fields included, which is what makes huge objects
    tractable — 362k organisations become 14k with PayingStatus__c=paying before a single
    record is fetched. Use describe_object to find the field and its valid values.

    direction: 'desc' (default) for biggest/latest, 'asc' for smallest/earliest — longest
    tenure is an ASCENDING sort on the start date. Ranking reads full records so custom
    fields are visible; pass `fields` for just the columns you want. Small jobs answer
    inline; large ones return a task_id to poll (task_status, then task_result). Records
    with no value in `field` are excluded rather than ranked as zero."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    direction = "asc" if str(direction).lower().startswith("asc") else "desc"
    want = min(max(int(top), 1), _HYDRATE_MAX)
    res = await _rank_top_by(o, field, direction, filter_field, filter_value, where, want,
                             fields, budget=_RANK_INLINE_PAGES)
    if res is not None:
        if not res["items"] and res["scanned"]:
            return {"error": f"no records carry a value in {field}.",
                    "scanned": res["scanned"], "candidates": res["candidates"],
                    "hint": f"confirm the field name with describe_object('{o}') — custom "
                            f"fields end in __c and are case-sensitive."}
        out = {"object": o, "ranked_by": f"{field} {direction}", "returned": len(res["items"]),
               "scanned": res["scanned"], "candidates": res["candidates"],
               "filtered_by": (f"{filter_field} = {filter_value}" if filter_field else None),
               "complete": res["exhausted"], "basis": "inline"}
        return _fit(res["items"], out)
    rec = _task_new("rank", f"{o} by {field} {direction}")
    _spawn(rec, _job_rank(rec, o, field, direction, filter_field, filter_value, where,
                          want, fields))
    return {"task_id": rec["task_id"], "status": rec["status"], "poll_interval_ms": TASK_POLL_MS,
            "next": f"too many candidates to rank in one turn — running in the background. "
                    f"task_status('{rec['task_id']}') until completed, then "
                    f"task_result('{rec['task_id']}') returns the ranked rows.",
            "hint": None if filter_field else
                    "a filter_field/filter_value would narrow this server-side and often "
                    "make it inline."}

@mcp.tool()
async def newest_by(object: str, date_field: str, ctx: Context, top: int = 50) -> Any:
    """Latest records by ANY date field — "the 50 most recently closed opportunities".

    Use this whenever the ranking field is not the created/updated stamp, e.g.
    newest_by('Opportunities', 'ACTUAL_CLOSE_DATE'). It does NOT scan the object: it prices
    a change window with one-record count probes, fetches only that window, ranks inside
    it, and reports the cost. On a 167k-opportunity org this answers in ~5 calls instead of
    336. `complete: true` means the answer is provably the true top N.

    Only works for fields that cannot postdate a record's last update (close dates, created
    dates). Forecast/due/renewal dates are rejected, because a future-dated value can sit
    outside any change window and the ranking would silently omit rows — export instead.

    Returned records are hydrated to full detail, so custom fields are present."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    field = (date_field or "").strip().upper()
    if not field:
        return {"error": "date_field is required, e.g. 'ACTUAL_CLOSE_DATE'."}
    if any(w in field for w in _FORWARD_DATED):
        return {"error": f"{field} can hold future dates, so it cannot be bounded by a "
                         "change window — the ranking would be silently incomplete.",
                "hint": "Rank on a field that is written when the event happens "
                        "(ACTUAL_CLOSE_DATE, DATE_CREATED_UTC), or run start_export and "
                        "rank the exported records."}
    return await _newest_by_field(_obj(object), field, min(max(int(top), 1), _HYDRATE_MAX))

@mcp.tool()
async def resolve_lookups(object: str, ids: list[int], ctx: Context) -> Any:
    """Turn a list of record ids into {id: name} — for the ORGANISATION_ID / CONTACT_ID
    style lookup fields that come back as bare numbers.

    Insightly has no batch-get and no field projection, so this is one GET per id (and the
    per-record endpoint ignores brief, returning ~10KB each). Doing it here means the model
    spends one tool call and gets back only the names, instead of pulling 47 full records
    into the conversation to read 47 strings. Unknown or deleted ids come back under
    `missing`."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    pk = PK.get(o)
    wanted = list(dict.fromkeys(int(i) for i in (ids or [])))[:_HYDRATE_MAX]
    if not wanted:
        return {"error": "ids is required (a list of record ids)."}
    names, missing = {}, []
    for rid in wanted:
        rec = await _request("GET", f"/{o}/{rid}")
        if not isinstance(rec, dict) or rec.get("error"):
            missing.append(rid)
            continue
        label = next((rec[f] for f in _NAME_FIELDS if rec.get(f)), None)
        if label is None:
            label = " ".join(str(rec[f]) for f in ("FIRST_NAME", "LAST_NAME") if rec.get(f)) or None
        names[str(rid)] = label
    out = {"object": o, "pk": pk, "names": names, "resolved": len(names),
           "requested": len(wanted)}
    if missing:
        out["missing"] = missing
    if len(ids or []) > _HYDRATE_MAX:
        out["note"] = (f"Only the first {_HYDRATE_MAX} ids were resolved — each one costs an "
                       "API call. Call again with the rest if you need them.")
    return out

@mcp.tool()
async def newest_records(object: str, ctx: Context, top: int = 25) -> Any:
    """The most recently created or updated records for an object, newest first.

    Use this for "latest", "recent" or "newest" questions. list_records cannot answer
    them: the API returns records in ascending id order and has no sort parameter, so its
    first page is the OLDEST records. This walks /{Object}/Search — which does honour a
    date filter — or reads the whole object when it is small enough, then ranks by the
    later of DATE_CREATED_UTC and DATE_UPDATED_UTC. The `basis` field says which."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    want = min(max(int(top), 1), _SCAN_CAP)
    items, total, basis = await _newest_records(o, want)
    if isinstance(items, dict) and items.get("error"):
        return items
    items, note = await _hydrate(o, items)
    out = {"returned": len(items), "detail_level": note,
           "sorted_by": _sort_newest_basis(items), "basis": basis}
    if total is not None:
        out["total"] = total
    return _fit(items, out)

@mcp.tool()
async def list_records(object: str, ctx: Context, top: int = 100, skip: int = 0, brief: bool = True,
                       order_by: Optional[str] = None, updated_after_utc: Optional[str] = None,
                       count_total: bool = False, fetch_all: bool = False, max_records: int = 500) -> Any:
    """List records for an object (e.g. 'Contacts'). Returns a paginated envelope:
    {items, returned, skip, top, has_more, next_skip[, total]}.

    - brief defaults True (top-level fields only — far smaller). Pass brief=false for
      every field incl. linked/custom fields.
    - Paging: default top=100 (max 500). If has_more is true, call again with the
      returned next_skip — OR pass fetch_all=true to get everything at once.
    - fetch_all=true pages through the whole object up to max_records (default 500 — one
      page, which reliably fits a tool result; raise it if you need more, hard cap 5000),
      rate-paced under the API limit; returns
      {items, total_fetched, truncated}. truncated=true means the cap was hit and more remain.
    - count_total=true adds the real `total` (from Insightly's X-Total-Count header).
    - updated_after_utc like '2026-01-01T00:00:00Z' for incremental pulls. NOTE the list
      endpoint ignores this filter; search_records applies it for real.
    - order_by like 'DATE_UPDATED_UTC desc' sorts the RETURNED records CLIENT-SIDE
      (the API has no sort param); combine with fetch_all for a global sort.

    For the newest records use newest_records — records come back in ascending id order,
    so page 1 is the OLDEST, and order_by on its own just re-sorts that oldest page.
    For finding specific records prefer search_records (exact) or filter_records (contains)."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    o = _obj(object)
    if fetch_all:
        res = await _fetch_all(o, brief=brief, updated_after_utc=updated_after_utc, max_records=max_records)
        if order_by and isinstance(res.get("items"), list):
            res["items"] = _apply_sort(res["items"], order_by)
        return _fit(res.pop("items", []), res)
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
    return _fit(env.pop("items", []), env)

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
                         brief: bool = False, max_scan: int = 1000) -> Any:
    """CONTAINS filter, done CLIENT-SIDE because Insightly's search is exact-match only.
    Scans up to max_scan records (default 1000, hard cap 5000, rate-paced) and returns
    those matching `contains` (case-insensitive) — in `field_name` if given, otherwise
    in ANY top-level field ("find anything mentioning X"). For exact match prefer
    search_records. Returns {items, matched, scanned, truncated}.

    If the object holds more than max_scan records the scan covers the NEWEST max_scan of
    them (truncated=true says so) — the API pages oldest-first, so scanning from the front
    would search only the stalest corner of a big object.

    brief defaults FALSE here on purpose: brief strips DETAILS and CUSTOMFIELDS, and those
    are exactly where a stray mention tends to hide, so a brief scan would quietly fail to
    match. Pass brief=true only when you know the term is in a top-level field and want the
    sweep to be cheaper."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    res = await _fetch_all(_obj(object), brief=brief, max_records=max_scan, newest_first=True)
    if res.get("error") and not res.get("items"):
        return res
    needle = (contains or "").lower()
    hits = [r for r in res["items"] if _record_contains(r, needle, field_name)]
    hits = _sort_newest(hits, _obj(object))
    return _fit(hits, {"matched": len(hits), "scanned": res.get("total_fetched", 0),
                       "scanned_from": "newest", "searched_fields": "every field" if not brief
                       else "top-level fields only (brief=true skips DETAILS/CUSTOMFIELDS)",
                       "truncated": res.get("truncated", False)})

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
    haven't touched yet. Returns `standard_fields` (the union of a sample of records, with
    `basis` stating exactly what that sample was) and compact
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
    """Build the field reference for an object. Shared by the tool and the resource.

    Standard fields are not published by any Insightly metadata endpoint, so they can only
    be read off actual records. This used to infer them from ONE record — the same silent
    failure mode we flag in other tools: if the API ever omitted null-valued fields, the
    list would be quietly short. Verified 2026-08 that every record of an object returns an
    identical key set (nulls included), but verified is not guaranteed, so this takes the
    UNION of a sample from both ends of the object, states how many records that was, and
    names any field that appeared in some records but not all. Custom fields come from
    /CustomFields/{object}, which IS authoritative.
    """
    out: dict = {"object": o, "pk": PK.get(o)}
    body, hdrs = await _request("GET", f"/{o}", params={"top": _DESCRIBE_SAMPLE,
                                                       "brief": "false",
                                                       "count_total": "true"},
                               want_headers=True)
    sample = [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []
    oldest, newest = len(sample), 0
    try:
        total = int((hdrs or {}).get("x-total-count", ""))
    except (TypeError, ValueError):
        total = None
    if total and total > _DESCRIBE_SAMPLE:
        # The newest end too: a field added by an admin last week can only show up there.
        tail = await _request("GET", f"/{o}", params={"top": _DESCRIBE_SAMPLE,
                                                     "skip": max(total - _DESCRIBE_SAMPLE, 0),
                                                     "brief": "false"})
        if isinstance(tail, list):
            rows = [r for r in tail if isinstance(r, dict)]
            newest = len(rows)
            sample += rows

    if isinstance(body, dict) and body.get("error"):
        out["standard_fields_error"] = body["error"]
        out["basis"] = "standard fields unavailable — the record read failed"
    elif not sample:
        out["standard_fields"] = []
        out["basis"] = "no records yet — standard fields cannot be read from data"
        out["note"] = ("create one record, or consult Insightly's API docs, for the "
                       "standard field list.")
    else:
        seen: dict = {}
        for rec in sample:
            for k in rec:
                if k in ("CUSTOMFIELDS", "ETag"):
                    continue
                seen[k] = seen.get(k, 0) + 1          # insertion order = API order
        out["standard_fields"] = list(seen)
        partial = [k for k, n in seen.items() if n < len(sample)]
        out["basis"] = (f"union of {len(sample)} records ({oldest} oldest + {newest} newest"
                        f"{f' of {total}' if total else ''}) — Insightly publishes no "
                        "standard-field metadata")
        out["sampled"] = len(sample)
        if total:
            out["total_records"] = total
        if partial:
            out["fields_partial"] = partial
            out["warning"] = ("these fields were absent from some sampled records, so this "
                              "object's field set varies by record and standard_fields may "
                              "still be incomplete — confirm against a record you care "
                              "about before relying on it.")

    cfs = await _request("GET", f"/CustomFields/{o}")
    if isinstance(cfs, list):
        out["custom_fields"] = [_cf_compact(f) for f in cfs if isinstance(f, dict)]
        out["custom_fields_basis"] = f"/CustomFields/{o} (authoritative)"
    else:
        out["custom_fields"] = []
        if isinstance(cfs, dict) and cfs.get("error"):
            out["custom_fields_error"] = cfs["error"]
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
    create_record('Contacts', {'FIRST_NAME':'Jane','LAST_NAME':'Doe'}).

    FOR TASKS, PREFER create_task. Setting OPPORTUNITY_ID / PROJECT_ID here fills the
    "Linked Opportunity/Project" field but does NOT put the task on that record's Activity
    tab — Insightly needs a separate Link too. create_task does both; otherwise follow this
    call with link_records(object='Tasks', record_id=<new task>,
    link_object_name='Opportunity'|'Project', link_object_id=<the record>)."""
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
async def create_task(title: str, ctx: Context, link_object: Optional[str] = None,
                      link_ids: Optional[list] = None, due_in_days: Optional[int] = None,
                      due_date: Optional[str] = None, details: Optional[str] = None,
                      responsible_user_id: Optional[int] = None,
                      priority: Optional[int] = None, status: Optional[str] = None) -> Any:
    """Create follow-up tasks against Opportunities or Projects — and make them actually
    appear on the record's Activity tab.

    USE THIS INSTEAD OF create_record FOR TASKS. Setting OPPORTUNITY_ID / PROJECT_ID on a
    Task fills Insightly's "Linked Opportunity/Project" field, but that alone does NOT put
    the task on that record's Activity tab — Insightly needs a separate true Link as well.
    This tool always does both, so a task created here is both associated and visible where
    people look for it.

    Pass link_ids to create one task per record in a single call — "a follow-up task on
    every open opportunity" is one call, not one per deal. Give due_in_days (e.g. 7) or an
    explicit due_date (YYYY-MM-DD)."""
    err = await _ensure(ctx)
    if err:
        return {"error": err}
    ids = [int(i) for i in (link_ids or [])][:100]
    lo = _obj(link_object) if link_object else None
    if ids and not lo:
        return {"error": "link_object is required when link_ids is given "
                         "(Opportunities or Projects)."}
    if lo and lo not in ("Opportunities", "Projects"):
        return {"error": f"tasks link to Opportunities or Projects, not '{lo}'.",
                "hint": "for other objects create the task then call link_records yourself."}
    due = due_date
    if not due and due_in_days is not None:
        due = (datetime.now(timezone.utc) + timedelta(days=int(due_in_days))).strftime("%Y-%m-%d")
    id_field = "PROJECT_ID" if lo == "Projects" else "OPPORTUNITY_ID"
    singular = "Project" if lo == "Projects" else "Opportunity"
    base: dict = {"TITLE": title, "COMPLETED": False}
    if due:
        base["DUE_DATE"] = due
    if details:
        base["DETAILS"] = details
    if responsible_user_id:
        base["RESPONSIBLE_USER_ID"] = responsible_user_id
    if priority is not None:
        base["PRIORITY"] = priority
    if status:
        base["STATUS"] = status

    created: list = []
    failed: list = []
    for rid in (ids or [None]):
        fields = dict(base)
        if rid is not None:
            fields[id_field] = rid
        task = await _request("POST", "/Tasks", json_body=fields)
        if not isinstance(task, dict) or task.get("error"):
            failed.append({"link_id": rid,
                           "error": (task or {}).get("error", "create failed")})
            continue
        row = {"task_id": task.get("TASK_ID"), "title": task.get("TITLE"),
               "due_date": task.get("DUE_DATE")}
        if rid is not None:
            row[id_field] = rid
            link = await _request("POST", f"/Tasks/{task.get('TASK_ID')}/Links",
                                  json_body={"LINK_OBJECT_NAME": singular,
                                             "LINK_OBJECT_ID": rid})
            if isinstance(link, dict) and link.get("error"):
                row["linked"] = False
                row["link_error"] = link["error"]
                row["warning"] = ("the task was created and associated, but the Activity-tab "
                                  "link failed — call link_records to finish it.")
            else:
                row["linked"] = True
                row["link_id"] = (link or {}).get("LINK_ID")
        created.append(row)

    out = {"created": len(created), "failed": len(failed), "errors": failed[:10],
           "linked_to": lo,
           "linked_count": sum(1 for r in created if r.get("linked")),
           "note": (f"each task carries {id_field} AND a true Link, so it shows on the "
                    f"{singular} Activity tab") if lo else
                   "standalone task (no record to link to)"}
    return _fit(created, out, "tasks")

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
    env = _fit(window, {"returned": len(window), "skip": start, "top": page,
                        "has_more": start + len(window) < len(items),
                        "next_skip": start + len(window), "count": len(items),
                        "status": rec["status"], "summary": rec["summary"]})
    if env.get("capped"):        # keep paging coherent after a trim
        env["returned"] = len(env["items"])
        env["next_skip"] = start + len(env["items"])
        env["has_more"] = env["next_skip"] < len(items)
    return env

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
