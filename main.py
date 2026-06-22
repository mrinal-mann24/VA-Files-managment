from urllib.parse import unquote

from fastapi import FastAPI, Request

import config
import db
import storage_sharepoint as storage
import teams

app = FastAPI()

# Dedup: Periskope fires the webhook once per connected GM in the group,
# so the same message arrives 2-3× within seconds. Track the last 500
# unique_ids and skip any repeat. An LRU-style list keeps memory bounded.
_SEEN_IDS: list[str] = []
_SEEN_IDS_MAX = 500


def _is_duplicate(unique_id: str) -> bool:
    if not unique_id:
        return False
    if unique_id in _SEEN_IDS:
        return True
    _SEEN_IDS.append(unique_id)
    if len(_SEEN_IDS) > _SEEN_IDS_MAX:
        _SEEN_IDS.pop(0)
    return False


def _build_media_url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return config.PERISKOPE_MEDIA_BASE_URL.rstrip("/") + path


def _ensure_ext(name: str, mimetype: str) -> str:
    """Append an extension from the mimetype if the name has none."""
    import os
    if name and "." not in os.path.basename(name):
        ext = _ext_from_mime(mimetype)
        if ext:
            return f"{name}{ext}"
    return name


def _resolve_filename(media_obj: dict, mimetype: str) -> str | None:
    """
    Best filename for a media file, in priority order:
      1. media.filename (the real WhatsApp filename)
      2. filename at the end of the media URL path
    Never uses the message caption. Returns None if neither is usable, in which
    case storage.save_media assigns a timestamped name. Extension is ensured
    from the mimetype so files open correctly.
    """
    name = media_obj.get("filename") or media_obj.get("file_name")
    if name:
        return _ensure_ext(name, mimetype)

    raw_path = media_obj.get("path") or media_obj.get("url") or ""
    if raw_path:
        tail = unquote(raw_path.split("?")[0].rstrip("/").split("/")[-1])
        if tail and "." in tail:
            return _ensure_ext(tail, mimetype)

    return None  # storage.save_media will assign a timestamped name


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("=== Incoming Periskope event ===")
    print(data)

    event = data.get("event") or data.get("event_type")
    if event != "message.created":
        return {"status": "ignored"}

    msg = data.get("data", {})
    unique_id = msg.get("unique_id") or msg.get("id", {}).get("id") or ""
    if _is_duplicate(unique_id):
        print(f"[main] Duplicate webhook for unique_id={unique_id!r} — skipping.")
        return {"status": "duplicate"}

    chat_id = msg.get("chat_id", "")
    is_group = chat_id.endswith("@g.us")
    sender_phone = msg.get("sender_phone") or msg.get("author") or ""
    message_type = (msg.get("message_type") or "").lower()
    body = msg.get("body") or ""
    media_obj = msg.get("media") or {}
    has_media = bool(media_obj)

    print(f"chat_id     : {chat_id}")
    print(f"sender_phone: {sender_phone}")
    print(f"type        : {message_type}")

    # ------------------------------------------------------------------ #
    # Step 1 — try to identify client by sender's number                  #
    # ------------------------------------------------------------------ #
    client = db.find_client_by_number(sender_phone)

    if client:
        # Number is known. If this is a group message and the group ID
        # isn't stored yet, fetch the group name from Periskope and save both.
        if is_group and not client.get("whatsapp_group_id"):
            group_name = db.fetch_group_name(chat_id)
            db.update_group_id(client["client_id"], chat_id, group_name or "")
            if group_name:
                client["folder_name"] = group_name

    elif is_group:
        # ------------------------------------------------------------------ #
        # Step 2 — unknown sender, but maybe we know the group               #
        # ------------------------------------------------------------------ #
        client = db.find_client_by_group(chat_id)

        if client:
            # Group is known → this is a new person (e.g. another contact
            # from the same company) sending in the group.
            # Auto-add their number to client_contacts so next time they're
            # recognised immediately without hitting Step 2 again.
            print(f"[main] New number '{sender_phone}' in known group — auto-adding to contacts.")
            db.add_contact(client["client_id"], sender_phone)
        else:
            # ------------------------------------------------------------------ #
            # Step 3 — unknown group: check if group name contains "<> VA" or    #
            # "<> Virtual Accounting" and auto-map to a Supabase client           #
            # ------------------------------------------------------------------ #
            client = db.find_client_by_group_members(chat_id)
            if client:
                print(f"[main] Auto-mapped group '{chat_id}' to '{client['client_name']}' via member match.")
                db.add_contact(client["client_id"], sender_phone)
            else:
                print(f"[main] Unmapped group '{chat_id}' — not a VA client group.")
                return {"status": "ok"}

    else:
        # 1:1 chat, unknown number — skip
        print(f"[main] Unknown sender '{sender_phone}' in 1:1 chat — skipping.")
        return {"status": "ok"}

    client_name = client["client_name"]
    folder_name = client.get("folder_name") or client_name
    print(f"[main] Client: '{client_name}' | Folder: '{folder_name}'")

    # ------------------------------------------------------------------ #
    # Step 4 — save the message / file                                    #
    # ------------------------------------------------------------------ #
    if message_type in ("text", "chat"):
        if body:
            storage.save_text_note(folder_name, body)

    elif message_type in ("image", "document", "video", "audio") and has_media:
        raw_path = media_obj.get("path", "")
        mimetype = media_obj.get("mimetype", "")
        file_size = media_obj.get("size") or 0

        filename = _resolve_filename(media_obj, mimetype)

        if not raw_path:
            print("[main] No media path — skipping.")
            return {"status": "ok"}

        media_url = _build_media_url(raw_path)
        print(f"Media URL : {media_url}")
        print(f"Filename  : {filename}")

        saved = storage.save_media(folder_name, media_url, filename)

        if saved:
            # Log to Supabase
            db.log_upload(
                client_id=client["client_id"],
                client_name=client_name,
                folder_name=folder_name,
                group_id=chat_id,
                group_name=folder_name,
                sender_phone=sender_phone,
                file_name=filename or "",
                file_type=message_type,
                file_size=file_size,
            )
            # Schedule Teams notification (debounced 60s)
            teams.notify(
                client_id=client["client_id"],
                client_name=client_name,
                folder_name=folder_name,
                sender_phone=sender_phone,
                count_fn=db.get_unnotified_count,
                mark_fn=db.mark_notified,
            )

    else:
        print(f"[main] Unhandled type '{message_type}' — skipping.")

    return {"status": "ok"}


def _ext_from_mime(mimetype: str) -> str:
    mapping = {
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
    return mapping.get(mimetype, "")
