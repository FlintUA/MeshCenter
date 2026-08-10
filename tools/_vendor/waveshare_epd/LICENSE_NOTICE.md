# Vendored code notice

`epd2in13g.py` and `epdconfig.py` in this directory are vendored, near-unmodified
copies of Waveshare's official Python demo driver for the 2.13" 4-color (G) e-Paper
HAT (black/white/yellow/red).

- Source: https://github.com/waveshareteam/e-Paper (repo moved from the older
  `github.com/waveshare/e-Paper` URL referenced in the original task doc; GitHub
  redirects the old URL to this one).
- Path: `RaspberryPi_JetsonNano/python/lib/waveshare_epd/{epd2in13g.py,epdconfig.py}`
- Retrieved: 2026-08-10, from the `master` branch.
- License: the repository has no root `LICENSE` file (GitHub's own license
  detection reports `null`), but every vendored file carries its own MIT-style
  permission notice in its header comment block, granting free use/copy/modify/
  distribute/sublicense with the notice retained. That per-file notice is left
  intact at the top of both vendored files below.

## Modifications from upstream

- None. The display on the dev node (`192.168.2.104`) is connected directly
  via the 40-pin HAT connector (confirmed 2026-08-10), so the vendor's
  standard `RaspberryPi` pin defaults (`RST=17`, `DC=25`, `CS=8`, `BUSY=24`,
  `PWR=18`) apply unmodified. An earlier revision of this file overrode
  `DC_PIN` to `23` based on a custom-wiring reference pinout in the e-Paper
  Stage 1 plan; that assumption no longer applies once wired via the HAT.
- This location (`tools/_vendor/`) is temporary, Phase 1 (standalone hardware
  test) only. Phase 2 moves/wraps this behind `modules/display/drivers/` and
  makes pins configurable instead of hardcoded class attributes.
