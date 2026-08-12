# Provenance notice: `ssd1681.py`

Unlike Stage 1's Waveshare driver (vendored near-verbatim from a per-file
MIT-licensed source), `ssd1681.py` in this directory is **original code**,
not a copy of WeAct Studio's reference driver.

## Why not vendor it like last time

WeAct Studio's repository
(`github.com/WeActStudio/WeActStudio.EpaperModule`, retrieved as a ZIP
from the product page 2026-08-12) has:
- No `LICENSE` file at the repo root.
- No per-file license header in the C source (unlike Waveshare's files,
  which each carried an embedded MIT-style permission notice - see Stage
  1's `modules/display/drivers/vendor/waveshare_epd/LICENSE_NOTICE.md`).

Without an explicit grant, copying their C source text (even translated
to Python) would be legally murkier than Stage 1's vendoring. Register
values, pin numbers, and protocol timing are facts, not copyrightable
expression - the SSD1681 controller's command set is a public,
datasheet-documented protocol, not proprietary to WeAct. So this module
independently implements that public protocol, using WeAct's example only
as a *factual reference* for details a datasheet alone won't tell you
(which pins, which SPI mode, which polarity this specific board actually
uses) - the same category of fact-checking Stage 1 did against Waveshare's
manual, just without also copying code text this time.

## What was cross-referenced (facts, not text)

Source: `Example/EpaperModuleTest_RaspberryPi/epaper/epaper.c` (`EPD154`
branch) in the WeAct ZIP above.

- Pins (BCM, same physical 40-pin HAT header as Stage 1's Waveshare
  panel): `RST=17`, `DC=25`, `CS=8` (hardware CE0 - no manual CS
  toggling), `BUSY=24`. No PWR/EN pin on this board.
- SPI: mode 3 (CPOL=1, CPHA=1), 1MHz. (Stage 1's Waveshare panel used
  mode 0 / 4MHz - do not assume these carry over between panels.)
- BUSY polarity: HIGH = busy, LOW = idle. This is the **opposite** of
  Stage 1's Waveshare 2.13g panel, which uses a different, non-SSD1681
  controller. Not yet physically confirmed on the actual dev cable - see
  `tools/test_epaper_weact.py`'s raw BUSY diagnostic, which prints BUSY's
  actual level during reset/power-up before the real init sequence ever
  runs, specifically so this assumption gets checked against real
  hardware instead of silently trusted.
- Full-refresh init/update/sleep register sequence (0x12 SWRESET, 0x01
  driver output control, 0x11 data entry mode, 0x44/0x45 RAM address
  window, 0x3C border waveform, 0x18 temp sensor, 0x4E/0x4F RAM counter,
  0x22+0x20 display-update-sequence pairs with values 0xF8/power-on,
  0xF4/full-update, 0x83/power-off, 0x10/deep-sleep) - this is the
  standard SSD1681 sequence, corroborated (not just assumed) by matching
  WeAct's own EPD154 branch byte-for-byte.

## What's still unconfirmed

The controller is not chip-photo-confirmed as SSD1681 - the command set
matching the standard SSD1681 register map is strong corroborating
evidence, not direct visual confirmation. If Phase 1's standalone test
fails in a way that doesn't match "wrong pin" or "polarity" (the two
usual suspects per Stage 1's playbook), a chip photo becomes the next
diagnostic step.
