#!/usr/bin/env python3
"""stdio <-> Streamable-HTTP bridge: Claude Desktop talks to a REMOTE MCP server through
the local-extension path it already trusts.

Claude Desktop launches this as a .mcpb extension and speaks MCP over stdio
(newline-delimited JSON-RPC). Every message is POSTed verbatim to the remote MCP endpoint
(BRIDGE_URL); every response — plain JSON or SSE — is written back to stdout verbatim.
The host cannot tell the server is remote, so everything that works for a local server
(including MCP Apps widget rendering) works here, while the logic itself lives in ONE
central deployment instead of on every laptop.

Deliberately not implemented (v0.1, matches the spike worker's stateless surface):
  * no GET listening stream — the stateless worker never sends server-initiated messages
  * no OAuth — the spike endpoint is public and serves synthetic data only

No API keys are involved anywhere in this bridge.
"""
import json
import os
import sys
import threading

import httpx

DEFAULT_URL = "https://insightly-mcp-spike.lukeswade.workers.dev/mcp"
URL = os.environ.get("BRIDGE_URL", "").strip() or DEFAULT_URL
TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "60"))

_stdout_lock = threading.Lock()
_state = {"session": None, "proto": None}
_client = httpx.Client(timeout=TIMEOUT)


def log(msg: str) -> None:
    """stderr only — a stray print to stdout would corrupt the JSON-RPC stream."""
    print(f"[bridge] {msg}", file=sys.stderr, flush=True)


def emit(msg: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _headers() -> dict:
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if _state["session"]:
        h["Mcp-Session-Id"] = _state["session"]
    if _state["proto"]:
        h["MCP-Protocol-Version"] = _state["proto"]
    return h


def _deliver(payload, origin_id) -> None:
    """Write upstream message(s) to the host, learning the negotiated protocol version
    from the initialize response as it passes through."""
    for m in (payload if isinstance(payload, list) else [payload]):
        if not isinstance(m, dict):
            continue
        if (origin_id is not None and m.get("id") == origin_id
                and isinstance(m.get("result"), dict)):
            pv = m["result"].get("protocolVersion")
            if pv:
                _state["proto"] = pv
        emit(m)


def relay(line: str) -> None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        log("dropped a non-JSON line from the host")
        return
    mid = msg.get("id")
    for attempt in (1, 2):                       # one retry absorbs transient edge errors
        try:
            with _client.stream("POST", URL, content=json.dumps(msg),
                                headers=_headers()) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    _state["session"] = sid
                if r.status_code >= 400:
                    body = r.read().decode(errors="replace")[:200]
                    raise RuntimeError(f"HTTP {r.status_code}: {body}")
                ctype = r.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    data: list = []
                    for raw in r.iter_lines():
                        if raw == "":            # blank line = event boundary
                            if data:
                                _deliver(json.loads("\n".join(data)), mid)
                                data = []
                        elif raw.startswith("data:"):
                            data.append(raw[5:].lstrip())
                    if data:
                        _deliver(json.loads("\n".join(data)), mid)
                else:
                    body = r.read()
                    if body.strip():             # notifications may come back empty
                        _deliver(json.loads(body), mid)
            return
        except Exception as e:                   # noqa: BLE001 — anything upstream
            if attempt == 1:
                log(f"retrying after: {e}")
                continue
            log(f"upstream request failed: {e}")
            if mid is not None:                  # never leave a request hanging
                emit({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32000,
                                "message": f"bridge: upstream request failed: {e}"}})
            return


def main() -> None:
    log(f"bridging stdio <-> {URL}")
    # One thread per in-flight message: the dashboard widget fires several tools/call
    # requests at mount, and serializing them would stall the panel.
    for line in sys.stdin:
        line = line.strip()
        if line:
            threading.Thread(target=relay, args=(line,), daemon=True).start()
    log("stdin closed — exiting")


if __name__ == "__main__":
    main()
