"""
Outbound WhatsApp messaging for the va-bot via Periskope.

Single responsibility: send a WhatsApp text message to a chat (1:1 or group)
via the Periskope API. This is the ONLY module that sends messages out.

    send_whatsapp_text(chat_id, text)  -> True on success, False on failure

`chat_id` is the Periskope chat identifier from the inbound webhook, e.g.:
    "919876543210@c.us"   — 1:1 conversation
    "120363xxxxxxx@g.us"  — group chat
"""

import httpx

import config

_SEND_URL = f"{config.PERISKOPE_BASE_URL}/message/send"

_HEADERS = {
    "authorization": f"Bearer {config.PERISKOPE_API_KEY}",
    "x-phone": config.PERISKOPE_PHONE,
    "Content-Type": "application/json",
}


def send_whatsapp_text(chat_id: str, text: str) -> bool:
    """
    Send a plain-text WhatsApp message into `chat_id` via Periskope.

    Returns True if the API accepted the message, False on any error.
    Failures are logged and never raised so a failed reply can't crash
    webhook processing.
    """
    if not chat_id or not text:
        print("[messaging] Missing chat_id or text; nothing to send.")
        return False

    if "CHANGE_ME" in (config.PERISKOPE_API_KEY + config.PERISKOPE_PHONE):
        print(
            "[messaging] PERISKOPE_API_KEY / PERISKOPE_PHONE not configured — "
            "skipping reply. Set them in config.py or as env vars."
        )
        return False

    payload = {
        "chat_id": chat_id,
        "message": text,
    }

    try:
        print(f"[messaging] Sending WhatsApp reply -> {chat_id}")
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(_SEND_URL, headers=_HEADERS, json=payload)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(
            f"[messaging] ERROR: send failed with HTTP "
            f"{e.response.status_code}: {e.response.text[:200]}"
        )
        return False
    except Exception as e:
        print(f"[messaging] ERROR sending WhatsApp reply: {e}")
        return False

    print(f"[messaging] Reply sent to {chat_id}.")
    return True
