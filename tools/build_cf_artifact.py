#!/usr/bin/env python3
"""Render the CF install guide with the real download link.

The guide's download button is a CREDENTIAL: it is what authorises fetching a bundle that
carries the endpoint secret. The guide's source lives in a public repo, so the source holds
a {{INSTALL_URL}} placeholder and the token is substituted only into a build copy that is
never committed. Publish the built file (passing the existing artifact URL) so the page
keeps its address.

    python3 tools/build_cf_artifact.py     ->  docs/artifact/build/insightly-se-mcp-cf-guide.html
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "artifact" / "insightly-se-mcp-cf-guide.html"
OUT = ROOT / "docs" / "artifact" / "build" / "insightly-se-mcp-cf-guide.html"
BASE = "https://insightly-se-mcp.lukeswade.workers.dev"
TOKEN_FILE = pathlib.Path(os.path.expanduser("~/.insightly-mcp/install_token"))


def main() -> int:
    if not TOKEN_FILE.exists():
        print(f"ERROR: no install token at {TOKEN_FILE}. It must match the worker's "
              f"INSTALL_TOKEN (npx wrangler secret list).", file=sys.stderr)
        return 1
    token = TOKEN_FILE.read_text().strip()
    src = SRC.read_text()
    if token in src:
        print("ERROR: the token is in the COMMITTED source — remove it and use "
              "{{INSTALL_URL}}.", file=sys.stderr)
        return 1
    if "{{INSTALL_URL}}" not in src:
        print("ERROR: no {{INSTALL_URL}} placeholder in the guide — nothing to render.",
              file=sys.stderr)
        return 1
    url = f"{BASE}/install/insightly-se-mcp-bridge.mcpb?t={token}"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(src.replace("{{INSTALL_URL}}", url))
    OUT.chmod(0o600)
    print(f"rendered {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes) with the live "
          f"install link")
    return 0


if __name__ == "__main__":
    sys.exit(main())
