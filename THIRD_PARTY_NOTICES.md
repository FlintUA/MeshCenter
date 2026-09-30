# Third-Party Notices

MeshCenter's own code is MIT-licensed (see [LICENSE](LICENSE)). This file covers the third-party dependencies that aren't permissively licensed, and why they don't affect the license of MeshCenter's own code.

## `meshtastic` (GPLv3)

[`meshtastic`](https://github.com/meshtastic/python) — the official Python API/CLI for talking to Meshtastic devices — is licensed under the GNU General Public License v3.0. It's the only load-bearing copyleft dependency anywhere in this project.

To keep GPLv3 code from linking into MeshCenter's own MIT-licensed process, `meshtastic` is used exclusively from `adapters/meshtastic/` — its own Python package, installed into its own virtual environment (`adapters/meshtastic/venv`, separate from Core's `venv/`), and imported only by code running in a **separate OS process** (the "adapter" subprocess, supervised by `meshsrv/adapter_ipc_client.py`). MeshCenter's Core (`server.py`, `api/`, `meshsrv/`, everything outside `adapters/`) talks to that process over a local IPC boundary — newline-delimited JSON on stdin/stdout — and never imports `meshtastic` directly. See `CLAUDE.md`'s "GPLv3 process isolation" section for the technical detail, and `adapters/meshtastic/LICENSE` for the full GPLv3 text.

`meshtastic`'s own dependencies (installed alongside it in `adapters/meshtastic/venv`, pinned in `adapters/meshtastic/requirements.txt`) are all permissively licensed: `bleak` (Bluetooth LE support, MIT — along with its own dependency `dbus-fast`, also MIT), `protobuf` (BSD-3-Clause), `pyserial` (BSD), `pyyaml` (MIT), `requests` (Apache-2.0), `tabulate` (MIT), `pypubsub` (BSD-2-Clause), `packaging` (Apache-2.0 or BSD-2-Clause).

## `ffmpeg` and `v4l-utils` (GPL-2.0-or-later)

USB camera support (`camera/usb_driver.py`, CAM-1, audit review finding F10) drives `ffmpeg` and `v4l2-ctl` (part of `v4l-utils`) as external system programs — invoked via `subprocess`, never imported as a library — the same arm's-length reasoning as the `meshtastic` CLI above. Neither is installed as a Python dependency (see `requirements.txt`); both are system packages `install.sh`/`meshcenter-firstboot.sh` install unconditionally, and are not bundled or vendored anywhere in this repository.

The Debian/Raspberry Pi OS `ffmpeg` build is **GPL-2.0-or-later** (confirmed from its own `/usr/share/doc/ffmpeg/copyright`: "some of the GPL licensed files are used, so the resulting binaries are licensed under GPL v2+" — not LGPL, despite many of ffmpeg's individual source files being LGPL). `v4l-utils`/`v4l2-ctl` is GPL-2.0-or-later (the library, `libv4l`, is LGPL-2.1; the command-line utilities are GPL). Both replace the old `linuxpy`/`v4l2py` Python package (GPL-3.0-or-later), which used to be imported directly into Core's own process — exactly the pattern the `meshtastic`-adapter process boundary above was built to avoid. This is the project's own established practice for a GPL-licensed external program, not a legal opinion.

## Chart.js (bundled, MIT)

[Chart.js](https://www.chartjs.org/) v4.4.0 is vendored as `static/chart.umd.min.js` (fetched pre-minified from jsDelivr, per the file's own header comment) and used for telemetry charts (`static/chat-telemetry.js`) and the CPU history chart (`static/chat.js`). MIT-licensed — permissive, no attribution requirement beyond retaining the license notice already present in the bundled file's own header.

## Leaflet (CDN, BSD-2-Clause)

[Leaflet](https://leafletjs.com/) v1.9.4 is loaded from the `unpkg.com` CDN (`templates/index.html`), not bundled — no local copy to track a license file for. BSD-2-Clause. Leaflet itself doesn't require on-map attribution for its own code, but see the OpenStreetMap entry below for the tile data it renders, which does.

## OpenStreetMap tiles (external service, attribution required)

The Map workspace (`static/chat-map.js`) renders tiles from `tile.openstreetmap.org` via a Leaflet `L.tileLayer`, with the required `&copy; OpenStreetMap contributors` attribution already wired into that same `L.tileLayer(...)` call — the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/) requires this attribution to remain visible on the map, not just exist in this file. Map data © OpenStreetMap contributors, [ODbL-licensed](https://www.openstreetmap.org/copyright); MeshCenter doesn't bundle or redistribute the tile data itself, only fetches it live from the map workspace UI. `settings.maps.provider` also offers a `google` option — that's an outbound "open in Google Maps" link, not an embedded/bundled dependency, so it doesn't need an entry here.

## Waveshare e-Paper driver (vendored, MIT-style per-file notice)

`modules/display/drivers/vendor/waveshare_epd/` vendors Waveshare's official demo driver for the 2.13" 4-color e-Paper HAT (G) — see [`LICENSE_NOTICE.md`](modules/display/drivers/vendor/waveshare_epd/LICENSE_NOTICE.md) in that directory for the full provenance (exact source URL, retrieval date, the GitHub-`master`-vs-official-ZIP divergence that matters for anyone re-vendoring this later) and the per-file MIT-style permission notice each vendored file carries in its own header.

## Everything else

Core's own dependencies (`requirements.txt`) — Flask, Pillow, requests, psutil, gunicorn, cbor2 (MCAttach codec, MIT), pynacl (MCAttach crypto, Apache-2.0) — are all permissively licensed (MIT/BSD/Apache-family). See each package's own PyPI page for its specific license. `v4l2py` was removed from this list in CAM-1 (it used to be listed here as permissive, which was wrong — v4l2py, and the `linuxpy` package it re-exported, are GPL-3.0-or-later; see the `ffmpeg`/`v4l-utils` entry above for what replaced it).
