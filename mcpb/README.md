# Insightly SE MCP — desktop extension (.mcpb)

One-click install of the server for the **Claude macOS desktop app** — no config file,
no Terminal JSON editing, and the API key is stored in Claude's secure settings (not
in plaintext).

## Install (for users)

1. **Install `uv`** once (the bundle uses it to run the server):
   `brew install uv`  — or  `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Download the bundle (direct link):
   https://github.com/lukeswade/insightly-mcp/raw/main/dist/insightly-se-mcp-2.1.0.mcpb
3. **Double-click it** (or Claude → Settings → Extensions → Advanced → Install Extension…).
4. In the install dialog, fill:
   - **Insightly API key** (Insightly → User Settings → API) — stored securely.
   - **Insightly pod** — `na1` unless your API URL says otherwise.
   - **Path to uv** — defaults to `/opt/homebrew/bin/uv` (Apple Silicon + Homebrew).
     If `which uv` in Terminal prints a different path (Intel: `/usr/local/bin/uv`;
     standalone installer: `~/.local/bin/uv`), set that.
5. Done — ask Claude *"list my Insightly contacts."*

## Build (for maintainers)

```bash
./mcpb/build.sh
```

Copies `insightly_mcp.py` into `mcpb/server/`, validates the manifest, and writes
`dist/insightly-se-mcp-<version>.mcpb`. Bump `SERVER_VERSION` in `insightly_mcp.py`
and the `version` in `mcpb/manifest.json` together, then rebuild.

## Troubleshooting

- **Extension installed but `insightly` never appears in chat:** Claude silently
  skips an extension whose required config is missing — make sure the **API key**
  field is filled, then toggle the extension off/on (or reinstall) and fully
  restart (Cmd+Q).
- **"Server disconnected" right after install:** the `uv` path is wrong for your
  Mac. Run `which uv` in Terminal and put that value in the extension's
  **Path to uv** setting (Settings → Extensions → Insightly SE MCP).

## Notes

- **Why `uv` is still a prerequisite:** the MCPB format can't portably bundle
  compiled Python deps (`pydantic-core`), so instead of vendoring we launch via
  `uv run --with …`, which fetches deps on first run. That keeps the bundle tiny and
  cross-Mac, at the cost of one prerequisite (`uv`) and a first-run download.
- **What the bundle removes vs. the manual setup:** no editing
  `claude_desktop_config.json`, no path typos (the `<angle-bracket>` trap), and the
  key lives in Claude's secure store instead of plaintext in the config.
