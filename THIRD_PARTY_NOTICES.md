# Third-Party Notices

MeshCenter's own code is MIT-licensed (see [LICENSE](LICENSE)). This file covers the one third-party dependency that isn't permissively licensed, and why it doesn't affect the license of MeshCenter's own code.

## `meshtastic` (GPLv3)

[`meshtastic`](https://github.com/meshtastic/python) — the official Python API/CLI for talking to Meshtastic devices — is licensed under the GNU General Public License v3.0. It's the only load-bearing copyleft dependency anywhere in this project.

To keep GPLv3 code from linking into MeshCenter's own MIT-licensed process, `meshtastic` is used exclusively from `adapters/meshtastic/` — its own Python package, installed into its own virtual environment (`adapters/meshtastic/venv`, separate from Core's `venv/`), and imported only by code running in a **separate OS process** (the "adapter" subprocess, supervised by `meshsrv/adapter_ipc_client.py`). MeshCenter's Core (`server.py`, `api/`, `meshsrv/`, everything outside `adapters/`) talks to that process over a local IPC boundary — newline-delimited JSON on stdin/stdout — and never imports `meshtastic` directly. See `CLAUDE.md`'s "GPLv3 process isolation" section for the technical detail, and `adapters/meshtastic/LICENSE` for the full GPLv3 text.

`meshtastic`'s own dependencies (installed alongside it in `adapters/meshtastic/venv`, pinned in `adapters/meshtastic/requirements.txt`) are all permissively licensed: `bleak` (Bluetooth LE support, MIT — along with its own dependency `dbus-fast`, also MIT), `protobuf` (BSD-3-Clause), `pyserial` (BSD), `pyyaml` (MIT), `requests` (Apache-2.0), `tabulate` (MIT), `pypubsub` (BSD-2-Clause), `packaging` (Apache-2.0 or BSD-2-Clause).

## Chart.js (bundled, MIT)

[Chart.js](https://www.chartjs.org/) v4.4.0 is vendored as `static/chart.umd.min.js` (fetched pre-minified from jsDelivr, per the file's own header comment) and used for telemetry charts (`static/chat-telemetry.js`) and the CPU history chart (`static/chat.js`). MIT-licensed — permissive, no attribution requirement beyond retaining the license notice already present in the bundled file's own header.

## Leaflet (CDN, BSD-2-Clause)

[Leaflet](https://leafletjs.com/) v1.9.4 is loaded from the `unpkg.com` CDN (`templates/index.html`), not bundled — no local copy to track a license file for. BSD-2-Clause. Leaflet itself doesn't require on-map attribution for its own code, but see the OpenStreetMap entry below for the tile data it renders, which does.

## OpenStreetMap tiles (external service, attribution required)

The Map workspace (`static/chat-map.js`) renders tiles from `tile.openstreetmap.org` via a Leaflet `L.tileLayer`, with the required `&copy; OpenStreetMap contributors` attribution already wired into that same `L.tileLayer(...)` call — the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/) requires this attribution to remain visible on the map, not just exist in this file. Map data © OpenStreetMap contributors, [ODbL-licensed](https://www.openstreetmap.org/copyright); MeshCenter doesn't bundle or redistribute the tile data itself, only fetches it live from the map workspace UI. `settings.maps.provider` also offers a `google` option — that's an outbound "open in Google Maps" link, not an embedded/bundled dependency, so it doesn't need an entry here.

## Waveshare e-Paper driver (vendored, MIT-style per-file notice)

`modules/display/drivers/vendor/waveshare_epd/` vendors Waveshare's official demo driver for the 2.13" 4-color e-Paper HAT (G) — see [`LICENSE_NOTICE.md`](modules/display/drivers/vendor/waveshare_epd/LICENSE_NOTICE.md) in that directory for the full provenance (exact source URL, retrieval date, the GitHub-`master`-vs-official-ZIP divergence that matters for anyone re-vendoring this later) and the per-file MIT-style permission notice each vendored file carries in its own header.

## Everything else

Core's own dependencies (`requirements.txt`) — Flask, Pillow, requests, psutil, v4l2py, gunicorn — are all permissively licensed (MIT/BSD/Apache-family). See each package's own PyPI page for its specific license.
