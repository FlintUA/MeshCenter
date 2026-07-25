# MeshCenter Components

Version: 1.0

---

# Overview

This document describes every reusable user interface component used throughout MeshCenter.

Whenever possible, new functionality should reuse existing components instead of introducing new visual styles.

---

# Panel

Panels are the primary layout containers.

Examples:

- Telemetry
- Weather
- System
- Radio Health
- Node Profile
- Notifications
- Workspace

Rules

- identical border radius
- identical border color
- identical background
- identical padding
- identical shadow

Panels should never compete visually.

---

# Card

Cards represent logical entities.

Examples

- Node
- Weather Forecast
- Wi-Fi Network
- Chart

Cards should:

- remain compact
- display the most important information first
- avoid decorative elements

---

# Tabs

Tabs switch between workspaces.

Examples

Chats

Camera

Media

Devices

Nodes

Tools

Rules

- identical width whenever possible
- equal height
- active tab uses accent color
- inactive tabs remain neutral

---

# Node Profile

The Node Profile represents the identity of a selected node.

Contains

Name

Alias

Hardware

Node ID

Status

Actions

Telemetry

Location

Signal

Rules

Node name receives highest priority.

Never truncate information unnecessarily.

---

# Segmented Control

Segmented controls combine actions and state.

Example

┌───┬───┬───┐
│⭐ │🚫 │⋮ │
└───┴───┴───┘

Segments indicate current state.

Selected segments remain highlighted.

---

# Status Bar

Located at the bottom.

Contains

MeshCenter

Online status

CPU

RAM

Temperature

Rules

No layout shifts.

Values must remain aligned.

---

# Notification

Notifications appear above the Status Bar.

Characteristics

small

temporary

non-blocking

Notification history is available through Notification Center.

---

# Notification Center

Stores recent application activity.

Examples

Node ignored

Node restored

Weather updated

Camera connected

Settings saved

The Notification Center is informational only.

---

# Workspace

Workspace stores user interface preferences.

Examples

Panel visibility

Appearance

Compact mode

Workspace is not intended for advanced system configuration.

---

# Button Types

Primary

Secondary

Danger

Ghost

Segment Button

Every button type should be reusable.

---

# Input Controls

Text input

Search

Dropdown

Switch

Checkbox

All controls should follow identical spacing.

---

# Charts

Charts are informational.

Animations should remain minimal.

Axes should remain readable.

Charts should never distract from operational data.

---

# Lists

Examples

Chat list

Node list

Wi-Fi list

Rules

Compact

Readable

Scrollable

Consistent spacing

---

# Popovers

Examples

Workspace

Notifications

Future Help panel

Rules

Shared background

Shared shadow

Shared border

Shared typography

---

# Icons

Icons communicate meaning.

Avoid decorative icons.

Use consistent icon sizes.

---

# Empty States

Every empty panel should explain what the user can do next.

Example

"No messages yet."

Instead of

"Empty"

---

# Future Components

Future UI should reuse existing components whenever possible.

If a completely new component is introduced,

update this document first.