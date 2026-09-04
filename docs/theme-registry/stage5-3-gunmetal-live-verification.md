# Theme registry — Stage 5.3: MeshCenter Gunmetal, live verification on dev

Verification stage for Gunmetal (Stages 5.1 `03ba2d6` + 5.2 `f7c0dd6`), run against the real running app on dev (`192.168.2.104`), driven through the actual Workspace popover UI (Playwright, never localStorage editing). One real bug was found and fixed — everything else checks out clean.

## Method

Same as Stage 4.3: an independent headless-Chromium Playwright script (`.theme_stage0_scratch/stage5_3_verify.py`, throwaway, not committed), not the Browser pane. The script drives the real Workspace popover radios (`page.check(...)`, dispatching real `change` events).

## Acceptance checklist (source doc section 13, as applicable to a fixed/dark family)

**No visually different colors without a registered exception.** Spot-checked node cards, the About view, Settings, and the Workspace popover across Original/Dark, Sharp, and Gunmetal. Everything Gunmetal doesn't override falls back cleanly to the base dark values.

**No new raw HEX.** No CSS/token changes planned this stage — one real bug was found and fixed (below); it introduces zero new hex (pure `var()` overrides). `check_new_hex.py static/ui-kit.css` stays clean (1002 known values).

**Focus visible on panel/card/control backgrounds.** Verified via genuine keyboard `Tab` presses (learned from Stage 4.3's false alarm — never trust scripted `.focus()`). Tabbing to a Gunmetal control shows `outlineColor: rgb(156, 205, 229)` (`#9ccde5`, exactly Gunmetal's `--mc-border-focus`), `matches(':focus-visible') === true`. Obviously legible against the dark surfaces, matching the 8.61:1 contrast target from Stage 5.1.

**The warning exception (5.2) is distinguishable in practice.** This is where the real bug was found — see below. After the fix: `.node-card.favorite` renders `rgb(71, 53, 41)` (`#473529`) under Gunmetal and `rgb(67, 55, 25)` (`#433719`) under Original/Dark — two different, distinguishable browns, confirming the exception is real and visible, not just correct in the token declaration. Warning's foreground/base were re-confirmed unchanged under Gunmetal: `.node-detail-activity.activity-away` background and `.text-warning` color both read `#f0c86a`, identical to base dark — exactly as Stage 5.2 intended (only the surface moved).

**Success/danger/info distinguishable, base dark values.** Confirmed via test elements: success `#72d69a` (green), danger `#ff7b85` (pink-red) — Gunmetal doesn't override either, both correctly inherit `html[data-theme="dark"]`. Clearly distinguishable by hue and lightness, not an isoluminant pair.

**Hover doesn't shift layout or add a stray shadow.** Same pre-existing, unrelated findings as Stage 4.3 (`.node-card:hover { transform: translateX(3px); }` and `.node-card.favorite:hover`'s golden shadow, both hardcoded in `style-part1.css`, unrelated to any `--mc-*` token or family) — re-confirmed present and still out of scope, not a Gunmetal regression.

**Light ↔ Dark ↔ Sharp ↔ Gunmetal switches live, no reload.** A `window.__stage53Marker` set once at initial load survived every switch performed during the run.

**Telemetry chart re-themes on every combination, including Original/Dark → Gunmetal specifically.** This is the direction Stage 4.3's fix was explicitly checked against here, not just assumed symmetric. Same modal-blocking constraint as Stage 4.3 applies (confirmed again via `elementFromPoint`), so the switch was invoked via `Workspace.update({ themeFamily: 'gunmetal' })` — the exact function the radio's own `change` handler calls. Result:

| Switch | `data-theme` before → after | `data-theme-family` before → after | refresh fired |
|---|---|---|---|
| Original/Dark → Gunmetal | `dark` → `dark` | `original` → `gunmetal` | **yes (1 call)** |
| Gunmetal → Sharp | `dark` → `light` | — | yes (sanity check, mode also flips) |

Confirms the Stage 4.3 fix (comparing `themeFamily` in addition to the resolved mode string) correctly covers the dark-side family-only switch too, not just the light-side one it was originally found and fixed for.

**Old theme preference migration still works.** Simulated a pre-Stage-3.2 `{theme: 'dark'}` localStorage shape and called `Workspace.load()`: correctly produced `{themeFamily: 'original', themeMode: 'dark', ...}`, unaffected by Gunmetal's addition.

## Bug found and fixed

`.node-card.favorite`'s (and `.selected`'s, and `.ignored`'s) intended background was **never actually rendering in any dark mode** — not just under Gunmetal, but under plain Original/Dark too, confirmed to predate all theme-family work. Root cause, found via a `getMatchedCSSRules`-equivalent introspection (not just visual observation):

```css
html[data-theme="dark"] :is(
    .chat-item, .node-card, .system-card, .settings-card,
    .about-card, .about-resource-card, .about-release-card,
    .about-note, .about-footer,
    .system-log-card, .radio-health-card,
    .wifi-card, .auto-recovery-card
) {
    background: var(--mc-bg-panel) !important;
    border-color: var(--mc-border) !important;
    color: var(--mc-text-primary) !important;
}
```

A Stage 1.x dark-mode normalization sweep, written without accounting for `.node-card`'s own semantic-state background variants. Since it targets plain `.node-card` (inside `:is(...)`) with `!important`, and `.node-card.favorite`/`.selected`/`.ignored`'s own rules (`ui-kit.css` ~263-277) are *not* `!important`, the sweep always won — silently discarding the favorite/selected/ignored surface colors in every dark mode, always, since whichever Stage 1.x PR introduced this rule.

**Verified NOT a real problem for `.selected`** despite an initial false alarm: `.node-card.selected`'s actual rendered appearance is governed by a separate, deliberate, pre-existing `chat.js:installCompactNodeCardStyles()`-injected stylesheet (a hardcoded pink/dark-red "v11" redesign, entirely independent of `--mc-*` tokens, unrelated to this bug or to theme families at all) — its `background-color: transparent` reading was just the normal shorthand-reset side effect of a gradient-only `background` declaration, not a bug. Caught by checking `backgroundImage` in addition to `backgroundColor`, same lesson as Stage 4.3's hover-shadow investigation: check the full picture, not one property.

**Fix** (`static/ui-kit.css`, right after the sweep):

```css
html[data-theme="dark"] .node-card.favorite { background: var(--mc-warning-soft) !important; }
html[data-theme="dark"] .node-card.selected { background: var(--mc-primary-soft) !important; }
html[data-theme="dark"] .node-card.ignored { background: var(--mc-danger-soft) !important; }
```

Small, scoped, three lines. The `.selected` line is effectively inert in practice (shadowed by the higher-specificity v11 stylesheet described above) but included for correctness/consistency with `.favorite`/`.ignored`, which are the two that actually needed it and are now fixed. Verified live on dev before and after: `.favorite`'s background went from indistinguishable-panel-gray to the correct, distinct `#473529` (Gunmetal) / `#433719` (Original/Dark); `.ignored` similarly confirmed to now show `#49252b` (dark's `--mc-danger-soft`).

## Known gaps re-confirmed, still out of scope

- **Swatch-preview gap:** confirmed again — while Gunmetal is active, the "MeshCenter Original" card's all 4 swatch slots show Gunmetal's colors instead of Original's own. Sharp's and Gunmetal's own cards are each correctly unaffected by whichever other family is active. Same single-card scope as before, nothing worse.
- **`#C2A669` (favorite/calm-accent role, flagged in 5.2):** confirmed nothing looks broken in its absence — no dangling reference, no visual gap. Not implemented here, per scope.
- **Panels/Display i18n debt:** unrelated, not touched.

## Screenshots

Captured (throwaway, `.theme_stage0_scratch/stage5_3/`, not committed) for Original/Dark, Sharp, and Gunmetal: main workspace view, node cards (including favorite ones, showing the fixed warning-surface distinction clearly), the About view, and the Workspace popover with the family picker open. Available on request.

## Deploy scope

Dev only (`192.168.2.104`), per the project's standing rule. Restored to `main` after verification.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
