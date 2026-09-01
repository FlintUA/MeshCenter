# Button class audit (Task 4 backlog)

Task 4 introduced a real shared button system (`static/ui-kit.css`: `.btn` base + `.btn-md`/`.btn-sm`/`.btn-xs` size modifiers + `.btn-icon`/`.btn-danger`) and migrated exactly three pilot locations onto it: System Log's Copy/Export, the Activity card's Copy/Export/Clear, and the Notifications popover's Copy/Export/Clear.

Everything below is a **separate, one-off button class** in `templates/index.html`, styled independently in `static/style-part*.css` with its own padding/font/radius/color, not sharing the new system. **None of these were touched by Task 4** - this file is the backlog for migrating them in a future pass, grouped by UI area. Regenerate the raw list any time with:

```bash
grep -rhoE 'class="[^"]*btn[^"]*"' templates/index.html | sort -u
```

## Camera

- `camera-defaults-btn`
- `camera-off-start-btn`
- `camera-power-btn`
- `camera-power-btn-icon`

## Chat / composer

- `chat-actions-btn`
- `clear-search-btn`
- `emoji-btn`
- `emoji-cat-btn` (+ `.active` state)
- `emoji-close-btn`
- `format-menu-btn` (+ `.cancel` variant)

## Map / Nodes

- `map-fit-btn`
- `nodes-tool-btn` (`.export` / `.import` variants)
- `rescan-nodes-btn`
- `reference-location-save-btn`

## Dock / Workspace

- `dock-panel-btn`
- `dock-workspace-btn` (+ `.dock-map-btn` variant)
- `dock-workspace-btn-icon`
- `workspace-nav-btn`
- `workspace-page-close-btn`

## Modals

- `confirm-btn` (`.confirm-cancel` / `.confirm-danger` variants)
- `modal-action-btn` (`.danger` variant, plus the standalone `schedule-save-btn`)

## System / refresh controls

- `system-action-btn` (`.danger` / `.warning` variants)
- `system-refresh-btn`
- `mc-refresh-btn` (shared by `media-refresh-btn`/`system-refresh-btn`)

## Media

- `screenshot-btn`

## Settings / Time / Devices

- `telemetry-btn` (`.environment` / `.power` variants)
- `time-btn` (+ `.active` state)
- `wifi-toggle-btn`

## Suggested next pass

A future migration should likely go area-by-area (Camera, then Chat, then Modals, ...) rather than all at once, and pair naturally with the mobile/tablet adaptation work already started here (the `.btn-icon`'s 768px touch-target bump) - several of these (camera controls, dock icons, emoji picker) are exactly the kind of icon-only controls that benefit most from a guaranteed minimum touch target.
