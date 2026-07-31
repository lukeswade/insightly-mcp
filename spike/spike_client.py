#!/usr/bin/env python3
"""Spike 1 harness: drives elicit_spike.py over stdio and answers the elicitation.

Acts as an MCP client that declares the `elicitation` capability, calls the spike's
tools, and — crucially — replies to the server-initiated `elicitation/create`
request that arrives mid-call. That reply is what Claude Code does for a user; if
this round trip works, `_prompt()` can be ported.

    uv run --with 'mcp==2.0.0' python spike/spike_client.py
"""
import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "elicit_spike.py")
ANSWER = {"api_key": "spike-key-not-real", "pod": "na1"}


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdin and proc.stdout

    stderr_lines: list[str] = []
    threading.Thread(target=lambda: stderr_lines.extend(proc.stderr or []), daemon=True).start()

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    results: dict[int, dict] = {}
    elicit_seen: list[dict] = []

    def pump_until(want_id: int, budget: int = 400) -> None:
        """Read messages, answering any server→client request, until want_id resolves."""
        for _ in range(budget):
            line = proc.stdout.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Server-initiated request (the whole point of the spike).
            if "method" in msg and "id" in msg:
                elicit_seen.append(msg)
                print(f"  ← server asked: {msg['method']}")
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "result": {"action": "accept", "content": ANSWER}})
                continue
            if msg.get("id") == want_id:
                results[want_id] = msg
                return

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {"elicitation": {}},
        "clientInfo": {"name": "spike-client", "version": "0"}}})
    pump_until(1)
    init = results.get(1, {}).get("result", {})
    print("initialize →", init.get("serverInfo"), "| proto", init.get("protocolVersion"))
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(cid: int, name: str) -> dict:
        print(f"\ncall {name}…")
        send({"jsonrpc": "2.0", "id": cid, "method": "tools/call",
              "params": {"name": name, "arguments": {}}})
        pump_until(cid)
        r = results.get(cid, {})
        if "error" in r:
            print("  tool error:", json.dumps(r["error"])[:300])
            return {}
        try:
            return json.loads(r["result"]["content"][0]["text"])
        except Exception:
            print("  raw:", json.dumps(r)[:400])
            return {}

    probe = call(2, "probe")
    for k, v in probe.items():
        if k == "connection_attrs":
            print(f"  {k}: {[a for a in v if 'sess' in a.lower() or 'elicit' in a.lower()] or '(none session-like)'}")
        else:
            print(f"  {k}: {v}")

    got = call(3, "connect_test")
    print("  result:", json.dumps(got)[:500])

    print("\n=== VERDICT ===")
    print("server-initiated request delivered:", bool(elicit_seen))
    print("elicitation round trip OK:", bool(got.get("ok")), "| route:", got.get("route"))
    if not got.get("ok") and got.get("error"):
        print("error:", got["error"])
    if stderr_lines:
        tail = "".join(stderr_lines)[-500:].strip()
        if tail:
            print("\nserver stderr tail:\n", tail)

    proc.terminate()
    return 0 if got.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
