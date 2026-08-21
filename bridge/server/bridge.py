#!/usr/bin/env python3
"""stdio <-> Streamable-HTTP bridge, v0.4: the KEY CUSTODIAN.

Claude Desktop launches this as a .mcpb extension and speaks MCP over stdio. Two jobs:

1. FORWARD every message to the remote MCP server (BRIDGE_URL), attaching the active
   environment's API key as per-request headers. The server executes tools statelessly
   and stores nothing; the host cannot tell the server is remote, so MCP Apps widgets
   render exactly as they do for a local server.

2. INTERCEPT the environment-management tools and run them HERE, against the same local
   keystore the classic .mcpb uses (~/.insightly-mcp/keys.json). Keys live on this
   machine only: they are sent per request over TLS to the worker, never stored
   server-side, never in the conversation, and always masked in listings.

Interception happens on tools/call only — tools/list still comes from the server (which
advertises these tools), so the host and the dashboard widget see one coherent surface.
"""
import json
import os
import pathlib
import platform
import queue
import sys
import threading

import httpx

DEFAULT_URL = "https://insightly-se-mcp.lukeswade.workers.dev/mcp"
URL = os.environ.get("BRIDGE_URL", "").strip() or DEFAULT_URL
TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "120"))
KEYS_FILE = os.path.expanduser("~/.insightly-mcp/keys.json")

# The worker endpoint is public, so it requires a shared credential before it will proxy
# anything to Insightly. It is injected into the bundle at build time, which is why the
# bundle is served from R2 behind a token rather than committed to this repo: the repo is
# public, and a published bundle would be a published secret. Making the user paste the
# token instead would be friction every tester pays forever to solve a hosting problem.
# Cloudflare Access is the upgrade when the audience outgrows one shared credential.
try:
    from _secret import BRIDGE_SECRET                      # type: ignore
except Exception:                                          # noqa: BLE001
    BRIDGE_SECRET = ""
# The environment wins, for tests and for a rebuild against a rotated secret.
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "").strip() or BRIDGE_SECRET

# stdin is served by a fixed pool, not a thread per line: a host that fires a burst (the
# dashboard widget can) used to spawn an unbounded number of threads. The bounded queue
# applies backpressure instead — slower under a flood, never unbounded.
RELAY_WORKERS = 12
_inbox: "queue.Queue[str]" = queue.Queue(maxsize=512)

_stdout_lock = threading.Lock()
_state_lock = threading.Lock()
_state = {"session": None, "proto": None}
_client = httpx.Client(timeout=TIMEOUT)

# ------------------------------------------------------------------ local keystore
ACTIVE = {"key": None, "pod": "na1", "name": None}


class Fatal(RuntimeError):
    """An upstream refusal that a retry cannot fix (auth, throttling, bad request)."""


def log(msg: str) -> None:
    print(f"[bridge] {msg}", file=sys.stderr, flush=True)


def load_saved() -> dict:
    try:
        with open(KEYS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_saved(d: dict) -> None:
    p = pathlib.Path(KEYS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(d, f, indent=2)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def mask(k) -> str:
    return ("…" + k[-4:]) if k and len(k) >= 4 else ("set" if k else "")


def boot_session() -> None:
    """Adopt the key from the install dialog AND persist it under the name the user chose.

    A first-time installer has no keystore, so without this they land connected-to-nothing
    with an empty environment picker — the exact dead end a new user hits. Writing the
    named entry on first boot means the picker, switching, and the dashboard all work from
    the very first question.
    """
    env_key = os.environ.get("INSIGHTLY_API_KEY", "").strip()
    if not env_key:
        return
    pod = os.environ.get("INSIGHTLY_POD", "na1").strip() or "na1"
    wanted = os.environ.get("INSIGHTLY_ENV_NAME", "").strip()
    saved = load_saved()

    # Already saved under some name? Reuse it — never duplicate the same key.
    for n, rec in saved.items():
        if rec.get("api_key") == env_key:
            ACTIVE.update(key=env_key, pod=rec.get("pod", pod), name=n)
            log(f"active environment: {n} (already saved)")
            return

    name = wanted or "demo1"
    if name in saved and saved[name].get("api_key") != env_key:
        # Same label, different key: keep the existing entry and disambiguate rather than
        # silently overwriting an environment the user still relies on.
        i = 2
        while f"{name}-{i}" in saved:
            i += 1
        name = f"{name}-{i}"
    saved[name] = {"api_key": env_key, "pod": pod}
    try:
        save_saved(saved)
        log(f"saved and activated environment: {name}")
    except Exception as e:                                     # noqa: BLE001
        log(f"could not write the keystore ({e}) — running with the key in memory only")
    ACTIVE.update(key=env_key, pod=pod, name=name)


# ------------------------------------------------------------- local tool handlers
def _verify_key(api_key: str, pod: str) -> dict:
    """Ask the WORKER (with the candidate key) whether Insightly accepts it — the
    verification logic stays central; the key makes one round trip and is not kept.

    Fails CLOSED. This used to read the answer out of `result` with defaults all the way
    down, so any response that wasn't a successful tool call — a JSON-RPC error, a 401 from
    the endpoint, an SSE frame it couldn't parse — produced an empty dict, no `error` key,
    and a verdict of "the key works". A bad key then got written to the keystore. A key is
    good only when Insightly actually answers.
    """
    try:
        r = _client.post(URL, content=json.dumps({
            "jsonrpc": "2.0", "id": "verify", "method": "tools/call",
            "params": {"name": "raw_request",
                       "arguments": {"method": "GET", "path": "/Instance"}}}),
            headers={**_headers("tools/call", "raw_request"),
                     "X-Insightly-Key": api_key, "X-Insightly-Pod": pod})
        if r.status_code >= 400:
            return {"ok": False, "error": f"the server refused the check: HTTP "
                                          f"{r.status_code} {r.text[:120]}"}
        body = _parse_single(r) or {}
        if body.get("error"):
            return {"ok": False, "error": str(body["error"])[:200]}
        content = ((body.get("result") or {}).get("content") or [])
        if not content or not content[0].get("text"):
            return {"ok": False, "error": "no answer from the verification call — "
                                          "nothing was saved."}
        info = json.loads(content[0]["text"])
        if not isinstance(info, dict) or info.get("error"):
            return {"ok": False, "error": str(info)[:200]}
        # A real /Instance answer names the instance. Anything else is not a working key.
        if not info.get("INSTANCE_NAME") and not info.get("INSTANCE_SUBDOMAIN"):
            return {"ok": False, "error": f"unexpected answer from Insightly: {str(info)[:120]}"}
        return {"ok": True, "info": info}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def handle_local(name: str, args: dict) -> dict:
    saved = load_saved()

    if name in ("list_saved", "app_envs"):
        envs = [{"name": n, "pod": saved[n].get("pod", "na1"),
                 "masked": mask(saved[n].get("api_key")), "active": n == ACTIVE["name"]}
                for n in sorted(saved)]
        if name == "list_saved":
            return {"saved": envs, "active": ACTIVE["name"],
                    "switch_with": "use_saved(name) — the key never enters the chat."}
        return {"active": ACTIVE["name"], "envs": envs, "count": len(envs)}

    if name in ("use_saved", "app_use_env"):
        n = str(args.get("name", "")).strip()
        if n not in saved:
            return {"connected": False, "available": sorted(saved),
                    "error": f"no saved environment called '{n}'."}
        ACTIVE.update(key=saved[n].get("api_key"), pod=saved[n].get("pod", "na1"), name=n)
        return {"connected": True, "as": n, "pod": ACTIVE["pod"]}

    if name in ("rename_saved", "app_rename_env"):
        n, new = str(args.get("name", "")).strip(), str(args.get("new_name", "")).strip()
        if n not in saved:
            return {"ok": False, "error": f"no saved environment called '{n}'."}
        if not new:
            return {"ok": False, "error": "new_name is required."}
        if new in saved:
            return {"ok": False, "error": f"'{new}' already exists — pick another name."}
        saved[new] = saved.pop(n)
        save_saved(saved)
        if ACTIVE["name"] == n:
            ACTIVE["name"] = new
        return {"ok": True, "renamed": {"from": n, "to": new}}

    if name in ("forget_saved", "app_remove_env"):
        n = str(args.get("name", "")).strip()
        if n not in saved:
            return {"ok": False, "error": f"no saved environment called '{n}'."}
        was_active = ACTIVE["name"] == n
        saved.pop(n)
        save_saved(saved)
        if was_active:
            ACTIVE.update(key=None, name=None)
        return {"ok": True, "removed": n, "was_active": was_active,
                "remaining": sorted(saved),
                "note": "only the key saved on this machine was dropped — nothing in "
                        "Insightly changes."}

    if name == "app_add_env":
        n = str(args.get("name", "")).strip()
        api_key = str(args.get("api_key", "")).strip()
        pod = str(args.get("pod", "na1")).strip() or "na1"
        if not n or not api_key:
            return {"ok": False, "saved": False, "error": "name and api_key are both required."}
        if n in saved:
            return {"ok": False, "saved": False, "error": f"'{n}' already exists — pick another name."}
        v = _verify_key(api_key, pod)
        if not v.get("ok"):
            return {"ok": False, "saved": False, "detail": v.get("error"),
                    "error": "that key didn't reach Insightly successfully — nothing was saved."}
        saved[n] = {"api_key": api_key, "pod": pod}
        save_saved(saved)
        ACTIVE.update(key=api_key, pod=pod, name=n)
        return {"ok": True, "saved": True, "name": n, "pod": pod, "connected": True, "as": n}

    if name == "set_api_key":
        api_key = str(args.get("api_key", "")).strip()
        pod = str(args.get("pod", "na1")).strip() or "na1"
        if not api_key:
            return {"error": "api_key is required."}
        ACTIVE.update(key=api_key, pod=pod, name="unsaved key")
        out = {"connected": True, "as": mask(api_key), "pod": pod}
        save_as = str(args.get("save_as", "")).strip()
        if save_as:
            saved[save_as] = {"api_key": api_key, "pod": pod}
            save_saved(saved)
            ACTIVE["name"] = save_as
            out["saved_as"] = save_as
        return out

    if name == "disconnect":
        ACTIVE.update(key=None, name=None)
        return {"ok": True}

    if name == "connect":
        return {"connected": bool(ACTIVE["key"]), "as": ACTIVE["name"],
                "hint": "Keys are managed on this machine. Say \"switch to <name>\" "
                        "(use_saved), add one via the dashboard's environment menu, or "
                        "set_api_key(api_key, pod, save_as)."
                        + (f" Saved: {', '.join(sorted(saved))}." if saved else "")}

    return {"error": f"bridge has no local handler for {name}"}


LOCAL_TOOLS = {"connect", "set_api_key", "disconnect", "use_saved", "list_saved",
               "rename_saved", "forget_saved", "app_envs", "app_use_env", "app_add_env",
               "app_rename_env", "app_remove_env"}

# ------------------------------------------------------------------- wire plumbing
def emit(msg: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _headers(method: str = "", name: str = "") -> dict:
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if method:
        # 2026-07-28 servers require the method mirrored in a header; harmless on 2025.
        h["Mcp-Method"] = method
    if name:
        # ...and the tool name / resource uri mirrored likewise on name-bearing methods.
        h["Mcp-Name"] = name
    with _state_lock:
        if _state["session"]:
            h["Mcp-Session-Id"] = _state["session"]
        if _state["proto"]:
            h["MCP-Protocol-Version"] = _state["proto"]
    if BRIDGE_SECRET:
        h["X-Bridge-Auth"] = BRIDGE_SECRET
    if ACTIVE["key"]:
        h["X-Insightly-Key"] = ACTIVE["key"]
        h["X-Insightly-Pod"] = ACTIVE["pod"]
        h["X-Insightly-Env"] = ACTIVE["name"] or ""
    return h


def _parse_single(r: httpx.Response):
    """One JSON-RPC message out of a non-streamed response (used by _verify_key)."""
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        data = [ln[5:].lstrip() for ln in r.text.splitlines() if ln.startswith("data:")]
        return json.loads(data[0]) if data else None
    return r.json() if r.text.strip() else None


def _deliver(payload, origin_id) -> None:
    for m in (payload if isinstance(payload, list) else [payload]):
        if not isinstance(m, dict):
            continue
        if (origin_id is not None and m.get("id") == origin_id
                and isinstance(m.get("result"), dict)):
            pv = m["result"].get("protocolVersion")
            if pv:
                with _state_lock:
                    _state["proto"] = pv
        emit(m)


def relay(line: str) -> None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        log("dropped a non-JSON line from the host")
        return
    mid = msg.get("id")

    # Environment tools run HERE — the keystore is on this machine.
    if msg.get("method") == "tools/call" and (msg.get("params") or {}).get("name") in LOCAL_TOOLS:
        name = msg["params"]["name"]
        args = (msg["params"] or {}).get("arguments") or {}
        try:
            result = handle_local(name, args)
        except Exception as e:                                 # noqa: BLE001
            result = {"error": f"bridge-local {name} failed: {e}"}
        if mid is not None:
            emit({"jsonrpc": "2.0", "id": mid,
                  "result": {"content": [{"type": "text", "text": json.dumps(result)}]}})
        return

    for attempt in (1, 2):
        try:
            with _client.stream("POST", URL, content=json.dumps(msg),
                                headers=_headers(str(msg.get("method") or ""),
                                                 str((msg.get("params") or {}).get("name")
                                                     or (msg.get("params") or {}).get("uri")
                                                     or ""))) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    with _state_lock:
                        _state["session"] = sid
                if r.status_code >= 400:
                    body = r.read().decode(errors="replace")[:200]
                    if r.status_code == 401:
                        raise Fatal("this bridge is out of date or unauthorized — reinstall "
                                    "the current Insightly SE MCP (Cloudflare) bundle")
                    if r.status_code == 429:
                        raise Fatal(f"the server is throttling this machine: {body}")
                    if 400 <= r.status_code < 500:
                        raise Fatal(f"HTTP {r.status_code}: {body}")
                    raise RuntimeError(f"HTTP {r.status_code}: {body}")
                ctype = r.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    data: list = []
                    for raw in r.iter_lines():
                        if raw == "":
                            if data:
                                _deliver(json.loads("\n".join(data)), mid)
                                data = []
                        elif raw.startswith("data:"):
                            data.append(raw[5:].lstrip())
                    if data:
                        _deliver(json.loads("\n".join(data)), mid)
                else:
                    body = r.read()
                    if body.strip():
                        _deliver(json.loads(body), mid)
            return
        except Fatal as e:
            log(f"upstream refused: {e}")
            if mid is not None:
                emit({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32001, "message": f"bridge: {e}"}})
            return
        except Exception as e:                                 # noqa: BLE001
            if attempt == 1:
                log(f"retrying after: {e}")
                continue
            log(f"upstream request failed: {e}")
            if mid is not None:
                emit({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32000,
                                "message": f"bridge: upstream request failed: {e}"}})
            return


def _tools_hash() -> str:
    """Hash of the upstream tool surface (names + descriptions + schemas)."""
    import hashlib
    r = _client.post(URL, content=json.dumps({
        "jsonrpc": "2.0", "id": "watch", "method": "tools/list", "params": {}}),
        headers=_headers("tools/list"))
    body = _parse_single(r) or {}
    tools = (body.get("result") or {}).get("tools") or []
    canon = json.dumps([[t.get("name"), t.get("description"), t.get("inputSchema")]
                        for t in tools], sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def schema_watchdog() -> None:
    """The server deploys independently of this long-lived process, but the host only
    re-reads tools/list when told to. Watch the upstream surface and announce changes,
    so a worker deploy reaches running sessions within minutes instead of at the next
    extension restart."""
    import time
    baseline = None
    delay = 20                       # first check soon after launch, then every 5 minutes
    while True:
        time.sleep(delay)
        delay = 300
        try:
            h = _tools_hash()
        except Exception as e:                                 # noqa: BLE001
            log(f"schema watchdog: poll failed ({e})")
            continue
        if baseline is None:
            baseline = h
            continue
        if h != baseline:
            baseline = h
            log("upstream tool surface changed — notifying the host")
            emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


def preflight() -> None:
    """Say enough on stderr that Claude's log alone explains a failed start.

    A bridge that dies before this prints means the launcher never reached Python at all
    (uv missing from PATH, or uv could not provision a Python) — which is itself the most
    useful thing the log can tell us.
    """
    if not BRIDGE_SECRET:
        log("WARNING: this build carries no endpoint credential — every request will come "
            "back 401. Download the bundle again from the install guide; a bundle built "
            "outside bridge/build.sh will not work.")
    log(f"python {sys.version.split()[0]} on {platform.system()} {platform.release()}")
    log(f"executable: {sys.executable}")
    log(f"httpx {httpx.__version__}")
    log(f"target: {URL}")
    log(f"keystore: {KEYS_FILE} (exists={os.path.exists(KEYS_FILE)})")
    try:
        r = _client.get(URL.rsplit("/mcp", 1)[0] + "/", timeout=20)
        log(f"worker reachable: HTTP {r.status_code}")
    except Exception as e:                                     # noqa: BLE001
        log(f"WORKER UNREACHABLE: {e} — check network/proxy access to *.workers.dev")


def _relay_worker() -> None:
    """One pool thread: take a line, relay it, never die on a single bad message."""
    while True:
        line = _inbox.get()
        try:
            relay(line)
        except Exception as e:                                 # noqa: BLE001
            log(f"relay crashed: {e}")
        finally:
            _inbox.task_done()


def main() -> None:
    preflight()
    boot_session()
    threading.Thread(target=schema_watchdog, daemon=True).start()
    for _ in range(RELAY_WORKERS):
        threading.Thread(target=_relay_worker, daemon=True).start()
    log(f"bridging stdio <-> {URL} (active env: {ACTIVE['name'] or 'none'}, "
        f"{RELAY_WORKERS} relay threads)")
    for line in sys.stdin:
        line = line.strip()
        if line:
            _inbox.put(line)
    log("stdin closed — exiting")


if __name__ == "__main__":
    main()
