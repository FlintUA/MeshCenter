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

- `epdconfig.py`: `RaspberryPi.DC_PIN` changed from the vendor default `25` to
  `23`, matching this project's confirmed physical wiring on the dev node
  (`192.168.2.104`) per the e-Paper Stage 1 plan's reference pinout. All other
  pins (`RST=17`, `CS=8`, `BUSY=24`, `PWR=18`) are left at vendor defaults,
  pending physical confirmation — see `tools/test_epaper.py`'s docstring.
- This location (`tools/_vendor/`) is temporary, Phase 1 (standalone hardware
  test) only. Phase 2 moves/wraps this behind `modules/display/drivers/` and
  makes pins configurable instead of hardcoded class attributes.
