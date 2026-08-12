# Vendored code notice

`epd2in13g.py`, `epd2in13g_v2.py`, and `epdconfig.py` in this directory are
vendored, near-unmodified copies of Waveshare's official Python demo driver
for the 2.13" 4-color (G) e-Paper HAT (black/white/yellow/red).

- Source: the official downloadable package linked from the product manual
  (https://www.waveshare.com/wiki/2.13inch_e-Paper_HAT_(G)_Manual), fetched
  from https://files.waveshare.com/wiki/2.13inch_e-Paper_HAT_G/2in13_e-Paper_G.zip
- Path within the package: `RaspberryPi_JetsonNano/python/lib/waveshare_epd/`
  - `epd2in13g.py` -> our `epd2in13g.py` (V1 driver, kept for reference)
  - `epd2in13g_V2.py` -> our `epd2in13g_v2.py` (V2 driver, adds quick-refresh
    `init_Fast()`; this is what the manual's Python demo instructions run by
    default, and what `modules/display/drivers/waveshare_213g.py` and
    `tools/test_epaper.py` both use)
  - `epdconfig.py` -> our `epdconfig.py` (shared by both V1 and V2)
- Retrieved: 2026-08-10.
- License: the repository has no root `LICENSE` file (GitHub's own license
  detection reports `null`), but every vendored file carries its own MIT-style
  permission notice in its header comment block, granting free use/copy/modify/
  distribute/sublicense with the notice retained. That per-file notice is left
  intact at the top of all three vendored files below.

## Important: GitHub `master` branch vs. the official ZIP disagree

An earlier version of this directory vendored `epdconfig.py` and `epd2in13g.py`
straight from `github.com/waveshareteam/e-Paper`'s `master` branch. That
`epdconfig.py`'s `RaspberryPi.module_init()` is **missing a BUSY-pin "kick"**
that the official ZIP's copy has:

```python
self.GPIO_BUSY_PIN.close()
self.GPIO_BUSY_PIN    = gpiozero.LED(self.BUSY_PIN)
self.GPIO_BUSY_PIN.on()
self.delay_ms(20)
self.GPIO_BUSY_PIN.close()
self.GPIO_BUSY_PIN   = gpiozero.Button(self.BUSY_PIN, pull_up = False)
```

Both files' header comments claim the same "V1.2 2022-10-29" version string,
so this divergence isn't visible from the version string alone - the GitHub
`master` branch is simply stale relative to Waveshare's own product-page ZIP.
This was diagnosed after the `master`-sourced driver left BUSY (GPIO24)
electrically floating (confirmed via raw gpiozero pull-up/pull-down probing)
and the panel never responded to any command. **Always prefer the official
ZIP from the product manual page over the GitHub `master` branch for this
driver.**

## Modifications from upstream

- None. Pins are the unmodified vendor defaults (`RST=17`, `DC=25`, `CS=8`,
  `BUSY=24`, `PWR=18`) - confirmed against the manual's own "Raspberry Pi
  connection pin correspondence" table, which matches exactly. These are
  now only *defaults*: `modules/display/drivers/waveshare_213g.py`
  reconfigures the vendor code's pins/SPI bus from its own constructor
  config at `start()` time (see that file's `_configure_vendor_pins()`)
  rather than relying on these hardcoded class attributes directly.
- This directory moved here from the Phase 1 standalone-test location
  (`tools/_vendor/waveshare_epd/`) as part of Phase 2 - see
  `modules/display/drivers/base.py` (the `DisplayDriver` interface) and
  `waveshare_213g.py` (the concrete driver wrapping this vendor code).
  `tools/test_epaper.py` (Phase 1) still imports directly from here as a
  minimal-dependency smoke test; `tools/test_epaper_driver.py` (Phase 2)
  exercises the same hardware through `DisplayDriver` instead.
