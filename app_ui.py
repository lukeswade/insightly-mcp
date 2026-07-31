"""HTML for the MCP Apps UI (io.modelcontextprotocol/ui).

Kept out of insightly_mcp.py so the server file stays readable. The host renders this
in a sandboxed iframe; it must be entirely self-contained (no CDNs, no external fonts).

Data delivery is the host's side of the ext-apps contract, and hosts differ, so the
page accepts the payload from any of the plausible channels (an injected global, or a
postMessage from the parent) and renders a clear waiting state until something arrives.
The tool returns the same numbers as plain text regardless, so a client without Apps
support loses nothing.
"""

ENV_DASHBOARD_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Insightly environment</title>
<style>
  :root {
    --bg: #FBF9F6; --card: #fff; --ink: #211B14; --muted: #6F6357;
    --line: #E4DED4; --accent: #D8382E; --good: #2E7D4F;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #17130E; --card: #201B15; --ink: #EDE6DC; --muted: #A79B8D;
            --line: #352D23; --accent: #F2604F; --good: #63B888; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 16px; background: var(--bg); color: var(--ink);
         font: 14px/1.5 "Avenir Next", -apple-system, system-ui, sans-serif; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 12px; margin-bottom: 14px; }
  h1 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
  .pill { font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
          padding: 3px 9px; border-radius: 999px; background: var(--card);
          border: 1px solid var(--line); color: var(--muted); }
  .pill.live { color: var(--good); border-color: color-mix(in srgb, var(--good) 40%, transparent); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 8px; }
  .tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
          padding: 11px 12px; position: relative; overflow: hidden; }
  .tile .n { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums;
             letter-spacing: -0.02em; }
  .tile .k { font-size: 11.5px; color: var(--muted); margin-top: 2px;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tile .bar { position: absolute; left: 0; bottom: 0; height: 2px; background: var(--accent);
               opacity: .55; }
  .tile.empty .n { color: var(--muted); font-weight: 500; }
  .note { margin-top: 14px; font-size: 12px; color: var(--muted); }
  .note b { color: var(--ink); font-weight: 600; }
  .wait { padding: 22px 0; color: var(--muted); font-size: 13px; }
  .unavail { margin-top: 10px; font-size: 12px; color: var(--muted); }
  .unavail code { font-size: 11px; }
</style>
<header>
  <h1>Insightly environment</h1>
  <span class="pill live" id="env">…</span>
  <span class="pill" id="pod">…</span>
  <span class="pill" id="quota" hidden></span>
</header>
<div id="body"><div class="wait">Waiting for environment data…</div></div>
<script>
  var LABELS = {
    Contacts: "Contacts", Organisations: "Organisations", Leads: "Leads",
    Opportunities: "Opportunities", Projects: "Projects", Tasks: "Tasks",
    Events: "Events", Notes: "Notes", Emails: "Emails", Ticket: "Tickets",
    Product: "Products", KnowledgeArticle: "KB articles", Users: "Users"
  };

  function render(d) {
    if (!d || !d.counts) return;
    document.getElementById("env").textContent = d.connected_as || "connected";
    document.getElementById("pod").textContent = "pod " + (d.pod || "?");
    if (d.daily_quota && d.daily_quota.remaining != null) {
      var q = document.getElementById("quota");
      q.hidden = false;
      q.textContent = Number(d.daily_quota.remaining).toLocaleString() + " API calls left today";
    }
    var counts = d.counts, keys = Object.keys(counts);
    var max = Math.max.apply(null, keys.map(function (k) { return counts[k] || 0; }).concat([1]));
    var html = '<div class="grid">' + keys.map(function (k) {
      var n = counts[k], has = typeof n === "number" && n > 0;
      var w = has ? Math.max(3, Math.round((n / max) * 100)) : 0;
      return '<div class="tile' + (has ? '' : ' empty') + '">'
        + '<div class="n">' + (typeof n === "number" ? n.toLocaleString() : "—") + '</div>'
        + '<div class="k">' + (LABELS[k] || k) + '</div>'
        + (has ? '<div class="bar" style="width:' + w + '%"></div>' : '')
        + '</div>';
    }).join("") + '</div>';
    if (d.failed && Object.keys(d.failed).length) {
      html += '<div class="unavail">Not available in this environment: '
        + Object.keys(d.failed).map(function (k) { return "<code>" + k + "</code>"; }).join(", ")
        + '</div>';
    }
    html += '<div class="note">Totals come straight from Insightly\\'s record counts — '
      + 'ask for any of these by name to drill in.</div>';
    document.getElementById("body").innerHTML = html;
  }

  // Hosts differ in how they hand a tool result to an app; accept the likely shapes.
  function dig(o) {
    if (!o || typeof o !== "object") return null;
    if (o.counts) return o;
    var keys = ["toolOutput", "tool_output", "structuredContent", "output", "result", "data"];
    for (var i = 0; i < keys.length; i++) {
      var v = o[keys[i]];
      if (v && typeof v === "object") { var hit = dig(v); if (hit) return hit; }
      if (typeof v === "string") { try { var p = JSON.parse(v); var h = dig(p); if (h) return h; } catch (e) {} }
    }
    return null;
  }
  ["mcpToolOutput", "__MCP_APP__", "mcpApp", "mcp"].forEach(function (g) {
    var hit = dig(window[g]); if (hit) render(hit);
  });
  window.addEventListener("message", function (e) { var hit = dig(e.data); if (hit) render(hit); });
</script>
"""
