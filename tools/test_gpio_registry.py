"""Unit test (no hardware, no HTTP) for GpioRegistry's release-before-check
invariant used by api/api_hardware_display.py's /reinit route (e-Paper
Stage 2 plan, Phase 5 follow-up). claim()/release() are deterministic
in-process dict operations - unlike BUSY polarity or wiring, nothing here
can behave differently on real hardware than in this test, so this is
exercised directly against GpioRegistry rather than over the network
against a running server (same approach as
tools/test_display_manager.py's Part 3 pinned-deadline test).

Run anywhere with the venv active, no dev-node SSH needed:

    python3 tools/test_gpio_registry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_old_claim_survives_rejected_reinit() -> bool:
    from modules.display.gpio_registry import GpioConflictError, GpioRegistry

    registry = GpioRegistry()

    # Step 1: claim pins under the "old" model, same as a running driver's
    # start() would.
    old_pins = {"rst": 17, "dc": 25, "cs": 8, "busy": 24}
    registry.claim(old_pins, owner="waveshare_213g")
    assert registry.get_owner(17) == "waveshare_213g"

    # Step 2: reproduce api_hardware_display.py's /reinit sequence -
    # release the old model's claim, then check the new model's pins,
    # where one of them (GPIO2) is reserved by something else entirely
    # (I2C), not the model being replaced.
    registry.release("waveshare_213g")
    conflict_raised = False
    try:
        registry.check({"dc": 2}, owner="weact_154")
    except GpioConflictError as exc:
        conflict_raised = True
        # Restore the old (untouched, still-running) driver's claim -
        # this is the fix under test.
        registry.claim(old_pins, owner="waveshare_213g")

    if not conflict_raised:
        print("FAIL: expected GpioConflictError, none was raised")
        return False

    # Step 3: the old model's claim must have survived the rejected
    # attempt, not been silently lost.
    owner = registry.get_owner(17)
    if owner != "waveshare_213g":
        print(f"FAIL: GPIO17 owner after rejected reinit = {owner!r}, expected 'waveshare_213g'")
        return False

    print("PASS: old model's claim survived the rejected reinit attempt")
    return True


def main() -> int:
    ok = test_old_claim_survives_rejected_reinit()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
