# Theme registry — Stage 4.3: MeshCenter Sharp, live verification on dev

Verification stage for Sharp (Stages 4.1 `eb2bca4` + 4.2 `6734d99`), run against the real running app on dev (`192.168.2.104`), driven through the actual Workspace popover UI (Playwright, never localStorage editing directly). One real bug was found and fixed (see below) — everything else checks out clean.

## Method

Screenshots were captured with an independent headless-Chromium Playwright script (`.theme_stage0_scratch/stage4_3_verify.py`, throwaway, not committed), not the Browser pane, per this project's standing rule about the pane's known PNG-compositing problem. The script drives the real Workspace popover radios (`page.check(...)`, dispatching real `change` events — the same code path a real click fires) rather than writing to `localStorage`.

## Acceptance checklist (source doc section 13, as applicable to a fixed/light family)

**No visually different colors without a registered exception.** Spot-checked node cards, the About view, Settings, and the Workspace popover itself across Original/Light, Original/Dark, and Sharp. Everything Sharp doesn't override (warning/info state colors, `--mc-border-active`, popover chrome, tab colors, etc.) correctly falls back to the base light values — no half-themed components, no components stuck showing dark-mode colors under Sharp. One small, pre-existing, out-of-scope residual noted: the checked/selected family-card's border in the popover stays Original's blue `--mc-border-active` (`#2f73d9`) under Sharp, since that token isn't part of Sharp's 21-role palette — cosmetically minor (the mint `--mc-primary-soft` background dominates visually) and correctly out of scope per Stage 4.1's own documented boundary.

**No new raw HEX.** No CSS/token changes this stage — `check_new_hex.py static/ui-kit.css` stays clean (984 known values, same as after Stage 4.2). The one code change (see Bug found, below) touches `chat.js` only, no hex involved.

**Focus visible on panel/card/control backgrounds.** Verified via genuine keyboard `Tab` presses (not scripted `.focus()` — Chromium's `:focus-visible` heuristic does not reliably engage for script-triggered focus, which produced a false alarm during this verification before being caught and corrected). Tabbing to the Sharp family radio and to the Compact Mode toggle both show `outline: ... solid` with `outlineColor: rgb(8, 122, 87)` (`#087a57`, Sharp's `--mc-border-focus`), confirmed via `matches(':focus-visible') === true`. Legible against both the white popover background and Sharp's card surfaces. One unrelated, pre-existing gap noted in passing: `.workspace-popover-close` (the popover's × button) has no `:focus-visible` rule at all in any theme, so it always shows the browser's default blue outline regardless of family — not a Sharp regression, not touched here.

**Success/danger distinguishable, not by hue alone.** Confirmed both the token values and real consumers: `--mc-success` → `#087a57` (dark green), `--mc-danger` → `#b42332` (dark red) — verified live via `.node-detail-activity.activity-online/-offline`, `.text-success/.text-danger`, and `.btn-danger` (text/border `#b42332`, background `#ffe9e9`), all matching Stage 4.2's mapping exactly. Green and red differ in both hue and relative luminance, not an isoluminant pair. One pre-existing, out-of-scope finding: `.notification-error`'s icon color (`style-part3.css:3783`) is raw `#ff9299`, not a `--mc-danger` consumer at all (documented in that file's own Stage 1.7 comment) — the live notification queue sampled during this check happened to contain only `.notification-error` items, so it couldn't be used as success/danger evidence; the dedicated test elements above cover that gap directly.

**Hover doesn't shift layout or add a stray shadow.** Investigated a real, measured 3px horizontal shift and a golden box-shadow appearing on hover of the first node card. Traced to source: `.node-card:hover { transform: translateX(3px); ... }` (`style-part1.css:984`) and `.node-card.favorite:hover { box-shadow: 0 2px 10px rgba(200,180,60,.2); ... }` (`style-part1.css:997`) — both pre-existing, deliberate, hardcoded-hex hover treatments entirely unrelated to `--mc-*` tokens or theme family. Confirmed via source, not just observation. Not a Sharp regression.

**Light ↔ Sharp ↔ Dark switches live, no reload.** A `window.__stage43Marker` set once at initial page load survived every family/mode switch performed during the run (`"alive"` at the end) — proves no navigation/reload occurred at any point.

**Telemetry chart re-themes on a fixed-family switch.** This is where the one real bug was found — see below.

**Old theme preference migration still works.** Simulated a pre-Stage-3.2 `{theme: 'dark'}` localStorage shape and called `Workspace.load()`: correctly produced `{themeFamily: 'original', themeMode: 'dark', ...}`, unaffected by Sharp's addition to `WORKSPACE_THEME_FAMILIES`.

## Bug found and fixed

`Workspace.applyTheme()`'s change-detection only compared the resolved light/dark mode string:

```js
const themeChanged = document.documentElement.dataset.theme !== resolvedTheme;
```

Switching from Original/Light to Sharp resolves `"light"` both before and after (Sharp is `kind: 'fixed', mode: 'light'`), so `themeChanged` was `false` and `_refreshTelemetryChartOnThemeChange()` never fired — even though the actual `--mc-*` colors did change. Confirmed empirically by spying on the function while switching:

| Switch | `data-theme` before → after | refresh calls fired |
|---|---|---|
| Original/Light → Sharp | `light` → `light` | **0** (bug) |
| Sharp → Original/Dark | `light` → `dark` | 1 (correct — mode string did flip) |

**Fix** (`static/chat.js`, `Workspace.applyTheme()`): also compare `themeFamily`:

```js
const themeChanged = document.documentElement.dataset.theme !== resolvedTheme
    || document.documentElement.dataset.themeFamily !== this.state.themeFamily;
```

Re-verified live after deploying the fix to dev: the same Original/Light → Sharp switch now fires the refresh (1 call, confirmed).

**Reachability note:** as of today this bug was not actually triggerable through normal clicking. The telemetry modal is a full-screen overlay that intercepts pointer events everywhere else on the page, including the Workspace popover button — confirmed via `elementFromPoint` at the button's own screen coordinates, which resolved to the modal itself, not the button. So a user cannot open the Workspace popover (and therefore cannot switch family) while a chart is visibly open, in any family, not just Sharp — this is correct, standard modal-trap behavior, not a bug. Verifying the actual fix therefore required calling `Workspace.update({ themeFamily: 'sharp' })` directly — the exact same function the radio's own `change` handler calls (`chat.js:8472`), not a bypass of it, just without needing the (currently unreachable) click. Fixed anyway because it's a real, small, and cheap-to-fix logic gap that Stage 5+ could make newly reachable (e.g. two future fixed families resolving to the same mode, or any change that relaxes the modal's click-trapping).

## Known gaps re-confirmed, still out of scope

- **Swatch-preview gap (flagged in Stage 4.1's PR):** confirmed exactly as scoped, and now precisely: while Sharp is active, **all 4** of the "MeshCenter Original" card's swatch slots (background, panel, primary, warning) read the currently-active family's colors instead of Original's own — not just the background slot. Sharp's own card correctly shows its own hardcoded colors regardless of which family is active. So the gap is "the Original card is wrong while any other family is active," confirmed to be exactly that one card, nothing worse.
- **Panels/Display i18n debt (Stage 1.8 backlog):** unrelated, not touched.

## Screenshots

Captured (throwaway, `.theme_stage0_scratch/stage4_3/`, not committed) for Original/Light, Original/Dark, and Sharp: main workspace view, node cards panel, the About view (dialog/popover stand-in), and the Workspace popover with the family picker open. All visually consistent with the findings above — available on request, not embedded in this doc to avoid bloating the repo with binary screenshots (no prior theme-registry stage has done so either).

## Deploy scope

Dev only (`192.168.2.104`), per the project's standing rule. Restored to `main` after verification.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
