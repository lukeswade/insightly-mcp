#!/usr/bin/env python3
"""Regenerate src/widget.ts from app_ui.py — the widget ships byte-identical."""
import json, pathlib, re
root = pathlib.Path(__file__).resolve().parents[1]
html = re.search(r'ENV_DASHBOARD_HTML = """(.*)"""', (root/"app_ui.py").read_text(), re.S).group(1)
dst = pathlib.Path(__file__).parent/"src"/"widget.ts"
dst.write_text("// GENERATED from app_ui.py by worker/extract-widget.py — do not edit.\n"
               "export const WIDGET_HTML: string = " + json.dumps(html) + ";\n")
print(f"wrote {dst} ({dst.stat().st_size} bytes)")
