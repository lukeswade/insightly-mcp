# Which edition is the product?

There are two servers in this repo and they implement the same product:

| | Classic | Cloudflare |
|---|---|---|
| Code | `insightly_mcp.py` | `worker/src/*.ts` |
| Ships as | `.mcpb` running on the user's machine | `.mcpb` bridge + a Worker at the edge |
| Keys | local keystore, used in-process | local keystore, sent per request, never stored |
| Acceptance suite | `spike/validate_v31.py` | `worker/validate_worker.py` |

Keeping two behavioural ports in step is real work, and the honest question is whether the
Python server is still a product or has become a reference spec. **The decision, as of
2026-08-21:**

**The Cloudflare edition is the product.** New capability lands there first. It is faster
on the same questions (parallel fan-out, no `uv` bootstrap), it updates without anyone
reinstalling anything, and it can do things a laptop process structurally cannot — R2
snapshots that outlive a chat, one shared rate budget per key, background work that
survives the request that started it.

**The classic server stays, as two things:** the fallback for anyone who cannot reach the
Worker, and the reference implementation the algorithms are specified in. Its suite is the
contract that says what "correct" means; several bugs were caught by the two editions
disagreeing.

## The rule that keeps this from rotting

`tools/check_parity.py` (run by both build scripts) enforces one direction only:

- the Worker must expose **every** tool the classic server exposes;
- anything the Worker has **in addition** must be declared in `WORKER_ONLY`, with the
  reason it stays CF-only.

So the worker may lead, but never by accident: a new tool either gets mirrored into the
classic server or gets a recorded decision. Drift becomes a failing build instead of a
discovery six weeks later.

## What that means in practice

- Fixing a *semantic* bug (wrong answer, wrong sort, wrong field list): fix both. The
  suites are how you prove you did.
- Adding something that needs edge infrastructure (KV, R2, Durable Objects, a public HTTPS
  endpoint): worker only, declared.
- Removing a tool: remove it from both, and drop its `WORKER_ONLY` line if it had one.
