import os
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
PERISKOPE_API_KEY  = os.getenv("PERISKOPE_API_KEY")
PERISKOPE_PHONE    = os.getenv("PERISKOPE_PHONE")
PERISKOPE_BASE_URL = os.getenv("PERISKOPE_BASE_URL", "https://api.periskope.app/v1")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")

MAX_GROUPS    = 4   # how many VA groups to check; set None for all
LOOKBACK_DAYS = 30  # widen window — flag may have been set days ago

# ── Quick test: hardcode a group ID here to skip group discovery ──────────────
# Set to None to run against all VA groups (normal mode)
TEST_GROUP_ID   = "120363426527271002@g.us"
TEST_GROUP_NAME = "Nirved Medicals<>VA"

# Set True to dump the raw first message from the API so you can see exact fields
DEBUG_RAW = True

# ── VA group detection (same patterns as db.py) ───────────────────────────────
_VA_PATTERNS = [
    re.compile(r"\bVirtual Accounting\b", re.IGNORECASE),
    re.compile(r"<>\s*VA\b",              re.IGNORECASE),
    re.compile(r"[\-<>xX]\s*VA\b\s*(?:Team|Group|Services)?\s*$", re.IGNORECASE),
]


def _is_va_group(name: str) -> bool:
    return any(p.search(name or "") for p in _VA_PATTERNS)


def _headers(phone: str | None = None) -> dict:
    return {
        "authorization": f"Bearer {PERISKOPE_API_KEY}",
        "x-phone":       phone or PERISKOPE_PHONE,
    }


# ── Phone / group discovery ───────────────────────────────────────────────────
def get_connected_phones(http: httpx.Client) -> list[tuple[str, str]]:
    """Return [(number, name)] for every CONNECTED GM phone."""
    try:
        r = http.get(
            f"{PERISKOPE_BASE_URL}/phones/all",
            headers=_headers(),
            timeout=15,
        )
        data = r.json()
        phones = data if isinstance(data, list) else data.get("phones", [])
    except Exception as e:
        print(f"[ERROR] /phones/all failed: {e}")
        return []

    return [
        (p.get("org_phone", "").replace("@c.us", ""), p.get("phone_name", ""))
        for p in phones
        if p.get("wa_state") == "CONNECTED"
    ]


def get_va_groups(http: httpx.Client) -> list[dict]:
    """
    Page through every chat on every connected GM phone.
    Return list of {name, chat_id, phone} for VA client groups only.
    Capped at MAX_GROUPS.
    """
    phones = get_connected_phones(http)
    if not phones:
        print("[WARN] No connected phones found.")
        return []

    seen: set[str] = set()
    groups: list[dict] = []

    for phone, phone_name in phones:
        print(f"  Querying {phone} ({phone_name})...")
        offset = 0
        while True:
            try:
                r = http.get(
                    f"{PERISKOPE_BASE_URL}/chats",
                    headers=_headers(phone),
                    params={"offset": offset, "limit": 100},
                    timeout=30,
                )
                batch = r.json().get("chats", []) if r.status_code == 200 else []
            except Exception as e:
                print(f"  [ERROR] chats fetch failed for {phone}: {e}")
                break

            for chat in batch:
                chat_id   = chat.get("chat_id", "")
                chat_name = chat.get("chat_name") or ""
                if not chat_id.endswith("@g.us") or chat_id in seen:
                    continue
                seen.add(chat_id)
                if _is_va_group(chat_name):
                    groups.append({"name": chat_name, "chat_id": chat_id, "phone": phone})
                    if MAX_GROUPS and len(groups) >= MAX_GROUPS:
                        return groups

            if len(batch) < 100:
                break
            offset += 100

    return groups


# ── Find which phone can see a group ─────────────────────────────────────────
def _find_phone_for_group(http: httpx.Client, chat_id: str) -> str | None:
    """
    Try each connected GM phone and return the first one that returns
    messages (count > 0) for the given group. Falls back to PERISKOPE_PHONE.
    """
    phones = get_connected_phones(http)
    if not phones:
        return PERISKOPE_PHONE

    print(f"  Probing {len(phones)} phone(s) to find one that can see this group...")
    for phone, name in phones:
        try:
            r = http.get(
                f"{PERISKOPE_BASE_URL}/chats/{chat_id}/messages",
                headers=_headers(phone),
                params={"limit": 1, "offset": 0},
                timeout=15,
            )
            if r.status_code == 200:
                data  = r.json()
                count = data.get("count", 0) if isinstance(data, dict) else len(data)
                print(f"    {phone} ({name}): count={count}")
                if count > 0:
                    return phone
        except Exception as e:
            print(f"    {phone}: error — {e}")

    print("  [WARN] No phone returned messages. Falling back to default phone.")
    return PERISKOPE_PHONE


# ── Flagged message fetching ──────────────────────────────────────────────────
def get_flagged_messages(http: httpx.Client, chat_id: str, phone: str) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    all_msgs: list[dict] = []

    # Paginate — default limit is 2000 but we do 2 pages of 500 to be safe
    for offset in range(0, 1001, 500):
        try:
            r = http.get(
                f"{PERISKOPE_BASE_URL}/chats/{chat_id}/messages",
                headers=_headers(phone),
                params={"limit": 500, "offset": offset},
                timeout=30,
            )
        except Exception as e:
            print(f"    [ERROR] messages fetch failed: {e}")
            break

        print(f"    [DEBUG] HTTP {r.status_code} | URL: {r.url}")
        print(f"    [DEBUG] Response (first 500 chars): {r.text[:500]}")

        if r.status_code != 200:
            print(f"    [WARN] HTTP {r.status_code}: {r.text[:300]}")
            break

        raw  = r.json()
        msgs = raw if isinstance(raw, list) else raw.get("messages", [])

        if DEBUG_RAW and offset == 0 and msgs:
            import json
            print("\n    [DEBUG] First message raw fields:")
            first = msgs[0]
            # Print only flag-related + key fields to keep output short
            debug_keys = [
                "unique_id", "body", "message_type", "timestamp", "sender_phone",
                "author", "is_starred", "flag_status", "flag_metadata",
            ]
            for k in debug_keys:
                print(f"      {k}: {json.dumps(first.get(k))}")
            print(f"      [total messages in this page: {len(msgs)}]")
            print()

        all_msgs.extend(msgs)
        if len(msgs) < 500:
            break  # last page

    print(f"    Fetched {len(all_msgs)} total messages, filtering flagged...")

    results = []
    for m in all_msgs:
        starred      = bool(m.get("is_starred"))
        flag_status  = bool(m.get("flag_status"))
        if not starred and not flag_status:
            continue

        # Body — for media-only messages, describe what it was
        body = m.get("body") or ""
        if not body:
            media = m.get("media") or {}
            mime  = media.get("mimetype", "")
            fname = media.get("filename", "")
            body  = f"[{mime} file: {fname}]" if mime else "[media — no text]"

        # Timestamp (postgres format: "2024-05-13 11:19:34+00")
        ts_raw = m.get("timestamp") or m.get("updated_at") or ""
        ts     = _parse_ts(ts_raw)
        if ts and ts < cutoff:
            continue

        # Sender
        sender = (
            m.get("sender_phone")
            or (m.get("author") or "").replace("@c.us", "")
            or "Unknown"
        ).replace("@c.us", "")

        # Who flagged it
        flag_meta  = m.get("flag_metadata") or {}
        flagged_by = flag_meta.get("response_email") or flag_meta.get("response_id") or ""
        flagged_ts = _parse_ts(flag_meta.get("response_timestamp") or "")

        results.append({
            "sender":      sender,
            "body":        body,
            "timestamp":   ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "unknown time",
            "flagged_by":  flagged_by,
            "flagged_at":  flagged_ts.strftime("%Y-%m-%d %H:%M UTC") if flagged_ts else "",
            "is_starred":  starred,
            "flag_status": flag_status,
        })

    return results


def _parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        if isinstance(raw, str):
            # "2024-05-13 11:19:34+00" → replace space with T for fromisoformat
            clean = raw.strip().replace(" ", "T")
            if clean.endswith("+00"):
                clean += ":00"
            return datetime.fromisoformat(clean)
    except Exception:
        pass
    return None


# ── GPT analysis ─────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """\
You are an assistant helping a Virtual Accountant (VA) stay on top of client messages.

A colleague has flagged (starred) WhatsApp messages in a client group.
Flagged messages need attention — they could be a client request, a question,
a document follow-up, a payment reminder, a deadline, or any action item.

For each flagged message, output EXACTLY this format:

  📌 Message from: <sender phone or name>
     Sent: <timestamp>
     Message: "<message text>"
     → Action for VA: <what the VA should do>
     → Urgency: High / Medium / Low

At the end, add a 1-line summary: "X action item(s) found — Y high priority."

If no clear action is needed, say: "No action required for this message."
Be brief and practical. The VA is busy.
"""


def ask_gpt(group_name: str, messages: list[dict]) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        return "[ERROR] openai not installed. Run: pip install openai"

    if not OPENAI_API_KEY:
        return "[ERROR] OPENAI_API_KEY not set in .env"

    oai = OpenAI(api_key=OPENAI_API_KEY)

    lines = [f"Client group: {group_name}", f"Flagged messages: {len(messages)}", ""]
    for i, m in enumerate(messages, 1):
        flag_type = []
        if m["is_starred"]:
            flag_type.append("starred")
        if m["flag_status"]:
            flag_type.append("flagged")
        flag_label = " + ".join(flag_type) if flag_type else "flagged"

        lines.append(f"--- Message {i} ({flag_label}) ---")
        lines.append(f"Sender:    {m['sender']}")
        lines.append(f"Sent at:   {m['timestamp']}")
        if m["flagged_by"]:
            lines.append(f"Flagged by: {m['flagged_by']} at {m['flagged_at']}")
        lines.append(f"Message:   {m['body']}")
        lines.append("")

    try:
        resp = oai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": "\n".join(lines)},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"[ERROR] GPT call failed: {e}"


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not PERISKOPE_API_KEY:
        print("[ERROR] PERISKOPE_API_KEY not set in .env")
        sys.exit(1)

    print("=" * 70)
    print("  VA Flagged Message Checker")
    print(f"  Last {LOOKBACK_DAYS} days | Up to {MAX_GROUPS} groups")
    print("=" * 70)
    print()

    with httpx.Client(timeout=30.0) as http:

        # ── Test mode: skip discovery, use hardcoded group ID ─────────────────
        if TEST_GROUP_ID:
            # Find which connected phone can actually see this group
            # (the group's Org Phone in Periskope UI may differ from PERISKOPE_PHONE)
            group_phone = _find_phone_for_group(http, TEST_GROUP_ID)
            if not group_phone:
                print(f"[ERROR] No connected phone can see group {TEST_GROUP_ID}")
                print("        Check that a GM phone is a member of this group in Periskope.")
                return
            group_name = TEST_GROUP_NAME or TEST_GROUP_ID
            groups = [{"name": group_name, "chat_id": TEST_GROUP_ID, "phone": group_phone}]
            print(f"TEST MODE — using group: {group_name} ({TEST_GROUP_ID})")
            print(f"TEST MODE — using phone: {group_phone}\n")

        # ── Normal mode: discover all VA groups ───────────────────────────────
        else:
            print("Discovering VA groups...")
            groups = get_va_groups(http)
            if not groups:
                print("No VA groups found. Check PERISKOPE_API_KEY and phone connection.")
                return
            print(f"\nChecking {len(groups)} group(s):\n")
            for g in groups:
                print(f"  • {g['name']}")
            print()

        summary = []

        for group in groups:
            print(f"{'─' * 60}")
            print(f"  {group['name']}")
            print(f"{'─' * 60}")

            flagged = get_flagged_messages(http, group["chat_id"], group["phone"])

            if not flagged:
                print(f"  No flagged messages in the last {LOOKBACK_DAYS} days.\n")
                summary.append((group["name"], 0, None))
                continue

            print(f"  {len(flagged)} flagged message(s) found.\n")
            for m in flagged:
                label = []
                if m["is_starred"]:  label.append("⭐ starred")
                if m["flag_status"]: label.append("🚩 flagged")
                print(f"  [{' | '.join(label)}] {m['sender']} @ {m['timestamp']}")
                print(f"  \"{m['body'][:120]}{'...' if len(m['body']) > 120 else ''}\"")
                print()

            print("  Asking GPT-4o...\n")
            analysis = ask_gpt(group["name"], flagged)
            summary.append((group["name"], len(flagged), analysis))

            print("  ── GPT Analysis ──────────────────────────────────────")
            for line in analysis.split("\n"):
                print(f"  {line}")
            print()

    # ── Final summary ─────────────────────────────────────────────────────────
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    for name, count, _ in summary:
        status = f"{count} flagged message(s)" if count else "nothing flagged"
        print(f"  {name}: {status}")
    print()
    print("Done. To send these to VAs via Teams, wire up db.get_va_for_client()")
    print("and teams.notify() using the same pattern as main.py.")


if __name__ == "__main__":
    main()
