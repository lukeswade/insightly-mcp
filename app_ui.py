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
  html { color-scheme: light dark; height: auto; }
  /* Non-zero height at first paint (a zero-height iframe is invisible), but only a
     floor — real height is reported to the host by the ResizeObserver below. */
  body { margin: 0; padding: 14px; min-height: 96px; background: var(--bg); color: var(--ink);
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
  a.rec { color: inherit; text-decoration: none; border-bottom: 1px solid transparent;
          cursor: pointer; }
  a.rec:hover { color: var(--accent); border-bottom-color: var(--accent); }
  a.rec:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
  .panel-head h2 a.rec:hover { color: var(--accent); }
  .ext { font-size: 9px; opacity: .55; vertical-align: super; margin-left: 2px; }
  .err { color: var(--accent); font-size: 12.5px; }
  .wait, .empty-state { padding: 18px 2px; color: var(--muted); font-size: 13px; }
  .skel { height: 8px; border-radius: 4px; background: var(--line); animation: pulse 1.1s infinite; }
  @keyframes pulse { 0%,100% { opacity: .45 } 50% { opacity: .9 } }
  @media (prefers-reduced-motion: reduce) { .skel { animation: none } }
  .note { margin-top: 12px; font-size: 11.5px; color: var(--muted); }
  /* The current-environment tag in the header IS the picker. */
  .envwrap { position: relative; }
  button.envtag { font-size: 11px; font-weight: 600; letter-spacing: .06em;
                  text-transform: uppercase; padding: 3px 9px 3px 11px; border-radius: 999px;
                  background: var(--card); border: 1px solid var(--line); color: var(--good);
                  border-color: color-mix(in srgb, var(--good) 45%, transparent);
                  display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
  button.envtag::after { content: "▾"; font-size: 9px; opacity: .7; }
  button.envtag:hover { border-color: var(--accent); color: var(--accent); }
  .envmenu { position: absolute; top: calc(100% + 6px); left: 0; z-index: 30; min-width: 268px;
             background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
             box-shadow: 0 8px 24px rgba(0,0,0,.28); padding: 6px; }
  .envmenu .mlbl { font-size: 10px; font-weight: 600; letter-spacing: .09em; color: var(--muted);
                   text-transform: uppercase; padding: 5px 8px 4px; }
  .envrow { display: flex; align-items: center; gap: 4px; }
  .envrow > button.pick { flex: 1 1 auto; text-align: left; border: none; background: none;
                          padding: 6px 8px; border-radius: 7px; font-size: 13px; }
  .envrow > button.pick:hover { background: var(--card2); border: none; }
  .envrow > button.pick .meta { display: block; font-size: 10.5px; color: var(--muted); }
  .envrow.on > button.pick { color: var(--good); font-weight: 600; }
  .envrow .iconbtn { border: none; background: none; padding: 4px 6px; font-size: 11px;
                     color: var(--muted); border-radius: 6px; }
  .envrow .iconbtn:hover { background: var(--card2); color: var(--accent); border: none; }
  .envmenu hr { border: none; border-top: 1px solid var(--line); margin: 5px 2px; }
  .envmenu .addenv { width: 100%; border-style: dashed; color: var(--muted); }
  .envmenu .confirm { padding: 6px 8px; font-size: 12px; }
  .envmenu .confirm b { color: var(--ink); }
  .envmenu .confirm .row { display: flex; gap: 6px; margin-top: 6px; }
  .envmenu input.rn { width: 100%; font: inherit; font-size: 13px; padding: 5px 8px;
                      color: var(--ink); background: var(--card2); border: 1px solid var(--line);
                      border-radius: 7px; }
  .envform { width: 100%; display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
             margin-top: 8px; padding-top: 10px; border-top: 1px solid var(--line); }
  @media (max-width: 520px) { .envform { grid-template-columns: 1fr; } }
  .envform label { font-size: 11px; font-weight: 600; letter-spacing: .06em; color: var(--muted);
                   text-transform: uppercase; display: block; margin-bottom: 3px; }
  .envform input { width: 100%; font: inherit; font-size: 13px; padding: 6px 9px;
                   color: var(--ink); background: var(--card); border: 1px solid var(--line);
                   border-radius: 8px; }
  .envform input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .envform .wide { grid-column: 1 / -1; }
  .envform .actions { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; }
  .envform .privacy { font-size: 11px; color: var(--muted); line-height: 1.45; }
  .envform .status { font-size: 12px; }
  .envform .status.bad { color: var(--accent); }
  .envform .status.good { color: var(--good); }
  .section { margin-top: 14px; }
  .section-head { display: flex; align-items: baseline; gap: 8px; margin: 0 0 8px; }
  .section-head h2 { margin: 0; font-size: 11px; font-weight: 600; letter-spacing: .08em;
                     text-transform: uppercase; color: var(--accent); }
  .section-head span { font-size: 11.5px; color: var(--muted); }
  .tile.custom .k { color: var(--ink); font-weight: 500; }
  .tile.custom .api { font-size: 10.5px; color: var(--muted); margin-top: 2px;
                      font-family: ui-monospace, Menlo, monospace;
                      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
</head>
<body>

<header>
  <h1>Insightly environment</h1>
  <span class="envwrap"><button class="envtag" id="envtag" aria-haspopup="menu" aria-expanded="false">…</button><span id="envmenu"></span></span>
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
</div>

<div id="body"><div class="wait">Waiting for environment data…</div></div>
<div id="custom"></div>
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

  // Dynamic height. Implementing the raw protocol means we own the size notifications
  // the SDK would otherwise send, so watch the document and tell the host whenever our
  // content grows or shrinks (opening a drill-in panel, loading custom objects...).
  var lastH = 0;
  function contentHeight() {
    // Bottom edge of the last visible child + the body's bottom padding. Unlike
    // scrollHeight this is independent of the iframe's current height, so it shrinks.
    var kids = document.body.children, bottom = 0;
    for (var i = 0; i < kids.length; i++) {
      var r = kids[i].getBoundingClientRect();
      if (r.height > 0 && r.bottom > bottom) bottom = r.bottom;
    }
    var pad = parseFloat(getComputedStyle(document.body).paddingBottom) || 0;
    return bottom > 0 ? Math.ceil(bottom + pad) : 0;
  }
  function reportSize() {
    var h = contentHeight();
    if (!h || Math.abs(h - lastH) < 2) return;
    lastH = h;
    notify("ui/notifications/size-changed", { width: document.documentElement.clientWidth, height: h });
  }
  if (typeof ResizeObserver === "function") {
    var ro = new ResizeObserver(function () { reportSize(); });
    ro.observe(document.documentElement);
    ro.observe(document.body);
  }
  window.addEventListener("load", reportSize);
  setInterval(reportSize, 1000);   // cheap backstop for late layout shifts

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
            + 'could not let this panel fetch it. The counts are in the message below.</div>';
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

  // ---------------------------------------------------------------- CRM deep links
  // Insightly's blade view keeps the list in place and slides the record over it, which is
  // what an SE actually wants; the full-page /details/... URL loses that context.
  //   list:   https://crm.{pod}.insightly.com/list/Organisation/
  //   record: https://crm.{pod}.insightly.com/list/Organisation/?blade=/details/organisation/{id}
  // The list segment is singular PascalCase; the blade segment is the same word lowercased.
  var CRM_SEG = {
    Contacts: "Contact", Organisations: "Organisation", Leads: "Lead",
    Opportunities: "Opportunity", Projects: "Project", Tasks: "Task", Events: "Event",
    Notes: "Note", Emails: "Email", Ticket: "Ticket", Product: "Product",
    Quotation: "Quotation", Pricebook: "Pricebook", Milestones: "Milestone",
    KnowledgeArticle: "KnowledgeArticle"
  };
  // Reference/config objects have no blade view — never fake a link for them.
  var NO_CRM = { Users: 1, Pipelines: 1, PipelineStages: 1, CustomObjects: 1, Currencies: 1,
                 Tags: 1, TeamMembers: 1, Teams: 1, Instance: 1 };

  function crmSegment(obj) {
    if (!obj || NO_CRM[obj]) return null;
    if (CRM_SEG[obj]) return CRM_SEG[obj];
    if (/__c$/.test(obj)) return obj;              // custom objects keep their API name
    return obj.replace(/ies$/, "y").replace(/s$/, "");
  }

  function crmBase() {
    var pod = (state.data && state.data.pod) || "na1";
    return "https://crm." + pod + ".insightly.com";
  }

  function listUrl(obj) {
    var seg = crmSegment(obj);
    return seg ? crmBase() + "/list/" + seg + "/" : null;
  }

  function recordUrl(obj, id) {
    var seg = crmSegment(obj);
    if (!seg || id === null || id === undefined || id === "") return null;
    return crmBase() + "/list/" + seg + "/?blade=/details/" + seg.toLowerCase() + "/" + id;
  }

  function link(url, text, extra) {
    if (!url) return esc(text);
    return '<a class="rec" data-open="' + esc(url) + '" href="' + esc(url)
      + '" title="Open in Insightly">' + esc(text) + '</a>'
      + (extra === false ? "" : '<span class="ext">&#8599;</span>');
  }

  function openExternal(url) {
    // Prefer the host bridge: the iframe sandbox normally blocks navigation. Fall back to
    // window.open where the host permits it.
    request("ui/open-link", { url: url }).catch(function () {
      try { window.open(url, "_blank", "noopener"); } catch (e) {}
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
  var NAME_FIELDS = ["RECORD_NAME", "ORGANISATION_NAME", "OPPORTUNITY_NAME", "PROJECT_NAME", "QUOTATION_NAME",
                     "PRODUCT_NAME", "TITLE", "Title", "SUBJECT", "NAME", "TASK_NAME",
                     "MILESTONE_NAME", "TICKET_TITLE"];
  var state = { data: null, open: null, custom: null, pipes: null, stages: null,
               envs: null, activeEnv: null, addOpen: false, menuOpen: false,
               renaming: null, removing: null };

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
    if (rec.EMAIL_ADDRESS) return rec.EMAIL_ADDRESS;
    // Custom objects name their field after themselves (e.g. Crew_Name__c), so fall back
    // to any *_NAME / *NAME__c key before giving up.
    var k = Object.keys(rec).filter(function (x) { return /NAME/i.test(x) && rec[x]; });
    return k.length ? rec[k[0]] : "(no name)";
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
    reportSize();
    if (state.envs === null) { state.envs = []; state.activeEnv = d.connected_as; loadEnvs(false); }
    else { envTag(); }
    if (state.custom === null) { state.custom = []; loadCustomObjects(); }
  }

  // ----------------------------------------------------------------------- detail panel
  function panel(title, inner, actions, titleUrl) {
    var el = document.getElementById("panel");
    var heading = titleUrl ? link(titleUrl, title) : esc(title);
    el.innerHTML = '<div class="panel"><div class="panel-head"><h2>' + heading + '</h2>'
      + '<span class="spacer"></span>' + (actions || "")
      + '<button class="ghost" data-close="1">Close</button></div>'
      + '<div class="panel-body" id="pbody">' + inner + '</div></div>';
    el.scrollIntoView({ block: "nearest" });
    reportSize();
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

  function recordsTable(items, obj) {
    if (!items || !items.length) return '<div class="empty-state">No records.</div>';
    var rows = items.slice(0, 25).map(function (r) {
      var id = idOf(r), url = recordUrl(obj, id);
      return '<tr><td>' + link(url, labelOf(r)) + '</td>'
        + '<td class="num muted">' + link(url, id == null ? "" : String(id), false) + '</td>'
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

  // ------------------------------------------------------------- environment picker
  // The header tag is the control: click it for a menu of saved environments. Switching
  // is secret-free — the server reads the key from its local store, so no key passes
  // through this app, the host, or the conversation.
  function envTag() {
    var t = document.getElementById("envtag");
    t.textContent = state.activeEnv || (state.data && state.data.connected_as) || "environment";
    t.setAttribute("aria-expanded", state.menuOpen ? "true" : "false");
  }

  function envMenu() {
    var el = document.getElementById("envmenu");
    if (!state.menuOpen) { el.innerHTML = ""; envTag(); reportSize(); return; }
    var envs = state.envs || [], active = state.activeEnv;
    var html = '<div class="envmenu" role="menu"><div class="mlbl">Switch environment</div>';
    if (!envs.length) {
      html += '<div class="confirm muted">No saved environments yet.</div>';
    } else {
      html += envs.map(function (e) {
        if (state.renaming === e.name) {
          return '<form class="confirm" data-rnform="' + esc(e.name) + '">'
            + '<input class="rn" name="new_name" value="' + esc(e.name) + '" autocomplete="off">'
            + '<div class="row"><button type="submit">Save name</button>'
            + '<button type="button" class="ghost" data-envcancel="1">Cancel</button></div></form>';
        }
        if (state.removing === e.name) {
          return '<div class="confirm">Remove <b>' + esc(e.name) + '</b> from this list?'
            + '<div class="row"><button data-rmyes="' + esc(e.name) + '">Remove</button>'
            + '<button class="ghost" data-envcancel="1">Cancel</button></div>'
            + '<div class="muted" style="margin-top:6px">Only the saved key on this Mac is '
            + 'dropped. Nothing in Insightly changes.</div></div>';
        }
        return '<div class="envrow' + (e.name === active ? " on" : "") + '" role="none">'
          + '<button class="pick" role="menuitem" data-env="' + esc(e.name) + '">'
          + esc(e.name) + (e.name === active ? " &#10003;" : "")
          + '<span class="meta">pod ' + esc(e.pod) + ' &middot; key ' + esc(e.masked) + '</span>'
          + '</button>'
          + '<button class="iconbtn" data-rn="' + esc(e.name) + '" title="Rename">Rename</button>'
          + '<button class="iconbtn" data-rm="' + esc(e.name) + '" title="Remove">Remove</button>'
          + '</div>';
      }).join("");
    }
    html += '<hr>';
    if (state.addOpen) {
      html += '<form class="envform" id="envform">'
        + '<div class="wide"><label for="envname">Name</label>'
        + '<input id="envname" name="name" placeholder="e.g. acme-demo" autocomplete="off" required></div>'
        + '<div class="wide"><label for="envpod">Pod</label>'
        + '<input id="envpod" name="pod" value="na1" autocomplete="off"></div>'
        + '<div class="wide"><label for="envkey">Insightly API key</label>'
        + '<input id="envkey" name="api_key" type="password" autocomplete="off"'
        + ' placeholder="Insightly then User Settings then API" required></div>'
        + '<div class="actions"><button type="submit" id="envsave">Verify and save</button>'
        + '<button type="button" data-envcancel="1" class="ghost">Cancel</button>'
        + '<span class="status" id="envstatus"></span></div>'
        + '<div class="wide privacy">Verified against Insightly, then stored on this Mac so you '
        + 'can switch by name. The key travels through this tool call and may appear in the '
        + 'conversation log - demo environments only.</div></form>';
    } else {
      html += '<button class="addenv" data-addenv="1">+ Add environment</button>';
    }
    html += '</div>';
    el.innerHTML = html;
    envTag();
    reportSize();
    var f = document.getElementById("envname");
    if (f && state.addOpen) f.focus();
  }

  function closeMenu() {
    state.menuOpen = false; state.addOpen = false;
    state.renaming = null; state.removing = null;
    envMenu();
  }

  function loadEnvs(afterSwitch) {
    return callTool("app_envs", {}, "list_saved").then(function (r) {
      var list = r.envs || r.saved || [];
      state.envs = list.map(function (e) {
        return { name: e.name, pod: e.pod || "na1", masked: e.masked || e.key || "" };
      });
      state.activeEnv = r.active || state.activeEnv;
      envMenu();
      if (afterSwitch) refreshDashboard();
    }).catch(function () { envTag(); });
  }

  function refreshDashboard() {
    return callTool("env_dashboard", {}).then(function (d) {
      if (d && d.counts) {
        state.custom = null;                     // custom objects differ per environment
        document.getElementById("custom").innerHTML = "";
        document.getElementById("panel").innerHTML = "";
        render(d);
        reportSize();                            // the new env may need LESS height
      }
    }).catch(function () {});
  }

  function switchEnv(name) {
    closeMenu();
    callTool("app_use_env", { name: name }, "use_saved")
      .then(function (r) {
        if (r && r.connected) { state.activeEnv = r.as || name; return loadEnvs(true); }
      })
      .catch(function () {});
  }

  function renameEnv(oldName, newName) {
    callTool("app_rename_env", { name: oldName, new_name: newName }, "rename_saved")
      .then(function (r) {
        if (r && r.ok && state.activeEnv === oldName) state.activeEnv = newName;
        state.renaming = null;
        loadEnvs(false);
      })
      .catch(function () { state.renaming = null; envMenu(); });
  }

  function removeEnv(name) {
    callTool("app_remove_env", { name: name }, "forget_saved")
      .then(function () { state.removing = null; loadEnvs(false); })
      .catch(function () { state.removing = null; envMenu(); });
  }

  function submitEnv(form) {
    var status = document.getElementById("envstatus"), btn = document.getElementById("envsave");
    var name = form.name.value.trim(), key = form.api_key.value.trim(),
        pod = (form.pod.value || "na1").trim();
    if (!name || !key) {
      status.className = "status bad"; status.textContent = "Name and API key are both required.";
      return;
    }
    btn.disabled = true;
    status.className = "status"; status.textContent = "Verifying against Insightly...";
    callTool("app_add_env", { name: name, api_key: key, pod: pod })
      .then(function (r) {
        if (r && r.saved) {
          form.api_key.value = "";               // do not leave the key in the DOM
          state.activeEnv = name;
          closeMenu();
          loadEnvs(true);
        } else {
          btn.disabled = false;
          status.className = "status bad";
          status.textContent = (r && (r.error || r.hint)) || "Could not save that environment.";
        }
      })
      .catch(function () {
        btn.disabled = false;
        status.className = "status bad";
        status.textContent = "This host would not let the panel save it - ask in chat instead.";
      });
  }

  function customObjectsGrid(rows) {
    if (!rows || !rows.length) return "";
    var max = Math.max.apply(null, rows.map(function (r) { return r.count || 0; }).concat([1]));
    return '<div class="section"><div class="section-head"><h2>Custom objects</h2>'
      + '<span>' + rows.length + ' defined in this environment</span></div><div class="grid">'
      + rows.map(function (r) {
          var n = r.count, has = typeof n === "number" && n > 0;
          var w = has ? Math.max(3, Math.round((n / max) * 100)) : 0;
          return '<button class="tile custom' + (has ? '' : ' empty') + '"'
            + ' data-object="' + esc(r.name) + '" aria-pressed="false"'
            + ' title="' + esc(r.label) + ' (' + esc(r.name) + ')">'
            + '<div class="n">' + fmt(n) + '</div>'
            + '<div class="k">' + esc(r.label) + '</div>'
            + '<div class="api">' + esc(r.name) + '</div>'
            + (has ? '<div class="bar" style="width:' + w + '%"></div>' : '')
            + '</button>';
        }).join("") + '</div></div>';
  }

  function loadCustomObjects() {
    callTool("app_custom_objects", {})
      .then(function (r) {
        state.custom = r.custom_objects || [];
        document.getElementById("custom").innerHTML = customObjectsGrid(state.custom);
        reportSize();
      })
      .catch(function () { /* core dashboard still stands on its own */ });
  }

  // /CustomObjects returns DEFINITIONS, not records — so render them as what they are.
  function showCustomObjects() {
    var rows = state.custom;
    if (!rows || !rows.length) {
      loading("Custom objects");
      callTool("app_custom_objects", {}).then(function (r) {
        state.custom = r.custom_objects || [];
        showCustomObjects();
      }).catch(function (e) {
        failed("Custom objects", e, "List the custom objects in this Insightly environment.");
      });
      return;
    }
    var body = '<div class="tablewrap"><table>'
      + '<tr><th>Custom object</th><th>API name</th><th>Records</th><th>Nav bar</th></tr>'
      + rows.map(function (r) {
          return '<tr><td>' + esc(r.label) + '</td>'
            + '<td class="muted">' + esc(r.name) + '</td>'
            + '<td class="num">' + fmt(r.count) + '</td>'
            + '<td class="muted">' + (r.in_navbar ? "yes" : "—") + '</td></tr>';
        }).join("") + '</table></div>'
      + '<div class="note">Click a custom-object card above to browse its records.</div>';
    panel("Custom objects · " + rows.length, body,
      '<button data-ask="Summarise the custom objects in this Insightly environment and what they are used for.">Ask in chat</button>');
  }

  // A custom object's API name (Revenue__c) is not what anyone calls it, so title the
  // panel with its display label and keep the API name as the subtitle chip.
  function displayName(obj) {
    var hit = (state.custom || []).filter(function (c) { return c.name === obj; })[0];
    return hit ? hit.label : (LABELS[obj] || obj);
  }

  function pipelinesView(pipes, stages) {
    var counts = {};
    (stages || []).forEach(function (st) {
      counts[st.PIPELINE_ID] = (counts[st.PIPELINE_ID] || 0) + 1;
    });
    var rows = (pipes || []).slice().sort(function (a, b) {
      return String(a.PIPELINE_NAME || "").localeCompare(String(b.PIPELINE_NAME || ""));
    });
    if (!rows.length) return '<div class="empty-state">No pipelines.</div>';
    return '<div class="tablewrap"><table>'
      + '<tr><th>Pipeline</th><th>Stages</th><th>Used for</th><th>Id</th></tr>'
      + rows.map(function (p) {
          var used = p.FOR_OPPORTUNITIES ? "Opportunities" : (p.FOR_PROJECTS ? "Projects" : "—");
          return '<tr><td>' + esc(p.PIPELINE_NAME) + '</td>'
            + '<td class="num">' + fmt(counts[p.PIPELINE_ID] || 0) + '</td>'
            + '<td class="muted">' + esc(used) + '</td>'
            + '<td class="num muted">' + esc(p.PIPELINE_ID) + '</td></tr>';
        }).join("") + '</table></div>';
  }

  // Grouped by parent pipeline, stage number descending inside each group.
  function stagesView(stages, pipes) {
    var names = {};
    (pipes || []).forEach(function (p) { names[p.PIPELINE_ID] = p.PIPELINE_NAME; });
    var groups = {};
    (stages || []).forEach(function (st) {
      var k = st.PIPELINE_ID;
      (groups[k] = groups[k] || []).push(st);
    });
    var keys = Object.keys(groups).sort(function (a, b) {
      return String(names[a] || a).localeCompare(String(names[b] || b));
    });
    if (!keys.length) return '<div class="empty-state">No pipeline stages.</div>';
    var out = "";
    keys.forEach(function (k) {
      var rows = groups[k].slice().sort(function (a, b) {
        return (a.STAGE_ORDER || 0) - (b.STAGE_ORDER || 0);   // stage number ASC: pipeline order
      });
      out += '<div class="section-head" style="margin-top:10px"><h2>'
        + esc(names[k] || ("Pipeline " + k)) + '</h2><span>' + rows.length + ' stages</span></div>'
        + '<div class="tablewrap"><table>'
        + '<tr><th>Pipeline</th><th>Stage no.</th><th>Stage</th><th>Id</th></tr>'
        + rows.map(function (st) {
            return '<tr><td class="muted">' + esc(names[k] || k) + '</td>'
              + '<td class="num">' + fmt(st.STAGE_ORDER) + '</td>'
              + '<td>' + esc(st.STAGE_NAME) + '</td>'
              + '<td class="num muted">' + esc(st.STAGE_ID) + '</td></tr>';
          }).join("") + '</table></div>';
    });
    return out;
  }

  function usersView(items) {
    if (!items || !items.length) return '<div class="empty-state">No users.</div>';
    var rows = items.slice().sort(function (a, b) {
      return String(a.LAST_NAME || "").localeCompare(String(b.LAST_NAME || ""));
    });
    return '<div class="tablewrap"><table>'
      + '<tr><th>User</th><th>Email</th><th>Role</th><th>Active</th></tr>'
      + rows.map(function (u) {
          var name = ((u.FIRST_NAME || "") + " " + (u.LAST_NAME || "")).trim() || "(no name)";
          return '<tr><td>' + esc(name) + '</td>'
            + '<td class="muted">' + esc(u.EMAIL_ADDRESS || "") + '</td>'
            + '<td class="muted">' + (u.ACCOUNT_OWNER ? "Account owner" : (u.ADMINISTRATOR ? "Admin" : "User")) + '</td>'
            + '<td class="muted">' + (u.ACTIVE === false ? "no" : "yes") + '</td></tr>';
        }).join("") + '</table></div>';
  }

  function showPipelines() {
    loading("Pipelines");
    Promise.all([
      callTool("app_records", { object: "Pipelines", top: 100 }, "list_records"),
      callTool("app_records", { object: "PipelineStages", top: 500 }, "list_records")
    ]).then(function (r) {
      var pipes = r[0].items || [], stages = r[1].items || [];
      state.pipes = pipes; state.stages = stages;
      panel("Pipelines - " + pipes.length, pipelinesView(pipes, stages),
        '<button data-explore="PipelineStages">Stages</button>');
    }).catch(function (e) {
      failed("Pipelines", e, "List the pipelines in this Insightly environment with their stage counts.");
    });
  }

  function showStages() {
    loading("Pipeline stages");
    Promise.all([
      callTool("app_records", { object: "PipelineStages", top: 500 }, "list_records"),
      callTool("app_records", { object: "Pipelines", top: 100 }, "list_records")
    ]).then(function (r) {
      var stages = r[0].items || [], pipes = r[1].items || [];
      panel("Pipeline stages - " + stages.length, stagesView(stages, pipes),
        '<button data-explore="Pipelines">Pipelines</button>');
    }).catch(function (e) {
      failed("Pipeline stages", e, "List the pipeline stages in this Insightly environment, grouped by pipeline.");
    });
  }

  function showUsers() {
    loading("Users");
    callTool("app_records", { object: "Users", top: 200 }, "list_records")
      .then(function (r) {
        var items = r.items || [];
        panel("Users - " + items.length, usersView(items));
      })
      .catch(function (e) { failed("Users", e, "List the users in this Insightly environment with their email addresses."); });
  }

  function showObject(obj) {
    var title = displayName(obj);
    var actions = '<button data-fields="' + esc(obj) + '">Fields</button>'
      + '<button data-ask="Summarise the ' + esc(title) + ' in this Insightly environment and flag anything that looks off.">Ask in chat</button>';
    loading(title);
    callTool("app_records", { object: obj, top: 25 }, "list_records")
      .then(function (r) {
        var items = r.items || [];
        var known = state.data && state.data.counts ? state.data.counts[obj] : null;
        var total = known != null ? known : (r.total != null ? r.total : items.length);
        var head = '<div class="chips" style="margin-bottom:10px"><span class="chip"><b>'
          + fmt(total) + '</b> total</span><span class="chip">showing newest ' + items.length + '</span>'
          + (title !== obj ? '<span class="chip">' + esc(obj) + '</span>' : '') + '</div>';
        panel(title, head + recordsTable(items, obj), actions, listUrl(obj));
      })
      .catch(function (e) { failed(title, e, "List the 10 newest " + title + " in Insightly."); });
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
        panel(displayName(obj), '<div class="chips" style="margin-bottom:10px"><span class="chip"><b>'
          + items.length + '</b> found</span></div>' + recordsTable(items, obj),
          '<button data-fields="' + esc(obj) + '">Fields</button>', listUrl(obj));
      })
      .catch(function (e) { failed(obj, e, "List the " + obj + " in this Insightly environment."); });
  }

  // -------------------------------------------------------------------------- wiring
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a[data-open]");
    if (a) {
      e.preventDefault();
      openExternal(a.getAttribute("data-open"));
      return;
    }
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
    if (t.id === "envtag") { state.menuOpen = !state.menuOpen; envMenu(); return; }
    if (t.dataset.env) { switchEnv(t.dataset.env); return; }
    if (t.dataset.addenv) { state.addOpen = true; envMenu(); return; }
    if (t.dataset.rn) { state.renaming = t.dataset.rn; state.removing = null; envMenu(); return; }
    if (t.dataset.rm) { state.removing = t.dataset.rm; state.renaming = null; envMenu(); return; }
    if (t.dataset.rmyes) { removeEnv(t.dataset.rmyes); return; }
    if (t.dataset.envcancel) { state.addOpen = false; state.renaming = null;
      state.removing = null; envMenu(); return; }
    if (t.dataset.close) { document.getElementById("panel").innerHTML = ""; markOpen(null); return; }
    if (t.dataset.ask) { askInChat(t.dataset.ask); return; }
    if (t.dataset.fields) { showFields(t.dataset.fields); markOpen(t.dataset.fields); return; }
    if (t.dataset.explore) {
      var what = t.dataset.explore;
      markOpen(null);
      if (what === "CustomObjects") { showCustomObjects(); return; }
      if (what === "Pipelines") { showPipelines(); return; }
      if (what === "PipelineStages") { showStages(); return; }
      if (what === "Users") { showUsers(); return; }
      showExplore(what);
      return;
    }
    if (t.dataset.object) { showObject(t.dataset.object); markOpen(t.dataset.object); return; }
  });

  document.addEventListener("submit", function (e) {
    if (!e.target) return;
    if (e.target.id === "envform") { e.preventDefault(); submitEnv(e.target); return; }
    if (e.target.dataset && e.target.dataset.rnform) {
      e.preventDefault();
      renameEnv(e.target.dataset.rnform, e.target.new_name.value.trim());
    }
  });

  // Click-away and Escape close the menu, as a menu should.
  document.addEventListener("click", function (e) {
    if (state.menuOpen && !e.target.closest(".envwrap")) closeMenu();
  }, true);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && state.menuOpen) closeMenu();
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
