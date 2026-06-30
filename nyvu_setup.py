"""
nyvu_setup.py — One-time setup script for NYVU sub-groups.

What it does:
  1. Fetches ALL chats from Periskope across all connected GM phones.
  2. Finds the 10 NYVU payment groups by name (fuzzy match list below).
  3. For each group:
     a. Creates a row in `clients` with parent_client_id = NYVU parent client.
     b. Saves whatsapp_group_id + whatsapp_group_name.
     c. Adds all group members to client_contacts.
     d. Backfills all existing files/notes into SharePoint under NYVU/{group_name}/.

Run once:
    python nyvu_setup.py

Safe to re-run — existing DB rows and SharePoint files are never duplicated.
"""

import sys
import time
import re
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx
from supabase import create_client

import config
import storage_sharepoint as storage

# ---------------------------------------------------------------------------
# NYVU parent client (already exists in Supabase)
# ---------------------------------------------------------------------------
NYVU_PARENT_CLIENT_ID   = "236bd38f-b966-4c12-a89e-c20061d3d5e5"
NYVU_PARENT_CLIENT_NAME = "Nyutech[Kiran]_AiA_2026"
# This is the actual SharePoint folder name (derived from whatsapp_group_name via _safe_name).
# Sub-group folders will be created INSIDE this existing folder.
NYVU_PARENT_FOLDER_NAME = "AI- Nyvu Technocrats Pvt Ltd - Virtual Accounting"

# ---------------------------------------------------------------------------
# The 10 NYVU payment groups — fuzzy name fragments to match against Periskope
# Add / remove names here as needed.
# ---------------------------------------------------------------------------
NYVU_GROUP_NAME_FRAGMENTS = [
    "GUARDANT Pymnt",
    "Clarnet Pymnt",
    "Ctrls BOM",
    "Factory Expenses",
    "KIA skootr",
    "Labour Issues",
    "Gigaplex",
    "GIIS payments",
    "Nyvu Assets",
    "Nyvu ID Payments",
]

# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------
_supabase = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)

# ---------------------------------------------------------------------------
# Periskope helpers
# ---------------------------------------------------------------------------
MIME_TO_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "video/mp4": ".mp4", "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
    "application/pdf": ".pdf", "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def _strip(wid: str) -> str:
    return (wid or "").split("@")[0]


def _ext_from_mime(mime: str) -> str:
    return MIME_TO_EXT.get(mime, "")


def _ensure_ext(name: str, mime: str) -> str:
    if name and "." not in name.split("/")[-1]:
        ext = _ext_from_mime(mime)
        if ext:
            return f"{name}{ext}"
    return name


def _resolve_filename(media_obj: dict, mime: str, timestamp: str) -> str:
    name = media_obj.get("filename") or media_obj.get("file_name")
    if name:
        return _ensure_ext(name, mime)
    raw = media_obj.get("path") or media_obj.get("url") or ""
    if raw:
        tail = unquote(raw.split("?")[0].rstrip("/").split("/")[-1])
        if tail and "." in tail:
            return _ensure_ext(tail, mime)
    ts = ""
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            ts = dt.strftime("%Y%m%d_%H%M%S")
        except Exception:
            pass
    base = f"file_{ts}" if ts else "file"
    return _ensure_ext(base, mime)


def _build_media_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return config.PERISKOPE_MEDIA_BASE_URL.rstrip("/") + path


def _get_connected_phones() -> list[tuple[str, str]]:
    try:
        r = httpx.get(
            f"{config.PERISKOPE_BASE_URL}/phones/all",
            headers={"authorization": f"Bearer {config.PERISKOPE_API_KEY}",
                     "x-phone": config.PERISKOPE_PHONE},
            timeout=15,
        )
        r.raise_for_status()
        phones = r.json() if isinstance(r.json(), list) else r.json().get("phones", [])
        return [
            (_strip(p.get("org_phone", "")), p.get("phone_name", ""))
            for p in phones if p.get("wa_state") == "CONNECTED"
        ]
    except Exception as e:
        print(f"[nyvu] ERROR fetching phones: {e} — using default.")
        return [(config.PERISKOPE_PHONE, "default")]


def _fetch_all_chats(phone: str) -> list[dict]:
    headers = {"authorization": f"Bearer {config.PERISKOPE_API_KEY}", "x-phone": phone}
    chats, offset = [], 0
    with httpx.Client(timeout=30.0) as client:
        while True:
            try:
                resp = client.get(
                    f"{config.PERISKOPE_BASE_URL}/chats",
                    headers=headers,
                    params={"offset": offset, "limit": 100},
                )
                resp.raise_for_status()
                batch = resp.json().get("chats", [])
            except Exception as e:
                print(f"  [nyvu] ERROR at offset {offset}: {e}")
                break
            if not batch:
                break
            chats.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
            time.sleep(0.2)
    return chats


def _fetch_group_members(chat_id: str, phone: str) -> dict:
    try:
        r = httpx.get(
            f"{config.PERISKOPE_BASE_URL}/chats/{chat_id}",
            headers={"authorization": f"Bearer {config.PERISKOPE_API_KEY}", "x-phone": phone},
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json().get("members") or {}
    except Exception as e:
        print(f"  [nyvu] ERROR fetching members for {chat_id}: {e}")
        return {}


def _fetch_all_messages(chat_id: str, phone: str) -> list[dict]:
    headers = {"authorization": f"Bearer {config.PERISKOPE_API_KEY}", "x-phone": phone}
    messages, offset = [], 0
    with httpx.Client(timeout=30.0) as client:
        while True:
            try:
                resp = client.get(
                    f"{config.PERISKOPE_BASE_URL}/chats/{chat_id}/messages",
                    headers=headers,
                    params={"offset": offset, "limit": 100},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  [nyvu] ERROR fetching messages at offset {offset}: {e}")
                break
            batch = data if isinstance(data, list) else (
                data.get("messages") or data.get("data") or data.get("results") or []
            )
            if not batch:
                break
            messages.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
            time.sleep(0.3)
    return messages


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _get_or_create_client_row(group_id: str, group_name: str) -> str | None:
    """
    Return client_id for this group. Creates the row if it doesn't exist yet.
    Never duplicates — checks by whatsapp_group_id first.
    """
    bare_id = _strip(group_id)

    # Check if already exists by group_id
    resp = _supabase.table("clients") \
        .select("client_id, client_name") \
        .eq("whatsapp_group_id", bare_id) \
        .limit(1).execute()
    if resp.data:
        row = resp.data[0]
        print(f"  [db] Already exists: '{row['client_name']}' (id={row['client_id']})")
        return row["client_id"]

    # Create new row
    insert_resp = _supabase.table("clients").insert({
        "client_name": group_name,
        "whatsapp_group_id": bare_id,
        "whatsapp_group_name": group_name,
        "parent_client_id": NYVU_PARENT_CLIENT_ID,
    }).execute()
    new_row = (insert_resp.data or [{}])[0]
    new_id = new_row.get("client_id")
    print(f"  [db] Created client row: '{group_name}' (id={new_id})")
    return new_id


def _add_contact(client_id: str, raw_number: str, group_name: str):
    bare = _strip(raw_number)
    if not bare:
        return
    # Skip if already exists
    existing = _supabase.table("client_contacts") \
        .select("contact_id").eq("whatsapp_number", bare).limit(1).execute()
    if existing.data:
        return
    try:
        _supabase.table("client_contacts").insert({
            "client_id": client_id,
            "whatsapp_number": bare,
            "contact_name": f"Auto-added ({bare})",
            "role": "member",
            "is_primary": False,
        }).execute()
    except Exception as e:
        print(f"  [db] ERROR adding contact {bare}: {e}")


def _log_upload(client_id: str, group_name: str, group_id: str,
                sender_phone: str, file_name: str, file_type: str, file_size: int):
    try:
        _supabase.table("document_uploads").insert({
            "client_id": client_id,
            "client_name": NYVU_PARENT_CLIENT_NAME,
            "folder_name": group_name,
            "group_id": _strip(group_id),
            "group_name": group_name,
            "sender_phone": _strip(sender_phone),
            "file_name": file_name,
            "file_type": file_type,
            "file_size": file_size,
            "notified": True,
        }).execute()
    except Exception as e:
        print(f"  [db] ERROR logging upload: {e}")


def _already_in_sharepoint(group_name: str, filename: str) -> bool:
    from storage_sharepoint import _safe_name, _get_token, GRAPH_BASE
    root = config.SHAREPOINT_ROOT_FOLDER.strip("/")
    parent = _safe_name(NYVU_PARENT_FOLDER_NAME)
    folder = _safe_name(group_name)
    safe_file = _safe_name(filename)
    path = f"/{root}/{parent}/{folder}/{safe_file}" if root else f"/{parent}/{folder}/{safe_file}"
    drive_id = config.SHAREPOINT_DRIVE_ID
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:{path}"
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {_get_token()}"}, timeout=10.0)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def find_nyvu_groups() -> list[dict]:
    """Find all NYVU payment groups across all connected GM phones."""
    print("\n[nyvu] Fetching connected GM phones...")
    phones = _get_connected_phones()
    print(f"[nyvu] {len(phones)} phone(s): {[p[0] for p in phones]}")

    seen_ids: set[str] = set()
    found: list[dict] = []

    with httpx.Client(timeout=30.0) as _:
        for phone, phone_name in phones:
            print(f"\n[nyvu] Scanning chats on {phone} ({phone_name})...")
            chats = _fetch_all_chats(phone)
            print(f"  {len(chats)} total chats scanned.")

            for chat in chats:
                chat_id = chat.get("chat_id") or chat.get("id") or ""
                if not str(chat_id).endswith("@g.us"):
                    continue
                if chat_id in seen_ids:
                    continue
                name = (chat.get("chat_name") or chat.get("name") or "").strip()
                # Check if this chat matches any NYVU fragment
                for fragment in NYVU_GROUP_NAME_FRAGMENTS:
                    if fragment.lower() in name.lower():
                        seen_ids.add(chat_id)
                        found.append({"chat_id": chat_id, "chat_name": name, "gm_phone": phone})
                        print(f"  [+] '{name}' ({chat_id})")
                        break

    print(f"\n[nyvu] Found {len(found)} NYVU group(s).")
    return found


def process_group(group: dict) -> tuple[int, int]:
    chat_id   = group["chat_id"]
    group_name = group["chat_name"]
    gm_phone  = group["gm_phone"]

    print(f"\n{'='*60}")
    print(f"[nyvu] Processing: '{group_name}'")
    print(f"{'='*60}")

    # 1. Create/get client row in Supabase
    client_id = _get_or_create_client_row(chat_id, group_name)
    if not client_id:
        print("  [nyvu] ERROR: could not get/create client row. Skipping.")
        return 0, 0

    # 2. Add all group members to client_contacts
    print("  [nyvu] Syncing group members to client_contacts...")
    members = _fetch_group_members(chat_id, gm_phone)
    for raw_number in members.keys():
        _add_contact(client_id, raw_number, group_name)
    print(f"  [nyvu] {len(members)} member(s) synced.")

    # 3. Fetch all messages
    print("  [nyvu] Fetching all messages...")
    messages = _fetch_all_messages(chat_id, gm_phone)
    print(f"  [nyvu] {len(messages)} message(s) found.")

    files_saved = notes_saved = already_have = skipped = 0

    for msg in messages:
        message_type = (msg.get("message_type") or msg.get("type") or "").lower()
        body         = msg.get("body") or msg.get("text") or ""
        media_obj    = msg.get("media") or {}
        timestamp    = msg.get("timestamp") or msg.get("created_at") or ""
        sender_phone = msg.get("sender_phone") or msg.get("author") or ""

        if message_type in ("text", "chat"):
            if body:
                ts_str = ""
                if timestamp:
                    try:
                        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                    except Exception:
                        ts_str = timestamp
                storage.save_text_note(
                    group_name,
                    f"[{ts_str}] {body}" if ts_str else body,
                    parent_name=NYVU_PARENT_FOLDER_NAME,
                )
                notes_saved += 1

        elif message_type in ("image", "document", "video", "audio") or media_obj:
            raw_path = media_obj.get("path") or media_obj.get("url") or ""
            if not raw_path:
                skipped += 1
                continue

            mime     = media_obj.get("mimetype") or media_obj.get("mime_type") or ""
            filename = _resolve_filename(media_obj, mime, timestamp)
            size     = media_obj.get("size") or 0

            if filename and _already_in_sharepoint(group_name, filename):
                already_have += 1
                continue

            media_url = _build_media_url(raw_path)
            saved = storage.save_media(
                group_name,
                media_url,
                filename,
                parent_name=NYVU_PARENT_FOLDER_NAME,
            )
            if saved:
                _log_upload(client_id, group_name, chat_id, sender_phone,
                            filename or "", message_type, size)
                files_saved += 1
            else:
                skipped += 1
        else:
            skipped += 1

    print(
        f"\n[nyvu] Done: {files_saved} file(s) saved, {already_have} already existed, "
        f"{notes_saved} note(s), {skipped} skipped."
    )
    return files_saved, notes_saved


def main():
    print("=" * 60)
    print("  NYVU Sub-Group Setup & Backfill")
    print(f"  Parent: {NYVU_PARENT_CLIENT_NAME}")
    print(f"  Parent ID: {NYVU_PARENT_CLIENT_ID}")
    print("=" * 60)

    groups = find_nyvu_groups()

    if not groups:
        print("\n[nyvu] No NYVU groups found.")
        print("  Check NYVU_GROUP_NAME_FRAGMENTS list in this script.")
        print("  Also verify PERISKOPE_API_KEY and PERISKOPE_PHONE in .env")
        sys.exit(0)

    print(f"\nWill process {len(groups)} group(s):")
    for g in groups:
        print(f"  - {g['chat_name']} ({g['chat_id']})")

    confirm = input("\nProceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    total_files = total_notes = 0
    for group in groups:
        f, n = process_group(group)
        total_files += f
        total_notes += n

    print("\n" + "=" * 60)
    print("  NYVU Setup Complete!")
    print(f"  Groups processed : {len(groups)}")
    print(f"  Files saved      : {total_files}")
    print(f"  Notes saved      : {total_notes}")
    print(f"  SharePoint path  : Client Files/{NYVU_PARENT_FOLDER_NAME}/{{group_name}}/")
    print("=" * 60)


if __name__ == "__main__":
    main()
