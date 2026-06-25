"""
Microsoft Teams notifications for the va-bot.

POSTs to an n8n webhook which forwards the message to a Teams group chat
using delegated OAuth2 credentials. Uses a 60-second debounce so bursts
of uploads collapse into one message.
"""

import asyncio
from datetime import datetime

import httpx

import config

# Per-client debounce: client_id -> asyncio.Task
_pending: dict[str, asyncio.Task] = {}

DEBOUNCE_SECONDS = 60


async def _send_teams_message(content: str, teams_chat_id: str | None = None):
    """POST to n8n webhook which forwards to Teams.
    If teams_chat_id is provided, sends to that personal chat via TEAMS_PERSONAL_WEBHOOK_URL.
    Falls back to the legacy group chat webhook otherwise.
    """
    if teams_chat_id and config.TEAMS_PERSONAL_WEBHOOK_URL:
        url = config.TEAMS_PERSONAL_WEBHOOK_URL
        payload = {"chat_id": teams_chat_id, "message": content}
        label = "VA personal chat"
    elif config.TEAMS_WEBHOOK_URL:
        url = config.TEAMS_WEBHOOK_URL
        payload = {"body": {"content": content}}
        label = "Teams group chat"
    else:
        print("[teams] No Teams webhook configured — skipping notification.")
        return

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
        print(f"[teams] Notification sent to {label}.")
    except Exception as e:
        print(f"[teams] ERROR sending Teams notification: {e}")


async def _debounced_notify(
    client_id: str,
    client_name: str,
    folder_name: str,
    sender_phone: str,
    count_fn,
    mark_fn,
    teams_chat_id: str | None = None,
):
    """Wait DEBOUNCE_SECONDS, then send one Teams message with the total count."""
    await asyncio.sleep(DEBOUNCE_SECONDS)

    count = count_fn(client_id)
    if count == 0:
        return

    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
    clean_phone = sender_phone.replace("@c.us", "").replace("@s.whatsapp.net", "")
    content = (
        f"<b>📁 New Upload — {folder_name}</b><br>"
        f"Documents uploaded: <b>{count}</b><br>"
        f"Sender: {clean_phone}<br>"
        f"Time: {now}"
    )
    await _send_teams_message(content, teams_chat_id=teams_chat_id)
    mark_fn(client_id)
    _pending.pop(client_id, None)


def notify(
    client_id: str,
    client_name: str,
    folder_name: str,
    sender_phone: str,
    count_fn,
    mark_fn,
    teams_chat_id: str | None = None,
):
    """
    Schedule a debounced Teams notification for this client.
    If one is already pending, cancel it and restart the timer
    so bursts of uploads collapse into one message.
    """
    existing = _pending.get(client_id)
    if existing and not existing.done():
        existing.cancel()

    loop = asyncio.get_event_loop()
    task = loop.create_task(
        _debounced_notify(
            client_id, client_name, folder_name, sender_phone, count_fn, mark_fn,
            teams_chat_id=teams_chat_id,
        )
    )
    _pending[client_id] = task
    print(f"[teams] Notification scheduled for '{client_name}' in {DEBOUNCE_SECONDS}s")
