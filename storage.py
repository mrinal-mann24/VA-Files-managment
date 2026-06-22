"""
Local-disk storage backend for testing.

Files are saved to:
    ./storage/<client_name>/<filename>
    ./storage/<client_name>/notes.txt
"""

import os
import re
from datetime import datetime, timezone

import httpx

import config

# Periskope media is stored in a public Google Storage bucket — no auth needed
_PERISKOPE_HEADERS = {}


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    # Replace <> and × separators with " - " (Windows forbids < and > in paths)
    # e.g. "Eiffel Landmarks<> VA" -> "Eiffel Landmarks - VA"
    # e.g. "Chemzeal x virtual accounting" stays as-is (lowercase x is fine)
    name = re.sub(r'\s*<>\s*', ' - ', name)
    name = re.sub(r'\s*×\s*', ' - ', name)  # Unicode multiplication sign
    # Strip remaining Windows-illegal characters
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    # Collapse multiple spaces
    name = re.sub(r'  +', ' ', name).strip(". ")
    return name or "unnamed"


def ensure_client_folder(client_name: str) -> str:
    """Create ./storage/<client_name>/ if it doesn't exist. Returns the path."""
    folder = os.path.join(config.STORAGE_ROOT, _safe_name(client_name))
    os.makedirs(folder, exist_ok=True)
    return folder


def _unique_path(folder: str, filename: str) -> str:
    """Return a file path that doesn't already exist, adding _1, _2 suffix if needed."""
    filename = _safe_name(filename)
    candidate = os.path.join(folder, filename)
    if not os.path.exists(candidate):
        return candidate

    if "." in filename:
        base, ext = filename.rsplit(".", 1)
        ext = "." + ext
    else:
        base, ext = filename, ""

    i = 1
    while True:
        candidate = os.path.join(folder, f"{base}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def save_media(client_name: str, url: str, filename: str | None) -> str | None:
    """Download a file from Periskope and save it to the client's local folder."""
    if not url:
        print("[storage] No media URL — skipping.")
        return None

    folder = ensure_client_folder(client_name)

    if not filename:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"file_{ts}"

    dest = _unique_path(folder, filename)

    try:
        print(f"[storage] Downloading -> {dest}")
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url, headers=_PERISKOPE_HEADERS)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(resp.content)
    except Exception as e:
        print(f"[storage] ERROR downloading media: {e}")
        return None

    print(f"[storage] Saved {os.path.getsize(dest)} bytes -> {dest}")
    return dest


def save_text_note(client_name: str, text: str) -> str | None:
    """Append a timestamped line to the client's notes.txt."""
    if not text:
        return None

    folder = ensure_client_folder(client_name)
    notes_path = os.path.join(folder, "notes.txt")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {text}\n"

    try:
        with open(notes_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[storage] ERROR writing note: {e}")
        return None

    print(f"[storage] Note saved -> {notes_path}")
    return notes_path
