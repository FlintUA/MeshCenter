"""Tracks which BCM GPIO pins are already claimed, so a display
configuration can be checked for conflicts before it's applied. e-Paper
Stage 1 plan, section 5.

Not a general-purpose GPIO manager - MeshCenter has no other hot-pluggable
GPIO peripheral today. I2C (BME280/INA226, see CLAUDE.md's telemetry
notes) is the only other fixed GPIO consumer that matters, and its pins
are reserved unconditionally below.
"""

from __future__ import annotations

# Fixed by the Pi's dedicated I2C header pins - never available for
# reassignment regardless of what's actually plugged into I2C.
_RESERVED: dict[int, str] = {
    2: "I2C SDA",
    3: "I2C SCL",
}


class GpioConflictError(Exception):
    def __init__(self, pin: int, owner: str, requested_by: str):
        super().__init__(
            f"GPIO{pin} is already used by {owner!r}, cannot assign it to {requested_by!r}"
        )
        self.pin = pin
        self.owner = owner
        self.requested_by = requested_by


class GpioRegistry:
    def __init__(self):
        self._claimed: dict[int, str] = dict(_RESERVED)

    def check(self, pins: dict[str, int], owner: str) -> None:
        """Raise GpioConflictError if any of `pins` (name -> BCM number)
        is already claimed by something other than `owner` itself."""
        for pin in pins.values():
            existing = self._claimed.get(pin)
            if existing is not None and existing != owner:
                raise GpioConflictError(pin, existing, owner)

    def claim(self, pins: dict[str, int], owner: str) -> None:
        """check() then register `pins` under `owner`. Re-claiming the
        same pins for the same owner (e.g. reconfiguring) is fine."""
        self.check(pins, owner)
        for pin in pins.values():
            self._claimed[pin] = owner

    def release(self, owner: str) -> None:
        for pin in [p for p, o in self._claimed.items() if o == owner]:
            del self._claimed[pin]
