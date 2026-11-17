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
import naming
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



def _is_va_group(name: str) -> bool:
    return any(p.search(name or "") for p in _CLIENT_GROUP_PATTERNS)


# One naming rule for live, backfill, recovery and catch-up (naming.py). Kept under the old names for callers.
MIME_TO_EXT = naming.MIME_TO_EXT
_build_media_url = naming.media_url
_ext_from_mime = naming.ext_from_mime
_ensure_ext = naming.ensure_ext
_resolve_filename = naming.resolve_filename


def _already_downloaded(group_name: str, filename: str, parent_name: str = None) -> bool:
    """
    Check if a file already exists in SharePoint so we don't re-upload on re-runs.
    Uses the Graph API to check the exact path.
    Respects parent/child folder structure when parent_name is provided.
    """
    from storage_sharepoint import _safe_name, _get_token, GRAPH_BASE, _q
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
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:{_q(path)}"
    # 200 = present, 404 = absent. Anything else (timeout, 429, 5xx) RAISES: treating an error as "absent" made the
    # caller upload the file again as name_1.ext.
    resp = _httpx.get(url, headers={"Authorization": f"Bearer {_get_token()}"}, timeout=10.0)
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    raise RuntimeError(f"SharePoint check failed with HTTP {resp.status_code} for '{safe_file}'")


def _fetch_existing_notes(group_name: str, parent_name: str = None) -> str:
    """
    Return the current text of the group's notes.txt (empty string if none).
    Fetched once per group so we can skip re-appending note lines we already
    wrote on a previous run. Respects parent/child folder structure.
    """
    from storage_sharepoint import _safe_name, _get_token, GRAPH_BASE, _q
    import httpx as _httpx
    root = config.SHAREPOINT_ROOT_FOLDER.strip("/")
    folder = _safe_name(group_name)

    base = f"/{root}" if root else ""
    if parent_name:
        path = f"{base}/{_safe_name(parent_name)}/{folder}/notes.txt"
    else:
        path = f"{base}/{folder}/notes.txt"

    drive_id = config.SHAREPOINT_DRIVE_ID
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:{_q(path)}:/content"
    try:
        resp = _httpx.get(
            url,
            headers={"Authorization": f"Bearer {_get_token()}"},
            timeout=15.0,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return ""


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
    # Use the GM phone that discovered this group. For groups that are only known from Supabase
    # (gm_phone None) send no x-phone: Periskope then searches every connected phone. The default
    # PERISKOPE_PHONE is not a member of most VA groups and returned nothing for them.
    headers = {"authorization": f"Bearer {config.PERISKOPE_API_KEY}"}
    if gm_phone:
        headers["x-phone"] = gm_phone
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
    """Fetch all group members from Periskope and add them to client_contacts (team numbers are skipped
    inside db.add_contact, so team members never become a client's contact)."""
    headers = {"authorization": f"Bearer {config.PERISKOPE_API_KEY}"}
    if gm_phone:
        headers["x-phone"] = gm_phone
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

    # Skip inactive clients entirely — the nightly cron must not fetch messages
    # or download files for a client marked inactive in Supabase.
    # find_client_by_group already returns None for inactive clients, so an
    # inactive (mapped) group produces client=None here and is skipped below.
    client = db.find_client_by_group(chat_id)
    if not client:
        print(f"[backfill] '{group_name}' has no active client mapping (unmapped or INACTIVE) — skipping.")
        return 0, 0

    # Add all group members to client_contacts with the client name.
    _add_group_members_as_contacts(chat_id, client["client_id"], client["client_name"], gm_phone)

    # Folder = the client's stored folder name (db.folder_name_for), the same one the live bot writes to.
    # Never WhatsApp's current group name: that is what created duplicate folders after group renames.
    folder = client["folder_name"]
    # If this group belongs to a parent client, files go inside the parent's folder.
    parent_name = client.get("parent_name")
    if parent_name:
        print(f"[backfill] Parent folder: '{parent_name}' -> sub-folder: '{folder}'")
    elif folder != group_name:
        print(f"[backfill] WhatsApp name '{group_name}' -> saving into the client's folder '{folder}'")

    messages = fetch_all_messages(chat_id, gm_phone)
    print(f"[backfill] {len(messages)} message(s) to process.")

    # Fetch the group's existing notes.txt once. Each note line embeds the
    # message's own timestamp ("[<msg-ts>] <body>"), which is stable across
    # runs, so we can skip re-appending lines we already wrote on a prior run.
    existing_notes = _fetch_existing_notes(folder, parent_name)

    files_saved = 0
    notes_saved = 0
    already_have = 0
    skipped = 0

    for msg in messages:
        message_type = (msg.get("message_type") or msg.get("type") or "").lower()
        body = msg.get("body") or msg.get("text") or ""
        media_obj = msg.get("media") or {}
        timestamp = msg.get("timestamp") or msg.get("created_at") or ""

        # Any message carrying a media object is an attachment worth saving,
        # regardless of its message_type string (payment cards, stickers,
        # contacts, unknown types). Mirrors the live bot in main.py so live and
        # historical capture never disagree.
        if media_obj and (media_obj.get("path") or media_obj.get("url")):
            raw_path = media_obj.get("path") or media_obj.get("url") or ""

            mimetype = media_obj.get("mimetype") or media_obj.get("mime_type") or ""
            filename = _resolve_filename(media_obj, mimetype, timestamp)

            # Skip if this exact file is already in the folder (re-run safety).
            # Unlike the live bot, the backfill does NOT re-download duplicates.
            try:
                present = bool(filename) and _already_downloaded(folder, filename, parent_name)
            except Exception as e:
                print(f"  [backfill] Could not check '{filename}' ({type(e).__name__}); skipped, re-run later.")
                skipped += 1
                continue
            if present:
                already_have += 1
                continue

            media_url = _build_media_url(raw_path)
            saved = storage.save_media(folder, media_url, filename, parent_name, message_timestamp=timestamp)
            if saved:
                files_saved += 1
            else:
                skipped += 1

        elif body:
            # No media — save any text content (plain text, chat, or a caption on
            # a non-downloadable message) as a timestamped note so nothing is lost.
            ts_str = ""
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception:
                    ts_str = timestamp
            note_text = f"[{ts_str}] {body}" if ts_str else body

            # Re-run safety: skip lines already in notes.txt. The message
            # timestamp + body is a stable key across runs (save_text_note
            # prepends its own upload-time stamp, so we match on the substring).
            if note_text in existing_notes:
                already_have += 1
                continue

            storage.save_text_note(folder, note_text, parent_name)
            existing_notes += note_text  # keep in-memory copy current within this run
            notes_saved += 1

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

def _merge_with_mapped_groups(va_groups: list[dict]) -> list[dict]:
    """
    Add already-mapped clients (from Supabase) that list_va_groups() would miss
    because their group name doesn't match _CLIENT_GROUP_PATTERNS — e.g.
    sub-groups like "BR Parlours Admin Group" or "NEE Accounts". Discovered
    groups keep their gm_phone (needed to fetch messages); mapped-only groups
    get gm_phone=None, which fetch_all_messages() falls back to config's
    default phone for.
    """
    seen_ids = {g["chat_id"] for g in va_groups}
    merged = list(va_groups)
    for g in db.list_mapped_groups():
        if g["chat_id"] not in seen_ids:
            seen_ids.add(g["chat_id"])
            merged.append({"chat_id": g["chat_id"], "chat_name": g["chat_name"], "gm_phone": None})
    return merged


def main():
    print("=" * 60)
    print("  va-bot Backfill — Historical file download")
    print("=" * 60)

    va_groups = _merge_with_mapped_groups(list_va_groups())

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
