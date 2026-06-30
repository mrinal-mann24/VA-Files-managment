"""
backfill.py — One-time script to fetch all historical files from VA WhatsApp groups.

What it does:
  1. Calls Periskope API to list all chats.
  2. Filters to groups whose name contains "<> VA" or "<> Virtual Accounting".
  3. Creates a local storage folder for each group (same name as the group).
  4. Pages through every message in the group and downloads all media files.
  5. Text messages are appended to notes.txt in each folder.

Run once from the project root:
    python backfill.py

Or resume / re-run safely — existing files are never overwritten (same _1, _2 suffix
logic as the live bot). Already-downloaded files won't be re-fetched.
"""

import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx

import config
import db
import storage_sharepoint as storage

# ---------------------------------------------------------------------------
# Periskope API helpers
# ---------------------------------------------------------------------------

_HEADERS = {
    "authorization": f"Bearer {config.PERISKOPE_API_KEY}",
    "x-phone": config.PERISKOPE_PHONE,
}

# Reuse the exact same VA-group patterns the live bot uses, so backfill and
# main.py never disagree about what counts as a VA group.
_CLIENT_GROUP_PATTERNS = db._CLIENT_GROUP_PATTERNS

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


def _is_va_group(name: str) -> bool:
    return any(p.search(name or "") for p in _CLIENT_GROUP_PATTERNS)


def _build_media_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return config.PERISKOPE_MEDIA_BASE_URL.rstrip("/") + path


def _ext_from_mime(mimetype: str) -> str:
    return MIME_TO_EXT.get(mimetype, "")


def _ensure_ext(name: str, mimetype: str) -> str:
    """Append an extension from the mimetype if the name has none."""
    if name and "." not in os.path.basename(name):
        ext = _ext_from_mime(mimetype)
        if ext:
            return f"{name}{ext}"
    return name


def _resolve_filename(media_obj, mimetype, timestamp):
    """
    Best filename for a media file, in priority order:
      1. media.filename / media.file_name (the real WhatsApp filename)
      2. the filename at the end of the media URL path
      3. a timestamped fallback (file_<ts>)
    Never uses the message caption. Extension from the mimetype is always
    ensured so files open correctly.
    """
    # 1. Explicit filename
    name = media_obj.get("filename") or media_obj.get("file_name")
    if name:
        return _ensure_ext(name, mimetype)

    # 2. Filename from the URL path
    raw_path = media_obj.get("path") or media_obj.get("url") or ""
    if raw_path:
        tail = unquote(raw_path.split("?")[0].rstrip("/").split("/")[-1])
        if tail and "." in tail:
            return _ensure_ext(tail, mimetype)

    # 3. Timestamped fallback
    ts = ""
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            ts = dt.strftime("%Y%m%d_%H%M%S")
        except Exception:
            ts = ""
    base = f"file_{ts}" if ts else "file"
    return _ensure_ext(base, mimetype)


def _already_downloaded(group_name: str, filename: str, parent_name: str = None) -> bool:
    """
    Check if a file already exists in SharePoint so we don't re-upload on re-runs.
    Uses the Graph API to check the exact path.
    Respects parent/child folder structure when parent_name is provided.
    """
    from storage_sharepoint import _safe_name, _get_token, GRAPH_BASE
    import httpx as _httpx
    root = config.SHAREPOINT_ROOT_FOLDER.strip("/")
    safe_file = _safe_name(filename)
    folder = _safe_name(group_name)

    if root:
        base = f"/{root}"
    else:
        base = ""

    if parent_name:
        path = f"{base}/{_safe_name(parent_name)}/{folder}/{safe_file}"
    else:
        path = f"{base}/{folder}/{safe_file}"

    drive_id = config.SHAREPOINT_DRIVE_ID
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:{path}"
    try:
        resp = _httpx.get(url, headers={"Authorization": f"Bearer {_get_token()}"}, timeout=10.0)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Step 1 — List all chats across ALL connected GM phones
# ---------------------------------------------------------------------------

def _get_connected_phones() -> list[tuple[str, str]]:
    """Return [(phone_number, phone_name)] for every CONNECTED GM."""
    try:
        r = httpx.get(
            f"{config.PERISKOPE_BASE_URL}/phones/all",
            headers=_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        phones = r.json() if isinstance(r.json(), list) else r.json().get("phones", [])
        return [
            (p.get("org_phone", "").replace("@c.us", ""), p.get("phone_name", ""))
            for p in phones if p.get("wa_state") == "CONNECTED"
        ]
    except Exception as e:
        print(f"[backfill] ERROR fetching phones: {e} — falling back to default phone.")
        return [(config.PERISKOPE_PHONE, "default")]


def _fetch_chats_for_phone(client: httpx.Client, phone: str) -> list[dict]:
    """Page through ALL chats visible to a specific GM phone."""
    headers = {
        "authorization": f"Bearer {config.PERISKOPE_API_KEY}",
        "x-phone": phone,
    }
    chats = []
    offset = 0
    while True:
        try:
            resp = client.get(
                f"{config.PERISKOPE_BASE_URL}/chats",
                headers=headers,
                params={"offset": offset, "limit": 100},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json().get("chats", [])
        except Exception as e:
            print(f"  [backfill] ERROR fetching chats for {phone} at offset {offset}: {e}")
            break
        if not batch:
            break
        chats.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.2)
    return chats


def list_va_groups() -> list[dict]:
    """
    Return deduplicated list of {chat_id, chat_name} for all VA groups.
    Queries every connected GM phone so groups only visible to one phone
    (like Doorlyst) are still captured.
    """
    print("\n[backfill] Fetching connected GM phones...")
    phones = _get_connected_phones()
    print(f"[backfill] {len(phones)} connected phone(s): {[p[0] for p in phones]}")

    seen_ids: set[str] = set()
    va_groups = []

    with httpx.Client(timeout=30.0) as client:
        for phone, phone_name in phones:
            print(f"\n[backfill] Querying chats for {phone} ({phone_name})...")
            chats = _fetch_chats_for_phone(client, phone)
            new_va = 0
            for chat in chats:
                chat_id = chat.get("chat_id") or chat.get("id") or ""
                if not str(chat_id).endswith("@g.us"):
                    continue
                if chat_id in seen_ids:
                    continue
                seen_ids.add(chat_id)
                name = chat.get("chat_name") or chat.get("name") or ""
                if _is_va_group(name):
                    va_groups.append({
                        "chat_id": chat_id,
                        "chat_name": name,
                        "gm_phone": phone,
                    })
                    print(f"  [+] '{name}' ({chat_id})")
                    new_va += 1
            print(f"  -> {len(chats)} chats scanned, {new_va} new VA group(s)")

    print(f"\n[backfill] {len(va_groups)} VA group(s) found across all phones.")
    return va_groups


# ---------------------------------------------------------------------------
# Step 2 — Fetch all messages for a group (paginated)
# ---------------------------------------------------------------------------

def fetch_all_messages(chat_id: str, gm_phone: str = None) -> list[dict]:
    """Return every message from a group, using the GM phone that can see it."""
    # Use the GM phone that discovered this group so we can actually fetch its messages
    headers = {
        "authorization": f"Bearer {config.PERISKOPE_API_KEY}",
        "x-phone": gm_phone or config.PERISKOPE_PHONE,
    }
    messages = []
    offset = 0
    page_size = 100

    with httpx.Client(timeout=30.0) as client:
        while True:
            try:
                resp = client.get(
                    f"{config.PERISKOPE_BASE_URL}/chats/{chat_id}/messages",
                    headers=headers,
                    params={"offset": offset, "limit": page_size},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  [backfill] ERROR fetching messages at offset {offset}: {e}")
                break

            batch = data if isinstance(data, list) else (
                data.get("messages") or data.get("data") or data.get("results") or []
            )

            if not batch:
                break

            messages.extend(batch)

            if len(batch) < page_size:
                break
            offset += page_size
            time.sleep(0.3)

    return messages


# ---------------------------------------------------------------------------
# Step 3 — Process messages and save files
# ---------------------------------------------------------------------------

def map_group_to_client(chat_id: str, group_name: str):
    """
    Save this group's whatsapp_group_id + whatsapp_group_name onto its client
    in Supabase, so the live bot recognises it instantly via Step 2 (known group)
    and writes into the exact same folder.

    Reuses db.find_client_by_group_members() which:
      - cross-references group members against client_contacts
      - calls update_group_id() (never overwrites an existing mapping)
    Skips groups already mapped, and groups that match no client.
    """
    # Already mapped? Skip the member lookup.
    existing = db.find_client_by_group(chat_id)
    if existing:
        print(f"  [map] '{group_name}' already mapped -> '{existing['client_name']}'.")
        return

    client = db.find_client_by_group_members(chat_id)
    if client:
        print(f"  [map] Mapped '{group_name}' -> '{client['client_name']}' (saved to Supabase).")
    else:
        print(f"  [map] No client matched '{group_name}' — left unmapped.")


def _add_group_members_as_contacts(chat_id: str, client_id: str, client_name: str, gm_phone: str = None):
    """Fetch all group members from Periskope and add them to client_contacts."""
    headers = {
        "authorization": f"Bearer {config.PERISKOPE_API_KEY}",
        "x-phone": gm_phone or config.PERISKOPE_PHONE,
    }
    try:
        resp = httpx.get(
            f"{config.PERISKOPE_BASE_URL}/chats/{chat_id}",
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        members = resp.json().get("members") or {}
    except Exception as e:
        print(f"  [backfill] Could not fetch members for contact sync: {e}")
        return

    for raw_number in members.keys():
        db.add_contact(client_id, raw_number, client_name)


def process_group(chat_id: str, group_name: str, gm_phone: str = None):
    print(f"\n[backfill] === Processing: '{group_name}' ===")

    # Lock in the group->client mapping in Supabase first (one-time).
    map_group_to_client(chat_id, group_name)

    # Add all group members to client_contacts with the client name.
    client = db.find_client_by_group(chat_id)
    if client:
        _add_group_members_as_contacts(chat_id, client["client_id"], client["client_name"], gm_phone)

    # If this group belongs to a parent client, files go inside the parent's folder.
    parent_name = client.get("parent_name") if client else None
    if parent_name:
        print(f"[backfill] Parent folder: '{parent_name}' -> sub-folder: '{group_name}'")

    messages = fetch_all_messages(chat_id, gm_phone)
    print(f"[backfill] {len(messages)} message(s) to process.")

    files_saved = 0
    notes_saved = 0
    already_have = 0
    skipped = 0

    for msg in messages:
        message_type = (msg.get("message_type") or msg.get("type") or "").lower()
        body = msg.get("body") or msg.get("text") or ""
        media_obj = msg.get("media") or {}
        timestamp = msg.get("timestamp") or msg.get("created_at") or ""

        if message_type in ("text", "chat"):
            if body:
                ts_str = ""
                if timestamp:
                    try:
                        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                    except Exception:
                        ts_str = timestamp
                storage.save_text_note(group_name, f"[{ts_str}] {body}" if ts_str else body, parent_name)
                notes_saved += 1

        elif message_type in ("image", "document", "video", "audio") or media_obj:
            raw_path = media_obj.get("path") or media_obj.get("url") or ""
            if not raw_path:
                skipped += 1
                continue

            mimetype = media_obj.get("mimetype") or media_obj.get("mime_type") or ""
            filename = _resolve_filename(media_obj, mimetype, timestamp)

            # Skip if this exact file is already in the folder (re-run safety).
            # Unlike the live bot, the backfill does NOT re-download duplicates.
            if filename and _already_downloaded(group_name, filename, parent_name):
                already_have += 1
                continue

            media_url = _build_media_url(raw_path)
            saved = storage.save_media(group_name, media_url, filename, parent_name)
            if saved:
                files_saved += 1
            else:
                skipped += 1
        else:
            skipped += 1

    print(
        f"[backfill] Done: {files_saved} new file(s), {already_have} already had, "
        f"{notes_saved} note(s), {skipped} skipped."
    )
    return files_saved, notes_saved


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  va-bot Backfill — Historical file download")
    print("=" * 60)

    va_groups = list_va_groups()

    if not va_groups:
        print("\n[backfill] No VA groups found. Check PERISKOPE_API_KEY and PERISKOPE_PHONE in config.py.")
        sys.exit(0)

    print(f"\nWill process {len(va_groups)} group(s):")
    for g in va_groups:
        print(f"  - {g['chat_name']}")

    total_files = 0
    total_notes = 0

    for group in va_groups:
        files, notes = process_group(group["chat_id"], group["chat_name"], group.get("gm_phone"))
        total_files += files
        total_notes += notes

    print("\n" + "=" * 60)
    print(f"  Backfill complete!")
    print(f"  Total files downloaded : {total_files}")
    print(f"  Total text notes saved : {total_notes}")
    print(f"  SharePoint             : https://aiaccountant0.sharepoint.com/sites/VADocuments")
    print("=" * 60)


if __name__ == "__main__":
    main()
