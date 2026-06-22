"""
list_groups.py — List ALL Virtual Accounting / VA groups from Periskope.

Queries every connected GM phone (from /phones/all) and merges results so
groups only visible to one particular GM are still captured.

Run:
    python list_groups.py
"""

import httpx

import config
import db


def _is_va_group(name: str) -> bool:
    return any(p.search(name or "") for p in db._CLIENT_GROUP_PATTERNS)


def get_all_phones(client: httpx.Client) -> list[str]:
    """Return list of bare phone numbers for all CONNECTED GMs."""
    r = client.get(
        f"{config.PERISKOPE_BASE_URL}/phones/all",
        headers={"authorization": f"Bearer {config.PERISKOPE_API_KEY}",
                 "x-phone": config.PERISKOPE_PHONE},
        timeout=15,
    )
    phones = r.json() if isinstance(r.json(), list) else r.json().get("phones", [])
    connected = []
    for p in phones:
        if p.get("wa_state") == "CONNECTED":
            num = p.get("org_phone", "").replace("@c.us", "")
            name = p.get("phone_name", "")
            connected.append((num, name))
    return connected


def fetch_all_chats_for_phone(client: httpx.Client, phone: str) -> list[dict]:
    """Page through all chats visible to a specific GM phone."""
    headers = {
        "authorization": f"Bearer {config.PERISKOPE_API_KEY}",
        "x-phone": phone,
    }
    chats = []
    offset = 0
    while True:
        r = client.get(
            f"{config.PERISKOPE_BASE_URL}/chats",
            headers=headers,
            params={"offset": offset, "limit": 100},
            timeout=30,
        )
        batch = r.json().get("chats", []) if r.status_code == 200 else []
        if not batch:
            break
        chats.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return chats


def main():
    print("Fetching all connected GM phones from Periskope...")
    seen_ids: set[str] = set()
    va_groups = []
    all_group_count = 0

    with httpx.Client(timeout=30.0) as client:
        phones = get_all_phones(client)
        print(f"Found {len(phones)} connected phone(s).\n")

        for phone, name in phones:
            print(f"  Querying {phone} ({name})...")
            chats = fetch_all_chats_for_phone(client, phone)
            new_groups = 0
            for chat in chats:
                chat_id = chat.get("chat_id") or ""
                if not str(chat_id).endswith("@g.us"):
                    continue
                if chat_id in seen_ids:
                    continue
                seen_ids.add(chat_id)
                all_group_count += 1
                chat_name = chat.get("chat_name") or ""
                if _is_va_group(chat_name):
                    va_groups.append({
                        "name": chat_name,
                        "chat_id": chat_id,
                        "members": chat.get("member_count"),
                        "found_via": name,
                    })
                    new_groups += 1
            print(f"    -> {len(chats)} chats, {new_groups} new VA group(s)")

    print(f"\n{'='*70}")
    print(f"  {len(va_groups)} VA / Virtual Accounting groups "
          f"(out of {all_group_count} unique groups across all phones)")
    print(f"{'='*70}\n")

    for i, g in enumerate(va_groups, 1):
        members = f"{g['members']} members" if g['members'] is not None else ""
        print(f"{i:>3}. {g['name']}")
        print(f"     id      : {g['chat_id']}")
        print(f"     members : {members}")
        print(f"     via GM  : {g['found_via']}\n")


if __name__ == "__main__":
    main()
