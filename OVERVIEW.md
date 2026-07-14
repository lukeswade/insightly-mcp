# Insightly SE MCP (internal) — Overview

*Talk to any of our Insightly environments in plain English, from Claude.*

---

## What is it?

A small **MCP server** that connects Claude (Claude Code, Claude Desktop) to the
**Insightly CRM API**. Once it's installed, you can simply ask Claude things like:

> *"Find the contact with email jane@acme.com and add a note that we spoke today."*
> *"Create an opportunity called 'Q3 renewal' for organisation Globex."*
> *"List the 10 most recently updated leads."*
> *"Delete task 4521."* (it will ask you to confirm)

…and Claude performs the real CRM operations — **read and write** — against
whichever Insightly environment you've connected.

**MCP (Model Context Protocol)** is the open standard for giving AI assistants
tools. This server is the "Insightly plug" for that standard: ~300 lines of Python
that translate Claude's tool calls into Insightly REST API calls.

## Why it's useful here

We work with **hundreds of Insightly demo environments**. This tool makes any of
them conversationally drivable in seconds:

- **Demo prep & seeding** — "create 5 sample contacts and an open opportunity."
- **Spot checks** — "does this env have any pipelines configured?"
- **Cleanup** — "find and delete the TEST contacts."
- **Cross-object work** — "add a follow-up task on every open opportunity."
- **No env juggling** — switching environments is just re-connecting with a
  different API key. No config files to edit in Claude Code.

## How it works (60 seconds)

```
 You (plain English)
        │
        ▼
 Claude  ──── decides which tool to call ────►  Insightly SE MCP server (local Python)
        ▲                                              │   HTTPS (Basic auth, your API key)
        │                                              ▼
 results, summaries                          api.{pod}.insightly.com/v3.1
```

1. **You ask** Claude something CRM-related.
2. Claude picks one of the server's **15 tools** — search, get, list, create,
   update, delete, add-note, or a raw escape hatch for any endpoint.
3. The server calls the **Insightly v3.1 REST API** with your key and returns JSON.
4. Claude turns the JSON into an answer, or chains more calls.

### Authentication — the nice part

**Nothing to configure.** The first time you use a tool, the server **prompts you
right in Claude** for an Insightly API key (Insightly → User Settings → API):

- Enter a key, optionally give it a **friendly name** ("acme-demo"), and tick
  **Save** if you want to reuse it next time — saved keys are then offered by name.
- Keys are held in memory for the session; saved ones go to a private local file
  (`~/.insightly-mcp/keys.json`, permissions 600) managed entirely by the server.
- Keys are **never typed into the chat**, never appear in the transcript, and the
  model never sees them.

Switching environments = run `connect` again and pick/enter a different key.

## What it can do (tool list)

| Category | Tools |
|---|---|
| Session | `connect` (prompts for a key), `set_api_key` (fallback), `connection_info`, `disconnect`, `list_saved`, `forget_saved` |
| Read | `list_records`, `search_records`, `get_record`, `list_supported_objects` |
| Write | `create_record`, `update_record`, `delete_record` (requires explicit confirm), `add_note` |
| Anything else | `raw_request` — any v3.1 endpoint, e.g. `/Opportunities/123/Tasks` |

Works on every Insightly object: Contacts, Organisations, Leads, Opportunities,
Projects, Tasks, Events, Notes, Products, custom objects, and more.

## Safety & guardrails

- **Deletes require `confirm=true`** — Claude can't delete on a whim.
- **Read-only mode** (`INSIGHTLY_READONLY=1`) blocks every write — good for trying
  it against a production org.
- **Rate-limit aware** — Insightly allows ~10 req/sec + a daily cap per plan; the
  server backs off and retries on HTTP 429, and page sizes default small.
- **Key hygiene** — keys never transit the model; saved keys are local-only,
  per-machine, and removable with `forget_saved`.

## What you need

| Requirement | Notes |
|---|---|
| **Claude Code** or the **Claude desktop app** | Claude Code's interactive key prompt uses MCP "elicitation"; the desktop app uses a one-time config file (see [ONBOARDING.md](ONBOARDING.md)) |
| **Python 3.10+** and **uv** | `brew install uv` — runs the server with zero setup |
| **An Insightly API key** | Insightly → User Settings → API (any demo env) |
| Hosting / servers | **None.** It runs locally on your laptop, launched by Claude on demand |

## Get started

See **[ONBOARDING.md](ONBOARDING.md)** for the 2-minute setup, or just run:

```bash
git clone <repo-url> ~/insightly-mcp
claude mcp add --scope user insightly \
  -- uv run --with mcp --with httpx --with pydantic python ~/insightly-mcp/insightly_mcp.py
```

Restart Claude Code, then ask: *"List my Insightly contacts."* It will prompt for
your key, and you're off. (Claude desktop app setup — and why claude.ai in the
browser can't run this — is covered in [ONBOARDING.md](ONBOARDING.md).)

And open **[demo.html](demo.html)** in a browser for a visual walkthrough of what
spin-up and usage look like.
