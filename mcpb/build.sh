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
echo "Server version: $VER"

# Keep a single source of truth: copy the canonical server into the bundle.
mkdir -p "$HERE/server" "$ROOT/dist"
cp "$ROOT/insightly_mcp.py" "$HERE/server/insightly_mcp.py"

MCPB=(npx -y @anthropic-ai/mcpb)
"${MCPB[@]}" validate "$HERE/manifest.json"
"${MCPB[@]}" pack "$HERE" "$ROOT/dist/insightly-se-mcp-$VER.mcpb"
echo "Built dist/insightly-se-mcp-$VER.mcpb"
