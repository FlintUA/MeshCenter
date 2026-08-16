# MeshCenter Architecture

**Version:** 1.0  
**Status:** Living Document

---

# Overview

MeshCenter is a lightweight desktop-oriented control center for Meshtastic nodes.

The application combines messaging, telemetry, monitoring, media, sensors and system management into a unified web interface running on a Raspberry Pi.

The project is intentionally designed around simplicity, modularity and low resource consumption.

---

# Design Goals

The architecture follows several principles.

- Modular
- Easy to understand
- Easy to extend
- Low hardware requirements
- Reliable operation
- Minimal external dependencies

---

# High-Level Architecture

```
                    Browser
                       │
              HTTP (fetch + polling)
                       │
                ┌───────────────┐
                │ Flask Backend │
                └───────────────┘
                 │      │      │
                 │      │      │
        Meshtastic   Camera   Sensors
           │            │         │
        Serial/TCP    CSI/USB   I²C/GPIO
                 │      │      │
                 └──────┴──────┘
                        │
                    Data Storage
```

---

# Main Modules

## Web Interface

Responsible for

- User interaction
- UI rendering
- Settings
- Notifications

---

## Backend

Provides

- REST API
- Data processing
- Module coordination
- Event handling

---

## Meshtastic Interface

Responsible for communication with the radio.

Functions

- receive packets

- send packets

- node discovery

- telemetry

- acknowledgements

---

## Camera

Provides

- live stream

- screenshots

- gallery

Future

- video recording

- motion detection

---

## Sensors

Collects

- voltage

- current

- power

- temperature

Future

- environmental sensors

- external modules

---

## System

Responsible for

- CPU

- RAM

- Disk

- Temperature

- Network

- Wi-Fi

---

# Data Storage

Application data is stored locally.

Examples

```
messages

nodes

telemetry

settings

logs

icons

gallery
```

The storage layer is intentionally simple and human-readable whenever possible.

---

# Frontend

Current frontend stack

- HTML

- CSS

- JavaScript

The interface is intentionally framework-independent.

This keeps the project lightweight and easy to understand.

---

# Backend

Current backend

Python

Flask

The backend is organized into independent modules whenever practical.

---

# Communication

Communication inside the application follows the following model.

```
Browser

↓

REST API

↓

Backend

↓

Module

↓

Hardware
```

Modules should avoid unnecessary direct dependencies.

---

# Future Architecture

The architecture is expected to remain modular.

New modules should integrate without requiring major restructuring.

Examples

- AI assistant

- GPS services

- Plugins

- Remote management

- OTA updates

- Multiple cameras

---

# Core Principles

Every module should

- have a single responsibility

- remain loosely coupled

- expose a clear interface

- be independently maintainable

---

# Vision

MeshCenter aims to become a complete control center for Meshtastic-based systems while remaining lightweight enough to run comfortably on a Raspberry Pi Zero 2 W.