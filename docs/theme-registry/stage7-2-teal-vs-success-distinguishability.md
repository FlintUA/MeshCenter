# Theme registry — Stage 7.2: Teal primary vs success distinguishability

Investigation stage, per the source doc's own stage plan (section 12, Stage 7). **Finding: no real gap — no code change in this stage.** One pre-existing, Teal-unrelated gap was found and is flagged as a backlog item, not fixed here (out of scope — see below).

## Why this stage exists

Teal has no state-color exception at all (confirmed in Stage 7.1 — section 8.3 has no Teal line), so Teal's own primary/accent (`--mc-primary`/`--mc-accent: #10C2C2`, bright cyan) and the base dark scheme's success green (`--mc-success: #72D69A`) sit side by side with zero Teal-specific differentiation in the token layer. This stage checks whether that actually causes confusion anywhere in the real UI.

## 1–2. Consumer enumeration and disambiguation check

Grepped every real (live-referenced-in-CSS) consumer of `--mc-primary`/`--mc-primary-hover`/`--mc-accent`/`--mc-accent-soft` and `--mc-success`/`--mc-success-soft`, then cross-checked each against the actual JS/HTML that renders it — not just the CSS selector, since several turned out to be dead code (see below).

**`--mc-primary`/`--mc-accent` family — every live consumer already carries a non-color cue:**

| Consumer | Cue |
|---|---|
| Active tab underline (`.nodes-list-tab`, `.about-tab.active`, etc.) | tab text label |
| Primary button fill (`.primary-btn`/`.screenshot-btn`/`.send-btn`, `.settings-segmented button.active`) | button text |
| `.btn:focus-visible` outline | transient, keyboard-only ring around whatever control has focus — never a bare color swatch |
| About-page accents (badges, links, resource cards, version number) | always paired with visible text |
| `.workspace-theme-swatch--primary` | a preview swatch, but inside a labeled family-picker card ("MeshCenter Teal" text right next to it) |
| `.dock-map-btn.map-active` | icon + "Map" label |
| `.node-card.selected` background | a wash across the whole card, which also shows the full node name |
| Dark-mode chat message sender name color (`--mc-primary-hover`) | the text itself *is* the label |

**`--mc-success` family — real usage is much narrower than the CSS selectors suggest.** The rule `.status-online, .status-ready, .status-running, .status-ok, .text-success { color: var(--mc-success); }` (`ui-kit.css` ~423-428) looks like 5 live consumers; grepping `static/*.js`/`templates/*.html` for actual class-name usage found only `.status-ok` is ever applied — `.status-online`, `.status-ready`, `.status-running`, and `.text-success` are dead CSS, never referenced anywhere. `--mc-success-soft` has **zero** consumers anywhere in the codebase (only its own token declarations).

The 3 real consumers:

| Consumer | Cue |
|---|---|
| `.status-ok` (header connection status) | always paired with a text label (`labelEl.textContent = label`, e.g. "Online"/"Idle"/"Error" — `chat.js:11272-11293`) |
| Dark-mode `.notification-success .notification-item-icon` | a distinct checkmark glyph (`✓`, from `notificationIcon()`, `chat.js:3456-3464`) *and* the notification's own message text (`<strong>${item.message}</strong>`) — the icon itself is `aria-hidden`, decorative on top of the real text |
| `.node-detail-activity.activity-online` | **no icon, no exposed text label** — see below |

## 3. The node-card activity indicator — the one real color-only consumer, checked specifically

`.node-detail-activity` (`style-part3.css:11-22`) is a small 7×18px bar next to a node's name in the node detail header, colored by one of 4 fixed roles depending on state: `--mc-success` (online), `--mc-warning` (away), `--mc-danger` (offline), or `--mc-text-muted` (unknown). It's rendered with `aria-hidden="true"` and a generic `title="Activity status"` tooltip (not a per-state label) — genuinely color-only for a sighted user glancing at it, not a list of 4 bars with position/order to lean on (contrary to one framing this stage's brief raised as a possibility): **it's a single bar per node card, showing only that node's current state** — there's no "list" to spatially disambiguate against.

Checked specifically whether Teal introduces a confusion risk here:

- **Does Teal's primary/accent color ever render on this bar?** No — its background is always one of the 4 fixed role tokens above; `--mc-primary`/`--mc-accent` are never assigned to it, under any family. Teal doesn't touch `--mc-success` either (confirmed in 7.1), so this bar renders identically under Original, Sharp, Gunmetal, Alpine, and Teal alike — it is not something that varies by family at all.
- **Does anything using `--mc-primary`/`--mc-accent` render immediately adjacent to it** (which could visually read as "a 5th state")? Checked the surrounding markup (`chat.js:6392-6409`, the node detail header/tabs): the tab navigation directly below it (`.node-detail-tab.active`, `style-part2.css:3542-3545`) uses a **hardcoded raw hex** (`#1a73e8`), not `var(--mc-primary)` — pre-existing, untouched by any theme-registry stage, unrelated to Teal. Nothing near the activity bar reads Teal's cyan.

**Conclusion for this consumer**: no Teal-specific confusion risk — verified via the actual DOM/CSS, not assumed. The bar's *own* pre-existing weakness (distinguishing its 4 states — success/warning/danger/muted — from each other by hue alone, with no icon or exposed label) is real, but it predates all theme-registry work, applies identically under every family including the ones already merged (Original/Sharp/Gunmetal/Alpine), and isn't made worse by Teal specifically, since Teal never touches any of the 4 tokens it reads. It also technically doesn't follow the project's own stated convention ("warning, danger, success and info always ship with an icon or text label in every theme" — section 8.3's closing rule) — flagging this as a **possible future backlog item** (a general accessibility/consistency gap in `.node-detail-activity`, not a Teal-scoped one), not fixing it here: fixing it would mean changing a shared component every family renders through, well beyond "verify Teal vs success," and Stage 7.2's brief is explicit that this stage doesn't invent scope beyond the distinguishability check itself.

One more spot-checked, unrelated near-miss ruled out: `.device-status-dot`/`.device-status-ok` (hardware/I2C device status, `style-part4.css:1313-1327`) is *also* a small color-only dot, but it uses a hardcoded hex (`#168447`/`currentColor`), not `--mc-success` at all — unaffected by any theme family, so out of scope here too.

## 4. Quantified color distance

| | Hex | HSL |
|---|---|---|
| Teal primary/accent | `#10C2C2` | hue 180.0°, sat 84.8%, light 41.2% |
| Base dark success | `#72D69A` | hue 144.0°, sat 54.9%, light 64.3% |

- **Hue angle difference**: 36.0° (teal is pure cyan; success sits toward yellow-green)
- **Saturation difference**: 30 points (teal is substantially more saturated/vivid — the intuition that "both read as bright, saturated, cool" doesn't hold up quantitatively: success is the *less* saturated of the two)
- **Lightness difference**: 23 points (success is notably lighter/more pastel; teal is a darker, punchier cyan)
- **CIE76 ΔE (Lab-space Euclidean distance)**: **33.2** — for reference, ΔE ≳ 10 is generally considered "colors read as clearly distinct," and ΔE ≳ 2–3 is the threshold for "reliably noticeable" to typical vision. 33.2 is roughly 3× the "clearly distinct" threshold.
- **Qualitative color-vision-deficiency note**: teal/cyan retains a strong blue-channel signal that green lacks, which is why blue-green vs. yellow-green pairs (unlike red vs. green) generally stay distinguishable under the common red-green CVD types — not a substitute for the quantified numbers above, just corroborating context.

This is supporting evidence, not the basis for the conclusion by itself (per the brief) — but it does confirm the consumer-level finding: these two colors are not a close, easily-confused pair even before accounting for the icon/label/position cues already documented above.

## Conclusion

**No real gap requiring a fix.** Every live consumer of Teal's primary/accent color already carries a non-color disambiguation cue (text label, icon, or button/tab context). The one color-only `--mc-success` consumer (`.node-detail-activity.activity-online`) never renders Teal's color and has nothing Teal-colored adjacent to it, so it presents no Teal-specific risk — its own pre-existing, family-independent weakness is flagged as a possible backlog item, not fixed in this stage. The quantified color distance (36° hue, ΔE 33.2) further supports that teal cyan and base-dark success green are not a close pair perceptually, even setting the UI-level cues aside.

No `--mc-*` token changes, no new palette values, no CSS/JS edits this stage — nothing to run through `check_new_hex.py`/`check-i18n.py`/cache-busting.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
