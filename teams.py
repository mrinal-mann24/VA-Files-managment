"""
Microsoft Teams notifications for the va-bot.

Sends a message to a Teams channel via an Incoming Webhook URL whenever
a client uploads documents. Uses a 60-second debounce — if the same client
uploads multiple files in a burst, they get batched into one Teams message.

    notify(client_id, client_name, folder_name, sender_phone, count)
"""

import asyncio
from datetime import datetime, timezone

import httpx

import config

# Per-client debounce: client_id -> asyncio.Task
_pending: dict[str, asyncio.Task] = {}

DEBOUNCE_SECONDS = 60  # wait 60s after last upload before sending


async def _send_teams_message(text: str):
    """POST a message to the Teams channel via Power Automate Workflows webhook."""
    if not config.TEAMS_WEBHOOK_URL or "CHANGE_ME" in config.TEAMS_WEBHOOK_URL:
        print("[teams] TEAMS_WEBHOOK_URL not configured — skipping notification.")
        return

    # Power Automate Workflows webhook requires Adaptive Card format
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": text,
                            "wrap": True,
                        }
                    ],
                },
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                config.TEAMS_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        print("[teams] Notification sent.")
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
    text = (
        f"**{folder_name}**\n\n"
        f"Uploaded **{count}** document(s)\n"
        f"Sender: {sender_phone}\n"
        f"Time: {now}"
    )
    await _send_teams_message(text)
    mark_fn(client_id)

    # Clean up
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
    # Cancel existing pending task for this client
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
