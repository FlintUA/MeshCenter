"""JSON storage helpers for MeshCenter."""

import json
import os


def safe_read_json(filepath, default=None):
    """Safely read JSON and remove stale temporary files."""
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
    """Safely write JSON atomically through a temporary file."""
    tmp_file = filepath + ".tmp"
    try:
        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)

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


def atomic_write_json(filepath, data):
    """Backward-compatible alias."""
    return safe_write_json(filepath, data)
