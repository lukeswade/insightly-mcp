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

# Keep a single source of truth: copy the canonical server (and its UI module — the
# server imports app_ui, so omitting it makes the bundle die on import) into the bundle.
mkdir -p "$HERE/server" "$ROOT/dist"
cp "$ROOT/insightly_mcp.py" "$HERE/server/insightly_mcp.py"
[ -f "$ROOT/app_ui.py" ] && cp "$ROOT/app_ui.py" "$HERE/server/app_ui.py"

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
