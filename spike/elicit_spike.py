#!/usr/bin/env python3
"""Spike 1 (v3.0 migration): elicitation on MCP SDK 2.x.

FINDING: the migration is a three-line import/constructor change. There are TWO
Context classes in SDK 2.x and only one is right:

  mcp.server.context.Context      → the middleware/dispatch context. NO .elicit.
                                    Annotating a tool with this crashes at
                                    registration (pydantic can't schema it).
  mcp.server.mcpserver.Context    → the FastMCP-equivalent tool context. HAS
                                    .elicit(message, schema) with the SAME
                                    signature as SDK 1.x, plus .session,
                                    .elicit_url, .input_responses (MRTR).

So insightly_mcp.py's `_prompt()` body does not change at all — only the imports
and the server constructor.

Run:  uv run --with 'mcp==2.0.0' --with 'pydantic<3' python spike/spike_client.py
"""
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.mcpserver import Context  # NOT mcp.server.context

mcp = MCPServer(name="elicit-spike", version="0.0.1")


class NewKey(BaseModel):
    """The same schema insightly_mcp.py prompts with today."""
    api_key: str = Field(description="Your Insightly API key.")
    pod: str = Field(default="na1", description="Region pod: na1, eu1, ap1, …")


@mcp.tool()
async def probe(ctx: Context) -> dict:
    """Report the elicitation surface available to a tool (no prompting)."""
    return {
        "context_class": f"{type(ctx).__module__}.{type(ctx).__name__}",
        "has_elicit": hasattr(ctx, "elicit"),
        "has_elicit_url": hasattr(ctx, "elicit_url"),
        "has_session": ctx.session is not None,
        "protocol_version": getattr(ctx, "protocol_version", None),
        "input_responses": getattr(ctx, "input_responses", None),
        "client_capabilities": str(getattr(ctx, "client_capabilities", None))[:120],
    }


@mcp.tool()
async def connect_test(ctx: Context) -> dict:
    """Prompt for an API key — verbatim the call insightly_mcp.py._prompt() makes today."""
    try:
        r = await ctx.elicit(message="Enter your Insightly API key.", schema=NewKey)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    action = getattr(r, "action", None)
    out = {"ok": action == "accept", "action": action}
    if action == "accept":
        # Mirror _prompt()'s use of r.data.<field> — proves validation into the model.
        out["got_key_len"] = len(r.data.api_key)
        out["got_pod"] = r.data.pod
    return out


if __name__ == "__main__":
    mcp.run()
