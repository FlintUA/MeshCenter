# MeshCenter on Docker

> **Status:** accepted on a Synology DS920+ (DSM 7.3, Container Manager)
> against a real T-Beam over TCP: one session of about 7 hours with **zero link
> resets**, automatic recovery after the radio was powered off and after another
> client took the radio's only slot. There is **no public image**; you build it
> yourself from this repository (see the licensing note at the end).

## What is supported

| | |
|---|---|
| Platform | `linux/amd64` only (tested target: Synology DS920+, DSM 7.3, Container Manager) |
| Radio link | **Meshtastic over TCP (Wi-Fi/Ethernet) only** |
| Radios | exactly one |
| Network | bridge, container port 5000 (publish it on any free host port, e.g. `5080:5000` on DSM) |

One image, one container, two processes inside: MeshCenter Core (gunicorn,
always one worker) and the Meshtastic adapter. The adapter has its own Python
environment, exactly like a native install.

Looking for a server **without** Docker? A PC or server running Debian/Ubuntu is
covered by the normal [Manual Installation](../INSTALL.md#manual-installation)
(USB radio, all features). Docker is only needed if you want the container.

### What the radio must offer

The container reaches the radio over the network, so the radio needs:

* **Wi-Fi or Ethernet.** In practice this means ESP32-based boards (T-Beam,
  Heltec V3, RAK WisMesh TAP / RAK3312, ...). nRF52 boards such as the RAK4631
  or T-Echo have no Wi-Fi unless an Ethernet module is fitted.
* **The TCP API on port 4403.** Meshtastic serves it once Wi-Fi/Ethernet is
  enabled in the node's Network settings (check the settings of your firmware
  version).
* **A fixed address** - a DHCP reservation in your router (or a static IP).
  MeshCenter reconnects to the stored address; a radio that comes back under a
  different IP is simply never found.

Configure the radio (region, channels, Wi-Fi) with the official Meshtastic app
first; MeshCenter uses what is already on the node and never changes it. On
ESP32 radios **enabling Wi-Fi switches Bluetooth off** (Meshtastic firmware
behaviour, see the [Network configuration](https://meshtastic.org/docs/configuration/radio/network/)
page), so configure everything you need over Bluetooth or USB *before* you turn
Wi-Fi on. Afterwards the phone app can reach the node only over the same single
TCP slot MeshCenter uses (see "One Meshtastic TCP client at a time").

### Disabled in container mode

These need the host and are refused by the server (and hidden in the UI):
USB/serial radio, Bluetooth, local camera (CSI and USB), e-Paper display, I2C / RTC /
BME280 setup, hardware-config editing, Wi-Fi/network management, device
reboot/shutdown, in-app self-update (**update by rebuilding or loading a new image**).

**Live video of network cameras is not available in this image yet.** It needs
`ffmpeg`, which is not included (a camera with Reolink API control still shows
snapshots, about one picture per second, and "Take picture" works; the Devices
card for local CSI/USB cameras says they are not available in Docker): Debian's own `ffmpeg` package would add
205 packages and about 625 MB (measured: 193 MB -> 818 MB on `python:3.11-slim`),
so the Network cameras card says "ffmpeg is not available in this Docker image"
instead. A minimal ffmpeg built in the image (RTSP/RTP input, H.264/H.265
parsers, MP4 and MJPEG output, LGPL, target 60 MB or less) is planned for the
next camera phase. Once it is there: use TCP (UDP rarely works through Docker's
bridge network) and an IP address, and keep the camera on your LAN (addresses of
the container's own network and of its gateway are refused; with
`network_mode: host` set `MESHCENTER_CAMERA_ALLOW_OWN_SUBNET=1`). Never forward a
camera's ports to the Internet.

Host-looking numbers: uptime, load and RAM on the System page are the **host's**
(a container shares them); hostname, OS, model and CPU temperature are hidden
because they would describe the container; disk shows the data volume. The
"top processes" list is hidden (it could only see the container).

## Step by step for first-time Docker users

This walks through the path that was actually used on the NAS: **build the image
on a PC, copy it to the NAS, run it with a compose file**. The reference sections
below the walkthrough explain each piece in more detail, and describe the other
variant (building on the NAS itself).

### The words you will meet

* **Image** - a packaged, read-only copy of MeshCenter with everything it needs
  (like an installer). You build it once and can copy it as a single file.
* **Container** - a running instance of an image. Delete and re-create it as often
  as you like; your data does not live inside it.
* **Compose file** (`compose.yaml`) - a small text file that says which image to run,
  which port to publish and which folders to share. **Container Manager** (Synology)
  calls a compose-based setup a **Project**.
* **Mounted folders** - two folders on the NAS (`data` and `config`) that the
  container reads and writes. Messages, settings and your password live there, so
  they survive updates and re-creating the container.

### 1. Check the prerequisites

* A NAS or server with an **x86-64 CPU** (Intel/AMD). The image is `linux/amd64`
  only. Many budget NAS models use **ARM** processors - those are not supported
  (no ARM image exists yet). On Synology, the model's spec sheet lists the CPU.
* **Docker**: Container Manager on DSM 7.2+ (called Docker on older DSM), or Docker
  Engine with the `docker compose` plugin on Linux.
* A Meshtastic radio with **Wi-Fi/Ethernet and the TCP API**, on a **fixed IP**
  (see "What the radio must offer"), reachable from the NAS (same LAN).
* On the PC where you build: **Docker Desktop** (Windows/macOS) or Docker Engine
  (Linux), and `git`.

### 2. Build the image on your PC

The base image is pinned to `python:3.11-slim-trixie` (Debian 13, the system the running image already uses). It is changed only on purpose, with a release note, never by a moving tag.

Build from a **release tag**, not from whatever `main` happens to be:

```bash
git clone https://github.com/FlintUA/MeshCenter
cd MeshCenter
git checkout v1.13.0
```

(`git checkout` of a tag prints a "detached HEAD" notice - that is fine.)

On Linux/macOS or Git Bash the helper script picks the version from the tag:

```bash
deploy/docker/build.sh              # builds meshcenter:1.13.0
```

On Windows without a POSIX shell (PowerShell / cmd) run the command it wraps:

```bash
docker build --platform linux/amd64 --build-arg MESHCENTER_VERSION=1.13.0 -t meshcenter:1.13.0 .
```

The version number you pass is what MeshCenter shows in its footer, so use a real
version (`1.13.0`), never a name like `test`. From v1.13.0 on the image also carries
OCI labels (`org.opencontainers.image.version`, `.revision` = the exact commit,
`.source`, `.title`, `.licenses`), readable with
`docker image inspect meshcenter:1.13.0 --format '{{json .Config.Labels}}'`;
`build.sh` fills the revision itself. With the plain `docker build` command add
`--build-arg MESHCENTER_REVISION=$(git rev-parse HEAD)` (optional; it defaults to
`unknown`). Images built from older versions have no labels: the tag and the footer
in the web UI are the source of truth.

### 3. Save the image and copy it to the NAS

```bash
docker save meshcenter:1.13.0 -o meshcenter-1.13.0.tar
```

Use `-o` (not `> file`): in PowerShell, `>` re-encodes the stream and corrupts the
archive. The result is a plain `.tar` of about 80-90 MB.

Copy it to the NAS with File Station (upload) or `scp`. Then import it, either:

* **by SSH** (this is what the NAS acceptance used), as root:
  ```bash
  sudo docker load -i /volume2/docker/meshcenter-1.13.0.tar
  ```
* **or in Container Manager**: *Image → Add → Add from file* and pick the `.tar`.
  Use the plain `.tar` that `docker save` wrote; it has not been verified which
  other extensions the GUI accepts, so do not gzip it for the GUI.

Either way the image `meshcenter:1.13.0` should now be listed under *Image*.

### 4. Create the folders and give the container the right owner

1. In File Station create `/volume2/docker/meshcenter/` (adjust the volume number
   to yours) with two sub-folders: `config` and `data`.
2. The container runs as a numeric user/group that must **own those two folders**,
   otherwise it stops at once with "not writable". Find your user's IDs over SSH
   (DSM: *Control Panel → Terminal & SNMP → Enable SSH service*, then `ssh <user>@<nas-ip>`):
   ```bash
   id <user>                       # e.g. uid=1026 gid=100
   sudo chown -R 1026:100 /volume2/docker/meshcenter/config /volume2/docker/meshcenter/data
   ```
   `1026:100` is the typical first DSM user and the default used below. If yours
   differ, use your numbers in the `chown` command **and** in the `user:` line of
   the compose file - that is all that is needed; the image does not have to be
   rebuilt for a different ID (see "Other owner IDs").

### 5. Write the compose file

Save this as `/volume2/docker/meshcenter/compose.yaml`. It is complete: it uses the
loaded image (no `build:` section) and has every value inline, so no `.env` file
is needed. Change `TZ` and, if needed, the `user:` line:

```yaml
name: meshcenter

services:
  meshcenter:
    container_name: meshcenter
    image: meshcenter:1.13.0
    user: "1026:100"                 # the owner of ./config and ./data
    restart: unless-stopped
    ports:
      - "5080:5000"                  # host:container; DSM itself uses 5000
    environment:
      TZ: Europe/Berlin              # your IANA time zone
      MESHCENTER_TRUST_HOST_CLOCK: "1"   # DSM keeps the NAS clock synchronized
      MESHCENTER_VERSION: "1.13.0"
    volumes:
      - ./config:/config
      - ./data:/data
    hostname: meshcenter
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    stop_grace_period: 30s
    healthcheck:
      test: ["CMD", "python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 150s
```

(`MESHCENTER_VERSION` is also baked into the image; keeping it in the file makes
the version visible next to the `image:` line you will change on every update.)

### 6. Create the project and start it

In **Container Manager → Project → Create**: give it a name (`meshcenter`), set
the path to `/volume2/docker/meshcenter`, choose to use the existing
`compose.yaml`, and start it. Over SSH the same is:

```bash
cd /volume2/docker/meshcenter
sudo docker compose up -d
sudo docker compose ps          # wait for (healthy); about 2 minutes at most
```

Open `http://<nas-ip>:5080/`.

### 7. First visit, radio, channel

1. **Set the password now.** The first visit goes to `/setup`. Do it immediately and
   keep the published port inside your LAN.
2. **Connect the radio:** *Node Manager → detect a radio by TCP*, enter the node's
   IP and port (4403), accept it. The service restarts itself (the container keeps
   running) and the header shows the radio once connected.
3. **MCAttach (only if you use file transfer):** edit `config/config.py` and set
   `MCA_CONTROL_CHANNEL_INDEX` to the index (0-7) of your *private* channel, then
   restart the container. See "MCAttach: set your private channel index".

### 8. Update and back up (plain steps)

* **Update:** on the PC `git fetch --tags && git checkout v<new>`, build and save
  the new image (steps 2-3), load it on the NAS, change the `image:` and
  `MESHCENTER_VERSION` lines in `compose.yaml`, then
  `sudo docker compose up -d`. `config/` and `data/` are untouched. The old image
  stays on the NAS: to **roll back**, put its tag back and `up -d` again.
* **Back up:** `sudo docker compose stop`, copy `config/` and `data/`, then
  `sudo docker compose start`. A copy of a running instance can be inconsistent
  (SQLite files).

### Other NAS brands and plain Linux

The same principle applies to **QNAP Container Station, Unraid, TrueNAS SCALE** and
a plain **Debian/Ubuntu server with `docker compose`**: get an x86-64 image onto the
machine (`docker load`), create the two folders owned by the user the container will
run as, run the compose file above with the paths adjusted, publish a free host
port. Menu names differ; these platforms were **not tested**. Only the Synology
DS920+ was.

## Reference

### Layout on the NAS

Two variants - the folder layout is the same except for the build files:

```
/volume2/docker/meshcenter/
├── compose.yaml      (both variants)
├── config/           (config.py, weather_secrets.py - created on first start)
├── data/             (everything MeshCenter stores - see below)
├── .env              (only when building on the NAS, from deploy/docker/.env.example)
└── src/              (only when building on the NAS: a checkout of this repository)
```

* **Loaded image** (the walkthrough above): `compose.yaml`, `config/`, `data/`.
  No `src/`, no `.env`: `TZ`, `MESHCENTER_TRUST_HOST_CLOCK`, `MESHCENTER_VERSION`
  and the owner (`user:`) are written directly into the compose file; there is
  no build step.
* **Build on the NAS:** additionally `src/` (the build context) and `.env`
  (`PUID`, `PGID`, `TZ`, `MESHCENTER_VERSION`, ...), with the `${...}`-style
  `compose.yaml` from `deploy/docker/compose.yaml`.

No `logs/` folder: logs go to stdout (`docker logs`). The two small log files
MeshCenter keeps (`system_events.jsonl`, per-profile `nodes_debug.log`) live
inside `data/`.

### Install on Synology: build on the NAS

DSM itself listens on port 5000 (and 5001), so **publish MeshCenter on another
host port** - the examples use `5080:5000` (host 5080 → container 5000). The
`ports:` line of `compose.yaml` is the only place to change.

Docker commands on DSM need root: run `sudo -i` once after logging in over SSH
(or prefix every command with `sudo`). All commands below assume a root shell.

1. In File Station create `/volume2/docker/meshcenter/` with `config/` and
   `data/` inside.
2. Find the owner IDs of the user that should own the files (SSH: `id <user>`;
   a typical DSM user is `1026:100`). Make the two folders owned by that user:
   `sudo chown -R 1026:100 /volume2/docker/meshcenter/config /volume2/docker/meshcenter/data`.
   The container never changes ownership itself; if it cannot write it stops
   with a clear message.
3. Put the repository in `src/` (`git clone https://github.com/FlintUA/MeshCenter src`,
   then `git -C src checkout v1.13.0`), and copy `src/deploy/docker/compose.yaml`
   and `src/deploy/docker/.env.example` (as `.env`) next to it. In `compose.yaml`
   set `build.context: ./src`.
4. Edit `.env`: `PUID`/`PGID` from step 2, `TZ`, `MESHCENTER_VERSION`
   (e.g. `1.13.0`, the output of `git -C src describe --tags` without the `v`).
5. Build and start (SSH, in `/volume2/docker/meshcenter`):
   ```bash
   sudo docker compose build
   sudo docker compose up -d
   ```
   (Container Manager → Project → Create, pointing at that folder, does the same.)
6. Open `http://<nas-ip>:5080/` (or whichever host port you published).

### Other owner IDs

The image is built with a default user `1026:100`, but `compose.yaml` runs the
container as `user: "<PUID>:<PGID>"`, which overrides it at start. By reading the
Dockerfile and entrypoint, a different runtime `user:` is enough: the entrypoint
only checks that `/data` and `/config` are writable by that user and never
`chown`s, the home directory is `/data/.home`, and nothing in MeshCenter looks the
user up by name. Rebuilding with `--build-arg PUID=... --build-arg PGID=...` only
changes the default and is optional. (Only `1026:100` was run on the NAS; other IDs
follow from the code, not from a test.)

### First start: set the password now

Password protection is on, and the first visit goes to the **/setup** page.
**Complete it immediately** - until a password exists, anyone who can reach the
published port (e.g. 5080) can claim the installation. Keep that port inside your
LAN.

### Connect the radio (TCP)

The radio's address is stored in MeshCenter's own profile data, not in an
environment variable. In the web UI open **Node Manager**, choose to detect a
radio **by TCP**, enter the node's IP and port (default 4403) and accept it.
The service restarts itself (the container keeps running). Enable Wi-Fi/TCP on
the node first and give it a DHCP reservation (or a fixed IP) in your router -
see "What the radio must offer".

Before you accept a radio, MeshCenter shows an empty placeholder profile
`00000000` (the "no radio yet" ID in `config.py`). Once a real radio is accepted
the untouched placeholder node and profile are hidden/removed (a placeholder
that holds messages is never deleted).

### MCAttach: set your private channel index

MCAttach (file transfer) must never send its control traffic on the public
primary channel, so MeshCenter has **no default** for it. Open
`config/config.py` and set `MCA_CONTROL_CHANNEL_INDEX` to the index (0-7) of
your *private* channel - the position shown in the Meshtastic app's channel
list (channel 0 is the primary, `1` is the first secondary, ...):

```python
MCA_CONTROL_CHANNEL_INDEX = 1   # e.g. your private secondary channel
```

Restart the container afterwards. While it is `None`, MCAttach is fail-closed:
every transfer is refused with "MCA control channel index None is invalid", and
a WARNING is logged at every start.

## One Meshtastic TCP client at a time

A Meshtastic node accepts **one** TCP client. While MeshCenter is connected,
the Meshtastic phone app or a second MeshCenter cannot use that node's TCP
port (and vice versa) - **one TCP client per radio**. The node drops the older
client whenever a new one connects, so two clients on one radio keep evicting
each other.

**To hand the radio to another client, press Release radio** (Node Manager, TCP
card; choose 15 minutes, 1 hour or "until I reconnect"): MeshCenter closes its
session at once and does **not** connect again - not on a timer, not after a
restart of the container - until the chosen time has passed or you press
**Reconnect**. The release is stored on disk, so it **survives a container
restart and a NAS reboot: a NAS rebooted while released stays released until you
press Reconnect** (or the chosen time has run out). While released, sending and
Node Tools answer "radio released" immediately, and the header shows "Radio
released (until 12:30)". `docker compose stop` still works as an alternative
(stop the container, use the radio elsewhere, start it again). Inside one
MeshCenter instance there is never more than one connection: a reconnect always
closes the old session first.

Practical consequences:

* **Phone app:** disconnect in the app when you are done. An app that stays
  connected may reconnect on its own - for example while MeshCenter is released
  - and then holds the radio's only slot against MeshCenter after you press
  Reconnect.
* **Another MeshCenter** (for example a native install with an auto-reconnecting
  TCP profile for the same radio) fights the container: each reconnect evicts the
  other. Switch that profile to another radio or stop that MeshCenter. On older
  versions the "auto-reconnect" checkbox alone does not stop it.
* **Contention notice:** if the radio keeps closing MeshCenter's sessions - however
  long each lasted, including another client that takes the radio at regular
  intervals - Node Manager says "the radio may be in use by another client" and
  MeshCenter retries more slowly (see "Radio behaviour you may see"). It never
  releases the radio by itself; use Release radio on the instance that should let go.
* **ESP32 radios:** with Wi-Fi on, Bluetooth is off (see "What the radio must
  offer"), so the phone app can use the node only through the same single TCP slot.
  If in doubt, check your firmware's network settings.

## If the radio goes away

MeshCenter keeps trying **forever** while the active radio is TCP: after a drop
(power cut, Wi-Fi gone, radio reboot, NAS or router restart) it waits about
25 s for the radio to release its connection, then retries after 30 s, 60 s, 90 s
and then every 2 minutes (each delay varies by about ±10 %), re-checks that it is
the same radio and reconnects on its own - no container restart needed. A session
only counts as recovered once it has stayed up for 20 s. The header chip and Node
Manager show "Reconnecting… (attempt N, next in X s)"; **Reconnect** (Node Manager)
tries immediately and restarts the schedule. A radio that answers as a *different*
node stays refused (see Node Manager) until you accept it.

## Radio behaviour you may see ("Link resets")

Occasional link resets are normal: a radio reboot or a Wi-Fi blip ends the
session. Node Manager counts them ("Link resets (last hour)"); MeshCenter never
resets a healthy connection itself. Messages that arrive while no session exists
may be missed (the radio does not hold them for a TCP client), and a send
attempted in that gap fails with "not connected".

After a reconnect the T-Beam often closes the **first** new session about 5
seconds after the configuration download; the retry is stable. **One reset after a
reconnect is therefore normal** and not a sign of trouble.

**Repeated quick drops almost always mean another client is using the same
radio** (the phone app, a second MeshCenter, a script): see "One Meshtastic TCP
client at a time". MeshCenter detects this from the radio closing its session
repeatedly (3 closes in 5 minutes, 4 in 10 minutes, or 4 in an hour at regular
intervals) and shows *"the radio may be in use by another client"*. A single
reboot, a Wi-Fi blip or a long power-off does not trigger it. While the notice is
set MeshCenter backs off to one attempt per 5 minutes (10 minutes after half an
hour); it goes away after a session has stayed up for 15 minutes.

If the radio is unreachable at start MeshCenter still starts and shows it as
disconnected. Startup takes about 40 seconds in that case (measured; the
container health check allows up to 150 s) before the web page answers.

## Delivery confirmations (direct messages)

Direct messages over TCP show **delivered** or **not delivered**. "Delivered" is
shown only when the confirmation comes from the destination node itself; a relay
passing the message on is not a delivery. A rejection such as `NO_CHANNEL` means
the destination cannot decrypt the message (the two nodes have not yet exchanged
their public keys); it is not a MeshCenter fault.

## Time

A container cannot verify that the clock is NTP-synchronized.
`MESHCENTER_TRUST_HOST_CLOCK=1` (set in the compose file / `.env.example`; DSM
keeps the NAS clock synchronized) lets the schedule engine and the radio time sync
run. Set it to `0` if your host clock is not trustworthy. `TZ` sets the time zone.

## Where data lives and persistence

Everything is under `data/` (messages, nodes, profiles, waypoints, attachments,
settings, the login hash and the session key) and survives
`docker compose down && up` and image changes. `config/config.py` is created
once from a template and **never overwritten**; edit it for advanced settings.
Weather API keys entered in the UI are saved in `config/weather_secrets.py`.

`data/` contains secrets (`secret_key.txt`, `auth.json`, MCAttach keys, and
`camera_secrets.json` with the passwords of saved network cameras): do not
share it as a public folder, and treat every backup or copy of `data/` as
secret too. `camera_secrets.json` is a plain, owner-only (0600) file, not
encrypted.

## Day to day

```bash
# hand the radio to another client: use "Release radio" in Node Manager (no container stop needed);
docker compose stop                 # ... or stop the whole container
docker compose start
docker compose restart
docker compose logs -f --timestamps meshcenter
docker compose ps                   # shows (healthy)
```

**Shutting the NAS down:** you do not need to `docker compose stop` first.
The container uses `restart: unless-stopped`: it stops with Docker when DSM
shuts down and comes back with it on the next boot. (Do *not* stop it by hand
before a shutdown - an explicitly stopped container stays stopped after the
reboot.) A radio you released stays released across that reboot until you press
Reconnect.

"Restart MeshCenter" in the System page restarts the service inside the
container; so does accepting a different radio profile.

## Health vs. radio status

`GET /healthz` (no login, always `{"status":"ok"}`) means the web app is up. It
says nothing about the radio. Radio and connection state are on the
authenticated pages/API (`/api/radio_health`, Settings → Meshtastic Radio).

## Update, roll back, back up

*Update (build on the NAS):* `git -C src fetch --tags && git -C src checkout v<new>`,
set `MESHCENTER_VERSION` in `.env` to the version without the `v` (a real version
label, never a name like `dk2` - that is not a release number and used to raise a
false "update available"), then `sudo docker compose build && sudo docker compose up -d`.
*Update (loaded image):* see step 8 of the walkthrough.
*Roll back:* images are tagged `meshcenter:<version>`; put the previous tag back
(`.env` or the `image:` line) and `up -d` again (restore `data/` from a backup if a
newer version migrated it).
*Back up:* stop the container (`docker compose stop`), copy `data/` and
`config/`, start again. A copy of a running instance can be inconsistent
(SQLite files).

## Troubleshooting

| Symptom | Check |
|---|---|
| Exits at once, "not writable" | `data/` and `config/` ownership vs the `user:` line (`PUID`/`PGID`) |
| Unhealthy for up to ~2.5 minutes after start | normal if the radio is unreachable; see "If the radio goes away" |
| Radio never connects | node's Wi-Fi and TCP API on? IP/port right? another client holding the single slot (phone app, second MeshCenter)? did you leave the radio **released**? |
| "Reconnecting…" for a long time | radio powered/on Wi-Fi? still at the same IP (DHCP reservation)? it retries every 2 minutes at most (every 5 while the "another client" notice is set) |
| "The radio may be in use by another client" | close the phone app / stop the other MeshCenter, or press **Release radio** on the one that should let go |
| "Radio released" in the header | you (or a timer) released it: press **Reconnect**; a TCP release survives restarts and NAS reboots |
| One "link reset" after a reconnect | normal on a T-Beam, see "Radio behaviour you may see" |
| MCAttach "control channel index None" | set `MCA_CONTROL_CHANNEL_INDEX` (see above) |
| Schedules never fire | `MESHCENTER_TRUST_HOST_CLOCK` / host time |
| Wrong times | `TZ` in the compose file / `.env` |
| "not available in a container" | a host-only feature, see the disabled list |
| Import in Container Manager fails | use the plain `.tar` from `docker save -o`, or `docker load -i` over SSH |

## Licensing note

The image contains the GPLv3 `meshtastic` package in a separate environment
(see `THIRD_PARTY_NOTICES.md`). Build it locally for your own use; **do not
publish or redistribute a prebuilt image** until the image-redistribution
documentation gap (`docs/architecture/docker-blockers.md`, section 5) is closed.
