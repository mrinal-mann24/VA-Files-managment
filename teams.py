"""
Microsoft Teams notifications for the va-bot.

Sends a message directly to a Teams group chat via Microsoft Graph API.
Uses a 60-second debounce — bursts of uploads collapse into one message.
"""

import asyncio
from datetime import datetime, timezone

import httpx

import config

# Per-client debounce: client_id -> asyncio.Task
_pending: dict[str, asyncio.Task] = {}

DEBOUNCE_SECONDS = 60

_GRAPH_TOKEN_URL = f"https://login.microsoftonline.com/{{}}/oauth2/v2.0/token"
_GRAPH_MSG_URL = "https://graph.microsoft.com/v1.0/chats/{chat_id}/messages"

_token_cache: dict = {}


async def _get_token() -> str | None:
    """Get a cached or fresh Graph API access token using client credentials."""
    import time
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["access_token"]

    if not all([config.SHAREPOINT_TENANT_ID, config.SHAREPOINT_CLIENT_ID, config.SHAREPOINT_CLIENT_SECRET]):
        print("[teams] Azure credentials not configured — skipping.")
        return None

    url = f"https://login.microsoftonline.com/{config.SHAREPOINT_TENANT_ID}/oauth2/v2.0/token"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, data={
                "grant_type": "client_credentials",
                "client_id": config.SHAREPOINT_CLIENT_ID,
                "client_secret": config.SHAREPOINT_CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
            })
            resp.raise_for_status()
            data = resp.json()
            _token_cache["access_token"] = data["access_token"]
            _token_cache["expires_at"] = now + data.get("expires_in", 3600)
            return _token_cache["access_token"]
    except Exception as e:
        print(f"[teams] ERROR getting Graph token: {e}")
        return None


async def _send_teams_message(text: str):
    """POST a message to the Teams group chat via Graph API."""
    chat_id = config.TEAMS_CHAT_ID
    if not chat_id:
        print("[teams] TEAMS_CHAT_ID not configured — skipping notification.")
        return

    token = await _get_token()
    if not token:
        return

    url = f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages"
    payload = {
        "body": {
            "contentType": "html",
            "content": text.replace("\n", "<br>").replace("**", "<b>").replace("**", "</b>"),
        }
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
        print("[teams] Notification sent to group chat.")
    except Exception as e:
        print(f"[teams] ERROR sending Teams message: {e}")


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
        f"**{folder_name}**\n"
        f"Uploaded **{count}** document(s)\n"
        f"Sender: {sender_phone}\n"
        f"Time: {now}"
    )
    await _send_teams_message(text)
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
