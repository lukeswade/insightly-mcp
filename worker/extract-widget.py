#!/usr/bin/env python3
"""Regenerate src/widget.ts from app_ui.py — the widget ships byte-identical."""
import json, pathlib, re
root = pathlib.Path(__file__).resolve().parents[1]
html = re.search(r'ENV_DASHBOARD_HTML = """(.*)"""', (root/"app_ui.py").read_text(), re.S).group(1)
# Same parse guard as the .mcpb build: a widget whose script does not compile renders as
# inert static HTML, and nothing else in the TS toolchain would notice.
import re as _re, subprocess, sys, tempfile
_script = _re.search(r"<script>\s*(.*?)\s*</script>", html, _re.S)
if not _script:
    sys.exit("no inline <script> found in the widget")
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as _fh:
    _fh.write(_script.group(1))
    _tmp = _fh.name
_chk = subprocess.run(["node", "--check", _tmp], capture_output=True, text=True)
if _chk.returncode != 0:
    sys.exit("widget inline script does not parse:\n" + _chk.stderr.strip()[:800])
print("widget inline script parses")

dst = pathlib.Path(__file__).parent/"src"/"widget.ts"
dst.write_text("// GENERATED from app_ui.py by worker/extract-widget.py — do not edit.\n"
               "export const WIDGET_HTML: string = " + json.dumps(html) + ";\n")
print(f"wrote {dst} ({dst.stat().st_size} bytes)")
