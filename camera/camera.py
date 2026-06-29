#!/usr/bin/env python3
"""
Camera manager for MeshCenter.

This module contains all Raspberry Pi camera, MJPEG video, photo capture,
settings and screenshot gallery logic. Flask routes stay in server.py.
"""

import base64
import io
import json
import os
import threading
import time
from datetime import datetime
from tkinter import Image

try:
    from config import DATA_DIR
except ImportError:
    DATA_DIR = "data"

# ============================================================
# CAMERA PATHS
# ============================================================

SCREENSHOTS_DIR = os.path.join(DATA_DIR, "screenshots")
CAMERA_CONFIG_FILE = os.path.join(DATA_DIR, "camera_config.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# ============================================================
# CAMERA CONFIG
# ============================================================

VIDEO_CONFIG = {
    "resolution": "640x480",
    "fps": 12,
    "quality": 75,
}

VIDEO_MODES = {
    "low": {"resolution": "320x240", "fps": 8, "quality": 60},
    "medium": {"resolution": "480x320", "fps": 10, "quality": 70},
    "high": {"resolution": "640x480", "fps": 12, "quality": 75},
    "hd": {"resolution": "1280x720", "fps": 15, "quality": 70},
}

RESOLUTIONS = [
    "320x240", "480x320", "640x480",
    "800x600", "1024x768", "1280x720",
    "1280x960", "1920x1080",
]

FPS_OPTIONS = [5, 8, 10, 12, 15, 20, 24, 30]

PHOTO_PREVIEW_CONFIG = {
    "resolution": "640x480",
    "quality": 85,
}

PHOTO_SAVE_CONFIG = {
    "resolution": "2592x1944",
    "quality": 95,
}

PHOTO_CONFIG = PHOTO_PREVIEW_CONFIG.copy()
PHOTO_PREVIEW_RESOLUTIONS = ["640x480", "1280x720", "1920x1080", "2592x1944"]
PHOTO_SAVE_RESOLUTION = "2592x1944"

# ============================================================
# CAMERA STATE
# ============================================================

CAMERA_AVAILABLE = False
CAMERA_MODE = "video"
CAMERA_ACTIVE = False

picam2 = None
camera_started = False
camera_lock = threading.RLock()
last_frame = None
last_frame_time = 0

# ============================================================
# JSON HELPERS
# ============================================================

def safe_read_json(filepath, default=None):
    if default is None:
        default = {}

    tmp_file = filepath + ".tmp"
    if os.path.exists(tmp_file):
        try:
            os.remove(tmp_file)
            print(f"[JSON] Removed stale tmp file: {tmp_file}", flush=True)
        except Exception as e:
            print(f"[JSON] Could not remove tmp file: {e}", flush=True)

    if not os.path.exists(filepath):
        return default

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[JSON] Read error: {e}, using default", flush=True)
        return default


def safe_write_json(filepath, data):
    tmp_file = filepath + ".tmp"
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, filepath)
        return True
    except Exception as e:
        print(f"[JSON] Write error: {e}", flush=True)
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except Exception:
            pass
        return False

# ============================================================
# SETTINGS
# ============================================================

def load_camera_settings():
    """Load camera settings from JSON file."""
    data = safe_read_json(CAMERA_CONFIG_FILE, {})
    if not data:
        return

    if "video" in data:
        VIDEO_CONFIG.update(data["video"])
    if "photo_preview" in data:
        PHOTO_CONFIG.update(data["photo_preview"])
    if "photo_save" in data:
        PHOTO_SAVE_CONFIG.update(data["photo_save"])

    print(
        f"[CAMERA] Loaded settings: preview={PHOTO_CONFIG['resolution']}@{PHOTO_CONFIG['quality']}%, "
        f"save={PHOTO_SAVE_CONFIG['resolution']}",
        flush=True,
    )


def save_camera_settings():
    data = {
        "video": VIDEO_CONFIG,
        "photo_preview": PHOTO_CONFIG,
        "photo_save": PHOTO_SAVE_CONFIG,
    }
    safe_write_json(CAMERA_CONFIG_FILE, data)
    print("[CAMERA] Saved settings", flush=True)

# ============================================================
# BASIC CAMERA CONTROL
# ============================================================

def fix_camera_colors(frame):
    """Convert BGR to RGB if needed."""
    if frame is not None and getattr(frame, "ndim", 0) == 3 and frame.shape[2] == 3:
        return frame[:, :, ::-1]
    return frame


def init_camera():
    """Initialize camera through Picamera2."""
    global CAMERA_AVAILABLE, picam2

    print("[CAMERA] 🔍 Initializing OV5647...", flush=True)

    try:
        from picamera2 import Picamera2

        print("[CAMERA] ✅ Picamera2 imported", flush=True)
        picam2 = Picamera2()

        props = picam2.camera_properties
        if props:
            print("[CAMERA] ✅ Camera found: OV5647", flush=True)
            CAMERA_AVAILABLE = True
            return True

        print("[CAMERA] ❌ No camera properties", flush=True)
        CAMERA_AVAILABLE = False
        return False

    except Exception as e:
        print(f"[CAMERA] ❌ Init error: {e}", flush=True)
        CAMERA_AVAILABLE = False
        return False


def stop_camera():
    """Safely stop camera."""
    global camera_started, CAMERA_ACTIVE

    with camera_lock:
        if camera_started and picam2 is not None:
            try:
                picam2.stop()
                print("[CAMERA] Stopped", flush=True)
            except Exception as e:
                print(f"[CAMERA] Stop error: {e}", flush=True)
        camera_started = False
        CAMERA_ACTIVE = False
        return True


def switch_camera_mode(mode, resolution=None, fps=None):
    """Switch camera mode to video or photo."""
    global camera_started, CAMERA_MODE, CAMERA_ACTIVE

    if mode not in ["video", "photo"]:
        return False
    if not CAMERA_AVAILABLE or picam2 is None:
        return False

    with camera_lock:
        stop_camera()
        try:
            if mode == "video":
                w, h = map(int, (resolution or VIDEO_CONFIG["resolution"]).split("x"))
                fps_val = fps or VIDEO_CONFIG["fps"]

                config = picam2.create_preview_configuration(
                    main={"size": (w, h), "format": "RGB888"},
                    controls={"FrameRate": fps_val},
                )
                picam2.configure(config)
                picam2.start()
                camera_started = True
                CAMERA_MODE = "video"
                CAMERA_ACTIVE = True
                print(f"[CAMERA] Video mode: {w}x{h} @ {fps_val} fps", flush=True)
                return True

            w, h = map(int, (resolution or PHOTO_CONFIG["resolution"]).split("x"))
            config = picam2.create_still_configuration(
                main={"size": (w, h), "format": "RGB888"}
            )
            picam2.configure(config)
            picam2.start()
            camera_started = True
            CAMERA_MODE = "photo"
            CAMERA_ACTIVE = True
            print(f"[CAMERA] Photo mode: {w}x{h}", flush=True)
            return True

        except Exception as e:
            print(f"[CAMERA] Switch mode error: {e}", flush=True)
            camera_started = False
            CAMERA_ACTIVE = False
            return False


def start_camera():
    """Start camera in video mode."""
    if not CAMERA_AVAILABLE:
        return False

    with camera_lock:
        if camera_started and CAMERA_MODE == "video":
            return True
        return switch_camera_mode("video")


def get_camera_frame():
    """Capture one camera frame."""
    global last_frame, last_frame_time, camera_started

    if not camera_started:
        if not start_camera():
            return None

    with camera_lock:
        try:
            if picam2 is None:
                return last_frame

            frame = picam2.capture_array()
            if frame is not None and frame.size > 0:
                frame = fix_camera_colors(frame)
                last_frame = frame
                last_frame_time = time.time()
                return frame

            return last_frame

        except Exception as e:
            print(f"[CAMERA] Frame error: {e}", flush=True)
            camera_started = False
            return last_frame

# ============================================================
# VIDEO STREAM
# ============================================================

def generate_mjpeg_stream():
    """Generate MJPEG stream with PIL, no OpenCV."""
    if not start_camera():
        print("[CAMERA] ❌ Cannot start camera", flush=True)
        return

    from PIL import Image

    frame_interval = 1.0 / VIDEO_CONFIG["fps"]
    last_send_time = 0
    quality = VIDEO_CONFIG["quality"]

    print(
        f"[CAMERA] 🎥 MJPEG stream started: {VIDEO_CONFIG['resolution']} @ {VIDEO_CONFIG['fps']} fps",
        flush=True,
    )

    while True:
        try:
            current_time = time.time()
            if current_time - last_send_time < frame_interval:
                time.sleep(0.01)
                continue

            frame = get_camera_frame()
            if frame is None:
                time.sleep(0.05)
                continue

            img = Image.fromarray(frame)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            jpeg_data = buf.getvalue()

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache\r\n\r\n" + jpeg_data + b"\r\n"
            )
            last_send_time = current_time

        except GeneratorExit:
            break
        except Exception as e:
            print(f"[CAMERA] Stream error: {e}", flush=True)
            time.sleep(1)

# ============================================================
# SCREENSHOTS / GALLERY
# ============================================================

def capture_screenshot():
    """Create screenshot from current video frame."""
    if not camera_started:
        if not start_camera():
            return {"success": False, "error": "Camera not ready"}

    try:
        from PIL import Image

        frame = get_camera_frame()
        if frame is None:
            return {"success": False, "error": "Failed to capture frame"}

        img = Image.fromarray(frame)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.jpg"
        filepath = os.path.join(SCREENSHOTS_DIR, filename)
        quality = VIDEO_CONFIG.get("quality", 90)
        img.save(filepath, "JPEG", quality=quality)

        return {
            "success": True,
            "filename": filename,
            "filepath": filepath,
            "size": os.path.getsize(filepath),
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def screenshot_exists(filename):
    return os.path.exists(os.path.join(SCREENSHOTS_DIR, filename))


def list_screenshots():
    try:
        if not os.path.exists(SCREENSHOTS_DIR):
            return {"screenshots": []}, 200

        files = []
        for filename in sorted(os.listdir(SCREENSHOTS_DIR), reverse=True):
            if not filename.endswith(".jpg"):
                continue
            filepath = os.path.join(SCREENSHOTS_DIR, filename)
            stat = os.stat(filepath)
            files.append({
                "filename": filename,
                "size": stat.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "url": f"/api/camera/screenshot/{filename}",
            })
        return {"screenshots": files}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


def delete_screenshot(filename):
    filepath = os.path.join(SCREENSHOTS_DIR, filename)
    if not os.path.exists(filepath):
        return {"ok": False, "error": "File not found"}, 404
    os.remove(filepath)
    return {"ok": True, "message": f"Deleted {filename}"}, 200


def delete_all_screenshots():
    if not os.path.exists(SCREENSHOTS_DIR):
        return {"ok": True, "message": "No screenshots to delete"}, 200

    count = 0
    for filename in os.listdir(SCREENSHOTS_DIR):
        if filename.endswith(".jpg"):
            os.remove(os.path.join(SCREENSHOTS_DIR, filename))
            count += 1
    return {"ok": True, "deleted_count": count}, 200

# ============================================================
# API-LIKE HELPERS FOR SERVER ROUTES
# ============================================================

def get_camera_status():
    return {
        "ok": CAMERA_AVAILABLE,
        "started": camera_started,
        "mode": CAMERA_MODE,
        "resolution": VIDEO_CONFIG["resolution"],
        "fps": VIDEO_CONFIG["fps"],
        "quality": VIDEO_CONFIG["quality"],
        "available_resolutions": RESOLUTIONS,
        "available_fps": FPS_OPTIONS,
        "video_modes": VIDEO_MODES,
    }


def api_switch_mode(data):
    mode = data.get("mode", "video")
    if mode not in ["video", "photo"]:
        return {"ok": False, "error": "Invalid mode"}, 400

    ok = switch_camera_mode(mode)
    if not ok:
        return {"ok": False, "error": "Failed to switch camera mode"}, 500

    resolution = VIDEO_CONFIG["resolution"] if mode == "video" else PHOTO_CONFIG["resolution"]
    return {"ok": True, "mode": mode, "resolution": resolution}, 200


def get_camera_settings():
    return {
        "ok": True,
        "config": VIDEO_CONFIG,
        "available_resolutions": RESOLUTIONS,
        "available_fps": FPS_OPTIONS,
        "video_modes": VIDEO_MODES,
    }


def update_camera_settings(data):
    changes = {}

    if "resolution" in data:
        res = data["resolution"]
        if res not in RESOLUTIONS:
            return {"ok": False, "error": f"Invalid resolution. Available: {RESOLUTIONS}"}, 400
        changes["resolution"] = res

    if "fps" in data:
        fps = int(data["fps"])
        if fps not in FPS_OPTIONS:
            return {"ok": False, "error": f"Invalid FPS. Available: {FPS_OPTIONS}"}, 400
        changes["fps"] = fps

    if "quality" in data:
        quality = int(data["quality"])
        if not 40 <= quality <= 90:
            return {"ok": False, "error": "Quality must be between 40 and 90"}, 400
        changes["quality"] = quality

    if changes:
        with camera_lock:
            stop_camera()
            VIDEO_CONFIG.update(changes)
            save_camera_settings()
            start_camera()
        print(f"[CAMERA] ✅ Settings updated: {changes}", flush=True)

    return {"ok": True, "config": VIDEO_CONFIG, "changes": changes}, 200


def set_video_mode(mode):
    if mode not in VIDEO_MODES:
        return {"ok": False, "error": f"Invalid mode. Available: {list(VIDEO_MODES.keys())}"}, 400

    config = VIDEO_MODES[mode]
    with camera_lock:
        stop_camera()
        VIDEO_CONFIG.update(config)
        save_camera_settings()
        start_camera()

    print(f"[CAMERA] ✅ Switched to mode: {mode} ({config['resolution']} @ {config['fps']} fps)", flush=True)
    return {"ok": True, "mode": mode, "config": VIDEO_CONFIG}, 200

# ============================================================
# PHOTO
# ============================================================

def get_photo_settings():
    return {
        "ok": True,
        "config": PHOTO_CONFIG,
        "save_config": PHOTO_SAVE_CONFIG,
        "available_resolutions": PHOTO_PREVIEW_RESOLUTIONS,
        "save_resolution": PHOTO_SAVE_RESOLUTION,
    }


def update_photo_settings(data):
    changes = {}

    if "resolution" in data:
        res = data["resolution"]
        if res not in PHOTO_PREVIEW_RESOLUTIONS:
            return {"ok": False, "error": f"Invalid resolution. Available: {PHOTO_PREVIEW_RESOLUTIONS}"}, 400
        changes["resolution"] = res

    if "quality" in data:
        quality = int(data["quality"])
        if not 70 <= quality <= 100:
            return {"ok": False, "error": "Quality must be between 70 and 100"}, 400
        changes["quality"] = quality

    if changes:
        PHOTO_CONFIG.update(changes)
        save_camera_settings()
        print(f"[PHOTO] ✅ Settings updated: {changes}", flush=True)

    return {"ok": True, "config": PHOTO_CONFIG, "changes": changes}, 200


def _capture_still(resolution, quality, log_prefix="[PHOTO]"):
    from PIL import Image

    w, h = map(int, resolution.split("x"))
    print(f"{log_prefix} Capturing: {w}x{h}, quality={quality}%", flush=True)

    ok = switch_camera_mode("photo", resolution=resolution)
    if not ok:
        raise RuntimeError("Failed to switch camera to photo mode")

    time.sleep(0.5)
    frame = picam2.capture_array()
    frame = fix_camera_colors(frame)
    return Image.fromarray(frame), w, h


def capture_photo_preview():
    if not CAMERA_AVAILABLE:
        return {"ok": False, "error": "Camera not available"}, 503

    try:
        quality = PHOTO_CONFIG.get("quality", 85)
        img, w, h = _capture_still(PHOTO_CONFIG["resolution"], quality, "[PHOTO PREVIEW]")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        jpeg_data = base64.b64encode(buf.getvalue()).decode("utf-8")

        return {
            "ok": True,
            "image_data": jpeg_data,
            "width": w,
            "height": h,
            "preview_resolution": PHOTO_CONFIG["resolution"],
            "quality": quality,
            "mode": "photo",
        }, 200

    except Exception as e:
        print(f"[PHOTO] ❌ Capture error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            stop_camera()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}, 500


def save_highres_photo():
    if not CAMERA_AVAILABLE:
        return {"ok": False, "error": "Camera not available"}, 503

    try:
        from PIL import Image

        quality = PHOTO_SAVE_CONFIG["quality"]
        resolution = PHOTO_SAVE_CONFIG["resolution"]
        img, _w, _h = _capture_still(resolution, quality, "[PHOTO SAVE]")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"photo_{timestamp}.jpg"
        filepath = os.path.join(SCREENSHOTS_DIR, filename)
        img.save(filepath, "JPEG", quality=quality)

        preview_img = img.copy()
        preview_img.thumbnail((640, 480), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        preview_img.save(buf, format="JPEG", quality=85)
        preview_data = base64.b64encode(buf.getvalue()).decode("utf-8")

        # Return to photo preview mode, not to video mode.
        try:
            switch_camera_mode("photo", resolution=PHOTO_CONFIG["resolution"])
            print(f"[PHOTO] Returned to preview mode: {PHOTO_CONFIG['resolution']}", flush=True)
        except Exception as e:
            print(f"[PHOTO] Could not return to preview mode: {e}", flush=True)

        return {
            "ok": True,
            "filename": filename,
            "filepath": filepath,
            "size": os.path.getsize(filepath),
            "url": f"/api/camera/screenshot/{filename}",
            "preview_data": preview_data,
            "resolution": resolution,
        }, 200

    except Exception as e:
        print(f"[PHOTO] ❌ Save error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            stop_camera()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}, 500
