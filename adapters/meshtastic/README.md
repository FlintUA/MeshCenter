# MeshCenter Meshtastic Adapter

This is the Meshtastic radio transport adapter for [MeshCenter](https://github.com/FlintUA/MeshCenter). It implements the `RadioTransport` interface (`meshsrv/radio_transport.py`, "Backend Protocol v1") against the official [`meshtastic`](https://github.com/meshtastic/python) Python package, and runs as a separate OS process in its own Python virtual environment so that GPLv3-licensed code never links into MeshCenter Core's own MIT-licensed process. See MeshCenter's own `CLAUDE.md` ("GPLv3 process isolation") and `THIRD_PARTY_NOTICES.md` for the full architecture and reasoning.

## License

Everything under `adapters/meshtastic/` in this archive is **GPLv3-licensed as a whole** (not merely "MIT code that happens to import a GPLv3 dependency") — see [LICENSE](LICENSE) for the full text. This reflects that the adapter's own code is written specifically to drive the GPLv3 `meshtastic` package and ships as one inseparable unit with it.

This archive also includes a small number of files under `meshsrv/` and `hardware/` that the adapter has a real import dependency on (see MeshCenter's own `scripts/build-release.sh` for the exact list and why) — those files are **MIT-licensed Core code**, not part of the GPLv3 adapter itself. MIT permits redistributing them alongside GPLv3 code like this, provided MIT's own permission notice is kept: that notice is included in this archive as `LICENSE-MIT` (a copy of MeshCenter's own root [LICENSE](../../LICENSE), renamed by `scripts/build-release.sh` when it builds this archive — this repo's own copy of the adapter directory doesn't carry that renamed copy itself, only the built release archive does).

In short: files under `meshsrv/` and `hardware/` in this archive are MIT-licensed (see `LICENSE-MIT`); everything under `adapters/meshtastic/` is GPLv3 (see `LICENSE`).

## Running

This adapter is not meant to be run standalone — it's started and supervised by MeshCenter Core as a subprocess (`python -m adapters.meshtastic.ipc_server`, see `meshsrv/adapter_ipc_client.py`'s `AdapterSupervisor`), communicating over newline-delimited JSON on stdin/stdout (`meshsrv/ipc_protocol.py`, documented in MeshCenter's `docs/BACKEND_API.md`). Install its own dependencies (`requirements.txt` in this directory) into its own virtual environment, separate from Core's.
