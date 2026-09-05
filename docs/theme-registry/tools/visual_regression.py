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
        page.wait_for_timeout(150)
        name = f"waypoint_create_modal__{family}_{mode}"
        modal = page.locator("#waypointCreateModal .waypoint-create-dialog")
        if modal.count() > 0:
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
        page.screenshot(path=str(out_dir / f"{name}.png"))
        names.append(name)

        devices_tab = page.locator('button:has-text("Devices")').first
        if devices_tab.count() > 0:
            devices_tab.click()
            page.wait_for_timeout(300)
            name2 = f"legacy_devices_view__{family}_{mode}"
            page.screenshot(path=str(out_dir / f"{name2}.png"))
            names.append(name2)
            page.locator('button:has-text("Chats")').first.click()
            page.wait_for_timeout(150)

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
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(base_url + "/")
        page.wait_for_timeout(500)
        # The seeded nodes only reach the DOM once loadMessages() has run -
        # normally triggered by the app's own initial-load flow, but that
        # flow depends on channel/websocket state this hardware-free
        # harness doesn't have, so call it directly.
        page.evaluate("async () => { if (window.loadMessages) await window.loadMessages(); }")
        page.wait_for_timeout(300)
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
