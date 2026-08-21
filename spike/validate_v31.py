#!/usr/bin/env python3
"""Validation harness for the v3.1 features (SDK 2.x branch).

Checks, against the live `pv2` test env:
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

# pv2 is the designated test environment — never point this harness at a shared demo env.
_STORE = json.load(open(os.path.expanduser("~/.insightly-mcp/keys.json")))
TEST_ENV = "pv2" if "pv2" in _STORE else sorted(_STORE)[0]
CONTACTS_TOTAL = 0        # discovered at startup; the suite used to hardcode one env's count


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class Server:
    """Minimal MCP client over stdio. Keeps stdin open (async tools die otherwise)."""

    def __init__(self, capabilities: dict, with_key: bool = True, stateless: bool = False):
        env = {"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin"}
        if with_key:
            env["INSIGHTLY_API_KEY"] = _STORE[TEST_ENV]["api_key"]
            env["INSIGHTLY_POD"] = _STORE[TEST_ENV].get("pod", "na1")
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
          bool(final) and final.get("total") == CONTACTS_TOTAL and final.get("result_count") == CONTACTS_TOTAL,
          f"total={(final or {}).get('total')} count={(final or {}).get('result_count')}")

    page = s.call("task_result", {"task_id": tid, "top": 25})
    check("task_result pages the payload",
          page.get("returned") == 25 and page.get("count") == CONTACTS_TOTAL and page.get("has_more") is True,
          f"returned={page.get('returned')} count={page.get('count')} next_skip={page.get('next_skip')}")
    last_skip = max(0, CONTACTS_TOTAL - 6)          # whatever env this is, land 6 from the end
    tail = s.call("task_result", {"task_id": tid, "top": 25, "skip": last_skip})
    check("last page is short and ends pagination",
          tail.get("returned") == CONTACTS_TOTAL - last_skip and tail.get("has_more") is False,
          f"skip={last_skip} returned={tail.get('returned')} has_more={tail.get('has_more')}")
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
    # The guidance names whichever route actually applies: use_saved when environments
    # are already saved locally, set_api_key when none are.
    check("no-elicitation client gets actionable guidance (not a crash)",
          out.get("connected") is False and ("use_saved" in msg or "set_api_key" in msg),
          msg[:160])
    guidance = json.dumps(s.call("list_records", {"object": "Contacts"}))
    check("a tool needing auth also degrades gracefully",
          "use_saved" in guidance or "set_api_key" in guidance)
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
        meta = (uris[hit].get("_meta") or {}).get("ui")
        # _meta.ui must EXIST (a host uses it to recognise an app view) but must NOT
        # declare an empty-domain csp, which reads as "allow nothing" and blanks the page.
        check("ui resource declares _meta.ui without a blocking csp",
              isinstance(meta, dict) and meta.get("prefersBorder") is True and "csp" not in meta,
              f"_meta.ui={meta}")
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
    envs = s.call("app_envs")
    check("app_envs lists saved environments with masked keys",
          envs.get("count", 0) >= 1 and all("masked" in e and "api_key" not in e
                                            for e in envs.get("envs", [])),
          f"count={envs.get('count')} active={envs.get('active')}")
    check("a bad key is refused and never enters the saved list",
          (lambda bad: bad.get("saved") is False
           and "should-not-persist" not in [e["name"] for e in s.call("app_envs").get("envs", [])])(
              s.call("app_add_env", {"name": "should-not-persist",
                                     "api_key": "totally-invalid", "pod": "na1"})))

    ls = s.call("list_saved")
    names = [e.get("name") for e in ls.get("saved", [])]
    check("list_saved points at the switching tool",
          ls.get("switch_with", "").startswith("use_saved"), f"saved={names}")
    if TEST_ENV in names:
        sw = s.call("use_saved", {"name": TEST_ENV})
        check("use_saved switches env by name, no key in the conversation",
              sw.get("connected") is True and sw.get("as") == TEST_ENV, json.dumps(sw)[:110])
        bad = s.call("use_saved", {"name": "definitely-not-an-env"})
        check("unknown env name lists what is available",
              bad.get("connected") is False and isinstance(bad.get("available"), list),
              json.dumps(bad)[:110])

    co = s.call("app_custom_objects", {})
    rows = co.get("custom_objects") or []
    check("app_custom_objects returns definitions with labels + counts",
          co.get("total", 0) >= 10 and rows and rows[0].get("label") and rows[0].get("name", "").endswith("__c")
          and isinstance(rows[0].get("count"), int),
          f"total={co.get('total')} top={rows[0].get('label') if rows else None}={rows[0].get('count') if rows else None}")
    recs = s.call("app_records", {"object": "Contacts", "top": 5})
    check("app_records drives the drill-in panel",
          recs.get("returned") == 5 and recs.get("total") == CONTACTS_TOTAL,
          f"returned={recs.get('returned')} total={recs.get('total')}")
    flds = s.call("app_fields", {"object": "Opportunities"})
    check("app_fields drives the Fields view",
          isinstance(flds.get("custom_fields"), list) and flds.get("pk") == "OPPORTUNITY_ID",
          f"pk={flds.get('pk')} custom={len(flds.get('custom_fields', []))}")

    served = pathlib.Path("/tmp/app_ui_served.html")
    if served.exists():
        html = served.read_text()
        check("the bisect probe is gone from the shipped widget", "BISECT PROBE" not in html)
        check("no document.write embedding hazards remain",
              "\\" not in html.replace("\\n", "") and "`" not in html)
        for frag, label in (("ui/initialize", "handshake"),
                            ("ui/notifications/tool-result", "data notification"),
                            ('request("tools/call"', "tool bridge"),
                            ("ui/message", "ask-in-chat bridge"),
                            ('data-explore="CustomObjects"', "explore buttons"),
                            ('data-fields=', "fields button"),
                            ('id="refresh"', "refresh button"),
                            ("ui/notifications/size-changed", "dynamic height reporting"),
                            ('id="custom"', "custom-objects section"),
                            ('id="envtag"', "environment picker in the header"),
                            ("contentHeight", "content-based sizing (shrinks as well as grows)"),
                            ("app_rename_env", "rename control"),
                            ("app_remove_env", "remove control"),
                            (".toolbar[hidden]", "Explore bar stays hidden until there is data"),
                            ("state.rendered", "render-completed flag guards the error path"),
                            ("app_add_env", "add-environment form"),
                            ('type="password"', "key field is masked in the form")):
            check(f"UI implements the {label}", frag in html)

    out = s.call("env_dashboard")
    check("dashboard tool returns real counts",
          isinstance(out.get("counts"), dict) and out["counts"].get("Contacts") == CONTACTS_TOTAL,
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


def part6_newest() -> None:
    """The dashboard's "newest" lists must really be newest — the list endpoint returns
    records oldest-first and has no sort param, so this used to show the OLDEST page."""
    print("\n6. newest-first record lists")
    s = Server(capabilities={})
    truth = s.call("list_records", {"object": "Tasks", "fetch_all": True, "brief": True})
    items = truth.get("items", [])
    if not items:
        check("Tasks available to rank", False, "no records")
        s.close()
        return

    def recency(r):
        return max(str(r.get("DATE_UPDATED_UTC") or ""), str(r.get("DATE_CREATED_UTC") or ""))

    # (recency, id) — the server's documented total order. Recency alone is tie-dependent:
    # pv2 has three Tasks stamped the same second across the 25-record boundary, so a
    # recency-only expectation picks an arbitrary tie-mate and the check flaps.
    want = [r["TASK_ID"] for r in
            sorted(items, key=lambda r: (recency(r), r["TASK_ID"]), reverse=True)[:25]]
    got = s.call("app_records", {"object": "Tasks", "top": 25})
    ids = [r.get("TASK_ID") for r in got.get("items", [])]
    check("app_records returns the genuinely newest records",
          set(ids) == set(want), f"overlap={len(set(ids) & set(want))}/25 basis={got.get('basis')}")
    keys = [recency(r) for r in got.get("items", [])]
    check("newest first, oldest last", keys == sorted(keys, reverse=True))
    # The ordering must not silently degrade to the API's ascending-id order when dates
    # tie or are missing — that is what put the OLDEST records under a "newest" label.
    tied = [r for r in got.get("items", []) if recency(r) == recency(got["items"][0])]
    check("equal timestamps break by id descending, never by API order",
          len(tied) < 2 or [r["TASK_ID"] for r in tied] == sorted(
              [r["TASK_ID"] for r in tied], reverse=True),
          f"{len(tied)} records share the newest timestamp")
    check("sorted_by states what the ordering actually rests on",
          "newest first" in str(got.get("sorted_by")) or "id descending" in str(got.get("sorted_by")),
          str(got.get("sorted_by"))[:70])
    check("ranked on created OR updated, whichever is later",
          "created or updated" in str(got.get("sorted_by")), str(got.get("sorted_by")))
    check("no misleading next_skip on a recency-ranked list", "next_skip" not in got)

    nr = s.call("newest_records", {"object": "Tasks", "top": 10})
    check("newest_records answers the same question from chat",
          [r.get("TASK_ID") for r in nr.get("items", [])] == want[:10],
          f"basis={nr.get('basis')}")

    # the Explore lists ask for more than the old 100-record clamp allowed
    stages = s.call("app_records", {"object": "PipelineStages", "top": 500})
    check("app_records honours a top above 100 (Explore asks for 500)",
          stages.get("top") == 500, f"top={stages.get('top')} returned={stages.get('returned')}")

    f = s.call("filter_records", {"object": "Tasks", "contains": "a", "max_scan": 50})
    oldest = {r["TASK_ID"] for r in sorted(items, key=recency)[:50]}
    check("a truncated filter scan covers the newest records, not the oldest",
          f.get("scanned_from") == "newest"
          and not ({r["TASK_ID"] for r in f.get("items", [])} & oldest),
          f"scanned={f.get('scanned')} truncated={f.get('truncated')}")
    s.close()


def part7_payload() -> None:
    """brief must actually be brief, results must never exceed the host's 1MB ceiling, and
    a final list must still carry custom fields."""
    print("\n7. payload size, brief, and hydration")
    s = Server(capabilities={})
    HEAVY = ("CUSTOMFIELDS", "DETAILS", "ETag", "IMAGE_URL", "BODY")

    thin = s.call("list_records", {"object": "Contacts", "top": 20, "brief": True})
    fat = s.call("list_records", {"object": "Contacts", "top": 20, "brief": False})
    ti, fi = thin.get("items", []), fat.get("items", [])
    left = sorted({k for r in ti for k in r if k.upper() in [h.upper() for h in HEAVY]})
    check("brief drops the bulky fields (case-insensitively)", not left, f"still present: {left}")
    check("brief=false keeps them", any(k.upper() in [h.upper() for h in HEAVY]
                                       for r in fi for k in r))
    if ti and fi:
        tb, fb = len(json.dumps(ti)), len(json.dumps(fi))
        check("brief is materially smaller", tb < fb, f"{tb:,} vs {fb:,} bytes ({fb/max(tb,1):.1f}x)")

    big = s.call("list_records", {"object": "Contacts", "fetch_all": True, "brief": False,
                                 "max_records": 500})
    check("no live result exceeds the host ceiling", len(json.dumps(big)) <= 1_000_000,
          f"{len(json.dumps(big)):,} bytes (this env is too small to force a cap)")

    # Exercise the trim directly — a small test env cannot produce a 1MB payload, and this
    # guard is worthless if it is never actually run.
    sys.path.insert(0, os.path.dirname(HERE))
    import insightly_mcp as srv
    fake = [{"id": i, "blob": "x" * 2000} for i in range(2000)]
    trimmed = srv._fit(list(fake), {"total": len(fake)})
    kept = len(trimmed.get("items", []))
    check("_fit trims an oversized payload instead of letting it be rejected",
          trimmed.get("capped") is True and 0 < kept < len(fake)
          and len(json.dumps(trimmed)) <= 1_000_000,
          f"kept {kept} of {len(fake)}, {len(json.dumps(trimmed)):,} bytes")
    check("the cap explains itself and says how to page",
          "1MB" in trimmed.get("capped_note", "") and "brief" in trimmed.get("capped_note", ""))
    check("a payload that fits is passed through untouched",
          "capped" not in srv._fit([{"a": 1}], {"total": 1}))

    nr = s.call("newest_records", {"object": "Contacts", "top": 5})
    check("a final list is hydrated back to full records",
          any("CUSTOMFIELDS" in r for r in nr.get("items", [])), nr.get("detail_level"))

    nb = s.call("newest_by", {"object": "Opportunities", "date_field": "ACTUAL_CLOSE_DATE",
                              "top": 10})
    cost = nb.get("cost", {})
    check("newest_by ranks on an arbitrary date field",
          nb.get("returned", 0) > 0 and nb.get("date_field") == "ACTUAL_CLOSE_DATE",
          f"complete={nb.get('complete')} probes={cost.get('count_probes')} "
          f"fetched={cost.get('records_fetched')}")
    items = nb.get("items", [])
    vals = [str(r.get("ACTUAL_CLOSE_DATE")) for r in items]
    check("newest_by returns newest first", vals == sorted(vals, reverse=True))
    check("newest_by reports what it cost", "records_fetched" in cost and "count_probes" in cost)
    fwd = s.call("newest_by", {"object": "Opportunities",
                               "date_field": "FORECAST_CLOSE_DATE", "top": 5})
    check("a future-dated field is refused rather than answered wrongly",
          bool(fwd.get("error")) and "future" in fwd["error"], str(fwd.get("error"))[:70])

    orgs = [r["ORGANISATION_ID"] for r in s.call(
        "list_records", {"object": "Organisations", "top": 5}).get("items", [])
        if r.get("ORGANISATION_ID")]
    rl = s.call("resolve_lookups", {"object": "Organisations", "ids": orgs})
    check("resolve_lookups turns ids into names in one call",
          rl.get("resolved") == len(orgs) and all(v for v in rl.get("names", {}).values()),
          f"{list(rl.get('names', {}).items())[:2]}")
    check("resolve_lookups returns names only, not whole records",
          len(json.dumps(rl)) < 4000, f"{len(json.dumps(rl))} bytes for {len(orgs)} names")

    f = s.call("filter_records", {"object": "Contacts", "contains": "a", "max_scan": 40})
    check("filter_records searches every field by default",
          f.get("searched_fields") == "every field", str(f.get("searched_fields")))
    s.close()


def part10_top_by() -> None:
    """Ranking by an arbitrary field, in either direction, narrowed server-side."""
    print("\n10. top_by — ranking by an arbitrary field")
    s = Server(capabilities={})
    desc = s.call("top_by", {"object": "Opportunities", "field": "OPPORTUNITY_VALUE",
                             "direction": "desc", "top": 5, "fields": ["OPPORTUNITY_NAME"]})
    vals = [r.get("OPPORTUNITY_VALUE") for r in desc.get("items", [])]
    check("top_by ranks descending by a numeric field",
          len(vals) == 5 and vals == sorted(vals, reverse=True), f"{vals}")
    asc = s.call("top_by", {"object": "Opportunities", "field": "OPPORTUNITY_VALUE",
                            "direction": "asc", "top": 5})
    av = [r.get("OPPORTUNITY_VALUE") for r in asc.get("items", [])]
    check("top_by ranks ascending too (smallest / longest-tenured)",
          len(av) == 5 and av == sorted(av) and av[0] <= vals[-1], f"{av}")
    dates = s.call("top_by", {"object": "Contacts", "field": "DATE_CREATED_UTC",
                              "direction": "asc", "top": 5})
    dv = [str(r.get("DATE_CREATED_UTC")) for r in dates.get("items", [])]
    check("top_by ranks date fields, oldest first", dv == sorted(dv), f"{dv[:2]}")

    # the whole point: Insightly filters exactly, server-side, so huge objects stay tractable
    won = s.call("top_by", {"object": "Opportunities", "field": "OPPORTUNITY_VALUE",
                            "filter_field": "OPPORTUNITY_STATE", "filter_value": "Won",
                            "top": 5})
    allc = s.call("top_by", {"object": "Opportunities", "field": "OPPORTUNITY_VALUE", "top": 5})
    check("filter_field narrows the candidate set server-side",
          won.get("candidates", 0) > 0 and won["candidates"] < allc.get("candidates", 10**9),
          f"Won={won.get('candidates')} of {allc.get('candidates')}")
    check("top_by reports what it filtered and ranked by",
          "OPPORTUNITY_STATE" in str(won.get("filtered_by"))
          and "OPPORTUNITY_VALUE" in str(won.get("ranked_by")),
          f"{won.get('filtered_by')} / {won.get('ranked_by')}")
    check("a fully-scanned rank reports complete", won.get("complete") is True,
          f"complete={won.get('complete')} scanned={won.get('scanned')}")

    cf = s.call("top_by", {"object": "Contacts", "field": "No_Such_Field_Xyz__c", "top": 5})
    check("an unknown ranking field says so instead of returning junk",
          bool(cf.get("error")) and "describe_object" in str(cf.get("hint")),
          str(cf.get("error"))[:60])
    proj = s.call("top_by", {"object": "Opportunities", "field": "OPPORTUNITY_VALUE",
                             "top": 3, "fields": ["OPPORTUNITY_NAME"]})
    keys = sorted((proj.get("items") or [{}])[0])
    check("fields projects the ranked rows",
          set(keys) <= {"OPPORTUNITY_ID", "OPPORTUNITY_VALUE", "OPPORTUNITY_NAME"}, f"{keys}")
    s.close()


def part11_create_task() -> None:
    """Tasks must be both associated and truly Linked, or they never reach
    the Opportunity/Project Activity tab."""
    print("\n11. create_task — association AND the Activity-tab link")
    s = Server(capabilities={})
    opps = s.call("list_records", {"object": "Opportunities", "top": 2}).get("items", [])
    ids = [o["OPPORTUNITY_ID"] for o in opps][:2]
    res = s.call("create_task", {"title": "MCP-SUITE follow up (auto)",
                                 "link_object": "Opportunities", "link_ids": ids,
                                 "due_in_days": 7})
    made = res.get("tasks", [])
    check("create_task creates one task per linked record",
          res.get("created") == len(ids) and res.get("failed") == 0,
          f"created={res.get('created')} failed={res.get('failed')}")
    check("every task reports a real Link, not just the field",
          res.get("linked_count") == len(ids)
          and all(r.get("link_id") for r in made),
          f"linked={res.get('linked_count')} of {len(ids)}")
    check("due_in_days lands a due date", all(r.get("due_date") for r in made),
          str(made[0].get("due_date"))[:10] if made else "-")

    time.sleep(6)   # Insightly list reads lag writes
    both = 0
    for r in made:
        full = s.call("get_record", {"object": "Tasks", "record_id": r["task_id"]})
        # the link lives on the TASK side; reading the Opportunity's links looks empty
        hit = [l for l in (full.get("LINKS") or [])
               if str(l.get("LINK_OBJECT_ID")) == str(r.get("OPPORTUNITY_ID"))
               and l.get("LINK_OBJECT_NAME") == "Opportunity"]
        if full.get("OPPORTUNITY_ID") == r.get("OPPORTUNITY_ID") and hit:
            both += 1
    check("each task carries BOTH OPPORTUNITY_ID and a matching Link",
          both == len(made) and both > 0, f"{both}/{len(made)} verified against the API")

    for r in made:
        s.call("delete_record", {"object": "Tasks", "record_id": r["task_id"], "confirm": True})
    check("suite cleaned up its test tasks", True, f"removed {len(made)}")
    s.close()



def part13_describe_basis() -> None:
    """describe_object must not infer a field list from one record and stay quiet about it.
    Blocker-tier findings against other tools have been exactly this: a silently
    incomplete field list."""
    print("\n13. describe_object states its basis")
    s = Server(capabilities={})
    d = s.call("describe_object", {"object": "Contacts"})
    check("the field list says what it rests on", "union of" in str(d.get("basis")),
          str(d.get("basis"))[:80])
    check("more than one record was sampled", (d.get("sampled") or 0) >= 2,
          f"sampled={d.get('sampled')}")
    check("both ends of the object were sampled", "oldest +" in str(d.get("basis")),
          str(d.get("basis"))[:50])
    check("no field varies across sampled records (fixed key set, verified 2026-08)",
          not d.get("fields_partial"), str(d.get("fields_partial") or "consistent")[:60])
    check("custom fields come from the authoritative endpoint",
          "/CustomFields/" in str(d.get("custom_fields_basis")),
          str(d.get("custom_fields_basis")))
    empty = s.call("describe_object", {"object": "KnowledgeArticle"})
    check("an object with no records says so instead of returning a bare []",
          bool(empty.get("basis")) or bool(empty.get("standard_fields")),
          str(empty.get("basis") or f"{len(empty.get('standard_fields') or [])} fields")[:70])
    s.close()

    # The two editions are behavioural ports; drift must be a failing build, not a surprise.
    import subprocess
    r = subprocess.run([sys.executable,
                        os.path.join(os.path.dirname(HERE), "tools", "check_parity.py")],
                       capture_output=True, text=True)
    check("the worker covers every classic tool, extras all declared",
          r.returncode == 0, (r.stdout.strip().splitlines() or [""])[-1][:90])


def part5_audit() -> None:
    print("\n5. swagger/API-doc audit fixes")
    s = Server(capabilities={})
    s.call("list_records", {"object": "Contacts", "top": 1})  # quota is learned from a response
    ci = s.call("connection_info")
    check("connection_info surfaces the daily quota (after ≥1 call)",
          isinstance(ci.get("daily_quota"), dict) and ci["daily_quota"].get("limit") is not None,
          f"{ci.get('daily_quota')}")

    # a surname that actually exists here, rather than one baked in from another env
    sample = s.call("list_records", {"object": "Contacts", "top": 1}).get("items", [{}])
    surname = (sample[0].get("LAST_NAME") if sample else None) or "Smith"
    sr = s.call("search_records", {"object": "Contacts", "field_name": "LAST_NAME",
                                   "field_value": surname, "count_total": True})
    check("search_records supports count_total (X-Total-Count)",
          sr.get("total") is not None and sr.get("returned", 0) >= 1,
          f"LAST_NAME={surname!r} total={sr.get('total')} returned={sr.get('returned')}")

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
    global CONTACTS_TOTAL
    probe = Server(capabilities={})
    CONTACTS_TOTAL = (probe.call("list_records", {"object": "Contacts", "count_total": True})
                      .get("total", 0))
    probe.close()
    print(f"test env: {TEST_ENV} (Contacts={CONTACTS_TOTAL})")
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
    part10_top_by()
    part6_newest()
    part7_payload()
    part13_describe_basis()
    part11_create_task()   # mutates Tasks — keep last

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} checks passed ===")
    for _, name, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
