# Theme registry — Stage 6.3: MeshCenter Alpine, live verification on dev

Verification stage for Alpine (Stages 6.1 `2fc2580` + 6.2 `706356e`), run against the real running app on dev (`192.168.2.104`), driven through the actual Workspace popover UI (Playwright, never localStorage editing). Everything checks out clean — **no code changes this stage**.

## Method

Same as Stages 4.3/5.3: an independent headless-Chromium Playwright script (`.theme_stage0_scratch/stage6_3_verify.py`, throwaway, not committed), not the Browser pane. The script drives the real Workspace popover radios (`page.check(...)`, dispatching real `change` events).

## Acceptance checklist (source doc section 13, as applicable to a fixed/dark family)

**No visually different colors without a registered exception.** Spot-checked node cards, the About view, Settings, and the Workspace popover across Original/Dark, Sharp, Gunmetal, and Alpine. Everything Alpine doesn't override falls back cleanly to the base dark values.

**No new raw HEX.** No code changes this stage at all.

**Focus visible on panel/card/control backgrounds.** This one needed real scrutiny — Alpine's 5.32:1 contrast target is noticeably tighter than Gunmetal's 8.61:1. Confirmed correct via genuine keyboard `Tab`: `outlineColor: rgb(186, 203, 217)` (`#bacbd9`, exactly Alpine's `--mc-border-focus`), `matches(':focus-visible') === true`. **Investigation note**: mid-session, an extended live-DOM-mutation debugging pass (many repeated inline-style toggles on the same popover elements, chasing down what first looked like a real bug) left the browser's computed-style cache for those specific elements returning a stale `currentColor` for `outline-color` regardless of what was actually declared — confirmed as a pure testing artifact, not a real issue, by reloading the page fresh and re-testing once, cleanly, which reproduced the correct `#bacbd9` immediately. Documented here as a caution for future stages: if a `getComputedStyle` result looks impossible (a value that no candidate CSS rule declares, that no amount of `!important`/inline override can change, and that a freshly-created synthetic element with the same class does *not* reproduce), suspect a stale computed-style artifact from heavy live mutation before concluding it's an app bug — reload and retest once, cleanly, before reporting.

**The warning exception (6.2) is distinguishable in practice.** Confirmed thoroughly given the fg/fill split Stage 6.2 navigated:

| Consumer | Rendered color | Expected |
|---|---|---|
| `.text-warning`/`.status-warning` | `#ff9a3d` | foreground ✓ |
| `.settings-card-note--warning` (dark mode) | `#ff9a3d` | foreground ✓ |
| dark-mode `.notification-warning` icon | `#ff9a3d` | foreground ✓ |
| `.node-detail-activity.activity-away` | `#ff9a3d` | the deliberate fill compromise ✓ |
| `.node-card.favorite` background | `#3a281a` | surface ✓ |

The `.activity-away` indicator bar correctly renders `#ff9a3d` (foreground) rather than the source doc's literal fill value (`#a84300`, which Stage 6.2 determined was unusable for `--mc-warning` since it fails AA text contrast) — sitting next to `.activity-online`/`.activity-offline`/`.activity-unknown` in the node detail pane, it reads as a distinct, clearly-orange indicator, not broken or mismatched with its siblings, just a lighter/brighter orange than the source doc's literal fill color. The known, deliberate compromise from 6.2 holds up fine visually.

**`.node-card.favorite`'s background** (`#3a281a`) confirms the Stage 5.3 fix (the `!important` dark-mode sweep restore, added for Gunmetal) generalizes correctly to a third family without any Alpine-specific change needed.

**Success/danger/info distinguishable, base dark values.** Confirmed via test elements: success `#72d69a` (green), danger `#ff7b85` (pink-red) — Alpine doesn't override either, both correctly inherit `html[data-theme="dark"]`. Clearly distinguishable by hue and lightness.

**Hover doesn't shift layout or add a stray shadow.** Same pre-existing, unrelated findings as Sharp/Gunmetal (`.node-card:hover { transform: translateX(3px); }` and `.node-card.favorite:hover`'s golden shadow, both hardcoded in `style-part1.css`, unrelated to any `--mc-*` token or family) — re-confirmed present a third time, still out of scope, not an Alpine regression.

**Light ↔ Dark ↔ Sharp ↔ Gunmetal ↔ Alpine switches live, no reload.** A `window.__stage63Marker` set once at initial load survived every switch performed during the run.

**Telemetry chart re-themes on every dark-family-only combination, checked explicitly rather than assumed.** Same modal-blocking constraint as Stages 4.3/5.3 (re-confirmed via `elementFromPoint`), so switches were invoked via `Workspace.update(...)` — the exact function the radio's own `change` handler calls. All three dark-side family-only transitions correctly fired the refresh:

| Switch | `data-theme` | refresh fired |
|---|---|---|
| Original/Dark → Alpine | `dark` (unchanged) | yes |
| Alpine → Gunmetal | `dark` (unchanged) | yes |
| Gunmetal → Alpine | `dark` (unchanged) | yes |

Confirms the Stage 4.3 fix generalizes to a third dark-fixed family and to switching directly *between* two dark-fixed families (Gunmetal ↔ Alpine), not just from/to Original.

**Old theme preference migration still works.** Simulated a pre-Stage-3.2 `{theme: 'dark'}` localStorage shape and called `Workspace.load()`: correctly produced `{themeFamily: 'original', themeMode: 'dark', ...}`, unaffected by Alpine's addition.

## Known gaps re-confirmed, still out of scope

- **Swatch-preview gap:** confirmed again — while Alpine is active, the "MeshCenter Original" card's swatches show Alpine's colors instead of Original's own. Sharp's, Gunmetal's, and Alpine's own cards are each correctly unaffected by whichever other family is active — Alpine's own swatch correctly shows the Stage 6.2 update (`#ff9a3d` in the warning slot). Same single-card scope as before, nothing worse.
- **`#997255` (favorite/warm-accent role, flagged in 6.2):** confirmed nothing looks broken in its absence.
- **`hex_registry.json` line-number drift:** documentation-only, unrelated to this stage, not touched.
- **Panels/Display i18n debt:** unrelated, not touched.

## Screenshots

Captured (throwaway, `.theme_stage0_scratch/stage6_3/`, not committed) for Original/Dark, Sharp, Gunmetal, and Alpine: main workspace view, node cards (including favorite ones, showing the warning-surface distinction), the About view, and the Workspace popover with the family picker open. Available on request.

## Deploy scope

Dev only (`192.168.2.104`), per the project's standing rule. Never left `main` this stage since no fix was needed.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
