"""
backfill_chats.py — One-time backfill of historical WhatsApp *text* messages
from Periskope into the client_messages knowledge-base table.

The live webhook (main.py) writes each new text message into client_messages via
db.save_message(). This script does the same thing for the chat history that
already exists in Periskope from *before* live ingestion started, so the
organizer service can extract facts/credentials from that history too.

What it does, per client:
  1. Resolves the client's chat_id from clients.whatsapp_group_id (same lookup
     main.py uses, via db.find_client_by_group).
  2. Pages through the client's full message history from the Periskope API.
  3. Keeps ONLY plain text messages — anything carrying a media object is
     skipped (no images, documents, audio, video, stickers, etc.).
  4. Applies the exact same capture gate the live path applies: only capture
     messages sent in the client's OWN registered VA group. Both directions
     (client and VA replies) are captured — from_me resolves to direction.
  5. Inserts each kept message with db.save_message(), passing the message's own
     Periskope unique_id (the dedup key), its own original timestamp (never the
     time this script runs), and its resolved direction.

Design constraints (do not change without re-reading the task):
  - Periskope API is the ONLY source. No SharePoint, no notes.txt.
  - Text only. Never fetch or store media/documents.
  - Reuse db.save_message() for every insert — no second insert path.
  - Dedup on Periskope's unique_id, exactly like the live webhook, so the table's
    UNIQUE(dedup_hash) constraint makes re-runs produce zero duplicate rows.
  - processed_at is left null on every row (save_message never sets it), so the
    organizer picks backfilled history up on its own schedule.
  - A client whose history can't be fetched (error / empty / unmapped / inactive)
    is skipped with a log line — never a fatal stop.

Run once from the project root.

    # Single known test client first (recommended before the full run):
    python backfill_chats.py <chat_id>          e.g. 120363xxxxxxxxx@g.us

    # All VA clients:
    python backfill_chats.py

This file is standalone: it is imported/called by nothing, is not part of the
running webhook app, and is not triggered by any scheduler.
"""

import sys

import backfill  # reuse the proven Periskope group-discovery + message paging
import db


def _capture_message(msg: dict, chat_id: str, client: dict) -> bool:
    """
    Insert one historical message into client_messages if — and only if — it is a
    plain text message the live path would also have captured.

    Returns True if we attempted an insert (save_message decides duplicate vs new
    and swallows its own errors), False if the message was skipped by the gate.

    The gate mirrors main.py's knowledge-base rules exactly:
      1. Text only — skip anything carrying a media object.
      2. Must have a non-empty body.
    Both directions (client and VA) are captured — from_me tells us which side.
    (The "own registered VA group" rule is enforced once per client by the caller,
    since every message here comes from that single resolved group.)
    """
    # 1. Text only — a media object means image/doc/audio/video/sticker/etc.
    media_obj = msg.get("media") or {}
    if media_obj:
        return False

    # 2. Must actually have text.
    body = msg.get("body") or msg.get("text") or ""
    if not body.strip():
        return False

    # Periskope's own message id — the same dedup key the live webhook passes.
    # Fall back to the nested id.id shape main.py also tolerates.
    unique_id = msg.get("unique_id") or (msg.get("id") or {}).get("id") or ""

    # The message's OWN original timestamp — never "now". save_message writes this
    # straight into message_timestamp so history stays correctly time-ordered.
    timestamp = msg.get("timestamp") or msg.get("created_at") or ""

    db.save_message(
        client_id=client["client_id"],
        whatsapp_group_id=chat_id,
        sender_phone=msg.get("sender_phone") or msg.get("author") or "",
        body=body,
        message_timestamp=timestamp,
        unique_id=unique_id or None,
        direction="outgoing" if msg.get("from_me") else "incoming",
    )
    return True


def backfill_client(chat_id: str, group_name: str = "", gm_phone: str = None) -> tuple[int, int, int]:
    """
    Backfill one client's historical text messages into client_messages.

    Resolves the client from the chat_id via clients.whatsapp_group_id (the same
    lookup main.py uses). Returns (captured, skipped, total). Any problem —
    unmapped/inactive client, Periskope error, empty history — is logged and
    returns zeros instead of raising, so the caller moves on to the next client.
    """
    label = group_name or chat_id
    print(f"\n[backfill_chats] === {label} ===")

    # Resolve chat_id -> client the same way main.py does. This returns None for
    # an unmapped group AND for an inactive client (fail-closed), so both are
    # skipped here without any special-casing.
    client = db.find_client_by_group(chat_id)
    if not client:
        print(f"[backfill_chats] No active client mapped to '{label}' — skipping.")
        return 0, 0, 0

    # Only capture messages sent in the client's OWN registered VA group, exactly
    # like the live path. Since we resolved this client *from* chat_id via
    # whatsapp_group_id, chat_id already IS their registered group — but assert it
    # explicitly so the rule is visible and a mismatched call is skipped, not
    # silently mis-attributed.
    registered_group_id = db.get_registered_group_id(client["client_id"])
    if not registered_group_id or chat_id.split("@")[0] != registered_group_id:
        print(
            f"[backfill_chats] '{label}' ({chat_id}) is not "
            f"'{client['client_name']}' own registered VA group "
            f"({registered_group_id!r}) — skipping."
        )
        return 0, 0, 0

    print(f"[backfill_chats] Client: '{client['client_name']}' — fetching history...")

    # Page through the full history via the proven Periskope paging helper.
    # Errors inside are already caught and logged there; it returns [] on failure.
    messages = backfill.fetch_all_messages(chat_id, gm_phone)
    if not messages:
        print(f"[backfill_chats] No accessible history for '{label}' — skipping.")
        return 0, 0, 0

    print(f"[backfill_chats] {len(messages)} message(s) to scan (text only).")

    captured = 0
    skipped = 0
    for msg in messages:
        if _capture_message(msg, chat_id, client):
            captured += 1
        else:
            skipped += 1

    print(
        f"[backfill_chats] Done '{client['client_name']}': "
        f"{captured} text message(s) inserted/deduped, {skipped} non-text/skipped."
    )
    return captured, skipped, len(messages)


def _resolve_groups(target_chat_id: str | None) -> list[dict]:
    """
    Build the list of {chat_id, chat_name, gm_phone} groups to process.

    - No argument: discover every VA group across all connected phones, reusing
      backfill.list_va_groups() so we get the correct gm_phone for each group
      (some groups are only visible to one phone).
    - A chat_id argument: process just that one group. We still discover groups so
      we can pass the right gm_phone; if it isn't found we fall back to the
      default phone (None) rather than fail.
    """
    va_groups = backfill.list_va_groups()

    if not target_chat_id:
        return va_groups

    for g in va_groups:
        if g["chat_id"] == target_chat_id:
            return [g]

    # Not among the discovered VA groups — still allow an explicit run against it,
    # using the default phone. find_client_by_group will skip it if unmapped.
    print(
        f"[backfill_chats] '{target_chat_id}' not found among discovered VA "
        f"groups — trying it with the default phone."
    )
    return [{"chat_id": target_chat_id, "chat_name": target_chat_id, "gm_phone": None}]


def main():
    target_chat_id = sys.argv[1].strip() if len(sys.argv) > 1 else None

    print("=" * 60)
    print("  va-bot chat backfill — historical TEXT -> client_messages")
    if target_chat_id:
        print(f"  Single client: {target_chat_id}")
    else:
        print("  All VA clients")
    print("=" * 60)

    groups = _resolve_groups(target_chat_id)
    if not groups:
        print("\n[backfill_chats] No groups to process. Check Periskope config.")
        sys.exit(0)

    total_captured = 0
    total_scanned = 0
    for g in groups:
        captured, _skipped, scanned = backfill_client(
            g["chat_id"], g.get("chat_name", ""), g.get("gm_phone")
        )
        total_captured += captured
        total_scanned += scanned

    print("\n" + "=" * 60)
    print("  Chat backfill complete.")
    print(f"  Groups processed        : {len(groups)}")
    print(f"  Messages scanned        : {total_scanned}")
    print(f"  Text messages inserted  : {total_captured}  (duplicates auto-skipped)")
    print("=" * 60)


if __name__ == "__main__":
    main()
