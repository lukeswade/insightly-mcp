"""HTML for the MCP Apps UI (io.modelcontextprotocol/ui).

Kept out of insightly_mcp.py so the server file stays readable. The host renders this in a
sandboxed iframe; it must be entirely self-contained (no CDNs, no external fonts).

Wire protocol is the ext-apps contract (SEP-1865): JSON-RPC 2.0 over postMessage.
  app  → host   ui/initialize                  (handshake; response carries hostContext)
  app  → host   ui/notifications/initialized   (handshake complete)
  host → app    ui/notifications/tool-result   ({content, structuredContent}) — our data
  app  → host   tools/call                     ({name, arguments}) — the buttons
  app  → host   ui/message                     (inject a prompt into the conversation)

Everything degrades: if the handshake never lands we render nothing but a clear waiting
state, and if a `tools/call` is refused (a host may only allow app-visible tools) the
button falls back to asking the same question in chat via `ui/message`.
"""

ENV_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Insightly environment</title>
<style>
  :root {
    --bg: #FBF9F6; --card: #FFFFFF; --card2: #F7F4EF; --ink: #211B14;
    --muted: #6F6357; --line: #E4DED4; --accent: #D8382E; --good: #2E7D4F;
    --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #17130E; --card: #201B15; --card2: #191410; --ink: #EDE6DC; --muted: #A79B8D;
            --line: #352D23; --accent: #F2604F; --good: #63B888; }
  }
  :root[data-theme="dark"] {
    --bg: #17130E; --card: #201B15; --card2: #191410; --ink: #EDE6DC; --muted: #A79B8D;
    --line: #352D23; --accent: #F2604F; --good: #63B888;
  }
  :root[data-theme="light"] {
    --bg: #FBF9F6; --card: #FFFFFF; --card2: #F7F4EF; --ink: #211B14; --muted: #6F6357;
    --line: #E4DED4; --accent: #D8382E; --good: #2E7D4F;
  }
  * { box-sizing: border-box; }
  /* The widget must be legible before any host theming arrives: declare support for both
     schemes so the browser picks the right one, and paint an opaque surface rather than
     sitting transparent over an unknown background (dark ink on a dark host = "blank"). */
  html { color-scheme: light dark; }
  /* Guarantee a non-zero height at first paint: a zero-height iframe is invisible even
     when everything else is correct. */
  body { margin: 0; padding: 14px; min-height: 140px; background: var(--bg); color: var(--ink);
         font: 14px/1.5 var(--font-family, "Avenir Next", -apple-system, system-ui, sans-serif); }

  header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 12px; }
  h1 { margin: 0 4px 0 0; font-size: 16px; font-weight: 600; letter-spacing: -0.01em; }
  .pill { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
          padding: 3px 9px; border-radius: 999px; background: var(--card);
          border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
  .pill.live { color: var(--good); border-color: color-mix(in srgb, var(--good) 45%, transparent); }
  .spacer { flex: 1 1 auto; }

  button { font: inherit; color: inherit; cursor: pointer; background: var(--card);
           border: 1px solid var(--line); border-radius: 8px; padding: 5px 11px;
           font-size: 12.5px; font-weight: 500; transition: border-color .12s, background .12s; }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button:disabled { opacity: .5; cursor: default; }
  button.ghost { background: transparent; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }
  .toolbar .lbl { font-size: 11px; font-weight: 600; letter-spacing: .08em; color: var(--muted);
                  text-transform: uppercase; align-self: center; margin-right: 2px; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(124px, 1fr)); gap: 8px; }
  .tile { text-align: left; background: var(--card); border: 1px solid var(--line);
          border-radius: var(--radius); padding: 10px 11px 12px; position: relative;
          overflow: hidden; width: 100%; }
  .tile .n { font-size: 21px; font-weight: 600; font-variant-numeric: tabular-nums;
             letter-spacing: -0.02em; }
  .tile .k { font-size: 11.5px; color: var(--muted); margin-top: 1px;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tile .bar { position: absolute; left: 0; bottom: 0; height: 2px; background: var(--accent);
               opacity: .5; }
  .tile.empty .n { color: var(--muted); font-weight: 500; }
  .tile[aria-pressed="true"] { border-color: var(--accent); background: var(--card2); }

  .panel { margin-top: 12px; background: var(--card2); border: 1px solid var(--line);
           border-radius: var(--radius); overflow: hidden; }
  .panel-head { display: flex; align-items: center; gap: 8px; padding: 9px 12px;
                border-bottom: 1px solid var(--line); }
  .panel-head h2 { margin: 0; font-size: 13px; font-weight: 600; }
  .panel-body { padding: 10px 12px 12px; max-height: 340px; overflow: auto; }
  .tablewrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td { text-align: left; padding: 5px 10px 5px 0; vertical-align: top; white-space: nowrap; }
  th { font-size: 10.5px; letter-spacing: .07em; text-transform: uppercase; color: var(--muted);
       font-weight: 600; border-bottom: 1px solid var(--line); }
  tr + tr td { border-top: 1px solid var(--line); }
  td.num { font-variant-numeric: tabular-nums; }
  .chips { display: flex; flex-wrap: wrap; gap: 5px; }
  .chip { font-size: 11.5px; padding: 2px 8px; border-radius: 6px; background: var(--card);
          border: 1px solid var(--line); color: var(--muted); }
  .chip b { color: var(--ink); font-weight: 600; }
  .muted { color: var(--muted); }
  .err { color: var(--accent); font-size: 12.5px; }
  .wait, .empty-state { padding: 18px 2px; color: var(--muted); font-size: 13px; }
  .skel { height: 8px; border-radius: 4px; background: var(--line); animation: pulse 1.1s infinite; }
  @keyframes pulse { 0%,100% { opacity: .45 } 50% { opacity: .9 } }
  @media (prefers-reduced-motion: reduce) { .skel { animation: none } }
  .note { margin-top: 12px; font-size: 11.5px; color: var(--muted); }
</style>
</head>
<body>

<!-- BISECT PROBE: inline styles only, no CSS variables, no color-mix, no JS. If this
     line is visible but the rest of the widget is not, the host paints our document and
     the fault is in our CSS/JS. If even this is invisible, the host is not rendering the
     document at all and no amount of CSS work will help. -->
<div style="padding:8px 10px;margin:0 0 12px;border-radius:8px;background:#8b8b8b40;
            color:#f5f5f5;font:600 13px/1.4 -apple-system,system-ui,sans-serif">
  Insightly dashboard loaded ✓ <span style="font-weight:400;opacity:.8">(v3.1.6 probe)</span>
</div>

<header>
  <h1>Insightly environment</h1>
  <span class="pill live" id="env">…</span>
  <span class="pill" id="pod">…</span>
  <span class="pill" id="quota" hidden></span>
  <span class="spacer"></span>
  <button class="ghost" id="refresh" title="Re-read the environment">Refresh</button>
</header>

<div class="toolbar" id="toolbar" hidden>
  <span class="lbl">Explore</span>
  <button data-explore="CustomObjects">Custom objects</button>
  <button data-explore="Pipelines">Pipelines</button>
  <button data-explore="PipelineStages">Stages</button>
  <button data-explore="Users">Users</button>
  <button data-explore="Tags">Tags</button>
  <button data-explore="Currencies">Currencies</button>
</div>

<div id="body"><div class="wait">Waiting for environment data…</div></div>
<div id="panel"></div>

<script>
(function () {
  "use strict";

  // ---------------------------------------------------------------- JSON-RPC transport
  var nextId = 1, pending = {}, ready = false;

  function post(msg) { window.parent.postMessage(msg, "*"); }

  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      post({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
      setTimeout(function () {
        if (pending[id]) { delete pending[id]; reject(new Error("the host didn't answer " + method)); }
      }, 20000);
    });
  }

  function notify(method, params) {
    post({ jsonrpc: "2.0", method: method, params: params || {} });
  }

  window.addEventListener("message", function (e) {
    var m = e.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id != null && pending[m.id]) {
      var p = pending[m.id]; delete pending[m.id];
      if (m.error) p.reject(new Error(m.error.message || "host error"));
      else p.resolve(m.result);
      return;
    }
    if (m.method === "ui/notifications/tool-result") {
      render(unwrap(m.params));
    }
  });

  // Pull our payload out of whichever field the host populated.
  function unwrap(params) {
    if (!params) return null;
    if (params.structuredContent && params.structuredContent.counts) return params.structuredContent;
    var c = params.content;
    if (Array.isArray(c)) {
      for (var i = 0; i < c.length; i++) {
        if (c[i] && typeof c[i].text === "string") {
          try { var p = JSON.parse(c[i].text); if (p && p.counts) return p; } catch (err) {}
        }
      }
    }
    return params.counts ? params : null;
  }

  // ------------------------------------------------------------------------- handshake
  request("ui/initialize", {
    capabilities: {},
    clientInfo: { name: "Insightly SE MCP dashboard", version: "1.0.0" },
    protocolVersion: "2026-01-26",
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] }
  }).then(function (res) {
    var hc = (res && res.hostContext) || {};
    if (hc.theme === "light" || hc.theme === "dark") {
      document.documentElement.setAttribute("data-theme", hc.theme);
    }
    // Adopt host style variables so we look native rather than approximated.
    var vars = (hc.styles && hc.styles.variables) || {};
    Object.keys(vars).forEach(function (k) {
      document.documentElement.style.setProperty(k.indexOf("--") === 0 ? k : "--" + k, vars[k]);
    });
    finishHandshake();
  }).catch(function () {
    // Some hosts don't answer ui/initialize. Announce readiness anyway: a host that only
    // pushes tool-result after the initialized notification would otherwise never send
    // a permanently blank widget.
    finishHandshake();
  });

  function finishHandshake() {
    if (ready) return;
    ready = true;
    notify("ui/notifications/initialized", {});
    // Belt and braces: if no tool-result arrives shortly, fetch the data ourselves. A
    // rendered-but-empty panel is the worst outcome, and this removes that possibility
    // wherever the host allows an app-initiated tool call.
    setTimeout(function () {
      if (state.data) return;
      callTool("env_dashboard", {})
        .then(function (d) { if (d && d.counts) render(d); })
        .catch(function () {
          document.getElementById("body").innerHTML =
            '<div class="wait">The dashboard loaded but the host sent no data, and it '
            + 'wouldn\'t let this panel fetch it. The counts are in the message below.</div>';
        });
    }, 2500);
  }

  // ----------------------------------------------------------------------- tool bridge
  // Try the app-only tool first, then the model-facing one (hosts differ in what they
  // let an app call), then the caller falls back to asking in chat.
  function callTool(name, args, alt) {
    return request("tools/call", { name: name, arguments: args || {} }).catch(function (e) {
      if (!alt) throw e;
      return request("tools/call", { name: alt, arguments: args || {} });
    }).then(function (r) {
      if (r && r.structuredContent) return r.structuredContent;
      var c = r && r.content;
      if (Array.isArray(c)) {
        for (var i = 0; i < c.length; i++) {
          if (c[i] && typeof c[i].text === "string") {
            try { return JSON.parse(c[i].text); } catch (e) { return { text: c[i].text }; }
          }
        }
      }
      return r || {};
    });
  }

  function askInChat(text) {
    return request("ui/message", { role: "user", content: { type: "text", text: text } })
      .catch(function () {});
  }

  // ---------------------------------------------------------------------------- render
  var LABELS = {
    Contacts: "Contacts", Organisations: "Organisations", Leads: "Leads",
    Opportunities: "Opportunities", Projects: "Projects", Tasks: "Tasks",
    Events: "Events", Notes: "Notes", Emails: "Emails", Ticket: "Tickets",
    Product: "Products", KnowledgeArticle: "KB articles", Users: "Users"
  };
  var NAME_FIELDS = ["ORGANISATION_NAME", "OPPORTUNITY_NAME", "PROJECT_NAME", "QUOTATION_NAME",
                     "PRODUCT_NAME", "TITLE", "Title", "SUBJECT", "NAME", "TASK_NAME",
                     "MILESTONE_NAME", "TICKET_TITLE"];
  var state = { data: null, open: null };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c];
    });
  }

  function fmt(n) {
    return typeof n === "number" ? n.toLocaleString() : "—";
  }

  function labelOf(rec) {
    for (var i = 0; i < NAME_FIELDS.length; i++) {
      if (rec[NAME_FIELDS[i]]) return rec[NAME_FIELDS[i]];
    }
    var first = rec.FIRST_NAME || "", last = rec.LAST_NAME || "";
    if (first || last) return (first + " " + last).trim();
    return rec.EMAIL_ADDRESS || "(no name)";
  }

  function idOf(rec) {
    var k = Object.keys(rec).filter(function (x) { return /_ID$/.test(x); });
    return k.length ? rec[k[0]] : null;
  }

  function render(d) {
    if (!d || !d.counts) return;
    state.data = d;
    document.getElementById("env").textContent = d.connected_as || "connected";
    document.getElementById("pod").textContent = "pod " + (d.pod || "?");
    var q = document.getElementById("quota");
    if (d.daily_quota && d.daily_quota.remaining != null) {
      q.hidden = false;
      q.textContent = fmt(d.daily_quota.remaining) + " API calls left today";
    }
    document.getElementById("toolbar").hidden = false;

    var counts = d.counts, keys = Object.keys(counts);
    var max = Math.max.apply(null, keys.map(function (k) { return counts[k] || 0; }).concat([1]));
    var html = '<div class="grid">' + keys.map(function (k) {
      var n = counts[k], has = typeof n === "number" && n > 0;
      var w = has ? Math.max(3, Math.round((n / max) * 100)) : 0;
      return '<button class="tile' + (has ? '' : ' empty') + '" data-object="' + esc(k) + '"'
        + ' aria-pressed="false" title="' + (has ? 'Look inside ' + esc(k) : esc(k) + ' is empty') + '">'
        + '<div class="n">' + fmt(n) + '</div>'
        + '<div class="k">' + esc(LABELS[k] || k) + '</div>'
        + (has ? '<div class="bar" style="width:' + w + '%"></div>' : '')
        + '</button>';
    }).join("") + '</div>';

    if (d.failed && Object.keys(d.failed).length) {
      html += '<div class="note">Not available in this environment: '
        + Object.keys(d.failed).map(function (k) { return '<span class="chip">' + esc(k) + '</span>'; }).join(" ")
        + '</div>';
    }
    html += '<div class="note">Click any tile to look inside — newest records, or its full field list.</div>';
    document.getElementById("body").innerHTML = html;
  }

  // ----------------------------------------------------------------------- detail panel
  function panel(title, inner, actions) {
    var el = document.getElementById("panel");
    el.innerHTML = '<div class="panel"><div class="panel-head"><h2>' + esc(title) + '</h2>'
      + '<span class="spacer"></span>' + (actions || "")
      + '<button class="ghost" data-close="1">Close</button></div>'
      + '<div class="panel-body" id="pbody">' + inner + '</div></div>';
    el.scrollIntoView({ block: "nearest" });
  }

  function loading(title) {
    panel(title, '<div class="skel" style="width:70%;margin:6px 0"></div>'
      + '<div class="skel" style="width:45%;margin:6px 0"></div>'
      + '<div class="skel" style="width:58%;margin:6px 0"></div>');
  }

  function failed(title, err, fallbackPrompt) {
    panel(title, '<div class="err">' + esc(err.message || String(err)) + '</div>'
      + '<div class="note">Some hosts only let an app call its own tool. '
      + 'You can ask in the conversation instead.</div>'
      + '<button data-ask="' + esc(fallbackPrompt) + '" style="margin-top:8px">Ask in chat</button>');
  }

  function recordsTable(items) {
    if (!items || !items.length) return '<div class="empty-state">No records.</div>';
    var rows = items.slice(0, 25).map(function (r) {
      var id = idOf(r);
      return '<tr><td>' + esc(labelOf(r)) + '</td>'
        + '<td class="num muted">' + esc(id == null ? "" : id) + '</td>'
        + '<td class="muted">' + esc((r.DATE_UPDATED_UTC || r.DATE_CREATED_UTC || "").slice(0, 10)) + '</td></tr>';
    }).join("");
    return '<div class="tablewrap"><table><tr><th>Record</th><th>Id</th><th>Updated</th></tr>'
      + rows + '</table></div>';
  }

  function fieldsView(d) {
    var std = d.standard_fields || [], cf = d.custom_fields || [];
    var out = '<div class="chips" style="margin-bottom:10px">'
      + '<span class="chip"><b>' + std.length + '</b> standard</span>'
      + '<span class="chip"><b>' + cf.length + '</b> custom</span>'
      + (d.pk ? '<span class="chip">key <b>' + esc(d.pk) + '</b></span>' : '') + '</div>';
    if (cf.length) {
      out += '<div class="tablewrap"><table><tr><th>Custom field</th><th>Type</th><th>Accepts</th></tr>'
        + cf.map(function (f) {
            var accepts = f.options ? f.options.slice(0, 4).join(", ")
                            + (f.options.length > 4 ? " +" + (f.options.length - 4) : "")
                          : (f.links_to ? "→ " + f.links_to : "");
            return '<tr><td>' + esc(f.label || f.name) + '</td><td class="muted">'
              + esc((f.type || "").toLowerCase()) + '</td><td class="muted">' + esc(accepts) + '</td></tr>';
          }).join("") + '</table></div>';
    }
    if (std.length) {
      out += '<div class="note">Standard fields</div><div class="chips">'
        + std.map(function (f) { return '<span class="chip">' + esc(f) + '</span>'; }).join("") + '</div>';
    }
    return out;
  }

  function showObject(obj) {
    var actions = '<button data-fields="' + esc(obj) + '">Fields</button>'
      + '<button data-ask="Summarise the ' + esc(obj) + ' in this Insightly environment and flag anything that looks off.">Ask in chat</button>';
    loading(obj);
    callTool("app_records", { object: obj, top: 25 }, "list_records")
      .then(function (r) {
        var items = r.items || [];
        var head = '<div class="chips" style="margin-bottom:10px"><span class="chip"><b>'
          + fmt(state.data && state.data.counts ? state.data.counts[obj] : items.length)
          + '</b> total</span><span class="chip">showing newest ' + items.length + '</span></div>';
        panel(obj, head + recordsTable(items), actions);
      })
      .catch(function (e) { failed(obj, e, "List the 10 newest " + obj + " in Insightly."); });
  }

  function showFields(obj) {
    loading(obj + " · fields");
    callTool("app_fields", { object: obj }, "describe_object")
      .then(function (d) { panel(obj + " · fields", fieldsView(d),
        '<button data-object="' + esc(obj) + '">Records</button>'); })
      .catch(function (e) { failed(obj + " · fields", e, "What fields does the " + obj + " object have in Insightly?"); });
  }

  function showExplore(obj) {
    loading(obj);
    callTool("app_records", { object: obj, top: 50 }, "list_records")
      .then(function (r) {
        var items = r.items || [];
        panel(obj, '<div class="chips" style="margin-bottom:10px"><span class="chip"><b>'
          + items.length + '</b> found</span></div>' + recordsTable(items),
          '<button data-fields="' + esc(obj) + '">Fields</button>');
      })
      .catch(function (e) { failed(obj, e, "List the " + obj + " in this Insightly environment."); });
  }

  // -------------------------------------------------------------------------- wiring
  document.addEventListener("click", function (e) {
    var t = e.target.closest("button");
    if (!t) return;
    if (t.id === "refresh") {
      t.disabled = true;
      callTool("env_dashboard", {})
        .then(function (d) { if (d && d.counts) render(d); })
        .catch(function () {})
        .then(function () { t.disabled = false; });
      return;
    }
    if (t.dataset.close) { document.getElementById("panel").innerHTML = ""; markOpen(null); return; }
    if (t.dataset.ask) { askInChat(t.dataset.ask); return; }
    if (t.dataset.fields) { showFields(t.dataset.fields); markOpen(t.dataset.fields); return; }
    if (t.dataset.explore) { showExplore(t.dataset.explore); markOpen(null); return; }
    if (t.dataset.object) { showObject(t.dataset.object); markOpen(t.dataset.object); return; }
  });

  function markOpen(obj) {
    state.open = obj;
    Array.prototype.forEach.call(document.querySelectorAll(".tile"), function (el) {
      el.setAttribute("aria-pressed", String(el.dataset.object === obj));
    });
  }
})();
</script>

</body>
</html>
"""
