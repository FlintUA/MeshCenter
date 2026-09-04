# Theme registry — Stage 8.3: MeshCenter Teal Light / paired Teal, live verification on dev

Final sub-stage of Teal, and the **final stage of the entire seven-family plan** from `MeshCenterthemeregistryplanv0.1.md`. Verification stage for paired Teal (Stage 8.2 `b545e71`), run against the real running app on dev (`192.168.2.104`), driven through the actual Workspace popover UI (Playwright, never localStorage editing except for the two explicitly-simulated migration checks below). Everything checks out clean — **no code changes this stage**.

## Method

Same as Stages 4.3/5.3/6.3/7.3: an independent headless-Chromium Playwright script (`.theme_stage0_scratch/stage8_3_verify.py`, throwaway, not committed), not the Browser pane. Extended this time with `page.emulate_media(color_scheme=...)` to simulate the OS `prefers-color-scheme` setting for Teal's new Auto mode, and to cover all 7 family/mode states (Original×2, Sharp, Gunmetal, Alpine, Teal×2).

## Mechanism — Teal as a genuine paired family

**"Fixed Dark" label genuinely gone.** Selecting Teal renders `#workspaceThemeModeRow` containing a real `role="radiogroup"` with no "Fixed" text anywhere — confirmed programmatically and visually (screenshot shows the Auto/Light/Dark button row, matching `original`'s exactly).

**All three modes resolve correctly**, verified by actually flipping the simulated OS color-scheme, not just reading the code:

| Scenario | `data-theme` resolved |
|---|---|
| Auto, OS = dark | `dark` |
| Auto, OS = light | `light` |
| Light (forced), OS = dark | `light` — OS ignored, as expected |
| Dark (forced), OS = light | `dark` — OS ignored, as expected |

**Telemetry re-theme confirmed for every relevant transition**, including the two classes this stage specifically needed to check rather than assume:

| Switch | Real token change? | Refresh fired? |
|---|---|---|
| Teal/Light → Teal/Dark | yes | yes |
| Teal/Dark → Teal/Auto (OS=light, resolves light) | yes (dark→light) | yes |
| Teal/Auto → Teal/Light (both already resolve light) | **no** | **no — correctly skipped** |
| Teal → Original/Light | family only (both resolve `light`) | yes |
| Original/Light → Teal/Dark | yes | yes |
| Teal → Sharp | yes | yes |
| Sharp → Teal | yes | yes |

The one "no fire" case is not a gap: when the user's selected *mode* changes but the *resolved* theme string doesn't (Auto already resolving to Light, then switching explicitly to Light), the rendered `--mc-*` values are genuinely unchanged, so skipping the chart rebuild is correct, efficient behavior, not a miss. The **Teal → Original/Light** row is the new case this stage specifically needed: the first confirmed instance of the Stage 4.3 fix firing correctly for a **paired ↔ paired** family switch (both sides resolve to the same mode string, so only the `data-theme-family` comparison catches it) — previously only verified for fixed↔fixed and fixed↔paired combinations.

## Swatch preview — mode-aware, confirmed live

| Check | Result |
|---|---|
| Teal's own card, resolved Dark | `#021818` (Teal Dark's `--mc-bg-app`) ✓ |
| Teal's own card, resolved Light | `#eef8f8` (Teal Light's `--mc-bg-app`) ✓ |
| Original's card, while Teal (light) active | `#eef8f8` — **the known, documented gap**, exactly as Stage 8.2 described it: Original's card shows whatever family is currently active, not its own colors |
| Sharp's card, while Teal active | `#eceeef` — Sharp's own correct color, unaffected, confirming the gap is still isolated to the 2 paired families (`original`, now `teal`) and hasn't spread to the fixed ones |

Confirms the Stage 8.2 CSS fallthrough decision behaves exactly as documented — not just "should work from reading the CSS."

## Migration — exercised live via simulation (not observable "fresh" any other way)

Dev's own `localStorage` already carries the migration flag from Stage 8.2's own live testing, so this **can't be observed as a truly fresh, un-simulated first-run** on this instance — reported explicitly rather than left ambiguous. The practical equivalent, run live:

1. **Existing pre-8.2 Teal user**: seeded `{themeFamily: 'teal', themeMode: 'auto'}` directly in `localStorage` (simulating the exact stored shape such a user would have) with no migration flag set, then did a real page reload (not just a JS state update). Result: `data-theme` stayed `dark`, stored `themeMode` was rewritten to `"dark"`, migration flag got set to `"1"` — the fix works exactly as designed.
2. **Very old pre-Stage-3.2 legacy shape** (`{theme: 'dark'}`, no `themeFamily`/`themeMode` fields at all): reloaded fresh. `migrateLegacyThemeField()` correctly produced `{themeFamily: 'original', themeMode: 'dark'}` first; `migrateTealFixedToPaired()` ran immediately after (it runs unconditionally on every first-load, per its own design) and correctly no-op'd since `themeFamily` was `'original'`, not `'teal'` — its own migration flag still got set (consuming the one-time window), with no interference between the two migrations, no double-application, no data loss.

## Standard acceptance sweep

**No visually different colors without a registered exception**, spot-checked across all 7 states (screenshots below) — Teal Light and Teal Dark both fall back cleanly wherever Teal doesn't override.

**No new raw HEX.** No code changes this stage.

**Focus visible in both Teal modes.** Confirmed via genuine keyboard `Tab` + direct visual inspection of screenshots (per the Stage 7.3 lesson — trust the screenshot over `getComputedStyle().outlineColor` for this specific rule shape): the "MeshCenter Teal" card shows a clearly legible cyan focus ring in both Dark and Light screenshots, `matches(':focus-visible')` confirmed `true` in both.

**State colors identical to Original in both Teal modes**, not just Dark (Stage 7.3 only covered Dark):

| | Teal Dark | Original Dark | Teal Light | Original Light |
|---|---|---|---|---|
| `--mc-success` | `#72d69a` | `#72d69a` | `#27b96f` | `#27b96f` |
| `--mc-warning` | `#f0c86a` | `#f0c86a` | `#e6ad22` | `#e6ad22` |
| `--mc-danger` | `#ff7b85` | `#ff7b85` | `#e6505d` | `#e6505d` |
| `--mc-info` | `#78b6ff` | `#78b6ff` | `#2f80ed` | `#2f80ed` |

All 8 values match exactly — confirms Teal's "no state exception at all" claim (established 7.1, re-confirmed 7.2 and 8.1) holds for Light too, at runtime.

**Hover doesn't shift layout or add a stray shadow**, checked in both modes — same pre-existing, unrelated finding as every prior family (`.node-card:hover { transform: translateX(3px); }`, `.node-card.favorite:hover`'s golden shadow, both hardcoded, unrelated to any token or family), re-confirmed present, still out of scope.

**Old-shape localStorage migration** — see the Migration section above; both the pre-3.2 and pre-8.2 migration paths verified live, no interaction issues.

## Known gaps re-confirmed, still out of scope

- **Swatch-preview gap**: confirmed exactly as documented in Stage 8.2 — now shared by `original` and `teal` (both paired), the 3 fixed families unaffected. Nothing worse.
- **`.node-detail-activity` icon/label gap** (backlog since 7.2): not touched.
- **Dead `--mc-success-soft`/`.status-*` CSS** (found 7.2, corrected 7.3): not touched.
- **`hex_registry.json` line-number drift** (flagged 6.1): documentation-only, not touched.
- **Panels/Display i18n debt**: unrelated, not touched.

## Screenshots

Captured (throwaway, `.theme_stage0_scratch/stage8_3/`, not committed) for all 7 states — Original Light, Original Dark, Sharp, Gunmetal, Alpine, Teal Dark, Teal Light: main workspace view, node cards, the About view, and the Workspace popover with both the family picker and the Auto/Light/Dark radiogroup visible. Available on request.

## Deploy scope

Dev only (`192.168.2.104`), per the project's standing rule. Never left `main` this stage since no fix was needed.

## Plan completion

**All seven families from `MeshCenterthemeregistryplanv0.1.md` are now complete, merged, and live-verified:**

| Family | Merged | Live-verified |
|---|---|---|
| MeshCenter Original (Light) | pre-dates this plan | Stages 1.x–3.x (dark-mode normalization) |
| MeshCenter Original (Dark) | pre-dates this plan | Stages 1.x–3.x |
| MeshCenter Sharp | `eb2bca4`/`6734d99`/`9d957e1` | Stage 4.3 |
| MeshCenter Gunmetal | `03ba2d6`/`f7c0dd6`/`6dfb2d8` | Stage 5.3 |
| MeshCenter Alpine | `2fc2580`/`706356e`/`53cfed9` | Stage 6.3 |
| MeshCenter Teal Dark | `0ffe25d`/`d695ad2`/`3405ada` | Stage 7.3 |
| MeshCenter Teal Light (paired) | `5952187`/`b545e71` | **this stage** |

No open items from the source doc remain. Nothing else is planned as part of this initiative unless the owner opens a new stage.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
