# MeshCenter on Docker

> **Status: first implementation, not yet run on a real Docker host.** The
> image was written and statically checked (tests, compose/Dockerfile review)
> but has not been built or started yet — that is the next stage. There is
> **no public image**; you build it yourself from this repository.

## What is supported

| | |
|---|---|
| Platform | `linux/amd64` only (tested target: Synology DS920+, DSM 7.3, Container Manager) |
| Radio link | **Meshtastic over TCP (Wi-Fi/Ethernet) only** |
| Radios | exactly one |
| Network | bridge, port 5000 |

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
port (and vice versa). Stop the container (`docker compose stop`) to free it.

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
6. Open `http://<nas-ip>:5000/` .

### First start: set the password now

Password protection is on, and the first visit goes to the **/setup** page.
**Complete it immediately** — until a password exists, anyone who can reach
port 5000 can claim the installation. Keep port 5000 inside your LAN.

### Connect the radio (TCP)

The radio's address is stored in MeshCenter's own profile data, not in an
environment variable. In the web UI open **Node Manager**, choose to detect a
radio **by TCP**, enter the node's IP and port (default 4403) and accept it.
The service restarts itself (the container keeps running). Enable Wi-Fi/TCP on
the node first and give it a fixed IP or DHCP reservation.

If the radio is unreachable MeshCenter still starts and shows it as
disconnected; it keeps retrying in the background. Startup can take up to
about 80 seconds in that case before the web page answers — the container
health check allows for it.

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
sudo docker compose stop            # free the radio's TCP slot
sudo docker compose start
sudo docker compose restart
sudo docker compose logs -f --timestamps meshcenter
sudo docker compose ps              # shows (healthy)
```

"Restart MeshCenter" in the System page restarts the service inside the
container; so does accepting a different radio profile.

## Health vs. radio status

`GET /healthz` (no login, always `{"status":"ok"}`) means the web app is up. It
says nothing about the radio. Radio and connection state are on the
authenticated pages/API (`/api/radio_health`, Settings → Meshtastic Radio).

## Update, roll back, back up

*Update:* `git -C src pull`, bump `MESHCENTER_VERSION` in `.env`, then
`sudo docker compose build && sudo docker compose up -d`.
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
| Radio never connects | node's TCP enabled? IP/port right? another client holding the single slot? |
| Schedules never fire | `MESHCENTER_TRUST_HOST_CLOCK` / host time |
| Wrong times | `TZ` in `.env` |
| "not available in a container" | a host-only feature, see the disabled list |

## Licensing note

The image contains the GPLv3 `meshtastic` package in a separate environment
(see `THIRD_PARTY_NOTICES.md`). Build it locally for your own use; **do not
publish or redistribute a prebuilt image** until the image-redistribution
documentation gap (`docs/architecture/docker-blockers.md`, section 5) is closed.
