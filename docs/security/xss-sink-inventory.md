# XSS sink inventory (H2-D)

Full inventory of every inline event-handler attribute built from a JS
template-literal interpolation (`onclick="fn('${x}')"` shape - "List A"),
and every `.innerHTML =` / `.insertAdjacentHTML(...)` assignment ("List B"),
across the six `static/*.js` files most likely to render attacker-adjacent
data (mesh node names, Wi-Fi SSIDs, waypoints, telemetry). Produced for the
H2 batch task's H2-D step (PR #333); re-verified against `main` at `23d3671`,
not the `1cab7b3` counts the original task spec was written against.

**Maintenance policy** (per PR #333's review decision): List-A sites are
converted to `data-chat-action`/`data-files-action` + a delegated listener
*opportunistically* - whenever that render code is touched for an unrelated
reason, not in a dedicated sweep. Converting a site: update this doc's row
to **CONVERTED**, delete its entry from
`scripts/inline_handler_allowlist.json`, and run
`python scripts/check_inline_handlers.py` to confirm the ratchet agrees
nothing is stale. `templates/index.html`'s ~220 static inline handlers (no
interpolated data) are out of scope here - that's F1.4 (CSP) territory, not
started.

## Summary

| File | List A total | List A converted | List B (innerHTML) | List B UNSAFE |
|---|---|---|---|---|
| `chat.js` | 48 | 2 (notifications, PR #333) | 74 | 0 |
| `chat-map.js` | 14 (+1 false-positive, no interpolation) | 0 | 3 | 0 |
| `chat-updates-security.js` | 3 | 0 | 1 | 0 |
| `files.js` | 0 (already converted, prior "C10" cleanup) | - | 46 | 0 |
| `chat-telemetry.js` | 0 (all static) | - | 11 | 0 |
| `media.js` | 1 | 0 | 4 | 0 |

**Zero exploitable UNSAFE sinks found anywhere.** F1.1 (#311) already closed
the one real gap (the `escapeHtml()`/`escapeJsString()` context-correctness
bug). What's left is architectural hygiene - see "Findings" below - plus the
fact that every remaining inline handler blocks a real CSP (F1.4) from ever
dropping `unsafe-inline`.

No `.insertAdjacentHTML(` or `innerHTML +=` exists anywhere in these 6 files
- every List-B entry is a plain `.innerHTML =` assignment.

**Numeric values are marked SAFE only where the code finite-guards them**
(`Number.isFinite(...)` before use, or reachable only inside a branch gated
by such a check).

**Origin legend**: `radio` (mesh node name/NodeInfo/position/telemetry -
attacker-controlled by any radio in range), `network` (Wi-Fi SSID/scan
result - any AP in range), `user` (typed into this installation's own UI,
same-origin), `server-id` (a server-generated identifier - UUID/hex ID/enum,
not free text), `i18n` (translation catalog string, not runtime data),
`numeric` (guarded `Number.isFinite`/clamped).

## Findings (not fixed as a targeted patch - D2's conversion resolves all three as a side effect)

1. **`JSON.stringify()` instead of the project's own helpers, in a
   *single*-quoted attribute, for `node_id` - `chat.js:7643` and
   `chat-map.js:1498`.** `JSON.stringify()` does not escape `'`, so
   `onclick='openNodeMap(..., ${JSON.stringify(node.node_id)})'` would break
   out of the single-quoted attribute if `node_id` ever contained a `'`.
   `node_id` is server-validated elsewhere to `!` + 8 hex chars
   (`server.py:1735` `is_valid_node_id()`), so unreachable today - but these
   two sites depend on that external invariant instead of being safe by
   construction. `media.js:128` has the same JSON.stringify shape for a
   server-generated screenshot filename, with an explicit code comment
   (lines 18-29) already reasoning through why it's safe there - a
   documented, deliberate exception, not an oversight.
2. **Ad-hoc `.replace(/'/g, "\\'")` instead of `escapeJsString()` -
   `chat.js:10556` (`item.profile_id`) and `chat.js:10618`
   (`radio.node_id`).** Same category as #1 - escapes only `'`, not the
   full JS-string+HTML-attribute double-escaping `escapeJsString()` does.
   Both values are `!{8hex}`-validated server-side, so low practical risk,
   but it's a third, undocumented escaping method alongside the project's
   two established helpers.
3. **`escapeHtml()` alone (not `escapeJsString()`) for a JS-string-literal
   argument inside a double-quoted attribute, in ~20 sites across
   `chat.js`/`chat-map.js`** (e.g.
   `onclick="toggleFavorite('${escapeHtml(nodeId)}')"`). Architecturally the
   same bug class F1.1 fixed in `escapeJsString()` (a value containing `'`
   HTML-decodes back to a raw `'` before the JS engine sees it, breaking out
   of the inner JS string) - except here it's the *other* helper used
   alone. Every value this happens to is a format-validated identifier
   (`node_id`, schedule `rule.id`, timer `t.id`, `camera.id` - none free
   text), so none are reachable today, but the pattern stays safe only as
   long as every future caller remembers the origin constraint, not because
   the code enforces it locally.

None of these are exploitable today - no UNSAFE row below is unescaped free
text.

## `static/chat.js` - List A (48 sites)

| # | Line (as of `23d3671`) | Status | Value(s) + origin | Escaping | Verdict |
|---|---|---|---|---|---|
| 1 | ~548 | **CONVERTED** (`data-chat-action="notif-mark-read"`) | `n.id` - notification id, server-id | was: none | was SAFE, now escaped too |
| 2 | ~554 | **CONVERTED** (`data-chat-action="notif-dismiss"`) | `n.id` | was: none | was SAFE |
| 3 | 707 | open | `rule.id` - schedule rule id, server-id | escapeHtml (JS-ctx, finding #3) | SAFE |
| 4 | 712 | open | `rule.id` | escapeHtml | SAFE |
| 5 | 713 | open | `rule.id` | escapeHtml | SAFE |
| 6 | 782 | open | `prefix` - local hardcoded string | escapeHtml (moot) | SAFE |
| 7 | 786 | open | `prefix` | escapeHtml | SAFE |
| 8 | 1161 | open | `t.id` - timer id, server-id | escapeHtml (JS-ctx) | SAFE |
| 9 | 1162 | open | `t.id` | escapeHtml | SAFE |
| 10 | 1164 | open | `t.id` | escapeHtml | SAFE |
| 11 | 1165 | open | `t.id` | escapeHtml | SAFE |
| 12 | 1173 | open | `t.id` | escapeHtml | SAFE |
| 13 | 3297 | open | `latitude`,`longitude` - radio position | numeric, `Number.isFinite`-guarded at `renderNodeMapBadge()` entry | SAFE |
| 14 | 3871 | open | `item.id` - notification id, server-id | escapeJsString | SAFE |
| 15 | 3881 | open | `item.id` | escapeJsString | SAFE |
| 16 | ~4419 | open | `clickHandler` (pre-built via `escapeJsString(chat.id)`) + conditional `title=` | escapeJsString (built upstream) | SAFE |
| 17 | 5186 | open | `directionFilter` - local enum | escapeHtml (moot) | SAFE |
| 18 | 5302 | open | `node.node_id` - radio, format-validated | escapeHtml (JS-ctx) | SAFE |
| 19 | 7391 | open | `nodeId` | escapeHtml (JS-ctx) | SAFE |
| 20 | 7399 | open | `nodeId` | escapeHtml | SAFE |
| 21 | 7412 | open | `nodeId` | escapeHtml | SAFE |
| 22 | 7429 | open | `tab.id` (local enum), `nodeId` | escapeHtml | SAFE |
| 23 | 7446 | open | `nodeId`, `displayName` - radio node name (free text) | escapeJsString (both) | SAFE |
| 24 | 7449 | open | `position.latitude/longitude` | numeric, `hasPosition`-gated | SAFE |
| 25 | 7496 | open | `nodeId`, `displayName` | escapeJsString | SAFE |
| 26 | 7497 | open | `nodeId`, `displayName` | escapeJsString | SAFE |
| 27 | 7498 | open | `nodeId`, `displayName` | escapeJsString | SAFE |
| 28 | 7499 | open | `nodeId`, `displayName` | escapeJsString | SAFE |
| 29 | 7500 | open | `nodeId` | escapeHtml (JS-ctx) | SAFE |
| 30 | 7544 | open | `node.position.lat/lon` (guarded), `node.node_id` | numeric guarded; escapeHtml (JS-ctx) | SAFE |
| 31 | 7596 | open | `node.node_id`, `node.clean_name\|\|name\|\|node_id` (free text) | escapeJsString (both) | SAFE |
| 32 | 7597 | open | `node.node_id` | escapeHtml (JS-ctx) | SAFE |
| 33 | 7643 | open | `pos.latitude/longitude` (finite-guarded), `node.node_id` | numeric guarded; **JSON.stringify** - finding #1 | SAFE today, flagged |
| 34 | 7644 | open | `pos.latitude/longitude` | numeric guarded | SAFE |
| 35 | 7645 | open | `node.node_id` | escapeHtml (JS-ctx) | SAFE |
| 36 | 7646 | open | `node.node_id`, `node.clean_name\|\|...` (free text) | escapeJsString (both) | SAFE |
| 37 | 7652 | open | same as #36, no-position branch | escapeJsString | SAFE |
| 38 | 7782 | open | `nodeId`=`escapeHtml(node.node_id)` pre-escaped locally | escapeHtml (pre-computed) | SAFE |
| 39 | 7783 | open | `nodeId` (same pre-escaped var) | escapeHtml (pre-computed) | SAFE |
| 40 | 7784 | open | `nodeId` (same) | escapeHtml (pre-computed) | SAFE |
| 41 | 8304 | open | `nodeId` | escapeHtml (JS-ctx) | SAFE |
| 42 | 10284 | open | `type` - fixed enum (`'serial'`/`'tcp'`/`'bluetooth'`) | escapeHtml (moot) | SAFE |
| 43 | 10387 | open | `escapedTransport` - one of 3 hardcoded literals | ad-hoc replace, trivially safe (fixed enum) | SAFE |
| 44 | 10390 | open | same | same | SAFE |
| 45 | 10393 | open | same | same | SAFE |
| 46 | 10556 | open | `item.profile_id` - server-id, `!{8hex}`-validated | ad-hoc replace - finding #2 | SAFE today, flagged |
| 47 | 10618 | open | `radio.node_id` - same format class | ad-hoc replace - finding #2 | SAFE today, flagged |
| 48 | 11400 | open | `camera.id` - local USB device id, server-id | escapeHtml (JS-ctx) | SAFE |

## `static/chat.js` - List B (74 sites)

Grouped by pattern (74 individual rows would dwarf this doc without adding
signal); every site is still individually accounted for.

- **No interpolation at all (trivially SAFE):** lines ~522, 1814, 2880,
  5071, 12127, 12147, 11474, 11487, 8331, 7274, 7353, 7361, 4700 - 13 sites.
- **Interpolated, every value wrapped in `escapeHtml()`** (verified directly
  for a representative sample spanning notifications, schedules, timers,
  node cards, chat items, node detail panes, radio health, Wi-Fi scan,
  node-manager dashboard, camera selector, peripheral devices, CPU history,
  instance info - all follow the identical per-field `escapeHtml()` pattern
  used throughout this file): the remaining ~61 sites. SAFE.
- **`loadTopProcesses`:** process name/cmdline is host `ps`-equivalent
  output (`system/cpu_history.py`), not network/radio-reachable - escaped
  anyway. SAFE.

No UNSAFE rows.

## `static/chat-map.js` - List A (14 real sites + 1 false positive)

| # | Line | Value(s) + origin | Escaping | Verdict |
|---|---|---|---|---|
| 1 | 194 | `waypoint.waypoint_id` - server-id | escapeJsString | SAFE |
| 2 | 195 | lat/lon, inline `Number.isFinite` coercion | numeric guarded | SAFE |
| 3 | 196 | same | numeric guarded | SAFE |
| 4 | 197 | `waypoint.waypoint_id` | escapeJsString | SAFE |
| - | 198 | **false positive** - `onclick="closeWaypointPopup()"` has no interpolation at all; excluded from the real count | n/a | N/A |
| 5 | 252 | `id`=`String(item.waypoint_id)` | escapeJsString | SAFE |
| 6 | 253 | `id` | escapeJsString | SAFE |
| 7 | 258 | `id` | escapeJsString | SAFE |
| 8 | 259 | `id` | escapeJsString | SAFE |
| 9 | 1079 | `nodeId`,`nodeName` pre-escaped via `escapeJsString()` locally | escapeJsString (pre-computed) | SAFE |
| 10 | 1080 | same | escapeJsString | SAFE |
| 11 | 1081 | same | escapeJsString | SAFE |
| 12 | 1082 | same | escapeJsString | SAFE |
| 13 | 1083 | `pos.latitude/longitude` from `getNodePosition()` (finite-guarded, `null` otherwise) | numeric guarded | SAFE |
| 14 | 1498 | `latitude`,`longitude`, `node.node_id` via **JSON.stringify** | finding #1 | SAFE today, flagged |

## `static/chat-map.js` - List B (3 sites)

All three (238, 242, 461) wrap every interpolated field in `escapeHtml()`,
or restrict to a `Number.isInteger`-clamped 0-7 channel index (461). SAFE.

## `static/chat-updates-security.js` - List A (3 sites)

All three (314, 318, 385): values are a git SHA (server/update-service-
generated), a GitHub Releases semver string, and server-generated git
command text - none radio/network-attacker-controlled, all wrapped in
`escapeHtml()`. SAFE.

## `static/chat-updates-security.js` - List B (1 site)

Line 272, `renderUpdatesResult(html)`: plain passthrough. Both actual
callers build `html` from the List-A-adjacent template literals above, all
escaped. SAFE today - no local guard against an unescaped future caller.

## `static/files.js` - List A: none

Already converted in a prior cleanup ("C10") - uses `data-files-action` +
one document-level delegated listener (`closestAttr()`/`onDocumentClick`,
`files.js:1670`/`3391-3397`). This is the exact pattern H2-D replicates for
the other files via `data-chat-action`/`CHAT_ACTIONS`/`onChatActionClick`.

## `static/files.js` - List B (46 sites)

Every site uses the file's own `esc()` helper (equivalent to `escapeHtml`)
per-field, is a static/empty-string assignment, or passes through a builder
that itself escapes. SAFE throughout.

## `static/chat-telemetry.js` - List A: none

All `onclick=`/`onchange=` attributes are static literals.

## `static/chat-telemetry.js` - List B (11 sites)

All either static markup or `escapeHtml()`-wrapped i18n/error strings. SAFE.

## `static/media.js` - List A (1 site)

Line 128: `handlerFilename` via `mediaFilenameForHandler()` -
`JSON.stringify(filename).replaceAll("'", '\\u0027')` - already strips the
`'` JSON.stringify would otherwise leave raw (unlike finding #1's two
sites), with an explicit code comment (lines 18-29) reasoning through why.
`filename` origin: server-generated from a timestamp. SAFE - the most
careful of the three JSON.stringify-based sites in this codebase.

## `static/media.js` - List B (4 sites)

All four wrap every field in `mediaEscapeHtml()` or have no interpolation.
SAFE.
