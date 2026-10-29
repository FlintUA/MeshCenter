"""Takes the README screenshots from the demo server (scripts/demo_screenshots/demo_server.py) and writes them to
docs/images/, recompressed losslessly.

    python scripts/demo_screenshots/demo_server.py &              # fake data only
    python scripts/demo_screenshots/capture.py [http://127.0.0.1:8795] [output dir] [files]

A third argument `files` takes only the Files (MCAttach) image.

Needs Playwright with Microsoft Edge or Chromium (`pip install playwright`), Pillow and zopflipy (lossless PNG
recompression). Before every shot the page text is scrubbed of anything that belongs to the machine the demo runs on
(host name, IP addresses, file paths, the installation id), so no real value can end up in an image.
"""
import io
import os
import pathlib
import platform
import socket
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8795"
OUT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path(__file__).resolve().parents[2] / "docs" / "images"
DESKTOP = {"width": 1600, "height": 1305}
PHONE = {"width": 412, "height": 860}

HOST_NAMES = sorted({socket.gethostname(), platform.node(), os.environ.get("COMPUTERNAME", ""), os.environ.get("USERNAME", ""),
                     os.environ.get("USER", "")} - {""}, key=len, reverse=True)

SCRUB = """(names) => {
    const ip = /\\b\\d{1,3}(\\.\\d{1,3}){3}\\b/g;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
        let text = node.nodeValue;
        const original = text;
        for (const name of names) text = text.split(name).join('demo-host');
        text = text.replace(ip, '192.0.2.10').replace(/MC1-[A-Z0-9-]+/g, 'MC1-DEMO-0000-0000-0000-0000')
                   .replace(/[A-Za-z]:\\\\[^\\s<]*/g, '/opt/meshcenter/data').replace(/\\/(tmp|home|Users)\\/[^\\s<]*/g, '/opt/meshcenter/data');
        if (text !== original) node.nodeValue = text;
    }
    const host = document.getElementById('systemHostname');
    if (host) host.textContent = 'demo-host';
    document.querySelectorAll('[title]').forEach((el) => { el.title = el.title.replace(ip, '192.0.2.10'); });
}"""


def lossless(png_bytes):
    """The same pixels in fewer bytes (zopfli deflate, no palette or colour change)."""
    from zopfli import ZopfliPNG
    packer = ZopfliPNG()
    packer.lossy_transparent = False
    packer.lossy_8bit = False
    packer.iterations = 15
    return packer.optimize(png_bytes)


def save(page_or_bytes, name):
    data = page_or_bytes if isinstance(page_or_bytes, bytes) else page_or_bytes.screenshot(type="png")
    OUT.mkdir(parents=True, exist_ok=True)
    packed = lossless(data)
    # a safety net: the recompressed image must decode to exactly the same pixels
    assert list(Image.open(io.BytesIO(packed)).convert("RGBA").getdata()) == list(Image.open(io.BytesIO(data)).convert("RGBA").getdata())
    (OUT / name).write_bytes(packed)
    print(f"{name}: {len(data) // 1024} KB -> {len(packed) // 1024} KB", flush=True)


def fresh(browser, viewport, scheme, mobile=False):
    context = browser.new_context(
        viewport=viewport, device_scale_factor=1 if not mobile else 2, locale="en-US", color_scheme=scheme,
        has_touch=mobile,
    )
    page = context.new_page()
    page.goto(BASE)
    page.wait_for_function("document.querySelectorAll('.chat-item').length > 0", timeout=30000)
    page.wait_for_timeout(2500)
    return page


def settle(page, ms=1500):
    page.wait_for_timeout(ms)
    page.evaluate(SCRUB, HOST_NAMES)


def open_channel(page):
    page.evaluate("[...document.querySelectorAll('.chat-item')].find(e => /LongFast/.test(e.textContent)).click()")
    page.wait_for_function("document.querySelectorAll('.message').length > 2", timeout=15000)


def capture_files_only():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except Exception:
            browser = p.chromium.launch()
        page = fresh(browser, DESKTOP, "light")
        page.evaluate("switchMainTab('files')")
        page.wait_for_function("document.querySelectorAll('.files-transfer-item').length > 0", timeout=20000)
        page.evaluate("document.querySelector('.files-transfer-item').click()")
        settle(page, 2500)
        save(page, "MeshCenter_files_light_theme.png")
        browser.close()


def main():
    if len(sys.argv) > 3 and sys.argv[3] == "files":
        return capture_files_only()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except Exception:
            browser = p.chromium.launch()

        # 1. Chats, light
        page = fresh(browser, DESKTOP, "light")
        open_channel(page)
        settle(page)
        save(page, "MeshCenter_chats_light_theme.png")

        # 3. Files (MCAttach), light
        page.evaluate("switchMainTab('files')")
        page.wait_for_function("document.querySelectorAll('.files-transfer-item').length > 0", timeout=20000)
        page.evaluate("document.querySelector('.files-transfer-item').click()")
        settle(page, 2500)
        save(page, "MeshCenter_files_light_theme.png")

        # 4. Camera, light (a generated scene, see demo_server.py)
        page.evaluate("switchMainTab('video')")
        page.wait_for_timeout(1500)
        page.evaluate("[...document.querySelectorAll('button')].find(b => /Turn On Camera/i.test(b.textContent)).click()")
        settle(page, 5000)
        save(page, "MeshCenter_camera_light_theme.png")

        # 5. Devices, light
        page.evaluate("switchMainTab('devices')")
        settle(page, 2500)
        save(page, "MeshCenter_devices_light_theme.png")
        page.context.close()

        # 2. Nodes + map split, dark
        page = fresh(browser, DESKTOP, "dark")
        page.evaluate("Workspace.update({themeMode: 'dark'})")
        page.evaluate("typeof setMapLayoutMode === 'function' && setMapLayoutMode('split')")
        page.wait_for_timeout(2500)
        page.evaluate("[...document.querySelectorAll('button')].find(b => /Fit map/i.test(b.textContent)).click()")
        settle(page, 5000)  # map tiles
        save(page, "MeshCenter_map_split_dark_theme.png")

        # 6. System, dark
        page.evaluate("openWorkspacePage('system')")
        page.wait_for_timeout(2500)
        page.evaluate("document.getElementById('updatesCheckBtn').click()")
        page.wait_for_timeout(8000)
        # start at the Updates card: the Network card above lists connection details that mean nothing in a demo
        page.evaluate("document.getElementById('updatesCheckBtn').closest('.system-card').scrollIntoView({block: 'start'})")
        settle(page, 800)
        save(page, "MeshCenter_system_dark_theme.png")
        page.context.close()

        # 7. Phone: chat list, a conversation, the Nodes page (dark)
        shots = []
        page = fresh(browser, PHONE, "light", mobile=True)
        page.evaluate("typeof showChatList === 'function' && showChatList()")
        settle(page, 1200)
        shots.append(page.screenshot(type="png"))
        open_channel(page)
        settle(page, 1500)
        shots.append(page.screenshot(type="png"))
        page.context.close()
        page = fresh(browser, PHONE, "dark", mobile=True)
        page.evaluate("Workspace.update({themeMode: 'dark'}); MCMobileLayout.setPane('nodes')")
        settle(page, 1800)
        shots.append(page.screenshot(type="png"))
        page.context.close()
        browser.close()

    canvas = Image.new("RGB", (DESKTOP["width"], DESKTOP["height"]), (233, 238, 245))
    height = 1030
    images = [Image.open(io.BytesIO(raw)).convert("RGB") for raw in shots]
    scaled = [im.resize((round(im.width * height / im.height), height), Image.LANCZOS) for im in images]
    gap = 40
    total = sum(im.width for im in scaled) + gap * (len(scaled) - 1)
    x = (canvas.width - total) // 2
    y = (canvas.height - height) // 2
    for im in scaled:
        canvas.paste(im, (x, y))
        x += im.width + gap
    buffer = io.BytesIO()
    canvas.save(buffer, "PNG")
    save(buffer.getvalue(), "MeshCenter_phone.png")


if __name__ == "__main__":
    main()
