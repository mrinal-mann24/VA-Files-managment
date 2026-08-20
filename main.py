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

    if event == "message.flagged":
        return await _handle_flag(data.get("data", {}))

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

    # Only WhatsApp *group* chats are processed. 1:1 chats are ignored outright,
    # even when the sender is a known client contact: internal staff numbers sit
    # in client_contacts too (they're members of the VA groups), and their direct
    # chats with leads were being filed under whichever client they matched.
    if not is_group:
        print(f"[main] 1:1 chat '{chat_id}' — ignored (groups only).")
        return {"status": "ignored"}

    # ------------------------------------------------------------------ #
    # Step 1 — the group the message was posted in decides the client.   #
    # This must come BEFORE any sender-number lookup: team members and   #
    # shared contacts sit in many groups (and in client_contacts), so    #
    # matching by sender first files a post in client B's group under    #
    # client A. Same order the backfill scripts already use.             #
    # ------------------------------------------------------------------ #
    client = db.find_client_by_group(chat_id)

    if client:
        # Known group. Make sure the sender is in client_contacts for this
        # client (no-op if the number already exists anywhere).
        db.add_contact(client["client_id"], sender_phone, client["client_name"])

    else:
        # ------------------------------------------------------------------ #
        # Step 2 — unmapped group. If the sender belongs to a client that    #
        # has NO group yet, this is that client's first group message:       #
        # map the group to them now. A sender whose client already has a    #
        # different group is NOT used — that is exactly the mis-filing case. #
        # ------------------------------------------------------------------ #
        by_number = db.find_client_by_number(sender_phone)
        if by_number and not by_number.get("whatsapp_group_id"):
            group_name = db.fetch_group_name(chat_id)
            db.update_group_id(by_number["client_id"], chat_id, group_name or "")
            if group_name:
                by_number["folder_name"] = group_name
            client = by_number
        else:
            # ------------------------------------------------------------------ #
            # Step 3 — unknown group: check if group name contains "<> VA" or    #
            # "<> Virtual Accounting" and auto-map to a Supabase client           #
            # ------------------------------------------------------------------ #
            client = db.find_client_by_group_members(chat_id)
            if client:
                print(f"[main] Auto-mapped group '{chat_id}' to '{client['client_name']}' via member match.")
                db.add_contact(client["client_id"], sender_phone, client["client_name"])
            else:
                print(f"[main] Unmapped group '{chat_id}' — not a VA client group.")
                return {"status": "ok"}

    client_name = client["client_name"]
    folder_name = client.get("folder_name") or client_name
    parent_name = client.get("parent_name")
    print(f"[main] Client: '{client_name}' | Folder: '{folder_name}' | Parent: '{parent_name}'")

    # ------------------------------------------------------------------ #
    # Step 4 — save the message / file                                    #
    # ------------------------------------------------------------------ #
    if has_media:
        # Any message carrying a media object is an attachment worth saving,
        # regardless of its message_type string. WhatsApp payment cards,
        # stickers, contacts, and unusual/unknown types all land here as long
        # as Periskope gives us a downloadable media object. This mirrors the
        # backfill script, so live and historical capture never disagree.
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

        saved = storage.save_media(folder_name, media_url, filename, parent_name)

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
            # Look up assigned VA's Teams personal chat
            va = db.get_va_for_client(client["client_id"])
            teams_chat_id = va["teams_chat_id"] if va else None
            if va:
                print(f"[main] Notifying VA '{va['va_name']}' via personal Teams chat.")
            # Schedule Teams notification (debounced 60s)
            teams.notify(
                client_id=client["client_id"],
                client_name=client_name,
                folder_name=folder_name,
                sender_phone=sender_phone,
                count_fn=db.get_unnotified_count,
                mark_fn=db.mark_notified,
                teams_chat_id=teams_chat_id,
            )

    elif body:
        # No media — save any text content (plain text, chat, or a caption on a
        # non-downloadable message) as a timestamped note so nothing is lost.
        storage.save_text_note(folder_name, body, parent_name)

        # Additionally land the raw message in the client_messages knowledge base
        # for later (separate) LLM fact extraction. This is isolated: save_message
        # swallows its own errors and never raises, so a DB/network/Supabase issue
        # here can never break the SharePoint save or Teams notify flow above.
        #
        # Capture rule (narrower than the SharePoint flow above, which saves from
        # any group a known contact appears in): only capture messages sent IN the
        # client's OWN registered VA group (chat_id == clients.whatsapp_group_id).
        # A known contact chatting in some other group (AIA, onboarding, a second
        # group) is NOT captured. Both directions are saved — from_me tells us
        # which side of the conversation each message is on.
        registered_group_id = db.get_registered_group_id(client["client_id"])
        in_own_va_group = (
            registered_group_id
            and chat_id.split("@")[0] == registered_group_id
        )
        if in_own_va_group:
            db.save_message(
                client_id=client["client_id"],
                whatsapp_group_id=chat_id,
                sender_phone=sender_phone,
                body=body,
                message_timestamp=msg.get("timestamp") or "",
                unique_id=unique_id,
                direction="outgoing" if msg.get("from_me") else "incoming",
            )

    else:
        print(f"[main] Empty '{message_type}' message with no media/body — skipping.")

    return {"status": "ok"}


async def _handle_flag(msg: dict):
    """
    A message was flagged in Periskope. If it came from a customer (not a VA)
    and we haven't already alerted on it, DM the client's assigned VA on Teams.
    """
    message_id = msg.get("message_id") or msg.get("unique_id") or msg.get("id", {}).get("id") or ""
    chat_id = msg.get("chat_id", "")
    sender_phone = msg.get("sender_phone") or msg.get("author") or ""
    body = msg.get("body") or ""

    print(f"[flag] message_id={message_id!r} chat={chat_id} sender={sender_phone}")

    # Skip flags raised by a VA — we only alert on customer messages.
    if db.is_va_number(sender_phone):
        print(f"[flag] Sender '{sender_phone}' is a VA — skipping.")
        return {"status": "ok"}

    # Identify the client: by sender number first, then by group.
    client = db.find_client_by_number(sender_phone)
    if not client and chat_id.endswith("@g.us"):
        client = db.find_client_by_group(chat_id)
    if not client:
        print(f"[flag] Could not map flag to a client — skipping.")
        return {"status": "ok"}

    # Dedup: record it; if already recorded, don't alert twice.
    if not db.record_flagged_message(
        message_id=message_id,
        client_id=client["client_id"],
        client_name=client["client_name"],
        chat_id=chat_id,
        sender_phone=sender_phone,
        body=body,
    ):
        print(f"[flag] Already processed message_id={message_id!r} — skipping.")
        return {"status": "duplicate"}

    folder_name = client.get("folder_name") or client["client_name"]
    va = db.get_va_for_client(client["client_id"])
    if not va:
        print(f"[flag] No VA configured for '{client['client_name']}' — nothing to notify.")
        return {"status": "ok"}

    print(f"[flag] Alerting VA '{va['va_name']}' for '{client['client_name']}'.")
    await teams.notify_flag(
        folder_name=folder_name,
        sender_phone=sender_phone,
        body=body,
        teams_chat_id=va["teams_chat_id"],
    )
    db.mark_flag_notified(message_id)
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
