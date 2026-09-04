# Theme registry — Stage 7.3: MeshCenter Teal Dark, live verification on dev

Verification stage for Teal Dark (Stages 7.1 `0ffe25d` + 7.2 `d695ad2`), run against the real running app on dev (`192.168.2.104`), driven through the actual Workspace popover UI (Playwright, never localStorage editing). Everything checks out clean — **no code changes this stage**. Two investigation notes below (one a correction to Stage 7.2's consumer table, one a testing-methodology lesson), neither a real app bug.

## Method

Same as Stages 4.3/5.3/6.3: an independent headless-Chromium Playwright script (`.theme_stage0_scratch/stage7_3_verify.py`, throwaway, not committed), not the Browser pane. Extended this time to switch across all 5 families (Original/Dark, Sharp, Gunmetal, Alpine, Teal).

## Acceptance checklist

**No visually different colors without a registered exception.** Spot-checked node cards, the About view, Settings, and the Workspace popover across all 5 families. Everything Teal doesn't override falls back cleanly to the base dark values.

**No new raw HEX.** No code changes this stage.

**Focus visible on panel/card/control backgrounds.** The actual rendered outline, confirmed by direct visual inspection of a screenshot, is correctly bright cyan around the "MeshCenter Teal" card when focused via genuine keyboard `Tab` — matching `--mc-border-focus: #51F0F0` as expected. **Investigation note**: `getComputedStyle(...).outlineColor` reported three different, all-wrong values across repeated attempts (Teal's own `--mc-text-secondary`, then a stale `--mc-border-focus` value from a previously-active family, then `--mc-popover-text`, a completely unrelated Original-theme token) — reproduced across fresh page loads, fresh tabs, and a cleared `localStorage`, ruling out the "stale computed-style cache from heavy live-DOM mutation" explanation that resolved an apparently similar Alpine finding in Stage 6.3. Since the *visual* outline is confirmed correct (bright cyan, not the wrong color `getComputedStyle` reported) and the underlying `--mc-border-focus` custom property itself resolves correctly everywhere it was checked, this is treated as a `getComputedStyle()` reliability issue specific to reading the *used value* of `outline-color` when it's driven by a CSS custom property inside a `:focus-visible` sibling-combinator rule, in this Chromium/Playwright/headless environment — not a real rendering bug. Documented as a testing-methodology lesson for future stages: for this specific component, trust a screenshot over `getComputedStyle().outlineColor`.

**The teal-vs-success finding (7.2) holds up live.** Confirmed with the family actually switched to Teal, not just by reading the CSS:

| Consumer | Rendered color | Expected |
|---|---|---|
| `.node-detail-activity.activity-online` (test element) | `#72d69a` | base dark success ✓, never teal's `#10c2c2` |
| dark-mode `.notification-success` icon (test element) | `#72d69a` | base dark success ✓ |
| `--mc-primary`/`--mc-accent` (live) | `#10c2c2` | Teal's own cyan ✓, confirms it isn't leaking into success's role |

**Correction to Stage 7.2's consumer table**: the actual header connection status element (`.dock-online-status.header-status.status-ok`, the one showing "Online" text) does **not** render via the generic `--mc-success`-driven `.status-ok` rule that 7.2's report cited — a more specific selector, `.header-status.status-ok { color: #3ddc84; }` (`style-part1.css:3376`, a hardcoded raw hex, pre-existing, unrelated to any theme token), wins the cascade every time this class combination appears in the live app (confirmed: every real `.status-ok` usage in `chat.js` always co-occurs with the `.header-status` base class, so the generic `ui-kit.css` rule never actually wins in practice — it's effectively dead code too, same as the other 4 selectors 7.2 already found unused). This **doesn't change 7.2's conclusion** — if anything it strengthens it, since a hardcoded, family-independent color can never be confused with Teal's cyan regardless of which family is active. Live-confirmed: `.status-ok`'s text reads "Online" in a clearly distinct green (`#3ddc84`), never teal cyan, under Teal.

**Success/warning/danger/info render identically to Original/Dark under Teal.** Confirmed via `getComputedStyle` on the live `<html>` element while Teal was active: `--mc-success: #72d69a`, `--mc-warning: #f0c86a`, `--mc-danger: #ff7b85`, `--mc-info: #78b6ff` — all four exactly match `html[data-theme="dark"]`'s base values, confirming Teal's "no state exception at all" claim (7.1/7.2) holds at runtime, not just in the CSS source.

**Hover doesn't shift layout or add a stray shadow.** Same pre-existing, unrelated findings as Sharp/Gunmetal/Alpine (`.node-card:hover { transform: translateX(3px); }` and `.node-card.favorite:hover`'s golden shadow, both hardcoded, unrelated to any `--mc-*` token or family) — re-confirmed present a fifth time, still out of scope.

**Light ↔ Dark ↔ Sharp ↔ Gunmetal ↔ Alpine ↔ Teal switches live, no reload.** A `window.__stage73Marker` set once at initial load survived every switch performed during the run.

**Telemetry chart re-themes on every dark-family-only combination involving Teal, checked explicitly.** Same modal-blocking constraint as prior stages (re-confirmed via `elementFromPoint`), switches invoked via `Workspace.update(...)` — the exact function the radio's own `change` handler calls. All 5 combinations correctly fired the refresh:

| Switch | refresh fired |
|---|---|
| Original/Dark → Teal | yes |
| Teal → Gunmetal | yes |
| Gunmetal → Teal | yes |
| Teal → Alpine | yes |
| Alpine → Teal | yes |

Confirms the Stage 4.3 fix generalizes to a fourth dark-fixed family and to switching directly between Teal and each of the other two dark-fixed families in both directions.

**Old theme preference migration still works.** Simulated a pre-Stage-3.2 `{theme: 'dark'}` localStorage shape and called `Workspace.load()`: correctly produced `{themeFamily: 'original', themeMode: 'dark', ...}`, unaffected by Teal's addition.

**Teal-specific: label, swatch, and fixed-mode UI.** Confirmed live: the family card reads "MeshCenter Teal" (not "Teal Dark", per Stage 7.1's naming decision), its swatch shows its own correct 4 colors (`#021818`/`#043030`/`#10c2c2`/`#f0c86a`, the last one being dark's inherited warning as Stage 7.1 intended — permanently, no exception to update later), and selecting it shows a "Fixed Dark" label with no Auto/Light/Dark radiogroup — `document.getElementById('workspaceThemeModeRow').innerHTML` confirmed exactly `<div class="workspace-theme-fixed-label">Fixed Dark</div>`, same treatment as Sharp/Gunmetal/Alpine today.

## Known gaps re-confirmed, still out of scope

- **Swatch-preview gap:** confirmed again — while Teal is active, the "MeshCenter Original" card's swatches show Teal's colors instead of Original's own. Every other family's own card (Sharp, Gunmetal, Alpine, Teal) is correctly unaffected. Same single-card scope, nothing worse.
- **`.node-detail-activity` icon/label gap (flagged backlog in 7.2):** not touched, as instructed.
- **Dead `--mc-success-soft`/`.status-online`/`.status-ready`/`.status-running`/`.text-success` CSS (flagged in 7.2):** not touched. (The `.status-ok` correction above adds one more selector to this same "technically present but never wins the cascade" category — noted, not cleaned up.)
- **`hex_registry.json` line-number drift:** documentation-only, unrelated, not touched.
- **Panels/Display i18n debt:** unrelated, not touched.

## Screenshots

Captured (throwaway, `.theme_stage0_scratch/stage7_3/`, not committed) for Original/Dark, Sharp, Gunmetal, Alpine, and Teal: main workspace view, node cards, the About view, and the Workspace popover with the family picker open. Available on request.

## Deploy scope

Dev only (`192.168.2.104`), per the project's standing rule. Never left `main` this stage since no fix was needed.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
