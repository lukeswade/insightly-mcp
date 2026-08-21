#!/usr/bin/env python3
"""Make edition drift LOUD.

Two servers implement the same product: insightly_mcp.py (classic, a .mcpb the user runs
locally) and worker/src/tools.ts (Cloudflare). They are behavioural ports of each other,
which is a fine discipline right up until a tool quietly lands on one side only and nobody
notices for a month.

The contract this enforces is one-way and deliberate:

    the worker must expose EVERY tool the classic server exposes  (worker >= classic)
    anything the worker has in addition must be declared in WORKER_ONLY below

So a new capability can ship on the worker first — that is the point of the worker being
the product — but only as an explicit entry here, never by accident. A tool that vanishes
from the worker, or appears there without a decision recorded, fails the build.

See docs/EDITIONS.md for which edition is which.
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Capabilities that exist ONLY on the Cloudflare edition, each with the reason it cannot
# (or should not) be ported to a laptop process. Adding a line here is a decision.
WORKER_ONLY = {
    "aggregate":       "streams a whole object through a group accumulator in the background",
    "task_query":      "queries a completed export held in a Durable Object",
    "export_csv":      "writes to R2 and returns a signed link — needs object storage",
    "snapshot_list":   "R2-backed snapshots outlive any single process",
    "snapshot_query":  "streams a stored snapshot from R2",
    "search_everywhere": "fans out across every object at once; only sane with edge concurrency",
    "join_related":    "parallel link resolution, 6-wide",
}


def classic_tools() -> set:
    """Names decorated with @mcp.tool() in the classic server."""
    tree = ast.parse((ROOT / "insightly_mcp.py").read_text())
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            f = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(f, ast.Attribute) and f.attr == "tool":
                out.add(node.name)
    return out


def worker_tools() -> set:
    """Registered tools, both the literal calls and the bridge-local/app tables.

    The env-key tools are registered from a table (["use_saved", "...", schema]) because
    the worker only ADVERTISES them — the bridge executes them on the user's machine so
    keys never leave it. They still count as present for parity purposes.
    """
    src = (ROOT / "worker" / "src" / "tools.ts").read_text()
    direct = re.findall(r'registerTool\(\s*"([A-Za-z0-9_]+)"', src)
    tabled = re.findall(r'^\s*\["([a-z0-9_]+)",\s*"', src, re.M)
    return set(direct) | set(tabled)


def main() -> int:
    classic, worker = classic_tools(), worker_tools()
    missing = sorted(classic - worker)
    extra = sorted(worker - classic - set(WORKER_ONLY))
    stale = sorted(set(WORKER_ONLY) - worker)

    print(f"classic {len(classic)} tools   worker {len(worker)} tools   "
          f"declared worker-only {len(WORKER_ONLY)}")
    ok = True
    if missing:
        ok = False
        print("\nERROR: the worker is MISSING tools the classic server has:")
        for t in missing:
            print(f"  - {t}")
        print("  The worker is the product; it cannot be behind. Port them or delete them.")
    if extra:
        ok = False
        print("\nERROR: worker tools with no parity decision recorded:")
        for t in extra:
            print(f"  - {t}")
        print("  Either mirror it into insightly_mcp.py, or add it to WORKER_ONLY in")
        print("  tools/check_parity.py with the reason it stays CF-only.")
    if stale:
        ok = False
        print("\nERROR: WORKER_ONLY lists tools the worker no longer has:")
        for t in stale:
            print(f"  - {t}")
    if ok:
        print("editions agree: worker covers every classic tool, extras all declared")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
