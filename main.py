import hmac
from collections import deque
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import config
import db
import failures
import naming
import storage_sharepoint as storage
import teams

@asynccontextmanager
async def _lifespan(_app):
    # Message processing runs in worker threads; Teams timers and posts are handed back to this loop.
    teams.MAIN_LOOP = asyncio.get_running_loop()
    if not config.WEBHOOK_SECRET:
        print("[main] WARNING: WEBHOOK_SECRET is not set, so anyone who knows the URL can post to /webhook.")
    yield


app = FastAPI(lifespan=_lifespan)

# Dedup: Periskope fires the webhook once per connected GM in the group,
# so the same message arrives 2-3× within seconds. Remember the last 500
# unique_ids that were SAVED and skip any repeat. A message is marked seen only
# after it was processed successfully, so if one copy fails, the next copy
# (or Periskope's retry) gets another chance instead of being dropped.
_SEEN_IDS_MAX = 500
_SEEN_ORDER: deque[str] = deque()
_SEEN_SET: set[str] = set()
# unique_ids being processed right now. The 2-3 copies Periskope sends arrive within seconds, often before the
# first copy has finished; without this they all ran at once (a second "_1" file, a doubled notes line).
_IN_FLIGHT: set[str] = set()


def _is_seen(unique_id: str) -> bool:
    return bool(unique_id) and unique_id in _SEEN_SET


def _mark_seen(unique_id: str) -> None:
    if not unique_id or unique_id in _SEEN_SET:
        return
    _SEEN_ORDER.append(unique_id)
    _SEEN_SET.add(unique_id)
    while len(_SEEN_ORDER) > _SEEN_IDS_MAX:
        _SEEN_SET.discard(_SEEN_ORDER.popleft())


def _message_unique_id(msg: dict) -> str:
    """unique_id, else id (a dict {"id": ...} or a plain string), else message_id."""
    uid = msg.get("unique_id")
    if uid:
        return str(uid)
    mid = msg.get("id")
    if isinstance(mid, dict):
        return str(mid.get("id") or "")
    return str(mid or msg.get("message_id") or "")


def _webhook_authorised(request: Request) -> bool:
    """True when no WEBHOOK_SECRET is configured, or the caller sent it (header or ?token=)."""
    if not config.WEBHOOK_SECRET:
        return True
    given = request.headers.get("x-webhook-secret") or request.query_params.get("token") or ""
    return hmac.compare_digest(given.encode(), config.WEBHOOK_SECRET.encode())


@app.post("/webhook")
async def webhook(request: Request):
    if not _webhook_authorised(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)

    data = await request.json()
    event = data.get("event") or data.get("event_type")
    # Log the event type only: full payloads hold client messages and must not sit in server logs.
    print(f"=== Incoming Periskope event: {event} ===")

    if event == "message.flagged":
        return await _handle_flag(data.get("data", {}))

    if event != "message.created":
        return {"status": "ignored"}

    msg = data.get("data", {}) or {}
    unique_id = _message_unique_id(msg)
    if _is_seen(unique_id):
        print(f"[main] Already saved unique_id={unique_id!r} — skipping duplicate.")
        return {"status": "duplicate"}
    if unique_id and unique_id in _IN_FLIGHT:
        print(f"[main] unique_id={unique_id!r} is being processed right now, skipping this copy.")
        return {"status": "duplicate"}
    if unique_id:
        _IN_FLIGHT.add(unique_id)
    try:
        return await _process_once(msg, unique_id)
    finally:
        # Always released, even if the request is cancelled mid-way, so a message can never stay blocked.
        _IN_FLIGHT.discard(unique_id)


async def _process_once(msg: dict, unique_id: str):
    try:
        # Downloads, uploads and database calls block; run them in a worker thread so one slow file
        # never stops the server from answering other webhooks.
        result = await run_in_threadpool(_process_message, msg, unique_id)
    except Exception as e:
        # Not marked seen: the next copy of this message (another phone) or Periskope's retry
        # processes it again instead of it being lost.
        print(f"[main] ERROR processing unique_id={unique_id!r}: {e}")
        if msg.get("media"):
            failures.record(
                "file",
                client_name=str(msg.get("chat_id") or ""),
                chat_id=str(msg.get("chat_id") or ""),
                unique_id=unique_id,
                filename=None,
                reason=f"exception: {e}",
            )
        return JSONResponse({"status": "error"}, status_code=500)

    if result.get("status") == "failed":
        return JSONResponse(result, status_code=500)
    _mark_seen(unique_id)
    return result


def _save_to_knowledge_base(client: dict, chat_id: str, sender_phone: str, body: str, msg: dict, unique_id: str) -> None:
    """Save a text message or caption to client_messages, only in the client's OWN registered group.
    Outgoing = sent from a connected Periskope phone (from_me) OR by any team number."""
    if not config.SAVE_CHAT_MESSAGES_ENABLED:
        return
    registered_group_id = db.get_registered_group_id(client["client_id"])
    if not (registered_group_id and chat_id.split("@")[0] == registered_group_id):
        return
    outgoing = bool(msg.get("from_me")) or db.is_team_number(sender_phone)
    db.save_message(
        client_id=client["client_id"],
        whatsapp_group_id=chat_id,
        sender_phone=sender_phone,
        body=body,
        message_timestamp=msg.get("timestamp") or "",
        unique_id=unique_id,
        direction="outgoing" if outgoing else "incoming",
    )


def _process_message(msg: dict, unique_id: str) -> dict:
    """Map the message to its client and save it. Returns {"status": "ok" | "ignored" | "failed"}."""
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
        # client (no-op if the number already exists anywhere, or is a team number).
        db.add_contact(client["client_id"], sender_phone, client["client_name"])

    elif db.group_is_mapped(chat_id):
        # The group already belongs to a client that is not active. Never let the sender / member
        # fallbacks below re-link it to some other client.
        print(f"[main] Group '{chat_id}' belongs to an inactive client — skipping.")
        return {"status": "ignored"}

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
                group_name = db.fetch_group_name(chat_id) or ""
                if db.is_va_group_name(group_name):
                    # Looks like a client group but no client matches: say so, never drop it silently.
                    failures.record_unlinked_group(chat_id, group_name, unique_id)
                    print(f"[main] VA group '{group_name}' ({chat_id}) is not linked to a client — recorded.")
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

        filename = naming.resolve_filename(media_obj, mimetype, msg.get("timestamp"))

        if not raw_path:
            # Periskope sometimes has not stored the file yet. Record it (so it is visible and
            # recoverable) and fail, so the next copy of this message tries again.
            failures.record("file", client_name=client_name, chat_id=chat_id, unique_id=unique_id,
                            filename=filename, reason="no media path in the webhook")
            return {"status": "failed", "reason": "no media path"}

        media_url = naming.media_url(raw_path)
        print(f"Media URL : {naming.url_for_log(media_url)}")
        print(f"Filename  : {filename}")

        # The message time picks the date sub-folder (and dates the file), the same rule catch-up uses.
        saved = storage.save_media(folder_name, media_url, filename, parent_name, message_timestamp=msg.get("timestamp"))

        if not saved:
            failures.record("file", client_name=client_name, chat_id=chat_id, unique_id=unique_id,
                            filename=filename, reason="download or SharePoint upload failed (see the log above)")
            return {"status": "failed", "reason": "file not saved"}

        # The caption on a photo or document is real text ("invoice for Oct, please book it"): keep it
        # in the knowledge base too, not only the file.
        if body.strip():
            _save_to_knowledge_base(client, chat_id, sender_phone, body, msg, unique_id)

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
        #
        # SAVE_CHAT_MESSAGES_ENABLED=false hands this capture to the Client-Score
        # webhook service, which applies the same rule. Only this write is
        # skipped — the notes.txt save above still runs.
        _save_to_knowledge_base(client, chat_id, sender_phone, body, msg, unique_id)

    else:
        print(f"[main] Empty '{message_type}' message with no media/body — skipping.")

    return {"status": "ok"}


async def _handle_flag(msg: dict):
    """
    A message was flagged in Periskope. If it came from a customer (not a VA)
    and we haven't already alerted on it, DM the client's assigned VA on Teams.
    """
    message_id = msg.get("message_id") or _message_unique_id(msg)
    chat_id = msg.get("chat_id", "")
    sender_phone = msg.get("sender_phone") or msg.get("author") or ""
    body = msg.get("body") or ""

    print(f"[flag] message_id={message_id!r} chat={chat_id} sender={sender_phone}")

    # Skip flags raised by a VA — we only alert on customer messages.
    if db.is_va_number(sender_phone) or db.is_team_number(sender_phone):
        print(f"[flag] Sender '{sender_phone}' is a VA — skipping.")
        return {"status": "ok"}

    # Identify the client: by the GROUP first (a contact can sit in several clients' groups, so the
    # sender's number alone can point at the wrong client), then by sender only for 1:1 chats.
    client = db.find_client_by_group(chat_id) if chat_id.endswith("@g.us") else None
    if not client and not chat_id.endswith("@g.us"):
        client = db.find_client_by_number(sender_phone)
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
    sent = await teams.notify_flag(
        folder_name=folder_name,
        sender_phone=sender_phone,
        body=body,
        teams_chat_id=va["teams_chat_id"],
    )
    if sent:
        db.mark_flag_notified(message_id)  # only when the VA was actually told
    return {"status": "ok"}


