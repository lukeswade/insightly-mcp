#!/usr/bin/env bash
# Pack the bridge .mcpb — a testing-only project, fully separate from the main bundle.
# Output goes to bridge/dist/ ONLY; the main dist/ and its -latest alias are never touched.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VER=$(python3 -c "import json;print(json.load(open('$HERE/manifest.json'))['version'])")
NAME=$(python3 -c "import json;print(json.load(open('$HERE/manifest.json'))['name'])")
[ "$NAME" = "insightly-se-mcp-bridge-test" ] || { echo "ERROR: unexpected manifest name $NAME" >&2; exit 1; }

# Edition drift is a failing build, not a discovery six weeks later.
python3 "$HERE/../tools/check_parity.py"
python3 -c "import ast;ast.parse(open('$HERE/server/bridge.py').read())"

# The endpoint credential is injected HERE and the finished bundle is published to R2,
# NOT to this repo (bridge/dist is gitignored for exactly this reason): the repo is public,
# so a committed bundle would be a published secret. A .pyc keeps the string too, so the
# stale compiled form goes first.
rm -rf "$HERE/server/__pycache__"
SECRET_FILE="${BRIDGE_SECRET_FILE:-$HOME/.insightly-mcp/bridge_secret}"
[ -s "$SECRET_FILE" ] || {
  echo "ERROR: no endpoint credential at $SECRET_FILE." >&2
  echo "       It must match the worker's BRIDGE_SECRET (npx wrangler secret list)." >&2
  exit 1; }
printf 'BRIDGE_SECRET = "%s"\n' "$(tr -d '\r\n' < "$SECRET_FILE")" > "$HERE/server/_secret.py"
chmod 600 "$HERE/server/_secret.py"
mkdir -p "$HERE/dist"
OUT="$HERE/dist/$NAME-$VER.mcpb"
npx -y @anthropic-ai/mcpb pack "$HERE" "$OUT"
# Stable alias the CF artifact's download button points at — same trick as the main
# bundle, so the link never needs editing on a release.
ALIAS="$HERE/dist/insightly-se-mcp-bridge-latest.mcpb"
cp "$OUT" "$ALIAS"
# The bundle MUST carry the credential (or it 401s on first use) and MUST NOT be tracked
# by git (or the credential is published). Both are checked, because either mistake is
# silent. Listing captured first, not piped: `set -o pipefail` plus `grep -q` closing the
# pipe early makes unzip die on SIGPIPE, which would fail this check on a GOOD bundle.
LISTING="$(unzip -Z1 "$OUT")"
case "$LISTING" in
  *server/_secret.py*) ;;
  *) echo "ERROR: the bundle carries no credential — it would 401 on install." >&2
     exit 1 ;;
esac
TRACKED="$(cd "$HERE/.." && git ls-files bridge/dist)"
[ -z "$TRACKED" ] || {
  echo "ERROR: bridge/dist is tracked by git and the bundle contains a credential:" >&2
  echo "$TRACKED" >&2
  echo "       Run: git rm --cached bridge/dist/*.mcpb" >&2
  exit 1; }
# Publish to R2. The install route streams this object, so uploading IS releasing.
npx -y wrangler@4 r2 object put "insightly-se-install/bridge/insightly-se-mcp-bridge-latest.mcpb" \
  --file "$OUT" --content-type application/octet-stream --remote \
  --config "$HERE/../worker/wrangler.jsonc" >/dev/null
echo "Built    $OUT"
echo "Alias    $ALIAS  (local only — never committed)"
echo "Released to R2: bridge/insightly-se-mcp-bridge-latest.mcpb"
