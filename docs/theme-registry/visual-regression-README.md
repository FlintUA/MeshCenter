# Theme registry visual + contract regression suite

theme-registry tooling PR 7. The first automated regression harness for
this project's UI - protects what PRs 2-5 fixed (bugs 1.1/1.2/1.4/1.5)
and captures a baseline of the legacy `--theme-*`/`--mc-legacy-*` layer
*before* PR 6 migrates it, so a PR 6 regression can be told apart from an
intended rendering change.

## Running it

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium   # one-time browser download, not fetched by pip
python docs/theme-registry/tools/visual_regression.py
```

Exit status 0 if `contract_validator.py` and the visual/token checks all
pass, 1 otherwise. `--skip-contract` runs the visual/token checks alone.
Prints a report of every mismatch found (token path + baseline/current
values, or screenshot name + percent of pixels that differ).

### Updating the baseline (intentional visual changes only)

```bash
python docs/theme-registry/tools/visual_regression.py --update-baseline
```

This overwrites `docs/theme-registry/visual-baseline/tokens.json` and the
PNGs under `docs/theme-registry/visual-baseline/screenshots/`. It is a
deliberate, separate command from the normal check - never something a
regular run does implicitly - and the resulting diff (`git diff`/`git
status` on those two paths) should be reviewed like any other code change
before committing: a reviewer should be able to see *exactly* what
rendering changed and confirm it matches what the PR intended, the same
way `check_new_hex.py`'s registry updates are reviewed. This is the
explicit mechanism PR 6 is expected to use once its migration is ready.

## Determinism: byte-identical screenshots, not just "within tolerance"

The first version of this suite compared screenshots with a pixel-diff
tolerance (still true - see "Screenshots" below), but the *baseline
images themselves* weren't reproducible: they captured real wall-clock
time, real host CPU/RAM/hostname/uptime, and briefly-mid-fetch "Loading"
placeholder states, and rendering itself had a few genuine (if tiny)
non-deterministic sources. Following an explicit merge-readiness bar, the
harness was hardened so that **running it twice, back to back, produces
byte-identical screenshot files** - not just close enough to pass the
tolerance. Verified: 5 consecutive independent `--update-baseline` runs,
compared pairwise via `sha256sum`, produced identical hashes for all 15
screenshots every time (see "Proof" below for the actual commands).

### What was non-deterministic, and the fix for each

- **Real wall-clock time** (`meshsrv/time_service.py`'s `get_status()`
  reads `time.time()` directly - this is the "12:34, System clock" widget
  and drives every "N seconds/minutes/days ago" relative-time string).
  Fixed two ways together: the client's `Date`/`Date.now()` is frozen to
  a fixed instant (2026-01-01T12:00:00Z) via a Playwright
  `add_init_script` (must run before the app's own `DOMContentLoaded`
  handler reads the real clock - a plain `page.evaluate()` after
  navigation would be too late), and `_visual_server.py`'s seeded nodes
  use that same fixed epoch for `last_seen` instead of `time.time()`, so
  "246 d" ago is always exactly 246 days from the frozen instant, not
  whatever the real gap happens to be when the harness runs.
- **Real host CPU/RAM/hostname/uptime** (`api/api_system.py`'s
  `/api/system/info` reads `platform.node()` and `/proc/uptime` directly;
  `system/cpu_history.py`'s `/api/system/cpu-history` reads real CPU%/RAM%
  even though the history-sampling worker thread itself is never started
  in this harness). Both intercepted via Playwright `page.route()` and
  served fixed, hardcoded JSON instead of ever reaching the real handler
  - see `MOCKED_API_RESPONSES` in `visual_regression.py` for the exact
  payloads and the reasoning per endpoint.
- **A real, hardware-adjacent radio-port probe on first load**
  (`static/chat.js`'s `loadChatList()` calls `/api/chats?refresh_channels=1`
  on its first invocation, which server-side drives
  `meshsrv/serial_port_supervisor.py`'s real port-in-use check against
  the synthetic serial-port path - observed taking ~23s on this dev
  machine, mostly an 8s x2 "utility missing" timeout, since Windows has no
  `fuser`/`lsof`-equivalent; a Linux box **would** have one, and could
  take a materially different code path/timing - exactly the kind of
  environment-dependent behavior the "no radio required, at all" bar
  rules out). Also mocked via `page.route()` (`**/api/chats*`), so this
  request never reaches the real handler, `SerialPortSupervisor`, or
  anything hardware-adjacent at all - confirmed by removing the mock
  temporarily and observing the real code path/timing described above,
  then re-adding it.
- **Mid-fetch "Loading..." placeholders.** No unified "app ready"
  signal (event, promise, or DOM attribute/class) exists anywhere in
  `chat.js` or `server.py` today - checked before adding anything, per
  the addendum's own instruction not to assume one already existed. Added
  a harness-only `_wait_until_settled()`: polls until no *visible* text
  node anywhere contains "Loading" (this app's own convention for every
  in-flight async widget), with a real timeout that **raises** rather
  than silently proceeding - a screenshot taken after giving up on this
  wait would just reintroduce the nondeterminism it exists to remove.
  Deliberately does not use Playwright's `networkidle` load-state: the
  app polls several endpoints (e.g. `/api/base_status`) on an ongoing
  ~1s timer by design, so the network is never idle and that signal would
  simply time out every run.
- **CSS transitions mid-flight** (already fixed before the addendum, kept
  here for completeness) - `*, *::before, *::after { transition: none;
  animation: none }`.
- **A blinking text-input caret** - the waypoint-create modal
  auto-focuses its Name field on open (`chat-map.js`); an OS-level caret
  blink cycle landing on/off at the exact capture instant produced a
  small (1-2 RGB unit), input-shaped diff region between runs. Fixed with
  `caret-color: transparent` plus explicitly blurring
  `document.activeElement` before every screenshot.
- **GPU rasterization jitter.** Even with every source above eliminated,
  a ~170-pixel region kept differing by exactly 1 RGB unit on an
  otherwise-random subset of runs. Chromium is launched with
  `--disable-gpu` (forces pure software rasterization, which is
  bit-reproducible; GPU-accelerated rendering has genuine driver/timing
  jitter) and `--force-color-profile=srgb` (removes any host-display
  color-management step).
- **Chromium's spell-checker** can asynchronously underline
  placeholder/typed text once its dictionary finishes loading, on its own
  timeline. Launched with `--disable-spell-checking`.
- **A native `<textarea>` resize-grip** (the modal's Description field is
  `resize: vertical` by default) and **`backdrop-filter: blur()`**
  (`.waypoint-create-modal`'s own backdrop, a compositor-level
  convolution whose output can vary slightly by internal accumulation
  order) were both identified as remaining sources by elimination and
  disabled for the capture only (`resize: none`, `backdrop-filter: none`)
  - neither one is part of what this suite tests (color/token wiring),
  so losing them costs nothing real.
- **Rounded-corner anti-aliasing on the modal's own form fields**, the
  very last source (intermittent, roughly 1 run in 3): Skia's software
  rasterizer compositing a `border-radius` corner together with a
  semi-transparent border color can still accumulate a sub-pixel rounding
  difference in rare cases, even with the GPU disabled - a genuine
  rendering-engine limitation, not app or harness state. Flattened
  (`border-radius: 0`) for the waypoint modal's own inputs/textarea/select
  only (not globally) - again, not something this suite tests.
- **A server-side real-wall-clock leak in the node-list "N d" age badge**
  (found during independent re-verification, after the round above): the
  badge isn't computed client-side at all - `chat.js` renders `node.age`,
  a string the server already built. `server.py`'s `age_text()` (~line
  3248) computes it as `int(time.time() - last_seen)` - a real,
  server-side wall-clock read with **no client-side equivalent**, so
  freezing the client's `Date` (above) has zero effect on it. The
  original fix pinned `last_seen` to the *same absolute epoch* as the
  frozen client `Date` - which still leaked real time indirectly: the
  server's `time.time()` keeps advancing with the real calendar, so
  `age_text()`'s result grows by exactly one calendar day every real day
  that passes between capturing the baseline and re-running the suite
  (confirmed live: baseline showed "246 d," a re-run one real day later
  showed "247 d," from byte-identical code and seed data). Fixed by
  anchoring `last_seen` to `time.time() - NODE_AGE_SECONDS` computed
  fresh at every boot (`_visual_server.py`'s `_seed_nodes()`), rather than
  to any absolute timestamp - `age_text()`'s subtraction then always
  lands on the same fixed `NODE_AGE_SECONDS` (200 days) regardless of
  which real calendar date the harness happens to run on. Verified with
  a standalone simulation (not just "ran it twice quickly"): computed the
  badge with `time.time()` mocked 60 real days apart, both landing on
  "200 d ago" - see this repo's own PR history for the exact snippet used.

All of the above are harness-only overrides, injected into the page after
navigation via `page.add_style_tag()`/`page.route()`/Chromium launch
args, or (for the age badge) a change to how test data is seeded - none
of them touch `static/*.css`, `chat.js`, or `server.py` itself.

### Font pinning

Two machines with "the same" font *name* installed (e.g. both claiming
"Arial") can still rasterize it to different pixels - different font
file, different version, different hinting tables. The only way to get
byte-identical text across machines is to make every machine render with
the *same font file*, not just the same font-family string. Chose to
**bundle one small, permissively-licensed font** (Roboto Regular, Apache
2.0, `docs/theme-registry/tools/fonts/Roboto-Regular.ttf` -
`LICENSE-Roboto.txt` alongside it) and embed it as a base64 `data:` URI
in a harness-injected `@font-face` + `* { font-family: ... !important }`
rule, over the alternatives:

- *Forcing a system font by name* (e.g. `font-family: Arial !important`)
  doesn't solve the problem at all, per the paragraph above.
- *Fixing the screenshot target to an explicit size* doesn't help either
  - it constrains layout, not glyph rasterization.
- *A CDN-hosted webfont* would need network access at capture time
  (fragile in a "fresh worktree, no cached artifacts" scenario) and
  wouldn't be reproducible if the CDN ever changed the file.

Bundling the actual font bytes in the repo is the least invasive option
relative to the real app (this override only ever runs inside this
harness's own page context, never touches `static/*.css`) and the most
robust across machines (no network dependency, no host-font dependency -
just the exact bytes committed to the repo).

### Pin the exact Chromium build, not just "chromium"

Font-bundling closed the vast majority of a residual layout gap on the
waypoint modal (26px down to 2px) during independent re-verification, but
didn't close it entirely. Root-caused, not left as "close enough": the
two environments were running genuinely different Chromium *builds* -
141.0.7390.37 vs 151.0.7922.34, ten major versions apart - despite both
having installed "chromium" via `python -m playwright install chromium`.
The reason is `requirements-dev.txt`'s own version range for the
`playwright` package: `playwright>=1.40,<2` lets `pip install` resolve to
whichever release happens to be newest at install time, and **each
playwright release bundles one specific, fixed Chromium build** - there
was no version-control record of which Chromium build a given screenshot
baseline was actually captured against, so two honestly-run installs,
months apart, legitimately got different renderers.

Fixed by pinning `playwright` to an **exact** version
(`playwright==1.62.0`) in `requirements-dev.txt`, rather than trying to
pin a Chromium revision directly (Playwright doesn't offer a clean,
supported way to do that independent of the wrapper package version - the
package version *is* the practical unit of pinning). This is a real fix,
not a documented limitation: it makes "which Chromium build" a tracked,
reviewable part of this repo's own version-controlled dependencies, the
same way any other pinned dependency is. The one thing it can't do
retroactively is update an *already-cached* browser binary from before
the pin - anyone hitting this needs to re-run both `pip install -r
requirements-dev.txt` and `python -m playwright install chromium` after
pulling the pin, not just the first one.

### Proof (byte-identical, not just within tolerance)

```bash
for i in 1 2 3 4 5; do
  python docs/theme-registry/tools/visual_regression.py --update-baseline --skip-contract
  cp -r docs/theme-registry/visual-baseline/screenshots /tmp/vr_check$i
done
cd /tmp/vr_check1 && for f in *.png; do
  ok=true
  h1=$(sha256sum "$f" | cut -d' ' -f1)
  for i in 2 3 4 5; do
    [ "$(sha256sum "../vr_check$i/$f" | cut -d' ' -f1)" != "$h1" ] && ok=false
  done
  $ok && echo "IDENTICAL x5: $f" || echo "DIFFERS: $f"
done
```

Result after the fixes above: all 15 screenshots identical across all 5
runs. `tokens.json` was not regenerated or touched by any of this work -
the token-snapshot mechanism was already fully deterministic before the
addendum (it reads computed-style strings, not rendered pixels) and
needed no changes.

The 5-consecutive-runs proof above ran within a few minutes of real time,
which wouldn't have caught the age-badge leak described above (it only
manifests across real calendar days). Verified that fix separately with a
standalone simulation rather than waiting a day or touching the system
clock:

```python
NODE_AGE_SECONDS = 200 * 86400
real_now_today = time.time()
real_now_60_days_later = real_now_today + 60 * 86400
last_seen_today = real_now_today - NODE_AGE_SECONDS
last_seen_later = real_now_60_days_later - NODE_AGE_SECONDS
# age_text(last_seen, now) == "seen 200 d ago" for BOTH pairs below,
# despite "now" being 60 real days apart:
age_text(last_seen_today, real_now_today + 2)          # -> "seen 200 d ago"
age_text(last_seen_later, real_now_60_days_later + 2)   # -> "seen 200 d ago"
```

confirming the fix holds by construction (last_seen is always
`boot-time - NODE_AGE_SECONDS`, so the subtraction always lands on the
same fixed value) rather than by coincidence of when the proof happened
to run.

## Why this architecture

**Capture tool: Playwright (headless Chromium).** No visual-regression
tooling existed in this repo before this PR (confirmed: nothing in
`requirements.txt`, no Selenium/Puppeteer config anywhere). Playwright
was chosen over the alternatives because (a) it drives a real, full
browser engine - required, since this suite needs actual CSS cascade
resolution, real `:hover` pseudo-class matching (`page.locator(...).hover()`
triggers genuine mouse-hover state, not a class-toggle simulation), and
real screenshot rendering, none of which a static-HTML/DOM-only tool can
give; (b) it installs cleanly via pip with no system package manager
dependency, confirmed by actually installing it and launching Chromium in
this exact dev environment before committing to it (`pip install
playwright && python -m playwright install chromium` - the whole
feasibility check for "don't add a heavy CI dependency that can't run
here" was done empirically, not assumed); (c) its `add_style_tag`/`evaluate`
API made it straightforward to drive `data-theme`/`data-theme-family`
directly (this project's own established test technique throughout the
whole theme-registry project) without needing real UI clicks for every
state.

**Real app, not static fixtures.** The harness boots an actual `server.py`
instance (`docs/theme-registry/tools/_visual_server.py`), using the same
hardware-free bootstrap approach as `tests/conftest.py`'s `server_module`
fixture (synthetic `config.py`, fake Meshtastic CLI/serial port, stubbed
`libcamera`) but as a plain function so it can run outside pytest, on a
free local port. This is a deliberate, separate ~60-line duplication of
that fixture's logic (not an import of it) - `conftest.py`'s helpers are
private and test-suite-scoped, and keeping the two independent means
either can evolve without silently breaking the other. Two synthetic
nodes are seeded directly into the in-memory `nodes` dict (one plain, one
`favorite: true`) so PR 3's favorite-card state has something real to
render - real component state, not an idle empty page.

## The two check mechanisms

### 1. Token snapshots (`docs/theme-registry/visual-baseline/tokens.json`)

The primary regression mechanism. For each of the 7 family/mode combos
(Original light/dark, Sharp, Gunmetal, Alpine, Teal light/dark), captures:

- Every `--mc-chat-*`/`--mc-tab-*`/`--mc-popover-*` custom property (PR 2,
  bug 1.1), `--mc-favorite`/`-soft`/`--mc-warning`/`-soft` (PR 3, bug 1.2),
  `--mc-border-hover`/`-active`/`-focus` (PR 4, bug 1.4), and
  `--mc-bg-subtle`/`--mc-surface-sunken`/`--mc-surface-control` (PR 5, bug
  1.5) - read from `getComputedStyle(document.documentElement)`.
- The actual rendered `background-color` of a live `.node-card.favorite`
  element and a plain `.node-card`'s hover border-color obtained via a
  **real Playwright `.hover()`** (not a class-toggle simulation) and a
  **real class-toggle** for `.node-card.selected` - this catches a
  cascade/specificity regression (the wrong rule winning) that a bare
  custom-property read wouldn't, since PR 4's whole bug was about
  specificity, not just which token a declaration names.

Diffed with **exact string equality, no tolerance** - these are
deterministic computed-style strings (hex colors, `rgb(...)` strings),
not rendered pixels, so there is no anti-aliasing/font-rendering noise to
tolerate. Any difference is a real change.

**A real flakiness source found and fixed while building this, not
tolerated with a threshold**: many components use `transition: all .2s`.
Changing `data-theme`/`data-theme-family` changes the custom properties
those transitions depend on, so an element genuinely mid-transitions to
its new color for ~200ms - reading `getComputedStyle()` during that
window returns a real, briefly-different interpolated value (observed:
two runs 5 RGB units apart on the same check, purely from transition
timing). Fixed by injecting `*, *::before, *::after { transition: none
!important; animation: none !important; }` once per run, not by widening
the tolerance - this removes the entire class of timing variance instead
of hoping a threshold is wide enough to hide it.

### 2. Screenshots (`docs/theme-registry/visual-baseline/screenshots/*.png`)

For surfaces where a token-level check alone wouldn't catch a regression
- specifically the pre-PR-6 legacy-layer baseline (see below), where the
question is "does this whole surface look the same," not "does this one
property hold a specific value."

**Diff method**: Pillow (already a project dependency), per-pixel RGB
difference via `ImageChops.difference()`, counting a pixel as "mismatched"
if any channel differs by more than **24** (out of 255), then failing if
more than **0.5%** of the image's pixels are mismatched. Both numbers
were chosen empirically, not guessed: 24 is well above the ~2-4 unit
noise Chromium's own sub-pixel text/font-hinting rendering introduces
between otherwise-identical runs (confirmed by running the same capture
twice against an unchanged baseline before finalizing this suite - zero
false positives), and 0.5% of pixels tolerates that same font-rendering
noise across an entire full-viewport screenshot without masking a real,
localized color change (a wrong-colored panel background - the kind of
regression this suite exists to catch - moves far more than 0.5% of a
1280x800 viewport). Proven to actually catch a regression, not just
pass silently: see "Proof it catches a regression" below.

## Scope: what's covered, and why not more

Per the brief's own risk flag (a full theme x component matrix goes flaky
and gets ignored) and "start small" instruction, this is **not** the full
7 x N matrix. Covered:

**Confirmed-bug regression (protects PRs 2-5):**
- PR 2 (bug 1.1): full chat/tab/popover token snapshot, all 7 combos.
- PR 3 (bug 1.2): `.node-card.favorite`'s actual rendered background vs.
  `--mc-warning`/`-soft` token values, all 7 combos - confirms
  distinguishable, not just "some token exists."
- PR 4 (bug 1.4): one representative hover (`.node-card:hover`, real
  Playwright hover) and one representative selected state
  (`.node-card.selected`, class toggle) per combo - not all ~54 repointed
  selectors, per the brief's own "enough to catch a regression in the
  wiring, not all of them" scope.
- PR 5 (bug 1.5): `--mc-bg-subtle`/`--mc-surface-sunken`/
  `--mc-surface-control` snapshotted every combo; a mismatch between them
  would itself indicate a regression even without a separate assertion,
  since the baseline was captured with them equal.

**Pre-PR-6 legacy layer baseline** (screenshots): see below.

Not covered in this initial baseline (documented gaps, not silent
omissions): `.node-manager-detect-radio-btn`, `.node-profile-badge.saved`,
`.waypoint-action-toast` (see "Population 1, corrected" below) - reaching
them needs deeper page interaction (opening Node Manager's radio-detect
flow, saving a profile, sending a waypoint) than this initial pass
covers. Node Manager profile cards and `.node-detail-card` specifically
(both named in the brief's population-2 list) are covered only
incidentally, wherever they happen to appear in the two full-viewport
views this suite navigates to (Chats, Devices) - not deliberately
navigated to and isolated. Expanding either is a natural, low-risk follow
-up once this initial harness has proven itself stable over a few PRs.

## The legacy `--theme-*`/`--mc-legacy-*` populations (verified, not assumed)

Built an independent selector inventory (a small script parsing
`static/style-part4.css` for every rule consuming `var(--theme-*)` or
`var(--mc-legacy-*)`, classifying each by whether its selector text
contains `data-theme="dark"` and/or `data-theme-family`) rather than
trusting the brief's grep. Found **95 total rules**, confirmed **zero**
are family-qualified (no `[data-theme-family="..."]` override exists
anywhere for any of them - Gunmetal/Alpine/Teal Dark really do all render
this whole surface set in the same hardcoded colors, exactly as the brief
described).

**Population 1 (unqualified by theme or family) - 23 rules, corrected
from the brief's hypothesis**: `.waypoint-create-*` accounts for most of
it (confirmed, and it's the population this suite screenshots, across
all 7 combos per the brief's instruction), but the brief's own scoping
missed 5 more real members: `.node-profile-card:not(.is-active):hover`,
`.node-profile-badge.saved`, `.node-manager-detect-radio-btn` (+ its own
`:hover`), and `.waypoint-action-toast` (+ `.sending`, `button`,
`button.retry`). These are genuinely unqualified too - flagged above as
a documented gap rather than silently added to scope this late, since
capturing them needs interaction flows this initial harness doesn't
drive yet.

**Population 2 (dark-qualified, family-unqualified) - 72 rules,
confirms the brief's read**: body/app shell, workspace panels and
headers, Node Manager cards, `.node-detail-card` and its many
sub-elements, `.waypoint-tools-item`, peripheral/device cards, and a
second `.waypoint-create-*` copy (this population duplicates several
`.waypoint-create-*` selectors under an explicit `[data-theme="dark"]`
qualifier - meaning the waypoint modal actually has *both* an unqualified
base style and a separate, equally-legacy dark override; both are
captured, since population 1's modal screenshots are taken across all 7
combos including dark ones). Screenshotted as two full-viewport views
(the default Chats view, and the Devices view) across the 4 in-scope
combos (Original dark, Gunmetal, Alpine, Teal Dark) rather than isolating
each of the 72 selectors individually - a whole-view comparison catches a
regression in any of them without a 72-screenshot matrix, at the cost of
only incidentally covering Node Manager/`.node-detail-card` (see "Not
covered" above).

## `static/style-part3.css` and `templates/login.html`

**`static/style-part3.css`**: has **zero** actual `--theme-*`/
`--mc-legacy-*` consumers. Its one match is a code *comment* (line 666,
part of an unrelated dead-`var(--color-*)` discussion) that merely
*mentions* "the `--theme-*`/`--mc-legacy-*` chain" for comparison - not a
live declaration. Nothing to capture; excluded because there is nothing
there.

**`templates/login.html`**: has 3 real `var(--theme-*, <fallback>)`
consumers, but the login page never loads `ui-kit.css` (only
`style-part1-4.css`) and is rendered before any session/theme-family
selection exists - `data-theme-family` is never present on this page in
the live app, and never can be, regardless of what PR 6 does to the
`--theme-*`/`--mc-legacy-*` wiring. Since PR 6's migration only changes
behavior when a family differs from Original-default, and this page can
never receive a family attribute, its rendering is provably unaffected by
that migration - excluded on that reasoning, not by default. (For the
curious: with `ui-kit.css` absent, `--mc-legacy-app-bg` etc. are
themselves undefined on this page, so `--theme-app-bg` resolves through
an undefined reference - whatever it renders today is unrelated to the
family system and will keep rendering the same after PR 6.)

## Proof it catches a regression

Before finalizing, deliberately changed dark canonical's
`--mc-favorite-soft` from `#655b34` to `#ff00ff` and re-ran the check
(not committed - reverted immediately after):

```
FAIL: 8 regression(s) found:
  TOKEN MISMATCH [original/dark/elements/favorite_card_background]: baseline='rgb(101, 91, 52)' current='rgb(255, 0, 255)'
  TOKEN MISMATCH [original/dark/root_tokens/--mc-favorite-soft]: baseline='#655b34' current='#ff00ff'
  TOKEN MISMATCH [teal/dark/elements/favorite_card_background]: baseline='rgb(101, 91, 52)' current='rgb(255, 0, 255)'
  TOKEN MISMATCH [teal/dark/root_tokens/--mc-favorite-soft]: baseline='#655b34' current='#ff00ff'
  SCREENSHOT MISMATCH [legacy_main_view__original_dark]: 3.1751% of pixels differ (tolerance 0.5000%)
  SCREENSHOT MISMATCH [legacy_devices_view__original_dark]: 3.1751% of pixels differ (tolerance 0.5000%)
  SCREENSHOT MISMATCH [legacy_main_view__teal_dark]: 3.1701% of pixels differ (tolerance 0.5000%)
  SCREENSHOT MISMATCH [legacy_devices_view__teal_dark]: 3.1701% of pixels differ (tolerance 0.5000%)
```

Correctly flagged only Original dark and Teal Dark (the two variants that
don't override `--mc-favorite-soft` themselves and so inherit the
canonical value directly) - Gunmetal and Alpine, which have their own PR
3 family overrides, were correctly *not* flagged. This is exactly the
kind of precise, scoped detection the suite is meant to provide, not a
blanket "something, somewhere changed" signal.

After reverting, three consecutive clean runs against the same baseline
produced zero mismatches (see git history for this file's own
development for the full log) - the transition-disabling fix eliminated
the one flake found during development (a 5-RGB-unit mismatch on a
class-toggle check, root-caused above) before this suite was considered
done.
