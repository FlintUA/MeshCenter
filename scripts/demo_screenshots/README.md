# README screenshots

`demo_server.py` runs the real app on a throwaway data folder filled with invented data (names like "Demo Relay North",
ids `!0000d001`, positions around a public landmark, a generated landscape as the camera picture; no real node, key,
callsign, address or photograph). `capture.py` takes the images in `docs/images/` with Playwright, scrubs host names,
IP addresses, paths and the installation id from the page text first, and recompresses every PNG losslessly (zopfli),
checking that the pixels are identical.

```bash
pip install -r requirements-dev.txt playwright zopflipy
python scripts/demo_screenshots/demo_server.py &
python scripts/demo_screenshots/capture.py            # writes docs/images/MeshCenter_*.png
```

Needs Microsoft Edge or Chromium for Playwright. Look at every image before committing it.
