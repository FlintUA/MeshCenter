"""Standalone fake adapter process for tests/test_adapter_ipc_client.py -
mimics adapters/meshtastic/ipc_server.py's stdin/stdout JSON protocol
without depending on meshtastic/bleak, so _AdapterSupervisor's real
spawn/write/read/kill/respawn behavior can be exercised against a real
subprocess and real OS pipes on any platform (including this project's
Windows dev machine), not mocked out.

Request params control behavior via a `_test_behavior` key the real
adapter protocol never uses:
  "echo" (default) - responds immediately, ok=true, result echoes params.
  "hang" - never responds (simulates a wedged adapter; the test's own
    timeout is what ends this, not the script).
  "crash" - exits immediately without responding (simulates a dead
    process being read from).
  "sleep:<seconds>" - sleeps that long, then responds ok=true - for
    proving a response that lands just inside vs. just outside the
    caller's deadline.

Writes its own PID to stdout on the FIRST request's response
(`_pid` key) so a test can confirm a kill+respawn actually launched a
different OS process, not the same one still running.
"""
import json
import os
import sys
import time

_PID = os.getpid()


def main() -> None:
    first = True
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        params = request.get("params") or {}
        behavior = params.get("_test_behavior", "echo")

        if behavior == "crash":
            sys.exit(1)

        if behavior == "hang":
            time.sleep(3600)
            continue

        if behavior.startswith("sleep:"):
            time.sleep(float(behavior.split(":", 1)[1]))

        response = {
            "protocol_version": 1,
            "ok": True,
            "result": {"echo": params, "_pid": _PID if first else None},
        }
        first = False
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
