# Camera control over the camera's own web API (CAM2 Phase 2)

Status: implemented and tested against a mock server; the real-camera behaviour is confirmed in the live
test (section 9). The RTSP preview (Phase 1) never depends on anything in this document.

## 1. What it is for

A saved network camera can optionally get a `control` section (type `reolink`). With it MeshCenter can:

* make **snapshots** with the camera's own JPEG encoder (used as the live view for browsers without
  MediaSource, for example an iPhone before iOS 17.1, and later for Media snapshots in Phase 3);
* **read** the lens zoom and focus position and the valid zoom range;
* move the lens **zoom** (device units, range checked, read back). Focus is read only.

It never changes a codec, a stream, the network or a user of the camera. The only write is zoom.

## 2. Where it lives

| Piece | File |
| --- | --- |
| HTTP(S) client, login, token, TLS pinning, abilities, zoom, snapshot | `camera/reolink_client.py` |
| Clients per source, snapshot sharing, viewer leases | `camera/reolink_control.py` |
| Routes | `api/api_camera_control.py` |
| `control` section of a saved source | `camera/source_store.py` (`default_control()`, `set_control_state()`) |
| Player (snapshot mode) | `static/camera-ip.js` |
| Source form, "Check control", certificate dialog | `static/camera-sources.js` |
| Mock camera for tests | `tests/fixtures/fake_reolink_server.py` |

Nothing in `camera/rtsp_stream.py` imports the client; a failing control API cannot touch the stream
(tests: `test_a_failing_control_api_leaves_the_rtsp_preview_running`).

## 3. Protocol used (from the publicly documented JSON API; no vendor SDK or code)

All commands are `POST /cgi-bin/api.cgi` with a JSON **array** of command objects (a single object is
refused by the camera).

| Step | Request | Answer |
| --- | --- | --- |
| Login | `?cmd=Login`, `[{"cmd":"Login","action":0,"param":{"User":{"Version":"0","userName":..,"password":..}}}]` | `value.Token.name` (the token) and `value.Token.leaseTime` (seconds) |
| Any command | `?token=<token>` | `code` 0 or `error.rspCode` / `error.detail` |
| Abilities | `GetAbility`, action 0, `param.User.userName` | `value.Ability` (entries with `permit`) |
| Zoom/focus read | `GetZoomFocus`, action 1, `param.channel` | `value.ZoomFocus.{zoom,focus}.pos` and the ranges |
| Zoom move | `StartZoomFocus`, action 0, `param.ZoomFocus.{channel,pos,op:"ZoomPos"}` | accepted; the lens takes a moment |
| Snapshot | `GET ?cmd=Snap&channel=N&rs=<random>&snapType=sub&token=<token>` (`snapType=sub` for the small live picture) | `image/jpeg` (a refusal is a small JSON answer) |
| Logout | `Logout` | best effort |

Error classification: `rspCode -6` or "login first" = token expired (re-login ONCE, then fail);
`rspCode -26` ("ability error") or a permission word = the account lacks the right (`permission_denied`,
with `right` = `snapshot` / `lens_control` / `live_view`); a failed `Login` = `login_failed`
(`too_many_sessions` when the camera says so).

Zoom positions are **device units** (the camera reports the range, for example 0 to 32), never percent
or an x-factor; a position outside the reported range is refused before anything is sent.

## 4. Credentials and tokens

* The control API uses the source's own user name and the password in `data/camera_secrets.json` (no
  second secret store). The password is sent once, in the body of the login request.
* The token goes in the query string (the camera requires it). It is never logged, never in an
  exception message and never returned to the browser; errors are machine codes plus generic texts.
  The snapshot route returns the JPEG only.
* A client is rebuilt when the source is edited (it is keyed on `updated_at`); the stored `pinned_sha256`
  and the probe result (`capabilities`) are written only by the server, never from user input, and are
  cleared when host, port, scheme, channel or type change (they belong to one device at one address).

## 5. TLS

* Verification is on by default. Verification is never switched off globally.
* A camera with a self-signed certificate is reached only with a per-source SHA-256 **fingerprint pin**
  the user confirmed: "Check control" shows the fingerprint, the user confirms, the server re-reads the
  certificate and stores the pin only when it equals what the user saw. The handshake is compared with
  the pin BEFORE any request byte (so before the password) is sent. A changed fingerprint fails closed and
  shows the new one for a fresh decision.
* Plain HTTP only with `allow_http` set on that source (the form shows a warning).
* The address policy applies on every connection and the connection goes to the validated IP; redirects
  are never followed.

## 6. Limits

* One command in flight per camera (a lock); zoom needs 0.5 s between commands (`rate_limited`); the
  probe and trust routes allow 6 requests a minute, zoom reads 60.
* A zoom move is read back (up to 6 s) and the answer says whether the lens arrived (`settled`).

## 7. Snapshot fallback (browsers without MediaSource)

* `GET /api/camera/snapshot` (login cookie, `Cache-Control: no-store`) for the active network camera.
* At most one picture per second per camera is fetched and shared by all fallback viewers (1 s cache,
  one fetch at a time); a failure is remembered for 3 s so polling viewers do not queue behind connect
  timeouts. Nothing is fetched while nobody asks; a hidden tab pauses its polling.
* Viewers are leases in the SAME limiter as stream viewers (4 per camera, 6 in total, 429
  `viewer_limit`); a client that stays silent for 15 s frees its slot, and a lease is dropped after 10
  minutes (the viewer is counted and authorised again), like a stream's lifetime.
* No ffmpeg and no transcode: the camera makes the JPEG. A generic RTSP camera without this API keeps
  the "browser cannot play the stream" message.

## 8. Permissions

A view-only account may not be allowed lens control. The probe reports each missing right
(`denied: [{command, right}]`), the zoom route answers 403 `permission_denied` with the `right`, and the
UI says "This account has no permission for lens control".

## 9. Confirmed on the real camera (RLC-811A, Step A, 2026-10-24, reads only)

Camera: Reolink RLC-811A, HTTPS on port 443 with a self-signed certificate (pinned through the fingerprint
confirm flow), channel 0, a full-function account. Nothing was written to the camera in Step A.

| Question | Result |
| --- | --- |
| Login (JSON array body, user name + password) | Works over HTTPS; about 0.5 s; `leaseTime` 3600 s; token re-used for later calls; logout works |
| `GetAbility` | 196 entries, each with a `permit` number (0 = not available, 1, 4, 6, 7 seen). Relevant ones: `snap` 6, `live` 4, `ptzCtrl` 7, `supportZoom` 6, `supportFocus` 6, `supportZoomAndFocusSliderCfg` 6, `disableAutoFocus` 6, `supportDigitalZoom` 0, `ptzType` 0, `supportPt` 6, `supportPtzSpeed` 6 |
| `GetZoomFocus` (action 1) | Returns the position and the ranges. Zoom range 0 to 28, focus range 0 to 238. Baseline before any lens move: **zoom 22, focus 222** |
| Snapshot, default `Snap` | JPEG 3840x2160, about 454 KB, 0.75 to 0.8 s per picture (Wi-Fi limited) |
| Snapshot, `snapType=sub` | JPEG **640x360, about 18 KB, about 0.5 s** (the width/height form `&width=640&height=360` gives the same picture, slower and less steady). The live-view fallback uses `snapType=sub`; full pictures stay available for Phase 3 Media |
| Token in query only for Snap | Yes: `Snap` works with the token alone, no user/password in the URL |

Notes: a 4K picture is 25 times larger than the sub picture, which matters on Wi-Fi and for an iPhone; this
is why the fallback asks for the sub picture. The abilities list is long: the client keeps only the names and
`permit` values and decides capabilities from what the real reads deliver (snapshot, zoom with range,
focus position), not from ability names alone.

### Snapshot mode on a phone and the cost on .104 (Step A, same day)

* iPhone SE, Safari 15.8.8 (no MediaSource): the Camera tab shows the picture refreshing about once a second with the
  "Snapshot mode" label, no drops over several minutes (Flint's observation).
* Server side: one new camera connection per picture. Before the cache fix the camera was asked only every 2.1 s
  (median gap) because the age of the cached picture counted from the END of its fetch (0.5 to 1 s), so the next
  poll one second later hit the cache and re-sent the same picture. With the age counted from the START of the
  fetch the median gap is 1.11 s (min 0.99 s). Commit `54268c5`.
* ffmpeg: 0 processes in snapshot mode (the RTSP upstream is not started).
* Python CPU for one picture (TLS handshake, request, 18 KB answer): about 16 ms measured on .104 (20 pictures,
  a new connection each time), so about 1% of one core at one picture per second.
* gunicorn CPU, 30 s windows on .104 (Pi Zero 2W class, 4 cores): 51.7% of one core with the phone in snapshot
  mode, 69.4% and 57.4% with no camera viewer at all (phone on Messages, a PC browser tab open). The process
  carries that load without the camera (many worker threads at 5 to 10% each), so the snapshot mode is not
  measurable against it; the high baseline is a separate observation about this node (10 minutes after a
  restart, two browsers open), not caused by this feature. Host: 7.5 to 21.5% of all 4 cores, 50 to 54 C, no
  throttling, no `[LISTENER EXITS]` in the journal.

### View-only account (Step A, same day)

With the account reduced to view-only rights: login works, `GetAbility`, `GetZoomFocus` (position 22 / 222, ranges
0..28 and 0..238) and the snapshot all work (`probe` reports snapshot, zoom read and focus read, nothing denied,
because the probe never writes). One `StartZoomFocus` to the CURRENT position (22, so the lens cannot move even
if the right existed) was refused by the camera with `rspCode -26`; the client reports `permission_denied`,
`right` = `lens_control`, command `StartZoomFocus`. Position after the attempt: zoom 22, focus 222, unchanged.
This confirms the real error code (the code `-26` previously came only from third-party documentation).

## 10. Still to confirm

* How long the lens takes to settle after a move, and whether `ZoomPos` reads back exactly (Step B, needs an
  account with lens rights).
* Focus writes: not implemented; only the position and range are read.
