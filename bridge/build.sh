#!/usr/bin/env bash
# Pack the bridge .mcpb — a testing-only project, fully separate from the main bundle.
# Output goes to bridge/dist/ ONLY; the main dist/ and its -latest alias are never touched.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VER=$(python3 -c "import json;print(json.load(open('$HERE/manifest.json'))['version'])")
NAME=$(python3 -c "import json;print(json.load(open('$HERE/manifest.json'))['name'])")
[ "$NAME" = "insightly-se-mcp-bridge-test" ] || { echo "ERROR: unexpected manifest name $NAME" >&2; exit 1; }
python3 -c "import ast;ast.parse(open('$HERE/server/bridge.py').read())"
mkdir -p "$HERE/dist"
OUT="$HERE/dist/$NAME-$VER.mcpb"
npx -y @anthropic-ai/mcpb pack "$HERE" "$OUT"
echo "Built $OUT"
