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
  "noisy:<n>" - writes `n` non-JSON garbage lines to stdout BEFORE the
    real JSON response (P0 stabilization follow-up: simulates a stray
    print() slipping onto the protocol channel despite
    ipc_server.py's redirect_stdout - proves AdapterSupervisor.call()'s
    reader tolerates up to MAX_NON_JSON_LINES/MAX_NON_JSON_BYTES of this
    before giving up, rather than killing the adapter on the very first
    bad line like the pre-fix code did).
  "stderr_flood:<bytes>" - writes at least `bytes` bytes to stderr
    (chunked, flushed) before responding normally on stdout - simulates
    enough adapter-side log volume to fill an undrained OS pipe buffer
    (typically 64KB on Linux); if AdapterSupervisor has no stderr-drain
    thread, the write blocks once the buffer fills and the whole call
    wedges until the caller's own timeout fires instead of completing
    normally.

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

        if behavior.startswith("noisy:"):
            count = int(behavior.split(":", 1)[1])
            for i in range(count):
                sys.stdout.write(f"not json at all, garbage line {i}\n")
                sys.stdout.flush()

        if behavior.startswith("stderr_flood:"):
            target_bytes = int(behavior.split(":", 1)[1])
            written = 0
            chunk = ("x" * 4096) + "\n"
            while written < target_bytes:
                sys.stderr.write(chunk)
                sys.stderr.flush()
                written += len(chunk)

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
