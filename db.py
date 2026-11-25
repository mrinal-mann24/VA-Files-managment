"""
Supabase lookups and writes for the va-bot.
"""

import hashlib
import re
from supabase import create_client, Client
import httpx
import config

_supabase: Client = create_client(
    config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY
)

# Patterns that mark a group as a VA client group.
# Matches "VA" or "Virtual Accounting" anywhere in the name, with or without
# the "<>" separator (e.g. "Client <> VA", "Client<>Virtual Accounting services",
# "Leapmile-Korefi - Virtual Accounting Team"). Not AI Accountant.
_CLIENT_GROUP_PATTERNS = [
    re.compile(r"\bVirtual Accounting\b", re.IGNORECASE),
    re.compile(r"<>\s*VA\b", re.IGNORECASE),
    # Matches "X VA", "- VA", "<> VA" at end of name (e.g. "Goodwill fabrics X VA")
    re.compile(r"[\-<>xX]\s*VA\b\s*(?:Team|Group|Services)?\s*$", re.IGNORECASE),
    # VA clients sold under the AiA name ("Azam <> AIA", "Rare you<>AIA"). Still needs a member who is a
    # contact of an unmapped VA client before a group is linked, so AI Accountant product groups are not.
    re.compile(r"<>\s*AIA\b", re.IGNORECASE),
]

# No x-phone: Periskope then answers for every connected org phone. Pinning one phone (a GM's, which is
# not a member of most VA groups) made group-name and member lookups return 404, so new groups could not
# be linked to their client.
_PERISKOPE_HEADERS = {
    "authorization": f"Bearer {config.PERISKOPE_API_KEY}",
}


# --- Team numbers ---------------------------------------------------------------------------------
# vas.whatsapp_number plus the settings row 'team_whatsapp_numbers' (comma list), last 10 digits.
# Team members are never added as client contacts and their messages count as outgoing.
_TEAM_CACHE: dict = {"at": 0.0, "numbers": frozenset()}
_TEAM_TTL_SECONDS = 300


def _last10(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    return digits[-10:] if len(digits) >= 10 else ""


def team_numbers() -> frozenset:
    import time
    now = time.monotonic()
    if _TEAM_CACHE["numbers"] and now - _TEAM_CACHE["at"] < _TEAM_TTL_SECONDS:
        return _TEAM_CACHE["numbers"]
    found: set[str] = set()
    try:
        for row in _supabase.table("vas").select("whatsapp_number").execute().data or []:
            n = _last10(row.get("whatsapp_number"))
            if n:
                found.add(n)
        rows = _supabase.table("settings").select("value").eq("key", "team_whatsapp_numbers").limit(1).execute().data or []
        for part in re.split(r"[,;\s]+", (rows[0].get("value") if rows else "") or ""):
            n = _last10(part)
            if n:
                found.add(n)
    except Exception as e:
        print(f"[db] ERROR loading team numbers (keeping the previous list): {e}")
        return _TEAM_CACHE["numbers"]
    _TEAM_CACHE["numbers"] = frozenset(found)
    _TEAM_CACHE["at"] = now
    return _TEAM_CACHE["numbers"]


def is_team_number(number: str | None) -> bool:
    n = _last10(number)
    return bool(n) and n in team_numbers()


def is_team_sender(msg: dict) -> bool:
    """True when a Periskope message was posted by our team: from a connected Periskope phone (from_me) or a
    team number. Files from the team are not saved to SharePoint: client folders hold only what the client sent."""
    sender = msg.get("sender_phone") or msg.get("author") or ""
    return bool(msg.get("from_me")) or is_team_number(sender)


def is_va_group_name(name: str | None) -> bool:
    return any(p.search(name or "") for p in _CLIENT_GROUP_PATTERNS)


def group_is_mapped(chat_id: str) -> bool:
    """True when ANY client (active or not) already owns this group. Such a group must never be re-linked
    to another client through the sender / member fallbacks."""
    try:
        rows = (
            _supabase.table("clients").select("client_id").eq("whatsapp_group_id", _strip(chat_id)).limit(1).execute().data
            or []
        )
        return bool(rows)
    except Exception as e:
        print(f"[db] ERROR checking group mapping for {chat_id}: {e}")
        return True  # fail closed: never re-link when unsure


def _strip(whatsapp_id: str) -> str:
    """Strip @c.us / @g.us suffix and return bare number/id."""
    return (whatsapp_id or "").split("@")[0]


def _is_active(status: str | None) -> bool:
    """
    A client is considered ACTIVE only when clients.status is exactly 'active'
    (case-insensitive). Anything else — 'Inactive', 'Paused', blank, NULL, a typo —
    is treated as inactive so the bot skips the group entirely. Fail-closed: if we
    don't clearly see 'active', we don't process.
    """
    return (status or "").strip().lower() == "active"


def is_client_active(client_id: str) -> bool:
    """
    Look up a client's status by id and return True only if it's active.
    Returns False on missing client or any error (fail-closed).
    """
    if not client_id:
        return False
    try:
        resp = (
            _supabase.table("clients")
            .select("status")
            .eq("client_id", client_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return False
        return _is_active(rows[0].get("status"))
    except Exception as e:
        print(f"[db] ERROR checking status for client {client_id}: {e}")
        return False


def get_registered_group_id(client_id: str) -> str | None:
    """
    Return the client's own registered whatsapp_group_id (bare, no @g.us) from
    the clients table, or None if unset / not found / on error.

    Read fresh from clients rather than trusting a lookup dict, because the Step-2
    lookup path fills the dict's whatsapp_group_id with the *incoming* group. Used
    to gate knowledge-base capture to a client's official VA group only.
    """
    if not client_id:
        return None
    try:
        resp = (
            _supabase.table("clients")
            .select("whatsapp_group_id")
            .eq("client_id", client_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        return _strip(rows[0].get("whatsapp_group_id") or "") or None
    except Exception as e:
        print(f"[db] ERROR fetching registered group id for client {client_id}: {e}")
        return None


def folder_name_for(row: dict) -> str:
    """The ONE rule for a client's SharePoint folder name: the stored whatsapp_group_name, else client_name.
    Never WhatsApp's current (live) group name: groups get renamed, and using the live name is what
    created duplicate folders (e.g. "KRRD" and "KRRD - Virtual Accounting"). Live bot, backfill and
    recovery all call this, so every file for a client lands in the same folder."""
    return (row.get("whatsapp_group_name") or "").strip() or row.get("client_name")


def _get_parent_name(parent_client_id: str | None) -> str | None:
    """
    Return the parent client's FOLDER name (its whatsapp_group_name, falling back
    to client_name). This must match exactly how the parent's own folder is named
    everywhere else — folder_name = whatsapp_group_name — so sub-group folders nest
    inside the parent's existing folder instead of creating a new one.
    Returns None if there is no parent.
    """
    if not parent_client_id:
        return None
    try:
        resp = (
            _supabase.table("clients")
            .select("client_name, whatsapp_group_name")
            .eq("client_id", parent_client_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        return folder_name_for(rows[0])
    except Exception as e:
        print(f"[db] ERROR fetching parent folder name: {e}")
        return None


def find_client_by_number(whatsapp_number: str) -> dict | None:
    """
    Look up a WhatsApp number in client_contacts.
    Returns dict {client_id, client_name, folder_name, whatsapp_group_id} or None.
    Tries with and without leading '+'.
    """
    if not whatsapp_number:
        return None

    number = _strip(whatsapp_number)
    candidates = [number, "+" + number] if not number.startswith("+") else [number, number[1:]]

    for candidate in candidates:
        try:
            resp = (
                _supabase.table("client_contacts")
                .select(
                    "contact_name, whatsapp_number, client_id, "
                    "clients(client_name, whatsapp_group_id, whatsapp_group_name, parent_client_id, status)"
                )
                .eq("whatsapp_number", candidate)
                .limit(1)
                .execute()
            )
        except Exception as e:
            print(f"[db] ERROR querying for {candidate}: {e}")
            return None

        rows = resp.data or []
        if rows:
            row = rows[0]
            client = row.get("clients") or {}
            client_name = client.get("client_name")
            if not client_name:
                return None
            # Skip inactive clients entirely — no folder, no upload, no notify.
            if not _is_active(client.get("status")):
                print(f"[db] Client '{client_name}' is INACTIVE (status={client.get('status')!r}) — skipping.")
                return None
            folder_name = folder_name_for(client)
            parent_name = _get_parent_name(client.get("parent_client_id"))
            print(f"[db] Number match: {candidate} -> '{client_name}' (folder: '{folder_name}', parent: '{parent_name}')")
            return {
                "client_id": row.get("client_id"),
                "client_name": client_name,
                "folder_name": folder_name,
                "contact_name": row.get("contact_name"),
                "whatsapp_group_id": client.get("whatsapp_group_id"),
                "parent_name": parent_name,
            }

    return None


def find_client_by_group(chat_id: str) -> dict | None:
    """
    Look up a client by their WhatsApp group ID.
    Returns dict {client_id, client_name, folder_name, whatsapp_group_id} or None.
    """
    if not chat_id:
        return None

    group_id = _strip(chat_id)

    try:
        resp = (
            _supabase.table("clients")
            .select("client_id, client_name, whatsapp_group_id, whatsapp_group_name, parent_client_id, status")
            .eq("whatsapp_group_id", group_id)
            # A group linked to a parent AND a subgroup resolves to the subgroup, the same choice recover_files and
            # catchup make, so live files and recovered files land in the same folder.
            .order("parent_client_id", nullsfirst=False)
            .limit(1)
            .execute()
        )
    except Exception as e:
        print(f"[db] ERROR querying for group {group_id}: {e}")
        return None

    rows = resp.data or []
    if rows:
        row = rows[0]
        # Skip inactive clients entirely — no folder, no upload, no notify.
        if not _is_active(row.get("status")):
            print(f"[db] Client '{row.get('client_name')}' is INACTIVE (status={row.get('status')!r}) — skipping.")
            return None
        # A blank stored name is NOT filled from WhatsApp here: this client's files already live in the
        # client_name folder, and switching to the WhatsApp name would start a second folder.
        folder_name = folder_name_for(row)
        parent_name = _get_parent_name(row.get("parent_client_id"))
        print(f"[db] Group match: {group_id} -> '{row['client_name']}' (folder: '{folder_name}', parent: '{parent_name}')")
        return {
            "client_id": row["client_id"],
            "client_name": row["client_name"],
            "folder_name": folder_name,
            "contact_name": None,
            "whatsapp_group_id": group_id,
            "parent_name": parent_name,
        }

    print(f"[db] No client for group '{group_id}'.")
    return None


def list_mapped_groups() -> list[dict]:
    """
    Return every ACTIVE client that already has a whatsapp_group_id saved in
    Supabase — i.e. every group backfill should process, regardless of whether
    its name matches _CLIENT_GROUP_PATTERNS.

    _CLIENT_GROUP_PATTERNS is only needed to *discover new, unmapped* groups by
    name (map_group_to_client). Once a group is mapped — including sub-groups
    like "BR Parlours Admin Group" or "NEE Accounts" that don't contain "VA" —
    it belongs here regardless of name, exactly like the live webhook (which
    trusts whatsapp_group_id, never the name, once mapped).

    Returns [{chat_id, chat_name}], chat_id bare (no @g.us — callers append it).
    """
    try:
        resp = (
            _supabase.table("clients")
            .select("client_id, client_name, whatsapp_group_id, whatsapp_group_name, status")
            .not_.is_("whatsapp_group_id", "null")
            .execute()
        )
    except Exception as e:
        print(f"[db] ERROR listing mapped groups: {e}")
        return []

    groups = []
    for row in resp.data or []:
        if not _is_active(row.get("status")):
            continue
        group_id = _strip(row.get("whatsapp_group_id") or "")
        if not group_id:
            continue
        groups.append({
            "chat_id": f"{group_id}@g.us",
            "chat_name": row.get("whatsapp_group_name") or row.get("client_name") or group_id,
        })
    return groups


def update_group_id(client_id: str, group_id: str, group_name: str):
    """
    Save whatsapp_group_id and whatsapp_group_name on a client row.
    Only writes if whatsapp_group_id is currently null (never overwrites).
    """
    bare = _strip(group_id)
    try:
        _supabase.table("clients").update({
            "whatsapp_group_id": bare,
            "whatsapp_group_name": group_name,
        }).eq("client_id", client_id).is_("whatsapp_group_id", "null").execute()
        print(f"[db] Saved group '{group_name}' (id={bare}) on client {client_id}")
    except Exception as e:
        print(f"[db] ERROR updating group: {e}")


def add_contact(client_id: str, whatsapp_number: str, contact_name: str = None):
    """
    Add a new number to client_contacts so future messages are auto-recognised.
    Silently skips if the number already exists.
    """
    bare = _strip(whatsapp_number)
    if not bare:
        return
    if is_team_number(bare):
        return  # team members are in every group; they must never become a client's contact

    try:
        resp = (
            _supabase.table("client_contacts")
            .select("contact_id")
            .eq("whatsapp_number", bare)
            .limit(1)
            .execute()
        )
        if resp.data:
            return  # already there
    except Exception as e:
        print(f"[db] ERROR checking existing contact: {e}")
        return

    try:
        _supabase.table("client_contacts").insert({
            "client_id": client_id,
            "whatsapp_number": bare,
            "contact_name": contact_name or f"Auto-added ({bare})",
            "role": "member",
            "is_primary": False,
        }).execute()
        print(f"[db] Auto-added contact {bare} to client {client_id}")
    except Exception as e:
        print(f"[db] ERROR inserting contact: {e}")


CAPTION_MAX = 2000


def insert_upload(row: dict, caption: str | None):
    """Insert one document_uploads row, with the file's caption when there is one. If the caption column is not
    there (Shadow's supabase/applied/2026-11-24-document-uploads-caption.sql, applied 24 Nov 2026, was rolled back), insert again without it, so the
    file is still logged."""
    text = (caption or "").strip()[:CAPTION_MAX]
    if not text:
        return _supabase.table("document_uploads").insert(row).execute()
    try:
        return _supabase.table("document_uploads").insert({**row, "caption": text}).execute()
    except Exception as e:
        if "caption" not in str(e):
            raise
        print("[db] document_uploads.caption is missing; logged the file without its caption.")
        return _supabase.table("document_uploads").insert(row).execute()


def log_upload(
    client_id: str,
    client_name: str,
    folder_name: str,
    group_id: str,
    group_name: str,
    sender_phone: str,
    file_name: str,
    file_type: str,
    file_size: int,
    caption: str = "",
) -> str | None:
    """
    Insert one row into document_uploads for every file saved.
    caption: the text the sender typed under the file on WhatsApp (stored on the row so Shadow can show it).
    Returns the upload_id (uuid) or None on failure.
    """
    try:
        row = {
            "client_id": client_id,
            "client_name": client_name,
            "folder_name": folder_name,
            "group_id": _strip(group_id),
            "group_name": group_name,
            "sender_phone": _strip(sender_phone),
            "file_name": file_name,
            "file_type": file_type,
            "file_size": file_size,
            "notified": False,
        }
        resp = insert_upload(row, caption)
        upload_id = (resp.data or [{}])[0].get("upload_id")
        print(f"[db] Logged upload '{file_name}' for '{client_name}' (id={upload_id})")
        return upload_id
    except Exception as e:
        print(f"[db] ERROR logging upload: {e}")
        return None


def _message_dedup_hash(
    client_id: str | None,
    whatsapp_group_id: str | None,
    message_timestamp: str,
    body: str,
) -> str:
    """
    Content-based fallback dedup key for a chat message.

    Used only when Periskope's unique_id isn't available (e.g. a future backfill
    from existing notes.txt files, which have no Periskope id). Hashes the
    client/group + message timestamp + normalized body so the same message always
    maps to the same key regardless of who ingested it or when. Kept general on
    purpose — not hardcoded to assume a unique_id exists.
    """
    normalized_body = (body or "").strip()
    parts = [
        client_id or "",
        _strip(whatsapp_group_id or ""),
        message_timestamp or "",
        normalized_body,
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_message(
    client_id: str | None,
    whatsapp_group_id: str | None,
    sender_phone: str,
    body: str,
    message_timestamp: str,
    unique_id: str | None = None,
    direction: str | None = None,
) -> str | None:
    """
    Insert one chat message into client_messages for the knowledge base.

    Dedup key is Periskope's unique_id when available, otherwise a content-based
    hash (see _message_dedup_hash) so a future notes.txt backfill can reuse this
    same path. The table's UNIQUE(dedup_hash) constraint is the persistent guard —
    a repeat insert simply fails and is treated as a no-op.

    direction ('incoming' | 'outgoing') is resolved by the caller from Periskope's
    from_me flag, never guessed here.

    Returns the message_id (uuid) or None on failure / duplicate. Callers must
    treat any failure here as non-fatal — this write must never interrupt the
    existing SharePoint save / Teams notify flow.
    """
    if not (body or "").strip():
        return None

    dedup_hash = (unique_id or "").strip() or _message_dedup_hash(
        client_id, whatsapp_group_id, message_timestamp, body
    )

    try:
        resp = _supabase.table("client_messages").insert({
            "client_id": client_id,
            "whatsapp_group_id": _strip(whatsapp_group_id or "") or None,
            "sender_phone": _strip(sender_phone or "") or None,
            "body": body,
            "message_timestamp": message_timestamp,
            "dedup_hash": dedup_hash,
            "direction": direction,
            "source": "periskope",
        }).execute()
        message_id = (resp.data or [{}])[0].get("message_id")
        print(f"[db] Saved message to knowledge base (id={message_id}, dedup={dedup_hash[:12]}...)")
        return message_id
    except Exception as e:
        # Never raise (this write must not break the file save). A duplicate is normal; anything else is a real loss
        # for now, which the nightly catch-up (catchup.py) recovers.
        if "23505" in str(e) or "duplicate key" in str(e).lower():
            print(f"[db] Message already saved (dedup={dedup_hash[:12]}...).")
        else:
            print(f"[db] ERROR saving message (dedup={dedup_hash[:12]}...): {type(e).__name__}; the nightly catch-up will retry.")
        return None


def get_unnotified_count(client_id: str) -> int:
    """Count how many uploads haven't been notified yet for this client."""
    try:
        resp = (
            _supabase.table("document_uploads")
            .select("upload_id", count="exact")
            .eq("client_id", client_id)
            .eq("notified", False)
            .execute()
        )
        return resp.count or 0
    except Exception as e:
        print(f"[db] ERROR counting unnotified uploads: {e}")
        return 0


def mark_notified(client_id: str):
    """Flip notified=true for all pending uploads of this client."""
    try:
        _supabase.table("document_uploads").update(
            {"notified": True}
        ).eq("client_id", client_id).eq("notified", False).execute()
        print(f"[db] Marked uploads as notified for client {client_id}")
    except Exception as e:
        print(f"[db] ERROR marking notified: {e}")


def get_va_for_client(client_id: str) -> dict | None:
    """
    Look up the assigned VA for a client.
    Returns dict {va_name, teams_chat_id} or None if not found / not configured.
    """
    try:
        resp = (
            _supabase.table("clients")
            .select("va_id, vas(va_name, teams_chat_id)")
            .eq("client_id", client_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        va = rows[0].get("vas")
        if not va or not va.get("teams_chat_id"):
            return None
        return {"va_name": va["va_name"], "teams_chat_id": va["teams_chat_id"]}
    except Exception as e:
        print(f"[db] ERROR fetching VA for client {client_id}: {e}")
        return None


def is_va_number(whatsapp_number: str) -> bool:
    """
    True if the given WhatsApp number belongs to a VA (not a customer).
    Used to skip flag alerts raised by VAs themselves.
    Compares bare digits against vas.whatsapp_number.
    """
    if not whatsapp_number:
        return False

    number = _strip(whatsapp_number)
    candidates = [number, "+" + number] if not number.startswith("+") else [number, number[1:]]

    try:
        resp = (
            _supabase.table("vas")
            .select("va_id")
            .in_("whatsapp_number", candidates)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as e:
        print(f"[db] ERROR checking VA number {whatsapp_number}: {e}")
        return False


def record_flagged_message(
    message_id: str,
    client_id: str | None,
    client_name: str | None,
    chat_id: str,
    sender_phone: str,
    body: str,
) -> bool:
    """
    Insert a flagged message into flagged_messages.
    Returns True if this is the first time we've seen this message_id (proceed),
    False if it already exists (duplicate — skip). Dedup relies on the
    message_id PRIMARY KEY, so a repeat insert fails and we return False.
    """
    if not message_id:
        # No id to dedup on — allow it through rather than lose the alert.
        return True
    try:
        _supabase.table("flagged_messages").insert({
            "message_id": message_id,
            "client_id": client_id,
            "client_name": client_name,
            "chat_id": _strip(chat_id),
            "sender_phone": _strip(sender_phone),
            "body": body,
        }).execute()
        return True
    except Exception as e:
        # Only a duplicate key means "already alerted". Any other error must not swallow the alert.
        text = str(e).lower()
        if "duplicate key" in text or "23505" in text:
            print(f"[db] Flag {message_id} already processed — skipping.")
            return False
        print(f"[db] ERROR recording flag {message_id} (alerting anyway): {e}")
        return True


def mark_flag_notified(message_id: str):
    """Stamp notified_at on a flagged message once the Teams alert is sent."""
    if not message_id:
        return
    try:
        _supabase.table("flagged_messages").update(
            {"notified_at": "now()"}
        ).eq("message_id", message_id).execute()
    except Exception as e:
        print(f"[db] ERROR stamping flag notified {message_id}: {e}")


def fetch_group_name(chat_id: str) -> str | None:
    """Fetch just the group name from Periskope for a given chat_id."""
    try:
        resp = httpx.get(
            f"{config.PERISKOPE_BASE_URL}/chats/{chat_id}",
            headers=_PERISKOPE_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("chat_name") or data.get("name") or None
    except Exception as e:
        print(f"[db] ERROR fetching group name for {chat_id}: {e}")
        return None


def find_client_by_group_members(chat_id: str) -> dict | None:
    """
    Step 3 — called only when a message arrives in an unmapped group.

    1. Fetch group from Periskope → check name has '<> VA' or '<> Virtual Accounting'.
    2. Fetch all member numbers from the group.
    3. Cross-reference each number against client_contacts in Supabase.
    4. First match → save group_id + group_name (once, never overwrite).
    5. Return client dict with folder_name = group_name.
    """
    # --- 1. Fetch group info ---
    try:
        resp = httpx.get(
            f"{config.PERISKOPE_BASE_URL}/chats/{chat_id}",
            headers=_PERISKOPE_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        group_data = resp.json()
    except Exception as e:
        print(f"[db] ERROR fetching group info for {chat_id}: {e}")
        return None

    group_name = group_data.get("chat_name") or group_data.get("name") or ""
    print(f"[db] Group name: '{group_name}'")

    # --- 2. Check it's a VA client group ---
    if not any(p.search(group_name) for p in _CLIENT_GROUP_PATTERNS):
        print(f"[db] '{group_name}' is not a VA client group — skipping.")
        return None

    # --- 3. Get all member numbers ---
    members: dict = group_data.get("members") or {}
    if not members:
        print(f"[db] No members found in group '{group_name}'.")
        return None

    print(f"[db] {len(members)} members — checking against Supabase...")

    # --- 4. Cross-reference members against client_contacts ---
    # Only a client that has NO group yet can claim this group. Internal team
    # numbers (and shared contacts) are members of many groups and registered
    # under some client that is already mapped — taking the first match would
    # file this new group under them.
    for raw_number in members.keys():
        if is_team_number(raw_number):
            continue  # a team member's number can never decide which client a group belongs to
        client = find_client_by_number(raw_number)
        if not client:
            continue
        if client.get("whatsapp_group_id"):
            print(f"[db] Member {raw_number} belongs to '{client['client_name']}' which is already mapped to another group — skipping.")
            continue
        print(f"[db] Member {raw_number} matched client '{client['client_name']}'")
        # --- 5. Save group ID + group name (once, never overwrite) ---
        update_group_id(client["client_id"], chat_id, group_name)
        # Always return folder_name = group_name
        client["folder_name"] = group_name
        # parent_name already populated by find_client_by_number
        return client

    print(f"[db] No unmapped client matched any member of '{group_name}'.")
    return None
