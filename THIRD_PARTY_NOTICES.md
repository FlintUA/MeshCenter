# Third-Party Notices

MeshCenter's own code is MIT-licensed (see [LICENSE](LICENSE)). This file covers the one third-party dependency that isn't permissively licensed, and why it doesn't affect the license of MeshCenter's own code.

## `meshtastic` (GPLv3)

[`meshtastic`](https://github.com/meshtastic/python) — the official Python API/CLI for talking to Meshtastic devices — is licensed under the GNU General Public License v3.0. It's the only load-bearing copyleft dependency anywhere in this project.

To keep GPLv3 code from linking into MeshCenter's own MIT-licensed process, `meshtastic` is used exclusively from `adapters/meshtastic/` — its own Python package, installed into its own virtual environment (`adapters/meshtastic/venv`, separate from Core's `venv/`), and imported only by code running in a **separate OS process** (the "adapter" subprocess, supervised by `meshsrv/adapter_ipc_client.py`). MeshCenter's Core (`server.py`, `api/`, `meshsrv/`, everything outside `adapters/`) talks to that process over a local IPC boundary — newline-delimited JSON on stdin/stdout — and never imports `meshtastic` directly. See `CLAUDE.md`'s "GPLv3 process isolation" section for the technical detail, and `adapters/meshtastic/LICENSE` for the full GPLv3 text.

`meshtastic`'s own dependencies (installed alongside it in `adapters/meshtastic/venv`, pinned in `adapters/meshtastic/requirements.txt`) are all permissively licensed: `bleak` (Bluetooth LE support, MIT — along with its own dependency `dbus-fast`, also MIT), `protobuf` (BSD-3-Clause), `pyserial` (BSD), `pyyaml` (MIT), `requests` (Apache-2.0), `tabulate` (MIT), `pypubsub` (BSD-2-Clause), `packaging` (Apache-2.0 or BSD-2-Clause).

## Everything else

Core's own dependencies (`requirements.txt`) — Flask, Pillow, requests, psutil, v4l2py, gunicorn — are all permissively licensed (MIT/BSD/Apache-family). See each package's own PyPI page for its specific license.
