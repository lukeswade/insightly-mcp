#!/usr/bin/env bash
# Build the Insightly SE MCP desktop-extension bundle (.mcpb).
#
#   ./mcpb/build.sh
#
# Copies the canonical server into the bundle, validates the manifest, and packs
# dist/insightly-se-mcp-<version>.mcpb. Requires Node (for the mcpb CLI, fetched via
# npx). The bundled server runs via `uv` at install time, so end users need `uv`
# installed but NOT the Python deps (uv fetches mcp/httpx/pydantic on first run).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

VER="$(grep -oE 'SERVER_VERSION = "[^"]+"' "$ROOT/insightly_mcp.py" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
MVER="$(grep -oE '"version": "[0-9]+\.[0-9]+\.[0-9]+"' "$HERE/manifest.json" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
if [ "$VER" != "$MVER" ]; then
  echo "ERROR: SERVER_VERSION ($VER) != manifest.json version ($MVER) — bump both together." >&2
  exit 1
fi
echo "Version: $VER"

# Edition drift is a failing build, not a discovery six weeks later.
python3 "$HERE/../tools/check_parity.py"

# Keep a single source of truth: copy the canonical server (and its UI module — the
# server imports app_ui, so omitting it makes the bundle die on import) into the bundle.
mkdir -p "$HERE/server" "$ROOT/dist"
cp "$ROOT/insightly_mcp.py" "$HERE/server/insightly_mcp.py"
[ -f "$ROOT/app_ui.py" ] && cp "$ROOT/app_ui.py" "$HERE/server/app_ui.py"
rm -rf "$HERE/server/__pycache__"   # from a previous run's tool-list check

# The install dialog shows manifest.tools verbatim, so it silently drifts from reality as
# tools are added. Ask the bundled server what it actually registers (tools/list needs no
# credentials) and fail on any mismatch. App-only tools are excluded on purpose: they are
# invisible to the model and shouldn't be advertised as capabilities.
python3 - "$HERE" <<'PYCHECK'
import json, pathlib, subprocess, sys, threading
here = pathlib.Path(sys.argv[1])
man = json.loads((here / "manifest.json").read_text())
declared = {t["name"] for t in man.get("tools", [])}
args = [a.replace("${__dirname}", str(here)) for a in man["server"]["mcp_config"]["args"]]
cmd = man["server"]["mcp_config"]["command"]
if cmd.startswith("${user_config."):
    cmd = "/opt/homebrew/bin/uv"
p = subprocess.Popen([cmd] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, bufsize=1)
threading.Thread(target=lambda: list(p.stderr), daemon=True).start()
def send(m): p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
def wait(i):
    for _ in range(800):
        line = p.stdout.readline()
        if not line: return None
        try: d = json.loads(line)
        except Exception: continue
        if d.get("id") == i: return d
send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",
      "capabilities":{},"clientInfo":{"name":"build-check","version":"0"}}})
wait(1)
send({"jsonrpc":"2.0","method":"notifications/initialized"})
send({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
tools = (wait(2) or {}).get("result", {}).get("tools", [])
p.terminate()
if not tools:
    print("WARNING: couldn't read tools/list; skipping the manifest tool check", file=sys.stderr)
    raise SystemExit(0)
model = {t["name"] for t in tools
         if ((t.get("_meta") or {}).get("ui") or {}).get("visibility") != ["app"]}
missing, phantom = sorted(model - declared), sorted(declared - model)
if missing or phantom:
    if missing: print(f"ERROR: manifest.tools is missing: {missing}", file=sys.stderr)
    if phantom: print(f"ERROR: manifest.tools lists unregistered tools: {phantom}", file=sys.stderr)
    raise SystemExit(1)
print(f"manifest tool list matches the server ({len(model)} model-facing tools)")
PYCHECK

# Every private helper the module calls must actually exist. Editing this file by slicing
# between two anchors can silently swallow a helper that lived between them (it ate _field
# once), and Python only notices at call time — i.e. in front of a user.
python3 - "$ROOT" <<'PYNAMES'
import ast, pathlib, sys
tree = ast.parse((pathlib.Path(sys.argv[1]) / "insightly_mcp.py").read_text())
defined = {n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
defined |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}
called = {n.func.id for n in ast.walk(tree)
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id.startswith("_")}
missing = sorted(called - defined)
if missing:
    print(f"ERROR: these private helpers are called but never defined: {missing}", file=sys.stderr)
    raise SystemExit(1)
print(f"every private helper resolves ({len(called)} referenced)")
PYNAMES

# The host injects the view via document.write(); a backslash escape or backtick in the
# document survives into a JS string it builds and produces
# "SyntaxError: Failed to execute 'write' on 'Document'" — a blank widget. Ban both.
python3 - "$ROOT" <<'PYUI'
import pathlib, re, sys
src = (pathlib.Path(sys.argv[1]) / "app_ui.py").read_text()
html = re.search(r'ENV_DASHBOARD_HTML = """(.*)"""', src, re.S).group(1)
bad = {"backslash": html.count("\\"), "backtick": html.count("`")}
if any(bad.values()):
    print(f"ERROR: widget HTML contains embedding hazards: {bad}", file=sys.stderr)
    raise SystemExit(1)
print("widget HTML is free of document.write embedding hazards")

# getElementById on an id the markup no longer has returns null, and the TypeError kills
# the rest of the function it was called from — in render() that means a permanently
# half-drawn dashboard. Rename an element and this catches the stragglers.
declared = set(re.findall(r'id="([A-Za-z][\w-]*)"', html))
used = set(re.findall(r'getElementById\("([^"]+)"\)', html))
missing = sorted(used - declared)
if missing:
    print(f"ERROR: widget script reads ids that do not exist: {missing}", file=sys.stderr)
    raise SystemExit(1)
print(f"widget element references all resolve ({len(used)} ids)")

# The inline script is never compiled by anything in this repo's toolchain, so a syntax
# error ships silently and the widget renders as raw static HTML with no behaviour. That
# has now happened twice (an escaped apostrophe, then a stray quote from an edit). Parse it.
script = re.search(r"<script>\s*(.*?)\s*</script>", html, re.S)
if not script:
    print("ERROR: no inline <script> found in the widget", file=sys.stderr)
    raise SystemExit(1)
import subprocess, tempfile
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
    fh.write(script.group(1))
    tmp = fh.name
chk = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
if chk.returncode != 0:
    print("ERROR: widget inline script does not parse:", file=sys.stderr)
    print(chk.stderr.strip()[:800], file=sys.stderr)
    raise SystemExit(1)
print("widget inline script parses")
PYUI

MCPB=(npx -y @anthropic-ai/mcpb)
# MCPB_SUFFIX builds a side-by-side artifact and does NOT move the -latest alias, so a
# pre-release can be install-tested without changing what colleagues download.
OUT="$ROOT/dist/insightly-se-mcp-$VER${MCPB_SUFFIX:-}.mcpb"
"${MCPB[@]}" validate "$HERE/manifest.json"
"${MCPB[@]}" pack "$HERE" "$OUT"
if [ -z "${MCPB_SUFFIX:-}" ]; then
  # Stable alias so shared links (docs, the internal artifact page) never go stale.
  cp "$OUT" "$ROOT/dist/insightly-se-mcp-latest.mcpb"
  echo "Built $(basename "$OUT") (+ insightly-se-mcp-latest.mcpb alias)"
else
  echo "Built $(basename "$OUT") — the -latest alias was left untouched."
fi
