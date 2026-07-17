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

# Keep a single source of truth: copy the canonical server into the bundle.
mkdir -p "$HERE/server" "$ROOT/dist"
cp "$ROOT/insightly_mcp.py" "$HERE/server/insightly_mcp.py"

MCPB=(npx -y @anthropic-ai/mcpb)
"${MCPB[@]}" validate "$HERE/manifest.json"
"${MCPB[@]}" pack "$HERE" "$ROOT/dist/insightly-se-mcp-$VER.mcpb"
# Stable alias so shared links (docs, the internal artifact page) never go stale.
cp "$ROOT/dist/insightly-se-mcp-$VER.mcpb" "$ROOT/dist/insightly-se-mcp-latest.mcpb"
echo "Built dist/insightly-se-mcp-$VER.mcpb (+ dist/insightly-se-mcp-latest.mcpb alias)"
