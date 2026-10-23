# Camera dependency review (CAM2 Phase 0)

Scope: every dependency the camera subsystem uses today and every one the IP camera work (RTSP, optional vendor control API, browser delivery) would add or consider. Written for the project's own licence hygiene; **it is not legal advice**.

How to read the **Confidence** column: `repo` = taken from this repository's own notices or requirements (`THIRD_PARTY_NOTICES.md`, `requirements.txt`); `known` = well-known public licence of the upstream project, to be re-checked against the exact version at adoption time; `verify` = I am not certain, nothing may be adopted on this row until it is checked against the upstream licence file for the exact version. No package was downloaded or installed for this review.

Project rule being applied: MIT-licensed Core never imports GPL code. GPL programs (ffmpeg, v4l-utils, the `meshtastic` CLI) are only started as separate processes. `scripts/check_core_gpl_imports.py` enforces the Python side in CI.

## 1. Current camera dependencies

| Package | Version | License (SPDX) | Purpose | Integration | Distribution | Obligations | Risk | Alternative | Confidence |
|---|---|---|---|---|---|---|---|---|---|
| Picamera2 | system package (`python3-picamera2`), via `--system-site-packages` | BSD-2-Clause | CSI camera capture | Python import, lazy and guarded (`camera/camera.py`) | Not bundled; installed by the OS | Keep notice if ever bundled (not done) | Low | none needed | known |
| libcamera | system package | LGPL-2.1-or-later (library) | Camera stack under Picamera2 | Loaded by Picamera2, never by MeshCenter code directly | Not bundled | None for dynamic use | Low | none | known |
| rpicam-apps (optional) | system package | BSD-2-Clause | Pulls the same `libav*` stack ffmpeg needs on Raspberry Pi OS | Not used by MeshCenter code | Not bundled | None | Low | none | known |
| Pillow | `>=10.0.0,<12.0.0` (`requirements.txt`) | HPND | JPEG decode/encode for CSI frames, photo validation (`_is_valid_jpeg`), image dimensions | Python import, in-process | Installed from PyPI by the installer; not vendored | Permissive; reproduce notice only if vendored | Low | none | repo / known |
| ffmpeg | distro build (Debian/Raspberry Pi OS); version recorded per node at install time | **GPL-2.0-or-later** for the Debian binary (per its `/usr/share/doc/ffmpeg/copyright`, as already documented in `THIRD_PARTY_NOTICES.md`); the upstream core is LGPL-2.1-or-later, the Debian build enables GPL components | USB camera capture and encoding (`camera/usb_driver.py`) | **External subprocess** via `subprocess`, never imported or linked | Not bundled in the repo or release archive; installed by `install.sh` / `meshcenter-firstboot.sh`. **Not** in the Docker image today | Provide source on request only if MeshCenter itself distributes the binary (it does not) | Low natively; becomes a redistribution question in the image (section 3) | none planned | repo |
| v4l-utils (`v4l2-ctl`) | distro build | GPL-2.0-or-later (utilities); libv4l LGPL-2.1-or-later | USB camera discovery, formats, controls (`camera/v4l2_ctl_parse.py`) | External subprocess | Not bundled | As ffmpeg | Low | none | repo |
| Flask / Werkzeug | `>=3.1.0,<4.0.0` | BSD-3-Clause | Routes, sessions, CSRF context | Python import | PyPI | Permissive | Low | none | repo / known |
| psutil | `>=5.9.0,<8.0.0` | BSD-3-Clause | Host metrics; planned reuse for "own IP addresses" in the address policy | Python import | PyPI | Permissive | Low | stdlib `socket`/`ifaddr` | repo / known |
| gunicorn | `>=23.0.0,<24.0.0` | MIT | WSGI server (8 gthread threads bound the number of simultaneous viewers) | Process | PyPI | Permissive | Low | none | repo / known |
| `meshsrv/subprocess_spawner.py`, `camera/v4l2_ctl_parse.py` | in-repo | MIT (project) | Fork-safe spawning, text parsers | In-repo | Release archive | none | Low | n/a | repo |

Removed earlier and must stay out: `linuxpy` / `v4l2py` (GPL-3.0-or-later), CAM-1.

## 2. Proposed or considered for IP camera support

| Package | Version | License (SPDX) | Purpose | Integration | Distribution | Obligations | Risk | Alternative | Confidence |
|---|---|---|---|---|---|---|---|---|---|
| **ffmpeg** (RTSP input, remux, optional MJPEG transcode) | same distro build as above; Docker: Debian `ffmpeg` package | GPL-2.0-or-later (Debian build) | RTSP client, `-c copy` fMP4 remux, JPEG transcode fallback | External subprocess (same pattern as USB) | Native: not bundled. Docker: **would be installed into the image**, see section 3 | As the current ffmpeg row; image adds redistribution | Medium for the image only | A reduced or LGPL-only ffmpeg build (changes size and codec set; evaluate if the image delta is too large) | repo |
| **ffprobe** (codec/resolution/fps detection) | ships in the same Debian `ffmpeg` package | same as ffmpeg | One short probe per connect | External subprocess | same | same | Low | parse the streaming ffmpeg's stderr banner only | known |
| Python standard library (`ipaddress`, `socket`, `ssl`, `hashlib`, `http.client`, `threading`) | CPython 3.11 | PSF-2.0 | Address policy, TLS fingerprint pinning, fMP4 box splitting, fan-out | In-process | Python itself | none | Low | n/a | known |
| `requests` | `>=2.32.4,<3.0.0` (already in `requirements.txt`) | Apache-2.0 | Optional: Reolink HTTP(S) control calls | Python import | PyPI | Permissive. Note: pinning a certificate fingerprint and refusing redirects are done with a custom adapter / `ssl` hooks, not by disabling verification | Low | stdlib `http.client` (fewer dependencies, more code) | repo / known |
| Browser MSE / `<video>` | native browser feature | n/a (web platform) | Play fragmented MP4 | Own ~150-line player in `static/`; no library | n/a | none | Low (browser matrix, see audit section 7) | HLS (needs a segmenter), WebRTC | known |
| **Reolink HTTP API** (protocol, not a package) | per camera firmware | Vendor documentation and device behaviour; terms of the vendor's API guide unknown | Optional zoom/focus, snapshot, abilities (Phase 2) | Own client written from the documented request/response shapes and from observation; **no vendor SDK, DLL or copied code** | n/a | Do not copy vendor text or code into the repo; name the vendor only to identify compatible hardware | Low if clean-room; unknown if the vendor guide's terms restrict reimplementation | none | verify (vendor terms) |

### Considered and not adopted (for the record)

| Package | License (SPDX) | Why not | Confidence |
|---|---|---|---|
| PyAV (`av`) | BSD-3-Clause wrapper; the wheels bundle FFmpeg libraries under their own (L)GPL terms | In-process linking of FFmpeg into Core breaks the "GPL stays in a separate process" rule; exact bundled-library licences must be checked per wheel | verify |
| OpenCV (`opencv-python`) | Apache-2.0 for OpenCV; bundled FFmpeg is LGPL | In-process, very large, same linking question, no benefit over a subprocess | verify |
| GStreamer (+ plugins) | LGPL-2.1-or-later core; some plugin sets GPL | Another large stack and another IPC model | known |
| MediaMTX / go2rtc | MIT | Separate Go binary to ship, supervise and secure, plus its own RTSP/WebRTC surface; only revisit if (b) and (a) both fail the decision rules | known (verify at adoption) |
| aiortc | BSD-3-Clause | WebRTC needs `av` (see PyAV) and ICE/NAT handling that bridge-mode Docker makes harder | verify |
| hls.js | Apache-2.0 | Requires a segmenter and adds latency; MSE direct is simpler for one live stream | known |
| mp4box.js / jmuxer | BSD-3-Clause / MIT | Not needed: ffmpeg already produces fMP4; box splitting is a few dozen lines | known |
| `reolink-aio` and other community Reolink libraries | verify per package | The task requires a client written from the documented protocol, and a third-party client adds a dependency to a security-sensitive path | verify |

No new **Python** package is proposed for Phases 1-3. If that changes, `requirements.txt` and this table must change in the same PR, and `scripts/check_core_gpl_imports.py` must still pass.

## 3. Docker image impact

- Today the image ships **no** ffmpeg and states `org.opencontainers.image.licenses="MIT AND GPL-3.0-only"` (MIT Core plus the GPLv3 adapter venv). The image is built locally and not published because D0 section 5 (the GPL redistribution documentation gap) is still open.
- Adding Debian's `ffmpeg` to the image means the image **distributes** a GPL-2.0-or-later program. GPL-2.0-or-later is compatible with the existing GPLv3 content, and the label would become `MIT AND GPL-3.0-only AND GPL-2.0-or-later`. This does not change the "do not publish" status; it adds a second reason to close D0 section 5 before ever publishing.
- Obligations to carry into Phase 4 if the image is ever published: ship or offer the corresponding source for the ffmpeg build and its linked GPL libraries (Debian source packages), keep the distro copyright files in the image (they are, by default), and record the build configuration (`ffmpeg -buildconf`) in the notices.
- Size: unmeasured. Estimate a few hundred MB for `apt-get install --no-install-recommends ffmpeg` on `python:3.11-slim`; Phase 1 measures it with a local build and records the number here.
- A reduced or LGPL-only ffmpeg would shrink the image but must still provide RTSP demux, H.264 parse/decode (for the MJPEG fallback), the MJPEG encoder and the MP4 muxer. Evaluate only if the measured delta is unacceptable.

## 4. Patents and codecs (information, not an opinion)

H.264 and H.265 are patent-licensed formats. MeshCenter implements no codec: it starts the distribution's ffmpeg. The remux path (`-c copy`) never decodes or encodes video; the MJPEG fallback decodes H.264 and encodes JPEG inside the distribution's ffmpeg. Natively the redistributor is the OS vendor. In the unpublished Docker image the builder is the redistributor of that binary. If MeshCenter or its image is ever published, take proper legal advice on this point.

## 5. Obligations summary

1. `THIRD_PARTY_NOTICES.md` already covers ffmpeg and v4l-utils as external programs; Phase 4 extends the entry to RTSP/ffprobe use, the Docker image, and (if added) the vendor control protocol statement ("compatible hardware is named for identification only; no vendor SDK or code is used").
2. No new Python or JavaScript library enters the release archive in Phases 1-3 under the current plan, so no new `licenses/` file is expected.
3. Do not describe ONVIF support, conformance or certification anywhere. It is future work only.
4. Re-run this review before any release that changes a row above, and record the exact `ffmpeg -version` / `-buildconf` of the nodes and the image used for the measurements in the Phase 0 report.

## 6. Open items

- Confirm the exact licence terms of the vendor's API guide before Phase 2 (row marked `verify`).
- Record Debian/Raspberry Pi OS ffmpeg versions actually installed on .104, .107 and the image; the local measurement PC uses a third-party "full" Windows build whose licence (it enables GPL and probably version 3 components) must not be taken as representative of the shipped build.
- Measure the Docker image delta and fill it into section 3.
