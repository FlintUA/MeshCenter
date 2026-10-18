# MeshCenter on Docker

> **Status:** built and smoke-tested with Docker Desktop (linux/amd64) against
> an unreachable and a fake TCP radio; **not yet run on the Synology NAS**.
> There is **no public image**; you build it yourself from this repository.

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

### Disabled in container mode

These need the host and are refused by the server (and hidden in the UI):
USB/serial radio, Bluetooth, camera (CSI and USB), e-Paper display, I2C / RTC /
BME280 setup, hardware-config editing, Wi-Fi/network management, device
reboot/shutdown, in-app self-update (**update by rebuilding the image**).

Host-looking numbers: uptime, load and RAM on the System page are the **host's**
(a container shares them); hostname, OS, model and CPU temperature are hidden
because they would describe the container; disk shows the data volume. The
"top processes" list is hidden (it could only see the container).

## One Meshtastic TCP client at a time

A Meshtastic node accepts **one** TCP client. While MeshCenter is connected,
the Meshtastic phone app or a second MeshCenter cannot use that node's TCP
port (and vice versa) - **one TCP client per radio**. To hand the radio to
another client, press **Release radio** (Node Manager, TCP card; choose 15
minutes, 1 hour or "until I reconnect"): MeshCenter closes its session at once
and does **not** connect again - not on a timer, not after a restart of the
container - until the chosen time has passed or you press **Reconnect**. While
released, sending and Node Tools answer "radio released" immediately, and the
header shows "Radio released (until 12:30)". `docker compose stop` still works
as an alternative. Inside one MeshCenter instance there is never more than one
connection: a reconnect always closes the old session first.

## Layout on the NAS

```
/volume2/docker/meshcenter/
├── compose.yaml      (from deploy/docker/compose.yaml)
├── .env              (from deploy/docker/.env.example)
├── src/              (checkout of this repository - the build context)
├── config/           (config.py, weather_secrets.py - created on first start)
└── data/             (everything MeshCenter stores - see below)
```

No `logs/` folder: logs go to stdout (`docker logs`). The two small log files
MeshCenter keeps (`system_events.jsonl`, per-profile `nodes_debug.log`) live
inside `data/`.

## Install on Synology (Container Manager)

DSM itself listens on port 5000 (and 5001), so **publish MeshCenter on another
host port** - the examples use `5080:5000` (host 5080 → container 5000). The
`ports:` line of `compose.yaml` is the only place to change.

Docker commands on DSM need root: run `sudo -i` once after logging in over SSH
(or prefix every command with `sudo`). All commands below assume a root shell.

There are two ways to get the image onto the NAS:

* **Build on the NAS** from a checkout (steps below), or
* **Build elsewhere and copy it** - no source tree and no `build:` section on the
  NAS (see "Install from a saved image").

1. In File Station create `/volume2/docker/meshcenter/` with `config/` and
   `data/` inside.
2. Find the owner IDs of the user that should own the files (SSH: `id <user>`;
   a typical DSM user is `1026:100`). Make the two folders owned by that user:
   `sudo chown -R 1026:100 /volume2/docker/meshcenter/config /volume2/docker/meshcenter/data`.
   The container never changes ownership itself; if it cannot write it stops
   with a clear message.
3. Put the repository in `src/` (`git clone https://github.com/FlintUA/MeshCenter src`),
   and copy `src/deploy/docker/compose.yaml` and `src/deploy/docker/.env.example`
   (as `.env`) next to it. In `compose.yaml` set `build.context: ./src`.
4. Edit `.env`: `PUID`/`PGID` from step 2, `TZ`, `MESHCENTER_VERSION`
   (e.g. the output of `git -C src describe --tags`).
5. Build and start (SSH, in `/volume2/docker/meshcenter`):
   ```bash
   sudo docker compose build
   sudo docker compose up -d
   ```
   (Container Manager → Project → Create, pointing at that folder, does the same.)
6. Open `http://<nas-ip>:5080/` (or whichever host port you published).

### Install from a saved image (`docker save` / `docker load`)

On a machine that has the source (any OS with Docker, `--platform linux/amd64`
for a DS920+):

```bash
deploy/docker/build.sh              # builds meshcenter:<git describe>, e.g. meshcenter:1.10.0-27-gabc1234
docker save meshcenter:1.10.0-27-gabc1234 | gzip > meshcenter.tar.gz   # use the tag build.sh printed
```

Copy the file to the NAS (File Station or `scp`), then as root on the NAS:

```bash
gunzip -c meshcenter.tar.gz | docker load
```

Use a `compose.yaml` **without** a `build:` section and with
`image: meshcenter:<the tag you loaded>`, then `docker compose up -d`.
`config/` and `data/` are never touched by an image change. To update later:
load the new image, change the `image:` tag, `docker compose up -d`.

### First start: set the password now

Password protection is on, and the first visit goes to the **/setup** page.
**Complete it immediately** — until a password exists, anyone who can reach
port 5000 can claim the installation. Keep port 5000 inside your LAN.

### Connect the radio (TCP)

The radio's address is stored in MeshCenter's own profile data, not in an
environment variable. In the web UI open **Node Manager**, choose to detect a
radio **by TCP**, enter the node's IP and port (default 4403) and accept it.
The service restarts itself (the container keeps running). Enable Wi-Fi/TCP on
the node first and **give it a DHCP reservation (or a fixed IP) in your router**:
MeshCenter reconnects to the stored address, so a radio that comes back under a
different IP is simply never found.

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

### If the radio goes away

MeshCenter keeps trying **forever** while the active radio is TCP: after a drop
(power cut, Wi-Fi gone, radio reboot, NAS or router restart) it waits about
25 s for the radio to release its connection, then retries after 30 s, 60 s, 90 s
and then every 2 minutes, re-checks that it is the same radio and reconnects on
its own - no container restart needed. A session only counts as recovered once
it has stayed up for 20 s. The header chip and Node Manager show "Reconnecting…
(attempt N, next in X s)"; **Reconnect** (Node Manager) tries immediately and
restarts the schedule. A radio that answers as a *different* node stays refused
(see Node Manager) until you accept it.

### Radio behaviour you may see ("Link resets")

A Meshtastic node serves **one** TCP client, and the T-Beam (ESP32, firmware
2.7.15) is known to close that session **by itself**, at irregular intervals
(roughly 1-8 minutes while idle, measured with a packet capture - the radio
sends the FIN; MeshCenter never resets a healthy connection). Right after such a
close it may close new sessions about a second after the config download for a
minute or two, and ignore new connections for up to ~2 minutes. MeshCenter
answers every close within a second, waits, and reconnects without piling on;
Node Manager shows how many "Link resets" happened in the last hour. They are
the radio's doing, not a MeshCenter fault. Messages that arrive while no session
exists may be missed (the radio does not hold them for a TCP client), and a send
attempted in that gap fails with "not connected".

If Node Manager says the radio *"may be in use by another client"*, something
else is connecting to the same radio (the Meshtastic phone app, a second
MeshCenter, a script): the radio drops the older client whenever a new one
connects, so the two keep evicting each other. This is detected from the radio
closing our session repeatedly - however long each session lasted (3 closes in 5
minutes, 4 in 10 minutes, or 4 in an hour at regular intervals). A single reboot,
a Wi-Fi blip or a long power-off does not trigger it. Press **Release radio** on
the instance that should let go (the other MeshCenter, or close the phone app);
meanwhile MeshCenter backs off to one attempt per 5 minutes (10 minutes after
half an hour). The notice goes away after a session has stayed up for 15 minutes.
MeshCenter never releases the radio by itself.

If the radio is unreachable at start MeshCenter still starts and shows it as
disconnected. Startup takes about 40 seconds in that case (measured) before the
web page answers — the container health check allows for it.

## Time

A container cannot verify that the clock is NTP-synchronized. `.env.example`
sets `MESHCENTER_TRUST_HOST_CLOCK=1` (DSM keeps the NAS clock synchronized),
which lets the schedule engine and the radio time sync run. Set it to `0` if
your host clock is not trustworthy. `TZ` sets the time zone.

## Where data lives and persistence

Everything is under `data/` (messages, nodes, profiles, waypoints, attachments,
settings, the login hash and the session key) and survives
`docker compose down && up` and image rebuilds. `config/config.py` is created
once from a template and **never overwritten**; edit it for advanced settings.
Weather API keys entered in the UI are saved in `config/weather_secrets.py`.

`data/` contains secrets (`secret_key.txt`, `auth.json`, MCAttach keys): do not
share it as a public folder.

## Day to day

```bash
# free the radio's TCP slot: use "Release radio" in Node Manager (no container stop needed);
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
reboot.)

"Restart MeshCenter" in the System page restarts the service inside the
container; so does accepting a different radio profile.

## Health vs. radio status

`GET /healthz` (no login, always `{"status":"ok"}`) means the web app is up. It
says nothing about the radio. Radio and connection state are on the
authenticated pages/API (`/api/radio_health`, Settings → Meshtastic Radio).

## Update, roll back, back up

*Update:* `git -C src pull`, set `MESHCENTER_VERSION` in `.env` to the output of
`git -C src describe --tags --always` (a real version label, never a name like
`dk2` - that is not a release number and used to raise a false "update available"),
then `sudo docker compose build && sudo docker compose up -d`.
*Roll back:* images are tagged `meshcenter:<MESHCENTER_VERSION>`; set the
previous version in `.env` and `up -d` again (restore `data/` from a backup if a
newer version migrated it).
*Back up:* stop the container (`docker compose stop`), copy `data/` and
`config/`, start again. A copy of a running instance can be inconsistent
(SQLite files).

## Troubleshooting

| Symptom | Check |
|---|---|
| Exits at once, "not writable" | `data/` and `config/` ownership vs `PUID`/`PGID` |
| Unhealthy for ~2 minutes after start | normal if the radio is unreachable; see Connect the radio |
| Radio never connects | node's TCP enabled? IP/port right? another client holding the single slot (phone app, second MeshCenter)? |
| "Reconnecting…" for a long time | radio powered/on Wi-Fi? still at the same IP (DHCP reservation)? it retries every 5 minutes at most |
| MCAttach "control channel index None" | set `MCA_CONTROL_CHANNEL_INDEX` (see above) |
| Schedules never fire | `MESHCENTER_TRUST_HOST_CLOCK` / host time |
| Wrong times | `TZ` in `.env` |
| "not available in a container" | a host-only feature, see the disabled list |

## Licensing note

The image contains the GPLv3 `meshtastic` package in a separate environment
(see `THIRD_PARTY_NOTICES.md`). Build it locally for your own use; **do not
publish or redistribute a prebuilt image** until the image-redistribution
documentation gap (`docs/architecture/docker-blockers.md`, section 5) is closed.
