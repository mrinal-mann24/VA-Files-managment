"""
SharePoint storage backend via Microsoft Graph API.

Uploads files directly to the VA Files SharePoint site:
    https://interropac1.sharepoint.com/teams/VAFiles/Shared Documents/<folder>/<filename>

Drop-in replacement for storage.py — same 3 public functions:
    ensure_client_folder(client_name)
    save_media(client_name, url, filename)
    save_text_note(client_name, text)
"""

import re
from datetime import datetime, timezone

import httpx

import config

_token_cache: dict = {}  # {"access_token": ..., "expires_at": ...}

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    # Replace <> and × with " - " (SharePoint forbids < > in folder names)
    name = re.sub(r'\s*<>\s*', ' - ', name)
    name = re.sub(r'\s*×\s*', ' - ', name)
    # Strip SharePoint-illegal characters: " * : < > ? / \ |
    name = re.sub(r'["\*:<>?/\\|]', '', name)
    # Collapse multiple spaces
    name = re.sub(r'  +', ' ', name).strip(". ")
    return name or "unnamed"


def _get_token() -> str:
    """Return a valid Graph API access token, refreshing if expired."""
    import time
    now = time.time()
    if _token_cache.get("access_token") and _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["access_token"]

    url = f"https://login.microsoftonline.com/{config.SHAREPOINT_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": config.SHAREPOINT_CLIENT_ID,
        "client_secret": config.SHAREPOINT_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }
    resp = httpx.post(url, data=data, timeout=15.0)
    resp.raise_for_status()
    result = resp.json()
    _token_cache["access_token"] = result["access_token"]
    _token_cache["expires_at"] = now + result.get("expires_in", 3600)
    print("[sharepoint] Token acquired.")
    return _token_cache["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}"}


def _drive_url(path: str) -> str:
    """Build a Graph API URL for a path inside the VA Files drive."""
    drive_id = config.SHAREPOINT_DRIVE_ID
    return f"{GRAPH_BASE}/drives/{drive_id}{path}"


def ensure_client_folder(client_name: str) -> str:
    """
    Create the client folder in SharePoint if it doesn't exist.
    Returns the folder path string (used for logging).
    """
    folder = _safe_name(client_name)
    root = config.SHAREPOINT_ROOT_FOLDER.strip("/")

    if root:
        # Create root folder first (idempotent)
        _create_folder_if_missing("", root)
        parent_path = f"/{root}"
    else:
        parent_path = ""

    _create_folder_if_missing(parent_path, folder)
    full_path = f"{parent_path}/{folder}" if root else f"/{folder}"
    return full_path


def _create_folder_if_missing(parent_path: str, folder_name: str):
    """POST to Graph to create a folder, silently ignoring nameAlreadyExists."""
    if parent_path:
        url = _drive_url(f"/root:{parent_path}:/children")
    else:
        url = _drive_url("/root/children")

    payload = {
        "name": folder_name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    try:
        resp = httpx.post(url, json=payload, headers=_headers(), timeout=15.0)
        if resp.status_code == 409:
            return  # already exists — fine
        resp.raise_for_status()
        print(f"[sharepoint] Folder created: {parent_path}/{folder_name}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            return  # race condition — already exists
        print(f"[sharepoint] ERROR creating folder '{folder_name}': {e}")
        raise


def _unique_upload_path(folder_path: str, filename: str) -> tuple[str, str]:
    """
    Return (upload_url, final_filename) where the filename won't overwrite
    an existing file. Appends _1, _2 etc. if needed.
    """
    drive_id = config.SHAREPOINT_DRIVE_ID

    def upload_url(name: str) -> str:
        return f"{GRAPH_BASE}/drives/{drive_id}/root:{folder_path}/{name}:/content"

    # Check if file exists
    check_url = f"{GRAPH_BASE}/drives/{drive_id}/root:{folder_path}/{filename}"
    resp = httpx.get(check_url, headers=_headers(), timeout=10.0)
    if resp.status_code == 404:
        return upload_url(filename), filename

    # File exists — find a free suffix
    if "." in filename:
        base, ext = filename.rsplit(".", 1)
        ext = "." + ext
    else:
        base, ext = filename, ""

    i = 1
    while True:
        new_name = f"{base}_{i}{ext}"
        check_url = f"{GRAPH_BASE}/drives/{drive_id}/root:{folder_path}/{new_name}"
        resp = httpx.get(check_url, headers=_headers(), timeout=10.0)
        if resp.status_code == 404:
            return upload_url(new_name), new_name
        i += 1


def save_media(client_name: str, url: str, filename: str | None) -> str | None:
    """Download file from Periskope and upload it to SharePoint."""
    if not url:
        print("[sharepoint] No media URL — skipping.")
        return None

    folder_path = ensure_client_folder(client_name)

    if not filename:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"file_{ts}"

    filename = _safe_name(filename)

    try:
        print(f"[sharepoint] Downloading {url}")
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            file_bytes = resp.content
    except Exception as e:
        print(f"[sharepoint] ERROR downloading media: {e}")
        return None

    try:
        upload_url, final_name = _unique_upload_path(folder_path, filename)
        print(f"[sharepoint] Uploading {len(file_bytes)} bytes -> {folder_path}/{final_name}")
        up_resp = httpx.put(
            upload_url,
            content=file_bytes,
            headers={**_headers(), "Content-Type": "application/octet-stream"},
            timeout=120.0,
        )
        up_resp.raise_for_status()
        result = up_resp.json()
        web_url = result.get("webUrl", "")
        print(f"[sharepoint] Uploaded -> {web_url}")
        return web_url
    except Exception as e:
        print(f"[sharepoint] ERROR uploading to SharePoint: {e}")
        return None


def save_text_note(client_name: str, text: str) -> str | None:
    """Append a timestamped line to the client's notes.txt in SharePoint."""
    if not text:
        return None

    folder_path = ensure_client_folder(client_name)
    drive_id = config.SHAREPOINT_DRIVE_ID
    notes_path = f"{folder_path}/notes.txt"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    new_line = f"[{ts}] {text}\n"

    # Download existing notes.txt if it exists
    existing_content = b""
    get_url = f"{GRAPH_BASE}/drives/{drive_id}/root:{notes_path}:/content"
    try:
        resp = httpx.get(get_url, headers=_headers(), timeout=15.0, follow_redirects=True)
        if resp.status_code == 200:
            existing_content = resp.content
    except Exception:
        pass  # file doesn't exist yet — start fresh

    updated_content = existing_content + new_line.encode("utf-8")

    upload_url = f"{GRAPH_BASE}/drives/{drive_id}/root:{notes_path}:/content"
    try:
        resp = httpx.put(
            upload_url,
            content=updated_content,
            headers={**_headers(), "Content-Type": "text/plain"},
            timeout=30.0,
        )
        resp.raise_for_status()
        print(f"[sharepoint] Note saved -> {notes_path}")
        return notes_path
    except Exception as e:
        print(f"[sharepoint] ERROR saving note: {e}")
        return None
