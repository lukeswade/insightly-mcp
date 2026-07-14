# Get started with the Insightly SE MCP (internal)

> **Name:** the server appears in Claude's UI as **"Insightly SE MCP (internal)"**.
> Its registration key stays `insightly` (the `claude mcp add insightly …` name and
> the `mcpServers.insightly` config key), so tool names are unchanged.

Drive any of our Insightly demo environments from Claude in plain English —
search, create, update, annotate, and (carefully) delete CRM records. Works in
**Claude Code** (zero config — it prompts you for an API key on first use) and the
**Claude desktop app** (one-time 5-line config). Setup takes about two minutes
either way.

> 📖 New to this? Read [OVERVIEW.md](OVERVIEW.md) first (what it is + how it works),
> and open [demo.html](demo.html) in a browser to *see* the flow before installing.

## Prerequisites

| You need | How to get it |
|---|---|
| **Claude Code** or the **Claude desktop app** | In Claude Code the interactive key prompt requires MCP elicitation support (recent version); the desktop app uses the config-file path below |
| **uv** (Python runner) | `brew install uv` — no venvs or pip installs needed |
| **An Insightly API key** | Any demo env: Insightly → **User Settings → API** → copy the key. Note the **pod** in your API URL (`na1` for North America) |

No servers, no hosting, no Docker — it runs locally and Claude launches it on demand.

## Install (once)

### Claude Code

```bash
# 1. Get the code
git clone https://github.com/lukeswade/insightly-mcp ~/insightly-mcp

# 2. Register it with Claude Code (user scope = available in all your projects)
claude mcp add --scope user insightly \
  -- uv run --with mcp --with httpx --with pydantic python ~/insightly-mcp/insightly_mcp.py
```

Restart Claude Code (or start a new session).

### Claude desktop app (chat)

Do step 1 above (the clone). The config goes in
`~/Library/Application Support/Claude/claude_desktop_config.json`, and it needs
**absolute paths** — `~` and `$HOME` are *not* expanded here, and the `uv` path
differs on Intel vs. Apple Silicon Macs.

Don't hand-type the paths — let the terminal fill them in. Paste this into
**Terminal** and it prints a ready-to-use block with *your* real values:

```bash
cat <<EOF
"insightly": {
  "command": "$(which uv)",
  "args": ["run", "--with", "mcp", "--with", "httpx", "--with", "pydantic", "python", "$HOME/insightly-mcp/insightly_mcp.py"],
  "env": { "INSIGHTLY_API_KEY": "PASTE-YOUR-KEY-HERE", "INSIGHTLY_POD": "na1" }
}
EOF
```

Then in Claude: **Settings → Developer → Edit Config**, paste that block inside a
top-level `"mcpServers": { … }` object (create it if it isn't there), and replace
`PASTE-YOUR-KEY-HERE` with your API key. The finished file looks like:

```json
{
  "mcpServers": {
    "insightly": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--with", "mcp", "--with", "httpx", "--with", "pydantic",
               "python", "/Users/jane/insightly-mcp/insightly_mcp.py"],
      "env": { "INSIGHTLY_API_KEY": "your-real-key", "INSIGHTLY_POD": "na1" }
    }
  }
}
```

> ⚠️ **No angle brackets in the final file.** `/Users/jane/…` above is an
> *example* — the generator command fills in your actual username. If you copy
> the template by hand, replace the **whole** `/Users/jane` (and `your-real-key`)
> with real values — a path like `/Users/<jane>/…` with literal `<` `>` points at
> a folder that doesn't exist and the server will show **"Server disconnected."**

Fully quit (**Cmd+Q**) and reopen Claude. The `env` block skips the interactive
key prompt — recommended here, since the desktop chat may not support
server-initiated prompts. Your key sits in that file in plain text (same as the
local key store), so: **demo envs only**.

> 🌐 **claude.ai in the browser can't run this** — local MCP servers work in
> Claude Code and the desktop app only. A hosted/shared version is on the ideas list.

## First use

Just ask Claude something Insightly-flavored:

> *"List my 10 most recent Insightly contacts."*

In Claude Code, a **prompt pops up** asking for your API key (plus pod, an optional
friendly name, and a "save for next time" checkbox). Fill it in once — done. Your
key goes to the local server only; it never appears in the chat or reaches the
model. (Desktop app with the `env` block: no prompt — it just answers.)

Things to try next:

- *"Find the contact with email **jane@acme.com** and add a note that we spoke today."*
- *"Create an organisation called **Acme Rockets** and a contact **Jane Doe** in it."*
- *"What opportunities are in this env? Show pipeline stages too."*
- *"Switch environments"* → runs `connect`, which re-prompts (pick a saved key by
  name or paste a new one). This is how you hop between demo envs.

## Safety notes

- This has **full write access** to whatever env your key belongs to. Deletes
  require an explicit `confirm=true`, and Claude will ask you first.
- Want read-only? Register with `--env INSIGHTLY_READONLY=1` appended after
  `--scope user insightly` (desktop app: add `"INSIGHTLY_READONLY": "1"` to the
  `env` block) — every write is then blocked at the server.
- Saved keys live in `~/.insightly-mcp/keys.json` (permissions 600, local to your
  machine). Remove one anytime: *"forget the saved key called acme-demo."*

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No such tool" / Claude doesn't see it | Restart Claude Code; check `claude mcp list` |
| Desktop app doesn't see it | Fully quit (**Cmd+Q**) and reopen — closing the window isn't enough. Check the tools menu in the chat input |
| Desktop app shows `insightly` as **failed** / "Server disconnected" | Open **Settings → Developer** and read the **Arguments** path. If it contains angle brackets like `/Users/<name>/…`, that's the bug — those `< >` are placeholders, delete them (real home dirs have none). Easiest fix: re-run the generator command above and paste fresh values. Confirm the file exists first: `ls "$HOME/insightly-mcp/insightly_mcp.py"`. Then **Cmd+Q** and reopen |
| "Authorization with the MCP server failed" / `ofid_…` / "ins test … connection expired" | **Not your local server.** That's a separate company-hosted Insightly *connector* under Settings → Connectors whose login expired. It's unrelated to your `insightly` setup — ignore it, or click into it and disconnect so the error stops popping up |
| Claude answers from the wrong Insightly instance (test/sandbox data) | Claude is probably using a company-level hosted Insightly *connector* instead of this local server (the browser version of claude.ai can **only** see those). Ask Claude *"which Insightly tools can you see?"* — you want one named `insightly`. Toggle other Insightly connectors off in the chat's tools menu, and verify with *"run connection_info"* (reports the org/pod you're connected to) |
| No key prompt appears | Your client may not support elicitation — update Claude Code, use the desktop app's `env` block, or say *"use set_api_key"* (key passes through chat — demo envs only) |
| `unauthorized (401)` | Wrong key or wrong pod (e.g. `eu1` org with `na1` pod). Re-run `connect` |
| `rate limited (429)` | Insightly caps ~10 req/sec + daily quota per plan; the server retries automatically — just slow down bulk asks |
| `uv: command not found` | `brew install uv` |

## Where things live

- **Repo:** https://github.com/lukeswade/insightly-mcp (server, docs, this guide, the visual demo)
- **Server code:** a single file, `insightly_mcp.py` (~350 lines, plain Python) — easy to read and extend
- **Insightly API reference:** https://api.na1.insightly.com/v3.1/

Questions or ideas (new tools, bulk operations, a hosted/shared version)? Ping Luke.
