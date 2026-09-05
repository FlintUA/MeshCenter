# Bundled test font

`Roboto-Regular.ttf` - downloaded from Google Fonts
(`fonts.gstatic.com`), licensed under the Apache License 2.0
(`LICENSE-Roboto.txt` in this directory).

Used only by `docs/theme-registry/tools/visual_regression.py`, embedded
as a base64 `data:` URI and force-applied to every element during
screenshot capture, so text renders with the exact same font *file*
(not just the same font *name*) regardless of what the host machine has
installed. See `docs/theme-registry/visual-regression-README.md`'s "Font
pinning" section for the full reasoning. Never loaded by the real app -
`static/*.css` does not reference this file.
