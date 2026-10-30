# Camera source audit (CAM2 Phase 0)

Status: Phase 0 deliverable, read-only audit. No production code was changed. After Flint's go, a throwaway harness measured the test camera (read only, one RTSP session at a time, no camera setting touched) from a Windows PC and from .104; results are in section 7.5. Figures elsewhere marked **estimate** that section 7.5 does not cover remain hypotheses. Facts about the code were read from `main` at f2d2722 (v1.12.3).

**Update after review (Flint, Phase 1):** the decisions below are final and this document was brought in line with them. D1 source store is instance-scoped. D2 viewer caps are 4 per source and 6 in total. D3 in a container the whole Docker bridge network(s) and the gateway are refused, not only the gateway. The `ps` exposure is solved by feeding ffmpeg a stdin ffconcat script with a minimal protocol whitelist (section 9.3). Codec, resolution and fps are read from the fMP4 init segment, not by ffprobe (section 11). Cold start target: a warm join is instant and the cold median is <= 8 s on a Pi Zero 2W, with a visible "Connecting..." state (section 7.5). The shipped code is in PRs #376 (Phase 1a, merged) and #377 (Phase 1b).

Companion document: [`docs/legal/camera-dependency-review.md`](../legal/camera-dependency-review.md).

## 1. Summary

The camera subsystem already has the right shape for a network source: a driver ABC, a registry/dispatcher, a persisted "active" id, one shared manager instance behind two route modules, and a source selector in the Camera tab. A network (RTSP) driver can plug in without a second manager, store or API.

Four things in the current design are **not** ready for it, and they are the real work of Phase 1:

1. The registry is built by **hardware discovery** (`build_camera_manager()`), rebuilt from scratch on every rescan. A saved, user-created source has no place to live and would be dropped on rescan.
2. The driver contract is **JPEG-only** (`stream_mjpeg()` yields JPEG bytes). H.264 from an IP camera does not fit it without a transcode. This is the key design question (section 7).
3. Most Camera-card settings and the screenshot button are bound to the **CSI module's globals** (`camera/camera.py`), not to the active driver.
4. Credentials: nothing in the camera path handles secrets, and `ffmpeg` takes the RTSP URL (with the password) on its **command line**, visible in `ps`.

Recommendation in one paragraph (provisional until the section 7 numbers exist): add one `RtspCameraDriver` behind the existing `CameraDriver` ABC; store sources in a new instance-scoped `data/camera_sources.json` (secrets in a separate 0600 file); keep exactly one upstream `ffmpeg` per source; deliver H.264 sources to the browser as **fragmented MP4 over MSE without transcoding** as the primary path, with **MJPEG via ffmpeg transcode** of the sub stream as the fallback and camera/RTSP **snapshot polling** as the cheap last resort. Existing CSI and USB code paths stay byte-for-byte as they are.

## 2. Components (exact paths)

| Area | File | What it is |
|---|---|---|
| Driver base | `devices/device_driver.py` `DeviceDriver` | Generic lifecycle ABC: `id`, `device_type`, `display_name`, `detect()`, `start(**options)`, `stop()`, `get_status()`. Documents that `id` must be stable across restarts. |
| Camera ABC | `camera/camera_driver.py` `CameraDriver` | Adds `stream_mjpeg() -> Iterator[bytes]` (raw JPEG per yield), `capture_photo(resolution)`, `list_resolutions()`, `get_controls()`, `set_controls()` (the last three have defaults: `[]`, `{}`, `False`). |
| Registry / dispatcher | `camera/camera_manager.py` `CameraManager` | In-memory dict of drivers, one `_active_id`. `set_active()`, `active()`, `list_drivers()`, `get_status()`, `stream_mjpeg()`, `mjpeg_multipart_stream()` (owns the `--frame` multipart envelope), `capture_photo()`, `list_resolutions()`. No file I/O of its own. |
| Discovery | `camera/camera_manager.py` `build_camera_manager(persisted_active_id)` | Builds the dict: registers `CsiCameraDriver` if `detect()` finds a sensor, then one `UsbCameraDriver` per `discover_usb_cameras()` hit, with vendor:product de-duplication between the two. Does real device I/O. |
| CSI | `camera/csi_driver.py` `CsiCameraDriver` (`id = "csi"`) | Thin wrapper over `camera/camera.py` module globals (Picamera2). |
| USB | `camera/usb_driver.py` `UsbCameraDriver` (`id = "usb:<vid>:<pid>:<videoN>"`) | One persistent `ffmpeg` subprocess per active camera, frames split by SOI/EOI into a shared `_last_frame`, fanned out to any number of `stream_mjpeg()` generators; `v4l2-ctl` for discovery and controls (`camera/v4l2_ctl_parse.py`). Spawns through `meshsrv/subprocess_spawner.py` (PDEATHSIG per-thread fix, F15). |
| CSI internals + gallery helpers | `camera/camera.py` | Picamera2 lifecycle, `VIDEO_CONFIG`/`PHOTO_CONFIG`/`CAMERA_CONTROLS` globals, `camera_config.json`, and the driver-agnostic screenshot helpers (`get_screenshot_day_dir`, `make_screenshot_filename`, `cleanup_old_screenshots`, `get_gallery_storage_info`). |
| Live-camera routes | `api/api_camera.py` `register_camera_routes(app, camera, camera_manager_state, device_manager, handle_errors)` | `GET /video_feed`, power (`/api/camera/power`), status, settings, stop, switch_mode, screenshot(s), photo capture/save. Owns `data/camera_power.json`. |
| Source routes | `api/api_camera_manager.py` `register_camera_manager_routes(...)` | `GET /api/devices/cameras`, `POST /api/devices/cameras/rescan`, `POST /api/camera/active`. |
| Persisted selection | `storage/device_manager.py` `DeviceManager` | `data/profiles/<id>/devices.json` schema v2: `active_camera_id` plus `devices.cameras{}` keyed by driver id (model bookkeeping only). Has a v1->v2 migration. |
| Wiring | `server.py` | `camera_manager_state = {"manager": None}` shared mutable dict; both route modules read `["manager"]` per request. Manager is built in `start_runtime()` (not at import) and only when `camera_power.json` says enabled; otherwise built lazily by `_ensure_manager()` in `api/api_camera.py` or by `/api/camera/active`. |
| Frontend | `static/chat-camera.js` (live view: `<img src="/video_feed">`, settings, screenshot), `static/chat-photo.js` (photo preview/save), `static/media.js` (gallery), `static/chat.js` (`renderCameraManagerCards`, `rescanCameras`, `setActiveCamera`, `loadCameraSourceSelector`, `onCameraSourceSelectChange`) | |
| Container switch | `meshsrv/runtime_env.py` | `is_container()`, `capabilities()["hardware_io"]`. `camera/camera.py` `init_camera()` returns False in a container. |

## 3. Current data flow

```
 Browser                          MeshCenter (one gunicorn worker, 8 threads)                 Hardware
 -------                          ---------------------------------------------               --------
 <img src=/video_feed> --GET-->   api_camera.video_feed()
                                    -> manager.mjpeg_multipart_stream()
                                         -> active driver.stream_mjpeg()   (one request thread per viewer)

   CSI :  stream_mjpeg() polls camera.get_camera_frame() -> PIL JPEG encode  -----------------> Picamera2 / libcamera
   USB :  stream_mjpeg() polls self._last_frame (sleep 0.01 loop)
                         ^ filled by reader thread <- ffmpeg stdout (SOI/EOI split) <- ffmpeg -f v4l2 -> /dev/videoN
                           (YUYV: transcode to MJPEG; MJPG camera: -c:v copy)

 POST /api/camera/active {driver_id}
   -> manager.set_active(id)  (stop previous, start new)
   -> devices.json["active_camera_id"] = id        (profile-scoped)

 POST /api/devices/cameras/rescan
   -> stop active driver, build_camera_manager() from scratch (device I/O)

 Startup: start_runtime() -> if camera_power.json enabled: build_camera_manager(persisted_active_id)
```

## 4. Answers to the spec's questions

**Where does source detection live?** `camera/camera_manager.py:build_camera_manager()`. CSI via `CsiCameraDriver.detect()` (opens Picamera2), USB via `camera/usb_driver.py:discover_usb_cameras()` (`/dev/video*` + `v4l2-ctl`).

**Which source types exist?** Two: `csi` (id `"csi"`) and `usb` (id `"usb:<vid>:<pid>:<videoN>"`). Both ids are derived from hardware, not chosen by the user.

**Startup-only or live switching?** Live. `POST /api/camera/active` calls `CameraManager.set_active()`, which stops the old driver and starts the new one with no MeshCenter restart. Hot-plug is not watched; a physical change needs a manual rescan.

**Several saved configs?** No. There is nothing user-created to save. `devices.json` stores only the last active id and a model string per detected driver.

**Are the UI settings tied to one camera?** Yes, to the CSI module. `GET/POST /api/camera/settings`, `/api/camera/switch_mode`, `/api/camera/mode/<mode>`, `/api/camera/stop` and `/api/camera/screenshot` call `camera.*` (module globals of `camera/camera.py`) directly, not the active driver. Their values live in the global `data/camera_config.json` (not per source, not per profile). Only `/video_feed`, `/api/camera/power`, `/api/camera/status`, `/api/photo/capture` and `/api/photo/save` dispatch through the manager. (Read from code; the screenshot behaviour with USB-only hardware is untested live - see problem P4.)

**Reusable APIs?** Yes: `/api/devices/cameras`, `/api/camera/active`, `/api/camera/status`, `/api/photo/save`, `/video_feed`, the `cameraSourceSelect` dropdown in the Camera tab (hidden when fewer than two cameras exist) and the Devices tab cards. CSRF is a global `before_request` hook for every non-GET `/api/` route (`api/api_auth.py:_enforce_csrf`, token via `static/csrf.js`), so new routes inherit it.

**What blocks RTSP?**
- Registry built only from hardware discovery (P1).
- JPEG-only driver contract and `<img>`-based player (P2).
- No secret handling and ffmpeg argv exposure (P3).
- CSI-bound settings/screenshot routes (P4).
- Container: `usb_tooling_missing` is computed from the host's ffmpeg/v4l2-ctl and the Docker image has no ffmpeg, so the UI would say "USB camera requires ffmpeg" (P8).
- Camera card/flows assume a local device; there is no concept of "connecting", "reconnecting", "unsupported codec" in `get_status()` (P6).

## 5. Must be preserved

- The `CameraDriver` contract for CSI and USB, `/video_feed` MJPEG behaviour, `build_camera_manager()` de-duplication logic, rescan semantics.
- ffmpeg as an external GPL program, never imported (`scripts/check_core_gpl_imports.py` guards the Python side).
- The spawning rule: every PDEATHSIG-armed `Popen` goes through `meshsrv/subprocess_spawner.py` (CLAUDE.md, F15). A new RTSP driver must reuse `camera/usb_driver.py:_popen_ffmpeg` / `_terminate_process` logic or lift them into a shared helper, not re-implement.
- The exit-code gotcha: ffmpeg exits 0 on a lost source; track `_stop_requested` explicitly (USB driver module docstring). The same applies to a camera that drops RTSP.
- Persisted-off behaviour: with `camera_power.json` disabled, nothing touches camera hardware or the network at startup.
- `gunicorn.conf.py`: one worker, 8 gthread threads (every long-lived viewer holds a thread; see P7).

## 6. Problems found

| # | Problem | Evidence | Impact on CAM2 |
|---|---|---|---|
| P1 | Registry is hardware-discovery only; rescan rebuilds everything | `build_camera_manager()`, `api_devices_cameras_rescan()` | Saved network sources must be injected by the builder from a store and must survive rescan; rescan must not make network connections. |
| P2 | Driver contract is JPEG only; player is `<img>` | `camera/camera_driver.py`, `static/chat-camera.js:1003` | H.264 needs a transcode or a second delivery kind (`fmp4`) and a `<video>` player. |
| P3 | Secrets and ps exposure | `usb_driver._build_ffmpeg_stream_argv` passes inputs as argv; no secret store exists for the camera path | RTSP password would be in `/proc/<pid>/cmdline` of ffmpeg for its lifetime. Needs a spike (section 9.3). |
| P4 | Settings, mode switching, `stop`, screenshot are CSI-global | `api/api_camera.py` `camera.update_camera_settings`, `camera.capture_screenshot` (calls `start_camera()`, which returns False unless `CAMERA_AVAILABLE`) | With an IP source active these routes would act on (or fail against) the CSI module. They must dispatch by source or be hidden per source. |
| P5 | `set_active()` assigns `_active_id` before `start()` and leaves the previous driver stopped on failure | `camera_manager.py:set_active` | For a network source whose start can fail on a network blip, in-memory active id can differ from what `/api/camera/active` persisted (it returns 404 and does not save). Network `start()` must be non-blocking and succeed into a "connecting" state, and `set_active` should be fixed to roll back or keep a consistent state. |
| P6 | `get_status()` has no connection-state vocabulary | `DeviceDriver.get_status` requires only `ok/started/model` | Need `state` (connecting/live/reconnecting/error/unsupported_codec), codec/resolution/fps, reconnect info. Additive keys only. |
| P7 | Fan-out is a 10 ms sleep poll per viewer; each viewer pins a gunicorn thread | `usb_driver.stream_mjpeg` loop (`time.sleep(0.01)`); `gunicorn.conf.py threads = 8` | Memory is bounded (a slow client skips frames, shared single buffer), which satisfies the "slow client never grows memory" rule. But the polling wakes 100x/s per viewer on a Pi Zero and 8 threads cap total viewers. The new fan-out should wake on a `threading.Condition`, cap viewers per source and globally, and 429/503 beyond the cap. |
| P8 | Container: `usb_tooling_missing` is True and the Camera UI is not gated by `MC_RUNTIME` | `api_camera_manager._summary`, `static/chat.js:11224` only gates USB/Bluetooth radio lists | In Docker the Devices tab would claim USB needs ffmpeg. Needs a container-aware message, and `build_camera_manager()` must register network sources regardless of `hardware_io`. |
| P9 | Gallery limits are hard-coded and global | `cleanup_old_screenshots(max_mb=500, keep_days=30)` repeated in `camera.py` and `api_camera.py` | A periodic or bulk IP snapshot could evict the user's real photos. Needs a per-source quota or a separate subfolder (section 10). |
| P10 | Camera status keys are inconsistent | CSI status has `mode`, `quality`, `controls`; USB has `pixel_format`, `last_frame_age_seconds` | UI branches on presence of keys. Keep additive; add `source_type`. |
| P11 | `devices.json` (where `active_camera_id` lives) is **profile-scoped** | `storage/device_manager.py` under `data/profiles/<id>/` | A LAN camera belongs to the installation, not to a radio profile. After a profile switch the saved active id would not exist and the manager would silently fall back to the first driver. Decision needed (section 8). |

These are findings from reading code. P4, P7 and P8 behaviours were not exercised live in Phase 0 (no node was touched).

## 7. Browser transport decision (the key design question)

The spec says "reuse the existing transport" (MJPEG) and "no transcoding". For an H.264 source these conflict: MJPEG requires decode + JPEG encode.

### 7.1 Options

| | Path | CPU | Browser bandwidth | Latency | New code |
|---|---|---|---|---|---|
| (a) | `ffmpeg -i rtsp://... -c:v mjpeg -q:v N -f mjpeg pipe:1`, reuse USB-style reader and `/video_feed` envelope | decode + encode (estimate: roughly 0.2-0.5 of one A53 core for 640x360@15; unmeasured) | MJPEG: roughly 3-5 Mbps per viewer (estimate), multiplies with viewers | lowest (well under 1 s, estimate) | small; reuses almost everything |
| (b) | `ffmpeg -i rtsp://... -an -c:v copy -f mp4 -movflags frag_keyframe+empty_moov+default_base_moof pipe:1`, Python caches the init segment, splits fragments, fans them out; browser plays via MSE | near zero (demux/remux only; estimate a few percent of one core) | the camera's own bitrate (sub stream is typically well under 1 Mbps; main 8 Mbps), independent of viewer count only per viewer | GOP-bound, roughly 1-3 s (estimate) | new route + new JS player + fMP4 box parsing |
| (c) | camera HTTP snapshot, or one RTSP frame per poll | very low | one JPEG per poll | 1-2 s per frame | small; also reusable for snapshots |
| WebRTC | not considered unless (a)-(c) fail: signalling, ICE/NAT in Docker bridge, a library (aiortc BSD-3 + `av`) or a Go sidecar (MediaMTX/go2rtc, MIT), all new distribution surface | | | | large |

Why (b) matters beyond CPU: it is the **only** way to show the 4K main stream without a transcode, which is the Pi Zero rule in Phase 1 item 7 ("main only when the chosen transport can do it without transcoding"). With (a), "main" on a Pi Zero is impossible; on a PC/NAS it is a 4K decode + encode per source.

(b) risks to verify: MSE support on the user's actual browsers (desktop Chrome/Firefox/Safari, Android Chrome, iOS Safari which only gained MSE via `ManagedMediaSource` in 17.1 - confirm), join latency (a new viewer waits for the next keyframe unless the server replays the init segment plus the last GOP), cameras whose GOP is long, and `profile-level-id`/codec string detection for `addSourceBuffer`.

### 7.2 Pre-registered decision rules

Written before measuring so the numbers decide, not the author:

- **(b) primary** if: plays in Chrome desktop, Android Chrome, Firefox desktop and Safari (desktop; iOS if available) on the sub stream; median glass-to-glass latency <= 3 s; ffmpeg remux CPU <= 10% of one core on the Pi Zero 2W; and a 30 s join test starts playback in <= 2x GOP length.
- **(a) kept as fallback** if its sustained ffmpeg CPU on the Pi Zero 2W is <= 50% of one core (sub stream only) with radio health unchanged (section 7.4). If it exceeds that, (a) is offered on PC/NAS-class hosts only and disabled by default on low-power hosts (`_low_power_host()` already exists in `camera/usb_driver.py`).
- **(c)** is always implemented for snapshots; as a live fallback only if both (a) and (b) are unusable on a host.
- If (b) fails the browser matrix, (a) becomes primary and "main stream" is simply not offered on low-power hosts.

### 7.3 Measurement plan (needs Flint's go, see section 12)

Cameras limit RTSP sessions, so every run uses **one** session at a time, sub stream unless stated, 5 minutes per path, 2 minutes idle between runs. The RLC-811A is read only: no setting, codec, stream or user is changed and no lens command is sent in Phase 0.

Hosts: **.104** (Pi Zero 2W, the worst case) and a **PC/NAS-class** host. ffmpeg 9.0 (Gyan full build) is already installed on Flint's Windows PC; on .104 the Debian ffmpeg from `install.sh` is used. Record `ffmpeg -version` and the build's `--enable-*` flags for the licence review.

Per run, collected by a small throwaway harness kept in the scratchpad (not committed):

| Metric | How |
|---|---|
| ffmpeg CPU | `/proc/<pid>/stat` utime+stime delta over the window, reported as % of one core and % of host; on Windows PowerShell `Get-Process` TotalProcessorTime |
| MeshCenter + browser-side CPU | `ps`/`top` sample every 5 s; for the browser, DevTools Performance for 60 s |
| RAM | RSS of ffmpeg and of the gunicorn worker, every 5 s |
| Load | `loadavg`, `vcgencmd get_throttled` and `measure_temp` on the Pi |
| Latency (glass to glass) | the camera's OSD clock vs an NTP-synced reference in the same browser screenshot; 10 samples, median and max |
| Time to first frame | process start to first decoded frame/fragment |
| Bandwidth | bytes in from the camera, bytes out to one browser |
| Frame health | decoded fps vs nominal 15, dropped/duplicated frames from ffmpeg stats |
| Radio health | `/api/radio_health` sampled every 10 s for 5 min idle baseline, then during each run (listener state, `listener_exits`, packet age). One DM to a node on **Flint-pvt [1] only**, never LongFast [0] |
| Join behaviour (b only) | 10 viewer joins at random times; time to first picture |

Paths tested: (a) ffmpeg transcode to MJPEG on the sub stream; (a') same with `h264_v4l2m2m` hardware decode on the Pi if the build exposes it (variant, may be absent); (b) fMP4 remux of the sub stream, then of the main stream (main remux only to confirm it stays cheap, no decode); (c) one RTSP frame grab per second via `ffmpeg -frames:v 1`, and one HTTP snapshot request if Flint allows Phase 2's login step early.

Credential handling for the harness: Flint runs it himself in his Terminal tab with a hidden prompt (`read -s`), the URL never reaches the transcript; the harness writes only numeric CSV and a scrubbed log. The camera address is never put in code, defaults, tests or this repository. Alternative if Flint prefers: wait for Phase 1's UI and measure with the real driver (slower to decide, same data).

### 7.4 Radio guardrail

ST1 showed serial claims are timing sensitive on a Pi Zero. During every .104 run the harness records radio health against the idle baseline. A run that causes a listener exit, a post-claim exit, or a failed DM is a failed run for that path on that host.

### 7.5 Measured results (2026-10-23)

Camera streams (ffprobe): sub 640x360 H.264 High, 15 fps; main 3840x2160 H.264 High level 5.1, 25 fps. Neither probe reports a bitrate. Sources: Windows PC (16 cores, ffmpeg 9.0 Gyan full build), .104 (Pi Zero 2W class, aarch64, 4 cores, 415 MB RAM, Debian ffmpeg 7.1.5, camera RTT 23 ms). 30 s per path on the PC, 300 s on .104 (main remux and snapshot loop 60 s). CPU is percent of one core. Single runs, treat as indicative. The first set of runs overlapped in time with other RTSP sessions to the same camera (Flint ran the harness on the PC at the same time); the two .104 sub-stream paths were therefore rerun with no other session on the camera (rows marked clean rerun). Local ffmpeg CPU was the same with and without the overlap (50.0% vs 50.7% in the two halves of the first .104 transcode run), so the CPU and RAM columns are not affected; first-frame times of .104 stayed 7-13 s in every run, so they are a property of that host.

| Path | Host | CPU | RSS | Out rate | First frame | Notes |
|---|---|---|---|---|---|---|
| (a) MJPEG transcode, sub | PC | 5.9% | 90 MB | 3.7 Mbps | 3.7 s | 402 frames in 30 s |
| (a) MJPEG transcode, sub | .104 | **46% avg, 57.5% max** | 62 MB | **3.7 Mbps** | 9.1 s | clean rerun, nothing else on the camera: 4418 frames in 303 s (14.6 fps); first run overlapped other sessions and gave 50% avg / 61% max, same within noise |
| (b) fMP4 remux, sub | PC | 0.3% | 24 MB | 0.1 Mbps | 5.7 s | |
| (b) fMP4 remux, sub | .104 | **1.0% avg, 2.4% max** | 44 MB | **0.13 Mbps** | 9.2 s | clean rerun: 78 fragments in 303 s (about 4 s GOP, keyframe fragments); first run 1.2% avg |
| (b) remux, main 4K | PC | 2.3% | 26 MB | 7.8 Mbps | 3.6 s | |
| (b) remux, main 4K | .104 | 11.9% avg, 47.8% max | 29 MB | 7.3 Mbps | 8.5 s | 27 fragments in 60 s; spikes likely the Wi-Fi/USB network path, not codec work |
| (a) transcode, main 4K | PC | **220%** | 1.45 GB | 144 Mbps | 3.2 s | not feasible on any Pi |
| (c) RTSP single-frame grab, sub | PC | - | - | 31 KB per frame | 3.9 s per grab | |
| (c) RTSP single-frame grab, main | PC | - | - | 400 KB per frame | 2.1 s per grab | |
| (c) RTSP single-frame grab, sub | .104 | - | - | - | 6.9 s per grab | 9 grabs in 60 s |
| stdin URL via ffconcat (no transport option) | PC, .104 | - | - | 0 | none | no data in 20 s: UDP was used. Rerun in Phase 1a with `option rtsp_transport tcp`: works (see 9.3) |

Reading the numbers against the pre-registered rules (section 7.2):

- **(b) remux, sub, on .104: 1.2% avg CPU, limit was 10%; passes the CPU rule by a wide margin.** The 4K main stream also remuxes without a transcode at 12% average on the Pi, which is the only way main is possible there.
- **(a) MJPEG transcode, sub, on .104: 46% avg CPU (clean rerun), 57.5% max, limit was 50% sustained: passes on the average, but with almost no headroom** (the max exceeds it, the host has 4 cores and a radio listener to run, and the stream ran at about 14.6 of 15 fps). At 3.7 Mbps per viewer it also multiplies on Wi-Fi. Result: (a) stays a fallback, off by default on low-power hosts and enabled only on request or on PC/NAS-class hosts.
- The camera's sub GOP is about 4 s (74 fragments in 300 s), so a new MSE viewer waits up to about 4 s unless the server replays the init segment plus the last GOP. First-frame times of 8-13 s on .104 are dominated by ffmpeg/RTSP start-up and probing on that host and must be cut in the driver (shorter `-analyzeduration`/`-probesize`, or a warm upstream) before this is acceptable for UX.
- **(c)** is slow for a live fallback (about 4 s per grab on the PC, 7 s on .104, because every grab is a new RTSP session and waits for a keyframe: the sub stream has one about every 4 s, the 4K main stream about every second, which is why main grabs took 2 s). It is fine for a snapshot to the gallery. A camera HTTP snapshot was not measured (it needs the Phase 2 login step).
- **Radio health on .104:** during the 20 minute run (16:18-16:36) the journal has 217 lines and no listener-exit, error or restart entries; uptime and load are normal afterwards. Caveats: `/api/radio_health` was not polled (it needs login), and no DM was sent, so delivery was not tested. Not yet sufficient to sign off the radio guardrail; the Phase 1 live test repeats it with a DM on Flint-pvt [1].
- **Browser playback of (b), measured with a throwaway MSE page** (Chromium engine of the desktop app, sub stream, one ffmpeg remux with a caching Python fan-out, run on the PC and on .104 with the page tunnelled over SSH):
  - Plays with no transcoding; `MediaSource.isTypeSupported('video/mp4; codecs="avc1.640033"')` is true (the camera's sub stream is High profile, level 5.1).
  - ffmpeg on .104 with one viewer attached: about 2.7% of one core, 12 MB RSS, 0.36 Mbps out of the Pi, with 0.5 s fragments.
  - **Findings that shape the real player and server:** (1) the response must be HTTP/1.1; as HTTP/1.0 with a `Transfer-Encoding: chunked` header the browser does not de-chunk and the chunk sizes end up in the video data (nothing plays and nothing errors). (2) With `frag_keyframe` the muxer emits a fragment only when the next keyframe arrives, so latency is at least one GOP (about 4 s on this camera); `-frag_duration 500000 -movflags empty_moov+default_base_moof` gives 0.5 s fragments and cut the displayed delay by about 3.8 s. Fragments no longer start on keyframes, so a joining viewer must be sent from the newest fragment whose first sample is a sync sample (read the `trun` first-sample-flags). (3) A player buffer target of 1.5 s stalls often because the camera delivers frames unevenly (about 13-14.5 fps instead of 15); a 3 s target played smoothly (2 stalls, both at start; about 13.8 fps decoded). (4) The browser throttles playback when its pane or tab is hidden. (5) Time to first picture was 2.3-5.5 s when the upstream was already running (joins from the cached fragment), versus 8-13 s for a cold ffmpeg start on .104.
  - **Delay to the screen:** the absolute value could not be measured, the camera clock is not synchronised (the on-screen clock lags the PC clock by 12-21 s, which includes an unknown clock offset). Relative result: keyframe fragments added about 4 s over 0.5 s fragments. Design estimate for the 0.5 s variant: 3 s buffer + 0.5 s fragment + about 1 s RTSP/ffmpeg = 4-5 s. That is an estimate, not a measurement.
- **Radio health, clean reruns on .104:** no listener-exit, error or traceback entries in the journal for the run windows (the check printed zero). Still no DM was sent during a stream run, so delivery is untested.
- **Cold start (Phase 1b, real driver):** measured through the full stack on the real camera: median first picture 2.7 s on the PC (one outlier 6.4 s) and 5.2-13 s (median 7.1 s) on .104. On .104 the time is ffmpeg start-up (about 1.9 s on that CPU), then the wait for the camera's next keyframe (its sub stream sends one about every 4 s, so 0-4 s of luck), then about 1.2 s of stream analysis. **Accepted target (Flint): a warm join is instant (the upstream stays up for 60 s after the last viewer, no longer, no pre-warm on tab open); cold median <= 8 s on a Pi Zero 2W, with a visible "Connecting..." state instead of a blank box.** Shorter probe options did not help in any measurement. The only real lever is on the camera: a shorter I-frame (keyframe) interval makes the first picture faster. MeshCenter never changes a camera's settings, so this is a one-line note in the User Guide.
- **Not measured:** Firefox, Safari, Android and iOS playback (needs real devices), the cold-start cut via a shorter probe, the `h264_v4l2m2m` variant, a DM during a stream.

Decision (backed by numbers for CPU and bandwidth; the browser matrix beyond Chromium is still open): **primary (b) H.264 remux to fMP4 over MSE; fallback (a) MJPEG transcode on capable hosts only; snapshots via (c) or the camera snapshot.** The browser playback and latency rules in 7.2 are still open and must be checked with a throwaway player page before Phase 1b commits.

## 8. Minimal-change proposal

1. **One new driver**, `camera/rtsp_driver.py` `RtspCameraDriver(CameraDriver)`, id `ip:<source-uuid>` (stable, user-independent of the host address). Registers into the existing `CameraManager`; no second manager.
2. **Source store**: new `data/camera_sources.json`, written with `storage/json_store.py:safe_write_json` (atomic, quarantines corruption). **Instance-scoped, not profile-scoped** (open decision, recommended): like `epaper_config.json`, a LAN camera belongs to the installation. Secrets in a separate `data/camera_secrets.json` (section 9). `active_camera_id` stays in `devices.json` for CSI/USB compatibility; the builder falls back cleanly if the id is unknown.
3. **Builder**: `build_camera_manager()` gains an optional `network_sources` argument supplied by `server.py`/the two route modules; it instantiates `RtspCameraDriver` objects without any network I/O. `detect()` for these drivers only validates config; the first connect happens in `start()`.
4. **Manager/route changes limited to**: `set_active()` consistency fix (P5), additive status keys (P6), a stream-kind field, source CRUD routes (new small route module following the existing `register_*_routes(...)` DI pattern), and routing CSI-global settings by source type (P4).
5. **Delivery**: `/video_feed` unchanged for CSI/USB. New `GET /api/camera/stream.mp4` (name to decide) for fMP4 sources; the client chooses `<img>` or `<video>` from `stream_kind`.
6. **Process model per source**: one `ffmpeg` upstream, started on first viewer, stopped after the idle watchdog (reuse the USB idle logic), supervised with exponential backoff capped at 120 s plus jitter (same ideas as `meshsrv/tcp_link_supervisor.py`, not the code). Viewers read from a condition-variable fan-out with a bounded, drop-oldest buffer.

Decisions (all taken by Flint after this review): **D1** the source store is instance-scoped (`data/camera_sources.json`, secrets in `data/camera_secrets.json`). **D2** at most 4 viewers per source and 6 in total, below the 8 gunicorn threads so the UI stays responsive. **D3** in a container the Docker host gateway (the NAS itself) AND the container's whole own network(s) (the bridge subnet) are refused; `MESHCENTER_CAMERA_ALLOW_OWN_SUBNET=1` exists for host networking, where the LAN is the container's own network (the gateway stays refused). **D4** the transport result of section 7: fMP4 remux over MediaSource as the primary path, MJPEG transcode as a fallback that is off on low-power hosts.

## 9. Security plan

### 9.1 Credential storage

- `data/camera_secrets.json`, mode **0600**, directory not world-readable; JSON keyed by source uuid; written with `safe_write_json` (its `mkstemp` temp file is created 0600 and `os.replace` keeps that mode; a test will assert the mode, and an explicit `os.chmod(0o600)` is added anyway). Docker: file lives on the `/data` volume with the container user's ownership.
- **Not encrypted at rest.** MeshCenter already keeps its Flask `secret_key.txt` and `auth.json` beside the data; a key stored next to the ciphertext is obfuscation, not encryption, and this plan will not call it encryption. The UI and docs say so plainly: "stored in a protected file readable by the MeshCenter user and by root".
- Secrets are excluded from every diagnostic, export and log path (none of the existing diagnostics bundles the camera files today; a test pins that the secrets file name never appears in any route response).

### 9.2 Write-only secret fields

API and UI never return a password, token or full URL with userinfo. Source objects expose `password_set: true|false`, `username`, host, ports, paths. Empty password field on edit means "keep"; a separate explicit "clear password" action exists. Responses and error messages pass through one scrubber.

### 9.3 ffmpeg argv exposure (resolved in Phase 1a)

`ffmpeg` receives the input URL as an argument, so `rtsp://user:pass@host/...` is world-readable in `/proc/<pid>/cmdline` on a default Linux. Candidates, to be tried first in Phase 1 against a fake RTSP server (no camera needed):

1. Feed the URL via stdin using the concat demuxer (`-f concat -safe 0 -protocol_whitelist pipe,fd,rtsp,rtp,tcp,udp -i pipe:0`, script `file '<url>'` then `option rtsp_transport tcp`, `option timeout ...`, `option allowed_media_types video`). **This is what shipped.** The whitelist was narrowed after review: no `file`, `http` or `crypto`, so even a directive that somehow reached the script cannot open a local file or an http URL. Injection is also blocked earlier: control characters (CR, LF, NUL, tab, DEL) in host, user, password or path are rejected outright, the URL parts are percent-encoded, and the script builder refuses any URL that is not plain printable ASCII without quote or backslash. **Resolved in Phase 1a: works.** The Phase 0 runs that recorded 'no data' were wrong: they omitted the `option rtsp_transport tcp` line, so the inner demuxer used UDP. With the option line placed after the `file` line, the stream opens against a real camera with ffmpeg 9.0 (Windows) and 7.1.5 (.104) and the ffmpeg process's argv contains neither password nor host (checked on both; PR #376).
2. ffmpeg's `-/option file` form: tested, `-/i` is not recognised. Rejected.
3. A tiny local credential-injecting forwarder is rejected as too much new code and a new attack surface.
4. The argv fallback was not needed: option 1 works, so "passwords never visible in `ps`" holds (verified on a real camera with ffmpeg 9.0 on Windows and 7.1.5 on .104).

Also: ffmpeg usually masks the password in its own log lines, but the driver will not rely on it; every stderr line passes the scrubber before it is stored or printed (`usb_driver._drain_stderr` only buffers today, `_handle_process_exit` prints the tail).

### 9.4 Address policy (SSRF)

As specified in Phase 1 item 6 (and D3: in a container also the whole container network and its gateway), with these implementation notes: parse with `ipaddress`, normalise IPv4-mapped IPv6, accept only RFC1918, `fc00::/7`, `fe80::/10` (with zone id handling) and the `169.254.0.0/16` link-local range **except** `169.254.169.254`; reject loopback, unspecified, multicast, the MeshCenter host's own interface addresses (`psutil.net_if_addrs()`, already a dependency), and everything public. For hostnames: resolve, require **every** returned address to pass, then **connect to the validated address, not the name** (otherwise DNS rebinding turns the check into a time-of-check/time-of-use gap); re-validate on every reconnect, not only at save time. Only the `rtsp` scheme is accepted from the user; the user supplies host/port/paths as separate fields, never a free URL. Reolink HTTP calls (Phase 2) use the same validated address and do not follow redirects. Error text for an unreachable or refused host is generic and uniform so the test-connection button cannot be used as a port-scan oracle; the test endpoint is rate limited.

### 9.5 Other

New routes inherit CSRF. Rate limit connect-test and source-write routes. Tokens (Phase 2) are never logged. No credentials in URLs for HTTP calls in normal operation (the Reolink `Snap` command accepts a token parameter, so the token, not the password, goes on the wire query).

## 10. Storage plan (snapshots, recordings)

- 8 Mbps main stream = 1 MB/s = **3.6 GB/hour** (matches the spec). The sub stream bitrate is unknown until measured (estimate: a few hundred MB per hour).
- Existing limits: `data/screenshots/YYYY/MM/DD/`, `cleanup_old_screenshots(max_mb=500, keep_days=30)` hard-coded in `camera/camera.py` and `api/api_camera.py`, shared by every source and the user's own photos (P9). I did not find any other configurable media limit in the camera code; if "F4" refers to something beyond this, it needs pointing out.
- **Proposal: snapshots are in v1, continuous recording is later.** A 4K Reolink snapshot is a full-resolution JPEG (estimate 1-2 MB), so 500 MB holds only 250-500 of them and could push out the user's own photos. Phase 3 should add either a per-source subfolder with its own cap, or a larger configurable limit, plus a disk-free guard; naming `MC_IP_<source-slug>_<timestamp>.jpg`, metadata (source, time, profile, resolution) in EXIF `ImageDescription` via Pillow or a sidecar, to be decided in Phase 3.
- Recording, when it comes, is its own design: segmented `-c copy` output, quota, retention, free-space guard, SD-card wear on the Pi, NAS volume sizing. Out of scope for CAM2 v1 unless Flint says otherwise.

## 11. Codec detection plan

- **Implemented differently from the first plan (no ffprobe, no second session):** the single streaming ffmpeg already produces the fMP4 init segment, and `camera/fmp4.py` reads the codec (`avc1` vs `hvc1`), the resolution and the MSE codec string (`avcC`) from it, and the frame rate and bitrate from the fragment headers and arrival times. This saves a whole extra RTSP session and a probe's worth of cold-start time on a Pi Zero. The first plan (one short `ffprobe -show_streams` before the stream) is kept here only as the rejected alternative.
- A pure-Python RTSP DESCRIBE/SDP check (a=rtpmap `H264` vs `H265`) is a possible cheap pre-check but needs Digest auth and SPS parsing for resolution; not planned unless ffprobe proves too slow on the Pi Zero.
- If the codec is `hevc`: state `unsupported_codec`, no stream is started, and the UI shows: "This stream uses H.265, which browsers usually cannot play. Switch the stream to H.264 in the camera settings or use the sub stream." The product never changes a camera's codec automatically. (Safari can play some HEVC; the message says "usually".)
- The codec string for MSE (`avc1.PPCCLL`) comes from the init segment's `avcC`.

## 12. Docker plan

- **Camera in container mode for network sources only.** Add a capability flag (e.g. `network_camera: true`, independent of `hardware_io`) in `meshsrv/runtime_env.py`. CSI/USB stay off (`init_camera()` already refuses; USB discovery finds no `/dev/video*`). The Devices tab message for an empty USB list must not say "requires ffmpeg" in a container (P8).
- **ffmpeg in the image (done in Phase 4, v1.13.8): a minimal LGPL ffmpeg 7.1.5 built in a builder stage, 15.9 MiB, see the dependency review.** The original reasoning follows. **Not with Debian's package.** Measured: Debian's `ffmpeg` adds 205 packages and about 625 MB (193 MB -> 818 MB on `python:3.11-slim`), over the 150 MB budget, so PR #377 does not put it in the image and the UI says "ffmpeg is not available in this Docker image yet". Phase 4: a minimal LGPL ffmpeg built in a builder stage, target <= 60 MB (details in the dependency review). The first plan below is kept for the record: `apt-get install --no-install-recommends ffmpeg` (provides ffprobe). Debian's ffmpeg pulls a large shared-library set; **image size delta is unmeasured** (estimate: a few hundred MB, to be measured with a local `docker build` on a PC). A smaller static or reduced build is an option to evaluate only if the delta is unacceptable; it changes the licence analysis (section in the dependency review).
- **Licence impact (as built in Phase 4: the LGPL ffmpeg, label `MIT AND GPL-3.0-only AND LGPL-2.1-or-later`; the following sentence is the original estimate for Debian's GPL build):** the image would redistribute a GPL-2.0-or-later ffmpeg (Debian build) in addition to the GPLv3 adapter. The label `org.opencontainers.image.licenses` becomes `MIT AND GPL-3.0-only AND GPL-2.0-or-later`. D0 section 5 is still open; the image stays **unpublished** (build locally), as today.
- **SIGTERM / orphans:** `tini` is PID 1 and forwards to gunicorn; ffmpeg children are spawned in their own session with `PR_SET_PDEATHSIG=SIGKILL` through the shared spawner thread. When the whole container stops the PID namespace ends, so orphans cannot survive. The realistic leak is **in-container restart** (`runtime_env.request_self_restart()` SIGTERMs the worker, master respawns it) and driver switches; test: start a stream, restart via the UI, `docker top meshcenter` must show no stale ffmpeg. Also kill the worker with SIGKILL and confirm PDEATHSIG reaps ffmpeg.
- **Bridge networking:** the container reaches a LAN camera through NAT; RTSP over TCP (interleaved) works, UDP RTP needs ephemeral port mapping and generally does not work under bridge, so UDP is shown with a warning in Docker and TCP stays the default. Host names: Docker's resolver may not resolve LAN or `.local` names, so the UI recommends an IP address. No `network_mode: host`, no privileged mode, no devices needed.
- Compose already drops all capabilities and sets `no-new-privileges`; no change expected.

## 13. Risk to existing cameras and test plan

Risk (CSI/USB): low if the changes stay additive. The risky edits are `set_active()` (P5), routing CSI-global settings by source type (P4), and any change to `build_camera_manager()`; each is covered by existing tests (`tests/test_usb_camera_driver.py`, `tests/test_api_camera.py`, `tests/test_camera_libcamera_optional.py`, `tests/test_docker_support.py`) which must stay green unmodified, plus new ones.

Test plan outline (details per phase in the task):

- Unit: address policy (private ok, loopback/public/metadata/own-IP/multicast/IPv4-mapped/rebinding rejected), source CRUD and migration, secret masking in API/logs/process args, reconnect backoff with a fake clock, bounded fan-out with a deliberately slow client, codec detection from fixture ffprobe JSON (H.264, H.265, missing), fMP4 box splitting on fixture data.
- Process: a fake RTSP/ffmpeg fixture in the style of `tests/fixtures/fake_ffmpeg.py`; orphan check after stop, switch, SIGTERM and SIGKILL of the worker.
- Regression: CSI with and without Picamera2, USB with fake ffmpeg, no camera, container mode, existing screenshot and photo routes.
- Live (after Flint's go only): .104 with the RLC-811A, reconnect by network unplug, 30-minute run with CPU/RAM/process count and radio health, OV5647 CSI regression on .104, optional USB regression on .107.

## 14. Phase plan confirmation

The phase structure in the task holds. Proposed adjustments and rough sizes (working days of focused work including tests and review loop; estimates only):

| Phase | Change | Estimate |
|---|---|---|
| 0 | this audit, plus the measurement run once approved | 1-2 days incl. measurements |
| 1 | split into **1a** (source store, secrets, address policy, CRUD routes, driver skeleton with fake RTSP, argv-exposure spike; merged as #376) and **1b** (stream delivery per the transport decision, player, reconnect, live tests on .104; PR #377). Both inside the same phase gate, but two reviewable PRs. | 5-8 days |
| 2 | as written; focus investigation is time-boxed, anything not verified live is not shipped | 3-4 days |
| 3 | as written; recording stays out unless requested | 3-4 days |
| 4 | as written; NAS soak is wall-clock time (2 h each) | 2-3 days |

## 15. Requests to Flint before anything touches the network

All of these were answered: the measurements ran (section 7.5) with a temporary test account on the camera that Flint deletes afterwards, on .104 and the Windows PC (the NAS was not touched), and D1-D3 are decided (section 8). The browser MSE playback was checked in Chromium only; Firefox, Safari/iPhone and Android are tested by Flint against .104 before #377 merges. The camera HTTP snapshot (option c) was not measured; it needs the Phase 2 login and stays an RTSP single-frame grab until then.

## 16. Watch list and backlog (from the Phase 1 live test, 2026-10-24)

- **Phase 3 watch list - CSI "Camera frontend has timed out":** on .104 the log showed `ERROR RPI pipeline_base.cpp ... Camera frontend has timed out! Please check that your camera sensor connector is attached securely` once, about 100 s after the CSI sensor was started by an IP -> CSI source switch while several MJPEG requests overlapped. It did not reproduce on three clean flows (plain start, idle 170 s; switch IP -> CSI, idle 150 s) and a clean `main` run behaves the same (39 vs 39 JPEG frames in 25 s). Cause not established (hardware ribbon, or overlapping MJPEG readers and mode switches). Keep an eye on it when Phase 3 reworks the Camera card; no code change planned.
- **AUTH2 (done in v1.13.2) - per-session revocation:** every login gets a random session id in the cookie; a normal logout revokes only that id (`data/revoked_sessions.json`, atomic, kept in memory, an entry lives as long as the cookie can); a password change, re-enabling protection and "Sign out everywhere" (`POST /api/auth/logout_all`) bump the global `auth_version`. A camera stream or MJPEG response asks the session again every 3 s (`meshsrv/session_guard.py`), so a logout ends it instead of waiting for the 10 minute cap (kept as the last resort). Cookies made before AUTH2 stay valid and get an id on their next request. A stream that is waiting for data (a camera that stopped sending) is not interrupted from outside: its own 15 s no-data limit ends it.
- **Phase 4 re-measurements:** cold start on a healthy Wi-Fi/Ethernet link (the .104 numbers, median about 9 s, were taken on a link with 200-750 ms RTT under load), the soak on a healthy link, and the real Docker image size.
- **Browser coverage after the Phase 1 test (Flint, 2026-10-24):** Chromium and Firefox on a PC work. Safari on iPhone (the `ManagedMediaSource` path, untested before) ended in a timeout and the cause is not investigated; the Android phone could not open the Camera tab because the phone layout of the app is not adapted (known postponed mobile work). Decision: focus on the PC browsers; phone browsers are a separate item once the mobile layout is done. The 10 minute seamless reconnect and the main-stream hint were part of the tested build (33ec3cb); during his tests .104 logged only 2 upstream stalls in 90 minutes.

## Main stream quality check (4K, 2026-10-31)

Question: does anything between the camera and the picture change a 3840x2160 main stream? Answer: no.

- **Pipeline:** the live stream is `-map 0:v:0 -an -sn -dn -c:v copy -f mp4 -frag_duration 500000 -movflags empty_moov+default_base_moof` (`camera/rtsp_input.py`): no filter, no scaling, no re-encode, no frame-rate change. The camera's own bitrate, resolution and frame rate (set in the camera app, never by MeshCenter) arrive unchanged.
- **Measured:** a real 3840x2160 High@5.1 H.264 stream (8.7 Mbit/s, 15 fps, 60 frames) was served by the fake RTSP camera and read through MeshCenter's exact ffmpeg argument list. The fMP4 init segment reports `avc1.640033`, 3840x2160. Decoding the source and the output gives the same MD5 for every frame compared (46 of 46 frames the stream carried: identical).
- **Player:** in Edge, MediaSource playback of that stream reports `video.videoWidth x videoHeight` = 3840 x 2160 at device pixel ratios 1 and 2, on the desktop and the phone layout. The `<video>` element has `object-fit: contain`, `image-rendering: auto`, no transform, no filter and zoom 1; the picture box follows the stream's aspect ratio, so there are no bars. The browser scales the picture down to the box (424 x 239 CSS pixels in the Camera card with both side panels open; 350 x 197 on a 412 px phone), which is ordinary compositor scaling, not a change to the stream. To see more of the 4K detail the box has to be bigger (the card with the side panels closed, or a full-screen view).
- Picture quality of a saved gallery picture is separate: "Take picture" asks the camera for its own full or small snapshot (Reolink API) or cuts one frame from the running stream (`camera/frame_grab.py`, mjpeg at `-q:v 2`).
