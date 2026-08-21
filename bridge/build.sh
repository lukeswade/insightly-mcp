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

# No credential goes INTO the bundle: this artifact is committed to a public repo, so
# anything inside it is published. The access token is an install-time field the user
# pastes (manifest user_config.access_token -> BRIDGE_SECRET).
rm -f "$HERE/server/_secret.py"
# ...including the compiled form: a .pyc keeps the string, and a stale one from an earlier
# build is exactly how a credential sneaks into a published bundle.
rm -rf "$HERE/server/__pycache__"
mkdir -p "$HERE/dist"
OUT="$HERE/dist/$NAME-$VER.mcpb"
npx -y @anthropic-ai/mcpb pack "$HERE" "$OUT"
# Stable alias the CF artifact's download button points at — same trick as the main
# bundle, so the link never needs editing on a release.
ALIAS="$HERE/dist/insightly-se-mcp-bridge-latest.mcpb"
cp "$OUT" "$ALIAS"
# The bundle is committed to a public repo. Nothing credential-shaped may be inside it.
# Listing captured first, not piped: `set -o pipefail` plus `grep -q` closing the pipe
# early makes unzip die on SIGPIPE, which would fail this check on a GOOD bundle.
LISTING="$(unzip -Z1 "$OUT")"
case "$LISTING" in
  *_secret*|*.env*|*credential*)
     echo "ERROR: the bundle contains a credential-shaped file — it must not be published." >&2
     echo "$LISTING" >&2
     exit 1 ;;
esac
python3 - "$OUT" <<'GUARD'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for n in z.namelist():
    if not n.endswith((".py", ".json", ".md", ".txt")):
        continue
    body = z.read(n).decode("utf-8", "replace")
    for line in body.splitlines():
        low = line.lower()
        if ("secret" in low or "token" in low) and "=" in low:
            val = line.split("=", 1)[1].strip().strip("\"'")
            if len(val) >= 32 and all(c in "0123456789abcdefABCDEF" for c in val):
                print(f"ERROR: {n} looks like it embeds a credential: {line[:40]}...",
                      file=sys.stderr)
                sys.exit(1)
GUARD
echo "Built $OUT"
echo "Alias  $ALIAS"
