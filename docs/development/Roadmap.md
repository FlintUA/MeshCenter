# MeshCenter Roadmap

---

# Vision

MeshCenter is evolving from a simple web chat into a complete control center for Meshtastic nodes.

The roadmap reflects long-term direction rather than fixed deadlines.

---

# Phase 1

## Foundation

Completed

- Web chat

- Node management

- Message history

- Camera

- Media gallery

- Weather

- Sensors

- System monitoring

- Unified desktop UI

---

# Phase 2

## User Experience

In Progress

- Documentation — this pass (H2-A): README/docs accuracy audit, a generated API route reference (`docs/API_ROUTES.md`), and CI checks that keep docs from drifting from the code again

- Design System

- Better settings

- Improved notifications

- Better accessibility

- Performance optimization — see [Phase 3](#phase-3) for the telemetry-specific work already done

Also current:

- Finishing the four-language interface: the i18n *infrastructure* is complete and live (language switching, the MCAttach/Files workspace fully translated in all four languages) — what remains is writing the actual German/Russian/Ukrainian translations (currently English placeholder text) for the rest of the interface, and wiring the JavaScript-rendered chat interface into the translation system at all

---

# Phase 3

## Advanced Monitoring

Completed

- CPU history

- Sensor history

- Bounded, debounced telemetry storage (10,000 local / 1,000 per remote node / 40,000 total records, flushed to disk at most once every 60 seconds instead of on every reading — see `docs/architecture/Architecture.md#data-storage`)

Planned

- Better historical charts and long-term statistics

- Additional sensor support and improved data export

- Power analytics

- Radio analytics

- Health dashboard

---

# Phase 4

## Node Management

Completed

- Favorites

Planned

- Node groups

- Tags

- Bulk operations

- Remote configuration

---

# Phase 5

## Automation

Completed

- Scheduled actions

- Notifications

Ideas

- Rules

- Event triggers

- Sensor alerts

---

# Phase 6

## Camera

Completed

- Multiple cameras

Ideas

- Motion detection

- Recording

- Time-lapse

---

# Phase 7

## Plugins

Long-term

Plugin API

Third-party modules

Custom widgets

External integrations — candidates under consideration: Telegram notifications, MQTT integration (brokers and/or a bridge), Grafana/InfluxDB exporters, an APRS gateway, Home Assistant, Node-RED

Also under consideration for the Network Map specifically: signal-quality overlays, routing/traceroute visualization, favorite-node emphasis, last-heard indicators

---

# Phase 8

## AI

Long-term

AI assistant

Message summaries

Telemetry analysis

Problem detection

Configuration suggestions

---

# Phase 9

## Mobile

Possible future

Responsive interface

Progressive Web App

Native applications

---

# Phase 10

## Community

Future

Translations

Contributors

Themes

Plugin ecosystem

---

# Guiding Principles

The roadmap is intentionally flexible.

Features may change as the project evolves.

Quality has priority over release speed.

Every new feature should follow the project's UI Guidelines and Architecture.