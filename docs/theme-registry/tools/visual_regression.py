#!/usr/bin/env python3
"""Theme registry visual + contract regression suite (theme-registry
tooling PR 7). Protects PRs 2-5's fixes and captures a pre-PR-6 baseline
of the legacy --theme-*/--mc-legacy-* layer, so PR 6's migration can be
told apart from an accidental regression.

Two check mechanisms, both against a real, locally-booted, hardware-free
server.py instance driven by headless Chromium (Playwright):

1. TOKEN SNAPSHOTS (JSON, exact-match diff, zero pixel-flake risk) -
   computed CSS custom-property values and computed style of specific
   live elements (including real :hover via Playwright, not simulated),
   captured across all 7 family/mode combos. This is the primary
   regression mechanism for "did the token wiring change" - deterministic
   strings, no anti-aliasing/font-rendering noise possible.

2. SCREENSHOTS (PNG, pixel diff via Pillow with a tolerance) - for
   surfaces where a token-level check alone wouldn't catch a regression:
   the waypoint-create modal (population 1 of the legacy-layer baseline)
   and a sample of the dark-only unqualified legacy surfaces (population
   2). See docs/theme-registry/visual-regression-README.md for the full
   selector inventory and reasoning behind what's covered here vs. not
   (deliberately not the full theme x component matrix - see that doc's
   "Scope" section).

Usage:
    python visual_regression.py                 # check against baseline
    python visual_regression.py --update-baseline   # regenerate baseline
    python visual_regression.py --skip-contract  # visual/token checks only

Exit status 0 if everything matches (or after a successful
--update-baseline write), 1 on any mismatch or error.
"""
import argparse
import base64
import json
import re
import sys
import tempfile
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parents[2]
BASELINE_DIR = TOOLS_DIR.parent / "visual-baseline"
TOKENS_BASELINE = BASELINE_DIR / "tokens.json"
SCREENSHOTS_DIR = BASELINE_DIR / "screenshots"
FONT_PATH = TOOLS_DIR / "fonts" / "Roboto-Regular.ttf"

sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(REPO_ROOT))

import _visual_server  # noqa: E402

COMBOS = [
    ("original", "light"),
    ("original", "dark"),
    ("sharp", "light"),
    ("gunmetal", "dark"),
    ("alpine", "dark"),
    ("teal", "light"),
    ("teal", "dark"),
]

# Combos whose dark-only legacy surfaces (population 2) are in scope -
# Original dark plus the 3 non-Original families that currently render
# with the exact same hardcoded legacy colors (confirmed: zero
# [data-theme-family=...] override exists for any --theme-*/--mc-legacy-*
# consumer - see the PR description for how this was verified).
LEGACY_POPULATION_2_COMBOS = [
    ("original", "dark"),
    ("gunmetal", "dark"),
    ("alpine", "dark"),
    ("teal", "dark"),
]

FROZEN_EPOCH_SECONDS = _visual_server.FROZEN_EPOCH_SECONDS

# Fixed, host-machine-independent replacements for the handful of API
# responses that are genuinely non-deterministic across machines even
# with start_runtime() never called - server.py/api_system.py read the
# real host's hostname, /proc/uptime, CPU%, RAM%, and wall-clock time
# directly (confirmed by reading api_system.py and time_service.py), so
# without mocking these, a screenshot captured on one machine could never
# byte-match one captured on another. Each entry's shape mirrors what the
# real route documents/returns - see the referenced source for the
# authoritative schema.
MOCKED_API_RESPONSES = {
    # meshsrv/time_service.py's get_status() - "utc" is real wall-clock
    # time otherwise.
    "**/api/time": {
        "utc": FROZEN_EPOCH_SECONDS, "timezone": "UTC", "source": "system",
        "synchronized": True, "quality": "system", "rtc_present": False,
        "rtc_detected": False, "rtc_configured": False, "rtc_readable": False,
    },
    # api/api_system.py's api_system_info() - hostname/uptime/ram/disk are
    # all real host reads otherwise.
    "**/api/system/info": {
        "hostname": "visual-regression-host", "uptime": "0d 0h 0m", "cpu_temp": None,
        "load_avg": None, "ram_total_mb": 1024, "ram_used_mb": 256, "ram_free_mb": 768,
        "disk_total_gb": 32, "disk_used_gb": 8, "disk_free_gb": 24, "model": None,
        "os": "visual-regression", "kernel": "visual-regression", "app_version": "test",
    },
    # system/cpu_history.py's api_system_cpu_history() - current/temperature/
    # ram_percent are real host reads otherwise (the history worker thread
    # itself is never started, so `records` is already deterministically
    # empty without mocking).
    "**/api/system/cpu-history*": {
        "ok": True, "range": "30m", "current": 0.0, "temperature": None,
        "ram_percent": 0.0, "records": [],
    },
    # static/chat.js's loadChatList() calls this with ?refresh_channels=1
    # on first load, which server-side (api/api_chat.py's
    # discover_radio_channels()) drives meshsrv/serial_port_supervisor.py's
    # real port-in-use probing (fuser/lsof-equivalent) against a real
    # serial device - not just slow (~23s observed, mostly an 8s x2
    # "utility missing" timeout on Windows) but genuinely hardware-adjacent
    # and platform-dependent (a Linux box with fuser/lsof installed could
    # take a materially different path/timing than one without) - exactly
    # what "hardware-free, no radio required at all" rules out. Mocked
    # with a fixed one-channel, zero-DM response matching the synthetic
    # config's own CHANNEL_CHAT_ID/CHANNEL_CHAT_NAME instead of letting
    # this reach the real handler at all.
    "**/api/chats*": {
        "chats": [{
            "id": "channel", "name": "LongFast", "type": "channel", "is_channel": True,
            "last_message": "", "last_time": "", "unread": 0,
        }],
        "total_unread": 0,
        "channels": [{"id": "channel", "name": "LongFast"}],
    },
}

ROOT_TOKEN_NAMES = [
    # PR 2 (bug 1.1) - chat/tab/popover
    "--mc-chat-bg", "--mc-chat-surface", "--mc-chat-surface-raised", "--mc-chat-border",
    "--mc-chat-input", "--mc-chat-text", "--mc-chat-muted",
    "--mc-tab-bg", "--mc-tab-bg-hover", "--mc-tab-bg-active", "--mc-tab-text",
    "--mc-tab-text-active", "--mc-tab-line-active",
    "--mc-popover-bg", "--mc-popover-header-bg", "--mc-popover-item-hover",
    "--mc-popover-divider", "--mc-popover-border", "--mc-popover-text",
    "--mc-popover-muted", "--mc-popover-control-bg", "--mc-popover-control-hover",
    # PR 3 (bug 1.2) - favorite/warning
    "--mc-favorite", "--mc-favorite-soft", "--mc-warning", "--mc-warning-soft",
    # PR 4 (bug 1.4) - state borders
    "--mc-border-hover", "--mc-border-active", "--mc-border-focus",
    # PR 5 (bug 1.5) - bg-subtle split
    "--mc-bg-subtle", "--mc-surface-sunken", "--mc-surface-control",
]

# (name, JS expression returning a plain string) - element-level computed
# styles, for catching a cascade/specificity regression that a bare
# custom-property read wouldn't (e.g. PR 4's rules referencing the right
# var() but a *different* rule winning the cascade).
ELEMENT_CHECKS = [
    ("favorite_card_background", """
        () => {
            const el = document.querySelector('.node-card.favorite:not(.selected)');
            return el ? getComputedStyle(el).backgroundColor : null;
        }
    """),
    ("plain_card_hover_border", """
        () => {
            const el = document.querySelector('.node-card:not(.favorite):not(.selected)');
            return el ? getComputedStyle(el).borderColor : null;
        }
    """),
]
# NOTE: .node-manager-detect-radio-btn, .node-profile-badge.saved and
# .waypoint-action-toast are additional population-1 (legacy layer,
# unqualified) selectors this harness's own scan found beyond the
# brief's .waypoint-create-* hypothesis (see the PR description) - not
# wired into an automated check yet because reaching them needs deeper
# page interaction (opening Node Manager's radio-detect flow, saving a
# profile, triggering a waypoint send) than this initial baseline's
# "start small" scope covers. Documented here as a known gap for a
# future expansion, not silently dropped.

# Elements to real-Playwright-hover, then read border-color from - a
# representative sample of PR 4's ~54 repointed selectors (not all of
# them; enough to catch a regression in the token wiring mechanism
# itself, per the brief).
HOVER_CHECKS = [
    ("node_card_hover_border", ".node-card:not(.favorite):not(.selected)"),
]

# (name, JS to run first, then class to add, then element to read) - for
# active/selected states that need a class toggle rather than a real
# pointer interaction.
CLASS_STATE_CHECKS = [
    ("node_card_selected_border", ".node-card:not(.favorite)", "selected"),
]

PIXEL_DIFF_TOLERANCE = 0.005  # 0.5% of pixels may differ - see README for why


def _freeze_clock(page):
    """Pins Date/Date.now() to FROZEN_EPOCH_SECONDS via an init script, so
    it's in effect before any app JS runs (a plain page.evaluate() after
    goto() would be too late - the app's own DOMContentLoaded handler,
    which reads the real clock for the "Time & Timers" card etc., has
    already run by then)."""
    epoch_ms = FROZEN_EPOCH_SECONDS * 1000
    page.add_init_script(f"""
        (() => {{
            const fixedMs = {epoch_ms};
            const RealDate = Date;
            class FrozenDate extends RealDate {{
                constructor(...args) {{
                    if (args.length === 0) super(fixedMs);
                    else super(...args);
                }}
                static now() {{ return fixedMs; }}
            }}
            window.Date = FrozenDate;
        }})();
    """)


def _mock_nondeterministic_apis(page):
    """Intercepts the handful of API responses that read real host-machine
    state (hostname, uptime, CPU/RAM, wall-clock time) even with
    start_runtime() never called, replacing each with a fixed response -
    see MOCKED_API_RESPONSES's own comment for why each one is otherwise
    non-deterministic across machines."""
    def _make_handler(body_dict):
        payload = json.dumps(body_dict)

        def _handler(route, request):  # noqa: ARG001 - Playwright always passes both
            route.fulfill(status=200, content_type="application/json", body=payload)

        return _handler

    for pattern, body in MOCKED_API_RESPONSES.items():
        page.route(pattern, _make_handler(body))


def _install_test_font(page):
    """Forces every element to render with one specific, repo-bundled font
    file (Roboto-Regular.ttf, Apache 2.0 - see docs/theme-registry/tools/
    fonts/), rather than whatever the host OS happens to have installed
    under a generic name like "Arial" or "sans-serif". Two machines with
    "the same" system font can still rasterize it to different pixels
    (different font file/version, different hinting) - embedding the
    actual font bytes as a data: URI, with no network fetch and no
    dependency on anything outside this repo, is what makes text
    byte-identical across machines. Least invasive to the real app: this
    only ever runs inside this harness's own page context, never touches
    static/*.css."""
    font_bytes = FONT_PATH.read_bytes()
    font_b64 = base64.b64encode(font_bytes).decode("ascii")
    page.add_style_tag(content=f"""
        @font-face {{
            font-family: 'MCVisualRegressionFont';
            src: url(data:font/ttf;base64,{font_b64}) format('truetype');
            font-weight: normal;
            font-style: normal;
        }}
        *, *::before, *::after {{
            font-family: 'MCVisualRegressionFont', sans-serif !important;
        }}
    """)


def _wait_until_settled(page, timeout_ms: int = 8000):
    """Polls until no visible element's text contains "Loading" (the
    app's own convention for every in-flight async widget: weather,
    peripheral devices, node telemetry, etc. - confirmed by reading their
    templates/JS, not guessed), so a screenshot always captures the same
    settled state rather than whichever mid-fetch frame happened to
    render first. Deliberately does NOT wait for Playwright's own
    "networkidle" load-state - the app polls several endpoints (e.g.
    /api/base_status) on an ongoing ~1s timer by design, so the network
    is never idle and that signal would simply time out every time.

    No unified "app ready" event/promise/DOM flag exists anywhere in
    chat.js or server.py today (checked before adding this) - this is a
    deliberate, harness-only addition rather than hooking something that
    was already there; not added to the shipped app itself; deliberately
    a *loud* failure (raises) rather than a silent timeout, since a
    screenshot taken after giving up on this wait would just reintroduce
    the exact nondeterminism this function exists to remove."""
    deadline_ms = timeout_ms
    poll_ms = 100
    waited = 0
    while waited <= deadline_ms:
        still_loading = page.evaluate("""
            () => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    if (node.nodeValue && node.nodeValue.includes('Loading')) {
                        const el = node.parentElement;
                        if (el && el.offsetParent !== null) return true;
                    }
                }
                return false;
            }
        """)
        if not still_loading:
            return
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
    raise RuntimeError(
        f"page did not settle (a visible element still contains 'Loading' text) within {timeout_ms}ms - "
        "capturing a screenshot now would bake in a nondeterministic mid-fetch frame."
    )


def _blur_active_element(page):
    """Removes focus from whatever element currently has it, right before
    a screenshot. Caret-color is already forced transparent (see
    run_checks()), but a focused element can still carry its own
    focus-ring/outline styling that isn't otherwise part of the state
    being captured (nothing in this suite's screenshots is meant to show
    "this field is focused" as the thing under test)."""
    page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")


def _wait_for_two_paints(page):
    """Waits for two consecutive requestAnimationFrame callbacks - the
    standard way to be sure the browser has actually painted the current
    DOM/style state at least once, not just that the JS/DOM work settled.
    Found necessary for full determinism: even with the clock frozen,
    APIs mocked, transitions/animations disabled and the caret hidden,
    two of the waypoint-modal screenshots (both light-mode, both the
    first family/mode combo touching their own code path) still differed
    by 1-2 RGB units in a small region across otherwise-identical runs -
    a layout/paint settling artifact on the modal's first show, not
    app-state nondeterminism."""
    page.evaluate("""
        () => new Promise(resolve => {
            requestAnimationFrame(() => requestAnimationFrame(resolve));
        })
    """)
    # The double-rAF alone still left a handful of runs with a 1-2 RGB
    # unit diff in the same modal region, on a different combo each time
    # (a genuine timing race, not tied to one specific family) - this
    # extra fixed margin is a belt-and-suspenders on top of it, not a
    # replacement for it.
    page.wait_for_timeout(150)


def _apply_theme(page, family: str, mode: str):
    page.evaluate(
        """([family, mode]) => {
            document.documentElement.setAttribute('data-theme', mode);
            if (family === 'original') {
                document.documentElement.removeAttribute('data-theme-family');
            } else {
                document.documentElement.setAttribute('data-theme-family', family);
            }
        }""",
        [family, mode],
    )
    # Force a real style/paint pass. Transitions are globally disabled for
    # the whole run (see run_checks()), so a short settle is enough - no
    # need to wait out a .2s transition window.
    page.evaluate("() => { void document.documentElement.offsetHeight; }")
    page.wait_for_timeout(50)


def _root_token_snapshot(page) -> dict:
    return page.evaluate(
        """(names) => {
            const cs = getComputedStyle(document.documentElement);
            const out = {};
            for (const n of names) out[n] = cs.getPropertyValue(n).trim();
            return out;
        }""",
        ROOT_TOKEN_NAMES,
    )


def _element_snapshot(page) -> dict:
    out = {}
    for name, js in ELEMENT_CHECKS:
        out[name] = page.evaluate(js)
    return out


def _hover_snapshot(page) -> dict:
    out = {}
    for name, selector in HOVER_CHECKS:
        loc = page.locator(selector).first
        if loc.count() == 0:
            out[name] = None
            continue
        loc.hover()
        out[name] = page.evaluate(
            "(sel) => { const el = document.querySelector(sel); return el ? getComputedStyle(el).borderColor : null; }",
            selector,
        )
        # move away so the next check isn't affected by a lingering :hover
        page.mouse.move(0, 0)
    return out


def _class_state_snapshot(page) -> dict:
    out = {}
    for name, selector, cls in CLASS_STATE_CHECKS:
        val = page.evaluate(
            """([sel, cls]) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                el.classList.add(cls);
                const v = getComputedStyle(el).borderColor;
                el.classList.remove(cls);
                return v;
            }""",
            [selector, cls],
        )
        out[name] = val
    return out


def capture_token_snapshot(page) -> dict:
    snapshot = {}
    for family, mode in COMBOS:
        _apply_theme(page, family, mode)
        combo_key = f"{family}/{mode}"
        snapshot[combo_key] = {
            "root_tokens": _root_token_snapshot(page),
            "elements": _element_snapshot(page),
            "hover": _hover_snapshot(page),
            "class_states": _class_state_snapshot(page),
        }
    return snapshot


def capture_screenshots(page, out_dir: Path) -> list[str]:
    """Returns the list of screenshot names captured (relative filenames,
    no extension) - used both when writing a fresh baseline and when
    checking against one, so both runs iterate the same set."""
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []

    # Population 1 (legacy layer, unqualified by theme/family): the
    # waypoint-create modal, across all 7 combos - the brief's own
    # "most likely to visibly change" population.
    for family, mode in COMBOS:
        _apply_theme(page, family, mode)
        page.evaluate("() => { if (window.openCreateWaypointDialog) window.openCreateWaypointDialog(52.5, 13.4); }")
        _wait_until_settled(page)
        name = f"waypoint_create_modal__{family}_{mode}"
        modal = page.locator("#waypointCreateModal .waypoint-create-dialog")
        if modal.count() > 0:
            _blur_active_element(page)
            _wait_for_two_paints(page)
            modal.screenshot(path=str(out_dir / f"{name}.png"))
            names.append(name)
        page.evaluate("() => { if (window.closeCreateWaypointDialog) window.closeCreateWaypointDialog(); }")

    # Population 2 (legacy layer, dark-qualified/family-unqualified): the
    # main chat view and the Devices view, across Original dark + the 3
    # non-Original dark-rendering families. Full-viewport shots, not
    # cropped to one selector - this population spans dozens of
    # selectors (body/app-container/panels/headers/node-manager cards),
    # a whole-view comparison catches a regression in any of them without
    # needing 15+ separate per-component screenshots (see the "start
    # small" scope note in the PR description for why this isn't the
    # full per-selector matrix).
    for family, mode in LEGACY_POPULATION_2_COMBOS:
        _apply_theme(page, family, mode)
        name = f"legacy_main_view__{family}_{mode}"
        _blur_active_element(page)
        page.screenshot(path=str(out_dir / f"{name}.png"))
        names.append(name)

        devices_tab = page.locator('button:has-text("Devices")').first
        if devices_tab.count() > 0:
            devices_tab.click()
            _wait_until_settled(page)
            name2 = f"legacy_devices_view__{family}_{mode}"
            _blur_active_element(page)
            page.screenshot(path=str(out_dir / f"{name2}.png"))
            names.append(name2)
            page.locator('button:has-text("Chats")').first.click()
            _wait_until_settled(page)

    return names


def _pixel_diff_fraction(path_a: Path, path_b: Path) -> float:
    from PIL import Image, ImageChops  # noqa: PLC0415 - optional-at-import-time, only needed for screenshot diffing

    img_a = Image.open(path_a).convert("RGB")
    img_b = Image.open(path_b).convert("RGB")
    if img_a.size != img_b.size:
        return 1.0
    diff = ImageChops.difference(img_a, img_b)
    bbox_pixels = diff.getdata()
    total = img_a.size[0] * img_a.size[1]
    mismatched = sum(1 for px in bbox_pixels if max(px) > 24)  # per-channel noise floor, see README
    return mismatched / total if total else 0.0


def run_checks(update_baseline: bool) -> int:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    sandbox = Path(tempfile.mkdtemp(prefix="mc_visual_regression_"))
    print(f"Booting hardware-free server.py in {sandbox} ...")
    server_module, base_url = _visual_server.boot_server(sandbox)
    print(f"Server up at {base_url}")

    failures = []
    with sync_playwright() as pw:
        # Byte-identical screenshots across machines/runs need Chromium's
        # own rendering to be deterministic, not just the page content -
        # GPU-accelerated rasterization introduces genuine (if tiny, ~1-2
        # RGB units) run-to-run variance from driver/timing jitter,
        # observed directly while building this harness (a waypoint-modal
        # region kept differing by 1-2 units between two runs even with
        # the clock frozen, APIs mocked, and the caret disabled - traced
        # to font/box rendering, not app state). `--disable-gpu` forces
        # pure software rasterization (bit-reproducible), and
        # `--force-color-profile=srgb` removes any color-management step
        # that could otherwise vary by host display profile.
        browser = pw.chromium.launch(args=[
            "--disable-gpu", "--force-color-profile=srgb",
            # Chromium's spell-checker can asynchronously underline
            # placeholder/typed text in <input>/<textarea> elements once
            # its dictionary finishes loading, on its own timeline -
            # exactly the kind of small, timing-dependent rendering
            # difference (~1-2 RGB units, always in the same
            # input-field-shaped region) observed while chasing down this
            # harness's own remaining non-determinism.
            "--disable-spell-checking",
        ])
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        # Order matters: the clock freeze must be an init script (runs
        # before any app JS, including the DOMContentLoaded handler that
        # reads the real clock) and the API mocks must be routed before
        # goto() so the very first requests are already intercepted -
        # both would be too late as a plain page.evaluate()/page.route()
        # called after navigation.
        _freeze_clock(page)
        _mock_nondeterministic_apis(page)

        page.goto(base_url + "/")
        _wait_until_settled(page)
        # The chat/DM sidebar and the seeded nodes only reach a settled
        # DOM once loadChatList()/loadMessages() have run - normally
        # triggered by the app's own initial-load flow, but that flow
        # depends on channel/websocket state this hardware-free harness
        # doesn't have, so call both directly. loadChatList()'s first call
        # would normally hit /api/chats?refresh_channels=1, which is
        # mocked above specifically so this never reaches the real,
        # radio-port-probing handler.
        page.evaluate("async () => { if (window.loadChatList) await window.loadChatList(); }")
        page.evaluate("async () => { if (window.loadMessages) await window.loadMessages(); }")
        _wait_until_settled(page)

        _install_test_font(page)

        # Disable every CSS transition/animation for the rest of this run.
        # Many components use `transition: all .2s` - reading a computed
        # style while one is mid-flight (theme switch, class toggle)
        # returns a real, briefly-different interpolated value, not a
        # cached/stale one. Confirmed while building this harness: a
        # class-toggle check landed on two different sub-pixel-adjacent
        # colors 5 RGB units apart across two runs before this fix, purely
        # from transition timing - exactly the kind of low-grade flake
        # the brief's own "start small, prove stable" concern is about.
        # Disabling transitions removes the whole class of timing
        # variance instead of papering over it with a tolerance.
        page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
        # Blinking text-input carets are the other classic source of a
        # tiny (1-2 RGB units, single-pixel-region) non-determinism
        # between two otherwise-identical screenshots - confirmed by
        # observing this harness's own waypoint-modal screenshots differ
        # only in a small box exactly where the auto-focused "Name" field
        # sits (chat-map.js focuses it on open), on/off depending on
        # exactly which half of the ~500ms OS caret-blink cycle the
        # capture landed in. `caret-color: transparent` removes the
        # caret's own rendering; blurring on every screenshot (see
        # capture_screenshots()) additionally removes any focus-ring.
        page.add_style_tag(content="* { caret-color: transparent !important; }")
        # A resizable <textarea> (the waypoint-create modal's Description
        # field) draws a native resize-grip in its bottom-right corner -
        # another well-known source of tiny cross-run rendering variance
        # in screenshot testing, alongside the rounded-corner (border-
        # radius) anti-aliasing on this same modal's inputs, which
        # together were the last remaining source of a ~170-pixel,
        # always-1-RGB-unit-off region traced during development (see
        # docs/theme-registry/visual-regression-README.md's "byte-
        # identical" section for the full chase). resize:none is
        # harness-only, same as every other override here.
        page.add_style_tag(content="textarea { resize: none !important; }")
        # backdrop-filter: blur() (used by .waypoint-create-modal, behind
        # the dialog this suite screenshots) is a compositor-level
        # convolution whose exact output can vary slightly run to run
        # depending on internal accumulation order - a genuine rendering-
        # engine nondeterminism source, not a page-content one. Disabled
        # for the capture only; the dialog itself never had a backdrop-
        # filter of its own; this doesn't touch what token-driven colors
        # look like anywhere.
        page.add_style_tag(content="* { backdrop-filter: none !important; -webkit-backdrop-filter: none !important; }")
        # Last remaining source of intermittent (roughly 1 in 3 runs) 1-2
        # RGB-unit jitter, isolated by elimination to the waypoint-create
        # modal's own input/textarea/select fields specifically: Skia's
        # software rasterizer compositing a rounded border-radius corner
        # together with a semi-transparent border color can accumulate a
        # sub-pixel rounding difference in rare cases, even with the GPU
        # disabled - a genuine rendering-engine limitation of "byte-
        # identical," not an app or harness state issue. Flattened corners
        # only inside this one modal's form fields (not globally) - this
        # suite isn't testing border-radius rendering, only color/token
        # wiring, so losing corner rounding here costs nothing real.
        page.add_style_tag(content=".waypoint-create-body input, .waypoint-create-body textarea, .waypoint-create-body select { border-radius: 0 !important; }")

        print("Capturing token snapshot across all 7 combos ...")
        token_snapshot = capture_token_snapshot(page)

        print("Capturing screenshots ...")
        if update_baseline:
            names = capture_screenshots(page, SCREENSHOTS_DIR)
        else:
            tmp_shots = Path(tempfile.mkdtemp(prefix="mc_visual_regression_shots_"))
            names = capture_screenshots(page, tmp_shots)

        browser.close()

    if update_baseline:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        TOKENS_BASELINE.write_text(json.dumps(token_snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote {TOKENS_BASELINE.relative_to(REPO_ROOT)}")
        print(f"Wrote {len(names)} baseline screenshot(s) to {SCREENSHOTS_DIR.relative_to(REPO_ROOT)}")
        return 0

    if not TOKENS_BASELINE.exists():
        print(f"error: no token baseline at {TOKENS_BASELINE} - run with --update-baseline first.", file=sys.stderr)
        return 1
    baseline_tokens = json.loads(TOKENS_BASELINE.read_text(encoding="utf-8"))
    current_serialized = json.loads(json.dumps(token_snapshot, sort_keys=True))
    if current_serialized != baseline_tokens:
        for combo_key in sorted(set(baseline_tokens) | set(current_serialized)):
            base_combo = baseline_tokens.get(combo_key, {})
            cur_combo = current_serialized.get(combo_key, {})
            if base_combo != cur_combo:
                for category in sorted(set(base_combo) | set(cur_combo)):
                    base_cat = base_combo.get(category, {})
                    cur_cat = cur_combo.get(category, {})
                    for key in sorted(set(base_cat) | set(cur_cat)):
                        b, c = base_cat.get(key), cur_cat.get(key)
                        if b != c:
                            failures.append(f"TOKEN MISMATCH [{combo_key}/{category}/{key}]: baseline={b!r} current={c!r}")

    for name in names:
        baseline_png = SCREENSHOTS_DIR / f"{name}.png"
        current_png = tmp_shots / f"{name}.png"
        if not baseline_png.exists():
            failures.append(f"SCREENSHOT MISSING FROM BASELINE: {name} (run --update-baseline if this is intentional)")
            continue
        frac = _pixel_diff_fraction(baseline_png, current_png)
        if frac > PIXEL_DIFF_TOLERANCE:
            failures.append(f"SCREENSHOT MISMATCH [{name}]: {frac:.4%} of pixels differ (tolerance {PIXEL_DIFF_TOLERANCE:.4%})")

    if failures:
        print(f"\nFAIL: {len(failures)} regression(s) found:\n")
        for f in failures:
            print(f"  {f}")
        return 1

    print(f"\nOK: token snapshot matches baseline, all {len(names)} screenshot(s) within tolerance.")
    return 0


def run_contract_check() -> int:
    sys.path.insert(0, str(TOOLS_DIR))
    import contract_validator  # noqa: PLC0415

    print("=== contract_validator.py ===")
    return contract_validator.main([])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-baseline", action="store_true",
                         help="Regenerate the committed baseline instead of checking against it. "
                              "Review the resulting diff like any other change before committing - "
                              "this is the explicit, reviewable way to accept an intentional visual change.")
    parser.add_argument("--skip-contract", action="store_true",
                         help="Skip contract_validator.py (visual/token checks only).")
    args = parser.parse_args(argv)

    exit_code = 0
    if not args.skip_contract and not args.update_baseline:
        exit_code = max(exit_code, run_contract_check())

    exit_code = max(exit_code, run_checks(args.update_baseline))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
