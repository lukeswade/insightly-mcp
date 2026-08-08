#!/usr/bin/env python3
"""Regenerate src/widget.ts from app_ui.py (JSON-stringified so no template-literal hazards)."""
import json, pathlib, re
root = pathlib.Path(__file__).resolve().parents[2]
html = re.search(r'ENV_DASHBOARD_HTML = """(.*)"""', (root/"app_ui.py").read_text(), re.S).group(1)
dst = pathlib.Path(__file__).parent/"src"/"widget.ts"
dst.write_text("// GENERATED from app_ui.py by spike/cf-remote/extract-widget.py — do not edit.\n"
               "export const WIDGET_HTML: string = " + json.dumps(html) + ";\n")
print(f"wrote {dst} ({dst.stat().st_size} bytes)")
