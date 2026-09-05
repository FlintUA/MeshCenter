# MeshCenter UI Style Guide

This is the practical reference for styling MeshCenter's web UI: what design
tokens exist, what each one means, and how to put them together when you
build a new card, panel, or control. It exists so a new card looks and
behaves like every other card without anyone having to reverse-engineer the
CSS history first.

If you're about to write `background: #f7f9fc` or any other raw hex color
in `static/*.css`, stop and check this document first — there is almost
certainly a token for it already.

## 1. How the token system is organized

All theming lives in **one file**: `static/ui-kit.css`. Look for the block
headed `MESHCENTER CANONICAL DESIGN TOKENS` — that comment is not decoration,
it's a rule: **that block is the only place `--mc-*` custom properties get
declared.** Nothing else should define a new `--mc-*` variable elsewhere in
the codebase.

```css
:root {
    --mc-bg-panel: #ffffff;
    /* ...light values... */
}
html[data-theme="dark"] {
    --mc-bg-panel: #1b2837;
    /* ...dark overrides, same variable names... */
}
```

Light values live in `:root`; dark values live in the paired
`html[data-theme="dark"] { ... }` block immediately below it, using the
*exact same variable names*. Every component then reads `var(--mc-*)`
without ever knowing which theme is active — the browser resolves it from
whichever block currently applies. **This is why you almost never need
`[data-theme="dark"] .my-thing { ... }` overrides for color:** if you built
`.my-thing` entirely out of `--mc-*` tokens, dark mode is already handled.

On top of light/dark, MeshCenter also has **theme families**
(MeshCenter Original, Sharp, Gunmetal, Alpine, Teal, ...). A family is
implemented purely as more values for the same token names, gated by
`html[data-theme-family="..."]` selectors layered in the same canonical
block — never as a parallel set of new variables and never as a separate
CSS file. If you're building a new *component*, you don't need to think
about families at all: style it with the semantic tokens below, and every
family gets it for free.

## 2. Token reference

Every value below is the **light** value; read the dark block in
`ui-kit.css` for the dark counterpart of the same name. Values are current
as of the theme-registry normalization project (Sept 2026) — always check
`ui-kit.css` itself for the live value, this table is a map, not the source
of truth.

### Surfaces (backgrounds)

| Token | Light value | Use for |
|---|---|---|
| `--mc-bg-app` | `#eef2f7` | The outermost app background, behind everything |
| `--mc-bg-workspace` | `#f5f7fa` | A workspace page's background (the area a panel sits on) |
| `--mc-bg-panel` | `#ffffff` | The main surface of a card or panel — this is your default |
| `--mc-bg-subtle` | `#f7f9fc` | A slightly recessed surface inside a panel (e.g. an empty-state well, a secondary card nested in a primary one) |
| `--mc-bg-muted` | `#eef3f8` | An even more recessed / muted fill (icon swatches, hover fills, disabled surfaces) |
| `--mc-bg-media-stage` | `#e9edf2` | Backdrop specifically behind media/video content |
| `--mc-surface-sunken` | `#f7f9fc` | An empty-state/placeholder well — reads as recessed, no elevation, paired with `--mc-text-muted` |
| `--mc-surface-control` | `#f7f9fc` | The rest state of an interactive control cluster with a real hover/active state machine (rest = this token, hover = `--mc-primary-soft`, active = `--mc-primary`) |

`--mc-surface-sunken` and `--mc-surface-control` share `--mc-bg-subtle`'s
value today but are structurally distinct roles, split out so either can
diverge independently later without dragging every other `--mc-bg-subtle`
consumer (card surfaces, nav chrome, scrollbar chrome, chat) along with it.
**If what you're building is a plain card/static surface, keep using
`--mc-bg-subtle` directly** — reach for `-sunken` only for an empty-state
well, `-control` only for a control that has the rest/hover/active pattern
described above.

Aliases you'll see used interchangeably in older code: `--mc-surface`
(= `--mc-bg-panel`), `--mc-surface-soft` / `--mc-panel` (= `--mc-bg-subtle`),
`--mc-surface-2` / `--mc-surface-alt` (= `--mc-bg-muted`), `--mc-card-bg`
(= `--mc-bg-panel`), `--mc-input-bg` (= `--mc-bg-panel` in light, its own
darker value in dark). **Prefer the `--mc-bg-*` names for new code** — the
aliases exist for historical reasons and some resolve differently in dark
mode than their name implies.

### Text

| Token | Light value | Use for |
|---|---|---|
| `--mc-text-primary` | `#10233f` | Titles, primary body text, anything that needs to read clearly |
| `--mc-text-secondary` | `#58708f` | Supporting text, captions, secondary labels, muted-but-legible metadata |
| `--mc-text-muted` | `#8798ad` | The most de-emphasized text role |
| `--mc-text-inverse` | `#ffffff` | Text on top of a solid accent/dark fill (e.g. a primary button) |

Aliases: `--mc-text` (= `--mc-text-primary`), `--mc-muted`
(= `--mc-text-secondary`) — a handful of older rules in `style-part1/3/4.css`
still use these short names. Prefer the full `--mc-text-*` names for new code.

**Important accessibility note:** `--mc-text-muted` fails WCAG AA
(4.5:1) against `--mc-bg-panel`/`--mc-bg-subtle` in light mode (it computes
to roughly 2.95:1). It reads fine visually but does not pass automated
contrast checks. **For any text that must pass AA — which is almost all
real UI text — use `--mc-text-secondary` instead**, even where `-muted`
seems like the more semantically "correct" name. This has come up
repeatedly during normalization: whenever a raw gray value failed contrast,
`--mc-text-secondary` was the fix, never `--mc-text-muted`. Reserve
`--mc-text-muted` for genuinely decorative or non-essential text where
failing AA is an accepted, deliberate tradeoff.

### Borders

| Token | Light value | Use for |
|---|---|---|
| `--mc-border` | `#d7e0ea` | Standard card/panel/input border |
| `--mc-border-soft` | `#e4eaf1` | A lighter divider — internal separators, less prominent than a card edge |
| `--mc-border-active` | `#2f73d9` | An active/selected border state |
| `--mc-border-focus` | `#1f6feb` | Keyboard focus ring |
| `--mc-border-hover` | `#b6c3d4` | Hover-state border — the single canonical replacement for the dozens of near-duplicate blue-gray hover borders found live across the theming CSS files before consolidation |

### Primary accent

| Token | Light value | Use for |
|---|---|---|
| `--mc-primary` | `#1f6fdb` | Primary buttons, active nav items, primary links |
| `--mc-primary-hover` | `#175fc0` | Hover/pressed state of the above |
| `--mc-primary-soft` | `#eaf3ff` | A tinted background behind primary-colored content (e.g. an active filter chip) |
| `--mc-primary-muted` | `#dbeafd` | A slightly stronger tint than `-soft` |
| `--mc-accent` | `#214f91` | A secondary, deeper accent (e.g. active tab underline) |
| `--mc-accent-soft` | `#eaf3ff` | A tinted background paired with `--mc-accent` (e.g. the About panel's tab/card treatments) — aliases to `--mc-primary-soft` |

### Semantic states (success / warning / danger / info)

| Token | Light value | Soft-bg pair |
|---|---|---|
| `--mc-success` | `#27b96f` | `--mc-success-soft` `#e9f8f0` |
| `--mc-warning` | `#e6ad22` | `--mc-warning-soft` `#fff7df` |
| `--mc-danger` | `#e6505d` | `--mc-danger-soft` `#fdecee` |
| `--mc-info` | `#2f80ed` | `--mc-info-soft` `#eaf3ff` |

**Read this before using a semantic color as text on its own `-soft`
background**: the base tokens (`--mc-success`, `--mc-warning`, `--mc-danger`,
`--mc-info`) do **not** reliably pass AA as *text* sitting on top of their
own `-soft` background — several combinations measure well under 4.5:1
(warning-on-warning-soft is as low as ~1.9:1). If you're building a status
badge or icon (colored text/icon over a tinted pill background), don't just
plug in the base token for both halves and assume it's accessible — check
the actual contrast of *foreground-on-that-specific-background*, and if it
fails, use a manually darkened variant of the same hue instead (see
`.notification-success/-warning/-error/-info .notification-item-icon` in
`static/style-part3.css` for a worked, documented example of exactly this
pattern — each icon color is a deliberately darkened, AA-passing sibling of
its semantic token, not the token itself).

### Favorite

| Token | Light value | Use for |
|---|---|---|
| `--mc-favorite` | `#8f763d` | A favorited/starred item's accent (e.g. `.node-card.favorite`) |
| `--mc-favorite-soft` | `#eadbb8` | Its paired tinted background |

A dedicated pair so a favorited item is visually distinguishable from a
warning state — `--mc-favorite-soft` is **not** derived the same way as the
other semantic `-soft` tokens (reusing `--mc-warning`'s soft-background
transform here would land too close to `--mc-warning-soft` itself); it's
an independently chosen warm tan/beige instead. Don't reuse `--mc-warning`
for a favorite/starred visual, even where a warm-yellow tone feels close —
that's the exact ambiguity this pair exists to remove.

### Secondary controls

| Token | Light value |
|---|---|
| `--mc-button-secondary-bg` | `#edf3f9` |
| `--mc-button-secondary-bg-hover` | `#e2ebf5` |
| `--mc-button-secondary-border` | `#cbd8e6` |
| `--mc-button-secondary-border-hover` | `#bdccdc` |
| `--mc-button-secondary-text` | `#274766` |

Use this family for a non-primary button (cancel, secondary action) rather
than composing one from surface + border + text tokens by hand.

Alias family: `--mc-dark-control-bg` / `-bg-hover` / `-border` / `-text` —
same values as the four `--mc-button-secondary-*` tokens above in both
themes, an older name for the same role. Prefer `--mc-button-secondary-*`
for new code.

### Tabs

`--mc-tab-bg`, `--mc-tab-bg-hover`, `--mc-tab-bg-active`, `--mc-tab-text`,
`--mc-tab-text-active`, `--mc-tab-line-active` — a self-contained set for
any tabbed control. Note `--mc-tab-text-active`'s dark value has been
reused at least once for an unrelated "bright text on dark" need (a modal
header's title text) purely because the byte value happened to match — the
name doesn't imply the token is generally safe outside an actual tab;
check for a semantically correct token first before assuming a numeric
match is license to reuse it.

### Shared popovers and menus

`--mc-popover-bg`, `--mc-popover-header-bg`, `--mc-popover-item-hover`,
`--mc-popover-divider`, `--mc-popover-border`, `--mc-popover-text`,
`--mc-popover-muted`, `--mc-popover-control-bg`,
`--mc-popover-control-hover`, `--mc-popover-shadow`. This is the "Unified
Popover Surfaces" system — the Workspace popover and the Notifications
popover share this single set of tokens rather than each having their own.
**If you're building a new popover/flyout, use this family** instead of
inventing another one-off surface set; it's what keeps every popover in the
app looking related.

### Chat, workspace, scrollbars, shadows

Smaller component-specific families exist for chat (`--mc-chat-*`),
workspace panel chrome (`--mc-workspace-*`), and scrollbars
(`--mc-scrollbar-*`) — read their block in `ui-kit.css` directly if you're
touching one of those specific areas, most of them just alias back to the
surface/text/border tokens above. Two shadow tokens exist for general use:
`--mc-shadow-panel` (a resting card shadow) and `--mc-shadow-hover` (a
slightly stronger hover-state shadow).

### Geometry & typography (shared across both themes)

These aren't colors and don't change between light/dark — they're the
shared layout constants: `--mc-card-radius` (`12px`), `--mc-card-pad`
(`16px`), `--mc-control-height` (`36px`), `--mc-control-radius` (`8px`),
`--mc-page-pad` (`14px`), plus font-size tokens (`--mc-body-size`,
`--mc-caption-size`, `--mc-card-title-size`, `--mc-page-title-size`, ...).
Use these instead of hardcoding `12px`/`16px`/etc. so a future global
spacing/radius change is a one-line edit.

### History: the legacy `--theme-*` family (removed)

Older sections of `style-part4.css` (Node Manager, Waypoints,
Devices/Peripherals, the Tools panel) used to read through a separate
14-token `var(--theme-panel-bg)` / `var(--theme-text)` / `var(--theme-border)`
alias family, pointed at its own `--mc-legacy-*` canonical block — kept
deliberately isolated from the main `--mc-*` family because these areas
predated the family-theme system and none of the 14 legacy values matched
a current `--mc-*` token consistently across both light and dark. That
meant Node Manager, Waypoints, Devices, Tools, and the workspace-panel
chrome stayed pinned to Original's colors regardless of which family a
user had selected.

**This layer no longer exists.** Theme registry PR 6 migrated every
consumer to a direct `--mc-*` reference and deleted both the `--theme-*`
alias block and the `--mc-legacy-*` canonical block — those surfaces now
follow family colors like everything else. If you see `var(--theme-*)` or
`var(--mc-legacy-*)` anywhere in the codebase after this, it's a leftover
that should be migrated, not a still-supported pattern. The full
per-token mapping and the reasoning behind each judgment call (a couple of
these weren't a straightforward name-to-name swap) is recorded in
`docs/theme-registry/pr6-legacy-token-mapping.md`; the PR-by-PR history is
findable with `git log --grep="^Theme registry"` (see section 8).

## 3. Building a new card or panel — worked patterns

### Pattern A: a simple content card

The most common shape. Use this for anything that's a self-contained block
of content sitting on a workspace background — most sidebar cards, info
cards, list-item cards.

```css
.my-new-card {
    background: var(--mc-bg-panel);
    border: 1px solid var(--mc-border);
    border-radius: var(--mc-card-radius);
    padding: var(--mc-card-pad);
    box-shadow: var(--mc-shadow-panel);
}

.my-new-card-title {
    color: var(--mc-text-primary);
    font-size: var(--mc-card-title-size);
    font-weight: 700;
}

.my-new-card-meta {
    color: var(--mc-text-secondary);
    font-size: var(--mc-caption-size);
}
```

That's it — no `[data-theme="dark"]` override needed anywhere in this
block. A real example of this exact shape: the live `.devices-panel` rule
in `static/ui-kit.css` (`border: 1px solid var(--mc-border); background:
var(--mc-bg-panel); box-shadow: var(--mc-shadow-panel);`). For a card with
a subtly two-tone surface instead of a flat one, see `.weather-card`/
`.time-card` in `static/style-part3.css`, which use a `--mc-bg-subtle` →
`--mc-bg-muted` gradient instead of a flat fill — same idea, slightly more
texture.

### Pattern B: a subtly-differentiated nested surface

When a card needs an inset area that reads as "one level back" from the
card itself (an empty state, a quote/callout, a nested sub-card):

```css
.my-card-inset {
    background: var(--mc-bg-subtle);   /* one step back from --mc-bg-panel */
    border: 1px solid var(--mc-border-soft);  /* softer than the card's own border */
    border-radius: var(--mc-control-radius);
}
```

Real example: `.devices-empty-state` in `static/ui-kit.css`.

### Pattern C: a full workspace page panel (Camera, Devices, Settings, ...)

If you're building an entire new workspace page/tab (not just a card inside
one), use the shared **workspace shell** rather than styling the panel from
scratch — this is what keeps Camera, Media, Devices, System, Settings, and
the Node Manager visually consistent:

```html
<section class="my-feature-view">
  <div class="my-feature-panel mc-workspace-panel">
    <header class="my-feature-header mc-workspace-header">...</header>
    ...
  </div>
</section>
```

```css
.my-feature-view { background: var(--mc-bg-workspace); }
/* .mc-workspace-panel and .mc-workspace-header in ui-kit.css already
   supply background/border/radius/shadow for both themes - you only need
   to add your own class for layout (flex direction, gaps, etc). */
```

Grep `mc-workspace-panel` in `static/ui-kit.css` to see everything the
shared shell already provides before adding your own background/border
rules — a component-specific rule that duplicates what the shell already
does is exactly the kind of dead code this project spent many stages
finding and documenting.

### Pattern D: a status badge / icon with a tinted background

```css
/* Real example: static/style-part3.css's .notification-success icon.
   #217648 is a deliberately darkened sibling of --mc-success (#27b96f) -
   plugging --mc-success straight in here would only reach ~2.3:1. */
.notification-success .notification-item-icon {
    color: #217648;
    background: var(--mc-success-soft);
}
```

Check the actual contrast of your foreground color against the exact
`-soft` background you're using before shipping — see the "Semantic
states" section above for why the plain `--mc-success`/`--mc-warning`/etc.
token often isn't AA-safe as foreground text on its own soft background.

## 4. Accessibility bar (non-negotiable)

- Normal text: **4.5:1** minimum contrast against its real, live background.
- Large text (≥18px, or ≥14px bold) and non-text UI elements (icons,
  control borders, focus indicators): **3:1** minimum.
- **Never round up a borderline number.** `4.47:1` is a fail, not "close
  enough" — if a value is that close, darken it until it actually clears
  the bar.
- Check contrast against the background the element **actually renders on
  in the browser**, not the background you assume from reading the CSS —
  cascade order, specificity, and later/shared rules frequently mean a
  different rule wins than the one you're editing. When in doubt, check
  the computed style in devtools.
- A quick way to compute contrast yourself: WCAG relative luminance from
  sRGB, then `(L_lighter + 0.05) / (L_darker + 0.05)`. Any relative-luminance
  contrast calculator online implements this; `docs/theme-registry/tools/
  contrast_check.py` in this repo also implements it (`python3
  contrast_check.py <fg-hex> <bg-hex>`) if you want a local, offline check.

## 5. Rules for adding or changing colors

1. **Search for an existing token before writing a new hex value.**
   Compute the RGB distance to the nearest plausible token — Euclidean
   distance across the three channels is close enough (`sqrt(ΔR² + ΔG² +
   ΔB²)`). Under roughly 10-15 total is a strong "this is the same color,
   just typo'd or slightly drifted" signal; above ~40-50 it's very likely
   a genuinely distinct color, not a near-duplicate to force together.
2. **Don't force a numerically-close token onto the wrong semantic role.**
   A background color that happens to be numerically near
   `--mc-bg-media-stage` doesn't belong there if the element isn't a media
   backdrop — pick the token whose *name* matches what the color is doing,
   not just the one with the smallest delta.
3. **If nothing fits, it's fine to leave a color raw** — as long as you
   leave a comment saying you checked, what the nearest token was, and why
   it wasn't close enough. A raw value with no comment looks like an
   oversight; a raw value with a documented "checked, d=63, distinct" is a
   deliberate decision future readers can trust.
4. **Some colors are deliberately theme-invariant** — a map canvas
   basemap, a camera-loading spinner accent, brand-specific accents like
   the "location green" family in the Devices reference-location card.
   These should **not** be mapped onto a `--mc-*` token even if the number
   happens to be close, because tokens resolve to *different* values per
   theme/family and a deliberately-fixed brand color must not silently
   start varying. Confirm invariance is actually intentional (check the
   live app in both themes) before treating it as such.
5. **Register any genuinely new raw hex** in
   `docs/theme-registry/hex_registry.json` (see the tool below) — this
   keeps the registry an accurate map of every color actually in the
   codebase, not just the ones from before this project started.
6. **Never invent a new `--mc-*` custom property outside the canonical
   block** in `ui-kit.css`, and never duplicate the token system into a
   separate file.
7. **If you're overriding a `--mc-chat-*`, `--mc-tab-*`, or `--mc-popover-*`
   token inside a `html[data-theme-family="..."]` block, cover the whole
   group or none of it.** These three groups each have a handful of
   tokens that are still their own literal in the canonical block (not a
   `var()` alias) — a family is free to leave the whole group on the
   alias chain, but overriding *some* of a group's still-literal tokens
   while leaving others on the canonical block's literal is the actual bug
   shape a past regression took (a family's own color sitting next to
   whatever the *other* mode's canonical value happened to be, because one
   token in the group was forgotten). `contract_validator.py` (see
   Tooling below) enforces this automatically — run it before opening a
   PR that touches a family block.

## 6. Tooling

- `docs/theme-registry/tools/check_new_hex.py` — flags any raw hex literal
  in the five theming CSS files that isn't already in the registry.
  `python3 check_new_hex.py static/*.css` checks the files as they stand;
  `python3 check_new_hex.py --diff <ref>` checks only what a diff added
  (useful before opening a PR). Note the `--diff` mode can occasionally
  false-positive on a hex value mentioned *inside a multi-line CSS
  comment* whose opening `/*` falls outside the diffed lines — if a
  flagged value only ever appears in a `/* ... */` block, not a live
  declaration, that's a tooling limitation, not a real issue; confirm with
  the whole-file mode, which strips comments correctly regardless of where
  they start, before registering anything.
- `docs/theme-registry/tools/contrast_check.py` — computes WCAG contrast
  between two hex colors (`python3 contrast_check.py <fg-hex> <bg-hex>`),
  reporting pass/fail against both the 4.5:1 normal-text and 3:1
  large-text/UI-component thresholds.
- `docs/theme-registry/tools/contract_validator.py` — checks that every
  `html[data-theme-family="..."]` block either overrides all of a
  `--mc-chat-*`/`--mc-tab-*`/`--mc-popover-*` group's still-literal tokens
  or none of them (rule 7 above). `python3 contract_validator.py` checks
  `static/ui-kit.css`; exits non-zero on a partial-override violation.
- `docs/theme-registry/tools/visual_regression.py` — a Playwright-driven
  visual + token regression suite: captures computed `--mc-*` values across
  every family/mode combo plus a set of full-page/component screenshots,
  and diffs both against a committed baseline
  (`docs/theme-registry/visual-baseline/`). `python3 visual_regression.py`
  checks against the baseline; `--update-baseline` regenerates it (review
  the resulting diff like any other change — see
  `docs/theme-registry/visual-regression-README.md` for what's covered and
  the reasoning behind the tolerance). Run this before and after any change
  that touches token wiring or a themed surface's CSS, not just a docs-only
  change like this one.
- `docs/theme-registry/tools/token_inventory.py` — a full (not sampled)
  inventory of every selector that consumes each `--mc-*` token via
  `var(--mc-*)` across the five theming CSS files. Useful for answering
  "what are all the roles this token plays?" or "does this token get
  consumed inside a family block?" without trusting a hand grep.
  `python3 token_inventory.py --token mc-bg-subtle` prints just one
  token's consumers; with no arguments it writes the full inventory to
  `docs/theme-registry/token-inventory.md`.
- `docs/theme-registry/hex_registry.json` — the registry itself
  (machine-readable; `raw-hex-audit.md` is the human-readable companion).

## 7. Cache-busting reminder

Any change to `static/style-part1.css` through `static/style-part4.css` or
`static/ui-kit.css` needs its `?v=` query string bumped on the matching
`<link>` in `templates/index.html`, or browsers with a cached copy won't
see the change. The same applies to `static/chat.js` and any other script
whose behavior changed. Only bump the files that actually changed — don't
bump ones you only added a comment to.

## 8. Where the rest of the history lives

This file is the "how to build something new" reference. The *why* behind
the current token values — which stage found which dead-code layer, which
raw hex got consolidated into which token and by how much, which
accessibility failures got fixed — is recorded stage-by-stage in this
repo's own git history: every theme-registry PR's description carries the
full reasoning for that stage's decisions, and they're all findable with
`git log --grep="^Theme registry"`. You shouldn't need to read that
history to build a new component correctly — but if a token's current
value looks surprising, that's where the reasoning is.
