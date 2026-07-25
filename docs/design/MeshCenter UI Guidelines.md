# MeshCenter UI Guidelines

**Version:** 1.0  
**Status:** Active Design Specification  
**Project:** MeshCenter – Meshtastic Control Center

---

# 1. Design Philosophy

MeshCenter is designed as a **desktop-first engineering application**, not as a traditional web dashboard.

The interface should feel similar to professional desktop software such as:

- Synology DSM
- Ubiquiti UniFi
- Visual Studio Code
- Figma
- Linear
- Grafana

Primary goals:

- Low visual noise
- Maximum information density
- Fast access to frequently used actions
- Predictable interaction
- Consistent appearance
- Calm and distraction-free interface

Every new feature should look as if it has always been part of MeshCenter.

---

# 2. Layout

MeshCenter uses a fixed three-column workspace.

```
┌────────────┬──────────────────────┬─────────────┐
│            │                      │             │
│ Local Node │     Work Area        │ Network     │
│            │                      │             │
└────────────┴──────────────────────┴─────────────┘
```

### Left Panel

Contains local node information.

Examples:

- Node Profile
- Telemetry
- Weather
- Charts

### Center Panel

Contains the current workspace.

Examples:

- Chats
- Camera
- Media
- Devices
- System

### Right Panel

Contains network information.

Examples:

- Node List
- Selected Node Profile

---

# 3. Visual Principles

The interface should always appear:

- balanced
- symmetrical
- calm
- readable
- consistent

Avoid:

- unnecessary animations
- oversized controls
- visual clutter
- duplicated information

---

# 4. Surface Levels

Only three surface levels should be used.

## Surface 0

Application background.

## Surface 1

Panels.

Examples:

- Workspace
- Notifications
- Node Profile
- Weather
- System

## Surface 2

Interactive elements.

Examples:

- Buttons
- Inputs
- Tabs
- Lists

---

# 5. Colors

Accent colors have semantic meaning.

## Blue

Primary action

## Green

Success

## Yellow

Warning

## Red

Danger

## Gray

Secondary information

Do not introduce random colors.

---

# 6. Typography

Hierarchy:

Panel Title

Section Title

Value

Caption

Hint

Values should always be visually stronger than labels.

---

# 7. Spacing

Use a consistent spacing scale.

```
4 px
8 px
12 px
16 px
24 px
32 px
```

Avoid arbitrary spacing values.

---

# 8. Cards

All cards should share:

- border radius
- shadow
- border style
- padding
- spacing

Cards represent logical objects.

Examples:

- Node
- Weather
- Telemetry
- Radio Health

---

# 9. Buttons

Buttons should be grouped by importance.

Primary

Secondary

Danger

Ghost

Destructive actions should always be visually distinguishable.

---

# 10. Segmented Controls

Segmented controls are preferred over multiple independent buttons whenever several actions belong to the same object.

Example:

```
┌───┬───┬───┐
│⭐ │🚫 │⋮ │
└───┴───┴───┘
```

Segmented controls can simultaneously display state and provide interaction.

---

# 11. Notifications

Notifications should never interrupt workflow.

Rules:

- appear above Status Bar
- fade in
- remain visible
- fade out
- stored in Notification Center

Notification types:

Success

Information

Warning

Error

---

# 12. Status Bar

Status Bar contains global application status.

Example:

```
MeshCenter • Online • CPU • RAM • TEMP
```

Rules:

- fixed layout
- no jumping
- consistent separators
- numeric values aligned

---

# 13. Node Profile

Node Profile is the identity of a node.

Contains:

- Name
- Alias
- Hardware
- Node ID
- Status
- Signal
- Position
- Actions

Actions should be grouped using a segmented control.

---

# 14. Workspace

Workspace provides user preferences.

Workspace is not a settings dialog.

Workspace should remain lightweight.

---

# 15. Notification Center

Notification Center stores recent application activity.

Examples:

- Node ignored
- Node restored
- Weather updated
- Network rescanned
- Icon uploaded

Notification history should help the user understand recent actions without interrupting workflow.

---

# 16. Animations

Animations should be subtle.

Preferred:

- Fade
- Slide

Avoid:

- Bounce
- Zoom
- Flashing
- Oversized transitions

---

# 17. Consistency Rule

Before adding a new component ask:

1. Does it match existing spacing?
2. Does it use existing colors?
3. Does it follow existing typography?
4. Does it duplicate existing information?
5. Can it reuse an existing component?

If the answer is "No", redesign before implementation.

---

# 18. Long-Term Vision

MeshCenter should evolve into a complete Meshtastic Control Center.

Future modules should follow the same visual language and interaction principles.

The user should always feel that every new feature naturally belongs to the application.

---

# Golden Rule

> Simplicity over decoration.
>
> Information over effects.
>
> Consistency over creativity.
>
> Every pixel should have a purpose.