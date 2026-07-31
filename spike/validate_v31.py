#!/usr/bin/env python3
"""Validation harness for the v3.1 features (SDK 2.x branch).

Checks, against the live `demo1` env:
  1. describe_object re-exposed as a CACHEABLE resource (ttl/scope actually on the wire)
  2. background TASKS: export with no cap, progress, paged result, cancel
  3. Spike 2: capability-aware elicitation (no-elicitation client gets guidance, not a crash)

Run:
    uv run --with 'mcp==2.0.0' --with 'httpx<1' --with 'pydantic<3' python spike/validate_v31.py
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "insightly_mcp.py")
UV = "/opt/homebrew/bin/uv"
DEPS = ["--with", "mcp==2.0.0", "--with", "httpx<1", "--with", "pydantic<3"]
PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class Server:
    """Minimal MCP client over stdio. Keeps stdin open (async tools die otherwise)."""

    def __init__(self, capabilities: dict, with_key: bool = True, stateless: bool = False):
        env = {"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin"}
        if with_key:
            keys = json.load(open(os.path.expanduser("~/.insightly-mcp/keys.json")))
            env["INSIGHTLY_API_KEY"] = keys["demo1"]["api_key"]
            env["INSIGHTLY_POD"] = "na1"
        self.p = subprocess.Popen([UV, "run", *DEPS, "python", SERVER],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self.err: list[str] = []
        threading.Thread(target=lambda: self.err.extend(self.p.stderr or []), daemon=True).start()
        self._id = 0
        self.stateless = stateless
        self.init = None
        if stateless:
            return  # no handshake: 2026-07-28 puts identity in each request's _meta
        self.send({"jsonrpc": "2.0", "id": self.nid(), "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": capabilities,
            "clientInfo": {"name": "validate", "version": "0"}}})
        self.init = self.wait(1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def nid(self) -> int:
        self._id += 1
        return self._id

    def send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def wait(self, want: int, budget: int = 2000):
        for _ in range(budget):
            line = self.p.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in msg and "id" in msg:      # answer server-initiated requests
                self.send({"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"action": "decline"}})
                continue
            if msg.get("id") == want:
                return msg
        return None

    def rpc(self, method: str, params: dict | None = None):
        i = self.nid()
        p = dict(params or {})
        if self.stateless:
            p["_meta"] = STATELESS_META
        self.send({"jsonrpc": "2.0", "id": i, "method": method, "params": p})
        return self.wait(i)

    def call(self, tool: str, args: dict | None = None):
        r = self.rpc("tools/call", {"name": tool, "arguments": args or {}})
        if not r or "result" not in r:
            return {"_rpc_error": (r or {}).get("error")}
        try:
            return json.loads(r["result"]["content"][0]["text"])
        except Exception:
            return {"_unparsed": str(r)[:300]}

    def close(self) -> None:
        self.p.terminate()


# Cacheable list results are a 2026-07-28 feature, and 2026-07-28 is the STATELESS
# protocol — it has no initialize handshake, so every request carries this envelope.
# A legacy `initialize` client negotiates 2025-11-25 at best and gets no cache hints.
STATELESS_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "validate", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def part1_caching() -> None:
    print("\n1. describe_object as a cacheable resource (stateless 2026-07-28 client)")
    s = Server(capabilities={}, stateless=True)

    tmpl = s.rpc("resources/templates/list")
    tres = (tmpl or {}).get("result", {})
    turis = [r.get("uriTemplate") for r in tres.get("resourceTemplates", [])]
    check("resource is advertised", any("insightly://" in str(u) for u in turis), f"templates={turis}")

    tl = (s.rpc("tools/list") or {}).get("result", {})
    check("tools/list carries a cache hint", tl.get("ttlMs") == 3_600_000,
          f"ttlMs={tl.get('ttlMs')} scope={tl.get('cacheScope')}")

    rres = (s.rpc("resources/read", {"uri": "insightly://Contacts/fields"}) or {}).get("result", {})
    check("resources/read carries a cache hint",
          rres.get("ttlMs") == 300_000 and rres.get("cacheScope") == "private",
          f"ttlMs={rres.get('ttlMs')} scope={rres.get('cacheScope')}")
    check("cache scope is private, never public (custom fields differ per env)",
          rres.get("cacheScope") == "private")
    try:
        payload = json.loads(rres["contents"][0]["text"])
    except Exception:
        payload = {}
        print("     raw read:", json.dumps(rres)[:300])
    check("resource returns real field metadata",
          payload.get("pk") == "CONTACT_ID" and len(payload.get("custom_fields", [])) > 10,
          f"pk={payload.get('pk')} custom_fields={len(payload.get('custom_fields', []))}")
    tool_out = s.call("describe_object", {"object": "Contacts"})
    check("tool and resource agree",
          tool_out.get("pk") == payload.get("pk")
          and len(tool_out.get("custom_fields", [])) == len(payload.get("custom_fields", [])),
          f"tool cf={len(tool_out.get('custom_fields', []))}")
    s.close()


def part2_tasks(s: Server) -> None:
    print("\n2. background tasks (no caps)")
    started = s.call("start_export", {"object": "Contacts", "brief": True})
    tid = started.get("task_id")
    check("start_export returns a task id immediately", bool(tid), f"status={started.get('status')}")
    if not tid:
        return
    final = None
    for _ in range(60):
        st = s.call("task_status", {"task_id": tid})
        if st.get("status") != "working":
            final = st
            break
        time.sleep(0.5)
    check("export completes", bool(final) and final.get("status") == "completed",
          f"status={(final or {}).get('status')} progress={(final or {}).get('progress')}")
    check("progress/total were tracked",
          bool(final) and final.get("total") == 81 and final.get("result_count") == 81,
          f"total={(final or {}).get('total')} count={(final or {}).get('result_count')}")

    page = s.call("task_result", {"task_id": tid, "top": 25})
    check("task_result pages the payload",
          page.get("returned") == 25 and page.get("count") == 81 and page.get("has_more") is True,
          f"returned={page.get('returned')} count={page.get('count')} next_skip={page.get('next_skip')}")
    tail = s.call("task_result", {"task_id": tid, "top": 25, "skip": 75})
    check("last page is short and ends pagination",
          tail.get("returned") == 6 and tail.get("has_more") is False,
          f"returned={tail.get('returned')} has_more={tail.get('has_more')}")
    check("brief stripping applied in export",
          all("Body" not in r for r in page.get("items", []) if isinstance(r, dict)))

    # spec surface over the same registry
    g = s.rpc("tasks/get", {"taskId": tid})
    gr = (g or {}).get("result", {})
    check("spec method tasks/get works", gr.get("status") == "completed" or gr.get("taskId") == tid,
          f"result={json.dumps(gr)[:120]}")
    lt = s.call("list_tasks")
    check("list_tasks includes the export", any(t.get("task_id") == tid for t in lt.get("tasks", [])))

    # cancel a bigger job mid-flight
    big = s.call("start_export", {"object": "Tasks", "brief": True})
    btid = big.get("task_id")
    if btid:
        s.call("cancel_task", {"task_id": btid})
        outcome = None
        for _ in range(40):
            st = s.call("task_status", {"task_id": btid})
            if st.get("status") != "working":
                outcome = st
                break
            time.sleep(0.5)
        check("cancel stops a running export",
              bool(outcome) and outcome.get("status") == "cancelled",
              f"status={(outcome or {}).get('status')} kept={(outcome or {}).get('result_count')}")


def part3_spike2(_: object) -> None:
    print("\n3. Spike 2 — capability-aware elicitation")
    # Client that declares NO elicitation support, and no key in env, so connect() must
    # explain itself instead of firing a doomed request.
    s = Server(capabilities={}, with_key=False)
    out = s.call("connect")
    msg = json.dumps(out)
    check("no-elicitation client gets actionable guidance (not a crash)",
          out.get("connected") is False and "set_api_key" in msg,
          msg[:160])
    check("a tool needing auth also degrades gracefully",
          "set_api_key" in json.dumps(s.call("list_records", {"object": "Contacts"})))
    s.close()

    # Client that declares form elicitation: server should actually prompt (we decline).
    s2 = Server(capabilities={"elicitation": {"form": {}}}, with_key=False)
    out2 = s2.call("connect")
    check("form-capable client is prompted",
          out2.get("connected") is False and "cancelled" in json.dumps(out2).lower(),
          json.dumps(out2)[:160])
    s2.close()


def main() -> int:
    part1_caching()
    s = Server(capabilities={"elicitation": {"form": {}}})
    print("\nserver:", (s.init or {}).get("result", {}).get("serverInfo"))
    try:
        part2_tasks(s)
    finally:
        s.close()
    part3_spike2(None)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} checks passed ===")
    for _, name, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
