"""
Microsoft Teams notifications for the va-bot.

POSTs to an n8n webhook which forwards the message to a Teams group chat
using delegated OAuth2 credentials. Uses a 60-second debounce so bursts
of uploads collapse into one message.
"""

import asyncio
from datetime import datetime, timezone

import httpx

import config

# Per-client debounce: client_id -> asyncio.Task
_pending: dict[str, asyncio.Task] = {}

DEBOUNCE_SECONDS = 60


async def _send_teams_message(content: str):
    """POST to n8n webhook which forwards to Teams group chat."""
    if not config.TEAMS_WEBHOOK_URL:
        print("[teams] TEAMS_WEBHOOK_URL not configured — skipping notification.")
        return

    payload = {"body": {"content": content}}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                config.TEAMS_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        print("[teams] Notification sent to Teams group chat.")
    except Exception as e:
        print(f"[teams] ERROR sending Teams notification: {e}")


async def _debounced_notify(
    client_id: str,
    client_name: str,
    folder_name: str,
    sender_phone: str,
    count_fn,
    mark_fn,
):
    """Wait DEBOUNCE_SECONDS, then send one Teams message with the total count."""
    await asyncio.sleep(DEBOUNCE_SECONDS)

    count = count_fn(client_id)
    if count == 0:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = (
        f"<b>{folder_name}</b><br>"
        f"Uploaded <b>{count}</b> document(s)<br>"
        f"Sender: {sender_phone}<br>"
        f"Time: {now}"
    )
    await _send_teams_message(content)
    mark_fn(client_id)
    _pending.pop(client_id, None)


def notify(
    client_id: str,
    client_name: str,
    folder_name: str,
    sender_phone: str,
    count_fn,
    mark_fn,
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
            client_id, client_name, folder_name, sender_phone, count_fn, mark_fn
        )
    )
    _pending[client_id] = task
    print(f"[teams] Notification scheduled for '{client_name}' in {DEBOUNCE_SECONDS}s")
