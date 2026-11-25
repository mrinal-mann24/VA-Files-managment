"""
naming.py: the ONE rule for a WhatsApp file's name and download URL, shared by the live bot (main.py), backfill.py,
recover_files.py and catchup.py. They used to keep their own copies; when a file had no name, the live bot named it
by UPLOAD time and the backfill by MESSAGE time, so a later catch-up saw a different name and uploaded it twice.
"""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import config

MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}

# Media is only downloaded from Periskope (and the storage it redirects to), never from any other host a payload
# names, so a forged webhook or a bad API row cannot make the server fetch arbitrary URLs.
_ALLOWED_MEDIA_HOSTS = {urlparse(config.PERISKOPE_MEDIA_BASE_URL).netloc.lower(), "storage.googleapis.com"}


def ext_from_mime(mimetype: str) -> str:
    return MIME_TO_EXT.get(mimetype or "", "")


def ensure_ext(name: str, mimetype: str) -> str:
    """Append an extension from the mimetype if the name has none."""
    if name and "." not in os.path.basename(name):
        ext = ext_from_mime(mimetype)
        if ext:
            return f"{name}{ext}"
    return name


def resolve_filename(media_obj: dict, mimetype: str, timestamp: str | None) -> str:
    """
    Name for a WhatsApp file, in priority order:
      1. media.filename / media.file_name (the real WhatsApp filename)
      2. the filename at the end of the media URL path
      3. file_<message time> (the WhatsApp message time, never the upload time, so every run picks the same name)
    Never uses the caption. The extension from the mimetype is always ensured so files open correctly.
    """
    name = media_obj.get("filename") or media_obj.get("file_name")
    if name:
        return ensure_ext(name, mimetype)

    raw_path = media_obj.get("path") or media_obj.get("url") or ""
    if raw_path:
        tail = unquote(raw_path.split("?")[0].rstrip("/").split("/")[-1])
        if tail and "." in tail:
            return ensure_ext(tail, mimetype)

    ts = ""
    if timestamp:
        try:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%Y%m%d_%H%M%S")
        except ValueError:
            ts = ""
    return ensure_ext(f"file_{ts}" if ts else "file", mimetype)


def message_text(msg: dict) -> str:
    """
    Text kept in client_messages for one WhatsApp message (live bot and catch-up use this same rule).
    A file becomes "[File shared: <name>]" followed by its caption, so a file sent with no caption still shows on
    the ticket timeline (for example our signed PDF closing a request). Whether the file itself is saved to
    SharePoint is decided elsewhere; our team's files never are.
    """
    body = (msg.get("body") or "").strip()
    media = msg.get("media") or {}
    if not media:
        return body
    name = resolve_filename(media, media.get("mimetype", ""), msg.get("timestamp"))
    marker = f"[File shared: {name}]"
    return f"{marker} {body}" if body else marker


IST = timezone(timedelta(hours=5, minutes=30))


def dated_subfolder(timestamp: str | None) -> str:
    """Date sub-folder ("YYYY-MM-DD", the WhatsApp message's IST date) for a file, or "" for the client folder itself.
    Only messages on or after config.DATE_FOLDERS_FROM get one, so files saved before the switch keep their place and
    the live bot, catch-up, recovery and backfill always look for a file in the same folder."""
    start = (config.DATE_FOLDERS_FROM or "").strip()
    if not start:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else datetime.now(timezone.utc)
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    day = dt.astimezone(IST).strftime("%Y-%m-%d")
    return day if day >= start else ""


def media_url(path: str) -> str:
    """Full download URL for a Periskope media path, or "" when the host is not Periskope's."""
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        host = urlparse(path).netloc.lower()
        if host in _ALLOWED_MEDIA_HOSTS or host.endswith(".periskope.app"):
            return path
        print(f"[naming] Media host '{host}' is not allowed, not downloading.")
        return ""
    return config.PERISKOPE_MEDIA_BASE_URL.rstrip("/") + path


def url_for_log(url: str) -> str:
    """Host and path only: media and webhook URLs can carry signed tokens in the query string."""
    p = urlparse(url or "")
    return f"{p.netloc}{p.path}" if p.netloc else (p.path or "")
