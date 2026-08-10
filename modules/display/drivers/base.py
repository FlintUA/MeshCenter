"""Abstract base for physical e-paper/e-ink display drivers.

Extends the project's existing DeviceDriver lifecycle
(devices/device_driver.py) with display-specific operations, the same way
camera/camera_driver.py's CameraDriver extends it for cameras - see that
file's docstring for the sibling shape this mirrors.

Method mapping vs the e-Paper Stage 1 plan's section 7/8 naming (probe /
init / render / sleep / clear / shutdown / get_status): kept consistent
with DeviceDriver's existing detect/start/stop verbs instead of introducing
new ones for the same concepts.
    probe    -> detect()
    init     -> start()
    shutdown -> stop()
render(), clear() and sleep() are new, display-only abstracts - cameras
have no equivalent concept.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from devices.device_driver import DeviceDriver


@dataclass(frozen=True)
class DisplayCapabilities:
    width: int
    height: int
    colors: tuple[str, ...]  # e.g. ("black", "white", "yellow", "red")
    supports_fast_refresh: bool = False


class DisplayDriver(DeviceDriver):
    device_type = "display"

    @property
    @abstractmethod
    def capabilities(self) -> DisplayCapabilities:
        """Static hardware capabilities - available without the device
        being started."""
        ...

    @abstractmethod
    def render(self, image: Any, fast: bool = False) -> None:
        """Push a fully-composed image (e.g. PIL.Image, matching
        capabilities' width/height) to the panel and physically refresh
        it. `fast` requests a quick/partial refresh when
        capabilities.supports_fast_refresh is True; drivers that don't
        support it should ignore the flag rather than raise."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Clear the panel to blank/white and refresh."""
        ...

    @abstractmethod
    def sleep(self) -> None:
        """Put the panel into low-power sleep. start() must be called
        again before the next render()/clear()."""
        ...
