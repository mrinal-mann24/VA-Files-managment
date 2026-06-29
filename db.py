"""
Supabase lookups and writes for the va-bot.
"""

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
]

_PERISKOPE_HEADERS = {
    "authorization": f"Bearer {config.PERISKOPE_API_KEY}",
    "x-phone": config.PERISKOPE_PHONE,
}


def _strip(whatsapp_id: str) -> str:
    """Strip @c.us / @g.us suffix and return bare number/id."""
    return (whatsapp_id or "").split("@")[0]


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
                    "clients(client_name, whatsapp_group_id, whatsapp_group_name)"
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
            # folder_name = group name if we have it, else fall back to client_name
            folder_name = client.get("whatsapp_group_name") or client_name
            print(f"[db] Number match: {candidate} -> '{client_name}' (folder: '{folder_name}')")
            return {
                "client_id": row.get("client_id"),
                "client_name": client_name,
                "folder_name": folder_name,
                "contact_name": row.get("contact_name"),
                "whatsapp_group_id": client.get("whatsapp_group_id"),
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
            .select("client_id, client_name, whatsapp_group_id, whatsapp_group_name")
            .eq("whatsapp_group_id", group_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        print(f"[db] ERROR querying for group {group_id}: {e}")
        return None

    rows = resp.data or []
    if rows:
        row = rows[0]
        group_name = row.get("whatsapp_group_name") or ""
        if not group_name:
            fetched = fetch_group_name(group_id)
            if fetched:
                group_name = fetched
                try:
                    _supabase.table("clients").update(
                        {"whatsapp_group_name": group_name}
                    ).eq("client_id", row["client_id"]).execute()
                    print(f"[db] Backfilled group name '{group_name}' for '{row['client_name']}'")
                except Exception as e:
                    print(f"[db] ERROR saving backfilled group name: {e}")
        folder_name = group_name or row.get("client_name")
        print(f"[db] Group match: {group_id} -> '{row['client_name']}' (folder: '{folder_name}')")
        return {
            "client_id": row["client_id"],
            "client_name": row["client_name"],
            "folder_name": folder_name,
            "contact_name": None,
            "whatsapp_group_id": group_id,
        }

    print(f"[db] No client for group '{group_id}'.")
    return None


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
) -> str | None:
    """
    Insert one row into document_uploads for every file saved.
    Returns the upload_id (uuid) or None on failure.
    """
    try:
        resp = _supabase.table("document_uploads").insert({
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
        }).execute()
        upload_id = (resp.data or [{}])[0].get("upload_id")
        print(f"[db] Logged upload '{file_name}' for '{client_name}' (id={upload_id})")
        return upload_id
    except Exception as e:
        print(f"[db] ERROR logging upload: {e}")
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
    for raw_number in members.keys():
        client = find_client_by_number(raw_number)
        if client:
            print(f"[db] Member {raw_number} matched client '{client['client_name']}'")
            # --- 5. Save group ID + group name (once, never overwrite) ---
            if not client.get("whatsapp_group_id"):
                update_group_id(client["client_id"], chat_id, group_name)
            # Always return folder_name = group_name
            client["folder_name"] = group_name
            return client

    print(f"[db] No member in '{group_name}' matched any client in Supabase.")
    return None
