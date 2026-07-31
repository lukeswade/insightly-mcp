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
import pathlib
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


def part4_apps() -> None:
    print("\n4. Apps UI (io.modelcontextprotocol/ui)")
    s = Server(capabilities={"elicitation": {"form": {}}})
    tl = (s.rpc("tools/list") or {}).get("result", {})
    tools = {t["name"]: t for t in tl.get("tools", [])}
    check("env_dashboard tool is registered", "env_dashboard" in tools,
          f"{len(tools)} tools total")
    meta = (tools.get("env_dashboard", {}) or {}).get("_meta", {})
    ui = meta.get("ui", {}) if isinstance(meta, dict) else {}
    check("tool carries _meta.ui.resourceUri",
          str(ui.get("resourceUri", "")).startswith("ui://"),
          f"ui={ui}")

    res = (s.rpc("resources/list") or {}).get("result", {})
    uris = {r.get("uri"): r for r in res.get("resources", [])}
    hit = next((u for u in uris if str(u).startswith("ui://")), None)
    check("ui:// resource is listed", bool(hit), f"uris={list(uris)}")
    if hit:
        check("resource declares the mcp-app MIME type",
              "profile=mcp-app" in str(uris[hit].get("mimeType", "")),
              f"mimeType={uris[hit].get('mimeType')}")
        rd = (s.rpc("resources/read", {"uri": hit}) or {}).get("result", {})
        body = (rd.get("contents") or [{}])[0].get("text", "")
        check("resource serves the HTML document", "<title>Insightly environment" in body,
              f"{len(body)} chars")
        pathlib.Path("/tmp/app_ui_served.html").write_text(body)

    # app-only tools: the buttons' backend, hidden from the model
    for nm in ("app_records", "app_fields"):
        t = tools.get(nm, {})
        vis = ((t.get("_meta") or {}).get("ui") or {}).get("visibility")
        check(f"{nm} registered app-only", nm in tools and vis == ["app"], f"visibility={vis}")
    recs = s.call("app_records", {"object": "Contacts", "top": 5})
    check("app_records drives the drill-in panel",
          recs.get("returned") == 5 and recs.get("total") == 81,
          f"returned={recs.get('returned')} total={recs.get('total')}")
    flds = s.call("app_fields", {"object": "Opportunities"})
    check("app_fields drives the Fields view",
          isinstance(flds.get("custom_fields"), list) and flds.get("pk") == "OPPORTUNITY_ID",
          f"pk={flds.get('pk')} custom={len(flds.get('custom_fields', []))}")

    served = pathlib.Path("/tmp/app_ui_served.html")
    if served.exists():
        html = served.read_text()
        for frag, label in (("ui/initialize", "handshake"),
                            ("ui/notifications/tool-result", "data notification"),
                            ('request("tools/call"', "tool bridge"),
                            ("ui/message", "ask-in-chat bridge"),
                            ('data-explore="CustomObjects"', "explore buttons"),
                            ('data-fields=', "fields button"),
                            ('id="refresh"', "refresh button")):
            check(f"UI implements the {label}", frag in html)

    out = s.call("env_dashboard")
    check("dashboard tool returns real counts",
          isinstance(out.get("counts"), dict) and out["counts"].get("Contacts") == 81,
          f"contacts={out.get('counts', {}).get('Contacts')}")
    check("explains why no UI rendered, citing the protocol",
          "2026-07-28" in str(out.get("ui", "")), str(out.get("ui"))[:120])
    # Luke's real situation: the client CAN render apps, but the legacy initialize
    # handshake never advertises the extensions map, so nothing renders.
    s2 = Server(capabilities={"extensions": {"io.modelcontextprotocol/ui": {}}})
    out2 = s2.call("env_dashboard")
    check("same explanation even when the client declares UI support",
          "2026-07-28" in str(out2.get("ui", "")), str(out2.get("ui"))[:120])
    s2.close()
    check("dashboard reports the daily quota",
          isinstance(out.get("daily_quota"), dict) and out["daily_quota"].get("remaining") is not None,
          f"quota={out.get('daily_quota')}")
    s.close()


def part5_audit() -> None:
    print("\n5. swagger/API-doc audit fixes")
    s = Server(capabilities={})
    s.call("list_records", {"object": "Contacts", "top": 1})  # quota is learned from a response
    ci = s.call("connection_info")
    check("connection_info surfaces the daily quota (after ≥1 call)",
          isinstance(ci.get("daily_quota"), dict) and ci["daily_quota"].get("limit") is not None,
          f"{ci.get('daily_quota')}")

    sr = s.call("search_records", {"object": "Contacts", "field_name": "LAST_NAME",
                                   "field_value": "Shedron", "count_total": True})
    check("search_records supports count_total (X-Total-Count)",
          sr.get("total") is not None and sr.get("returned", 0) >= 1,
          f"total={sr.get('total')} returned={sr.get('returned')}")

    ali = s.call("list_records", {"object": "quotes", "top": 1})
    check("'quotes' aliases to Quotation (docs: 'Quote' is rejected)",
          "error" not in ali or "405" not in str(ali.get("error")),
          str(ali)[:90])

    obj = s.call("list_supported_objects")
    names = obj.get("objects", [])
    check("audited collections are discoverable",
          all(n in names for n in ("Instance", "Prospect", "PricebookEntry", "TaskCategories")),
          f"{len(names)} objects listed")

    inst = s.call("list_records", {"object": "Instance", "top": 1})
    check("Instance endpoint works (identifies the env)",
          inst.get("returned", 0) >= 1 or isinstance(inst.get("items"), list),
          str(inst.get("items", [{}]))[:100])

    # If-Match: a deliberately stale ETag must be refused, a fresh one accepted.
    one = s.call("list_records", {"object": "Contacts", "top": 1, "brief": False})
    rec = (one.get("items") or [{}])[0]
    cid, etag = rec.get("CONTACT_ID"), rec.get("ETag")
    stale = s.call("update_record", {"object": "Contacts", "record_id": cid,
                                     "fields": {"TITLE": "concurrency probe"},
                                     "if_match": "BOGUSETAG="})
    check("stale If-Match is refused (with a hint)",
          "error" in stale and "hint" in stale, str(stale)[:110])
    fresh = s.call("update_record", {"object": "Contacts", "record_id": cid,
                                     "fields": {"TITLE": rec.get("TITLE")},
                                     "if_match": etag})
    check("correct If-Match is accepted", "error" not in fresh, str(fresh)[:90])

    links = s.call("list_links", {"object": "Contacts", "record_id": cid})
    check("list_links works on a linkable object", not (isinstance(links, dict) and links.get("error")),
          str(links)[:90])
    bad = s.call("list_links", {"object": "Users", "record_id": 1})
    check("non-linkable object is rejected clearly", "Linkable objects" in str(bad.get("error", "")),
          str(bad.get("error"))[:80])
    s.close()


def main() -> int:
    part1_caching()
    s = Server(capabilities={"elicitation": {"form": {}}})
    print("\nserver:", (s.init or {}).get("result", {}).get("serverInfo"))
    try:
        part2_tasks(s)
    finally:
        s.close()
    part3_spike2(None)
    part4_apps()
    part5_audit()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} checks passed ===")
    for _, name, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
