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

# The web server's event loop, set at start-up (main.py). Message processing runs in worker threads so
# blocking downloads never stall the server; anything async (Teams posts, debounce timers) is handed
# back to this loop through run_in_loop().
MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def run_in_loop(fn) -> None:
    """Call fn() on the server's event loop, from any thread. fn may create tasks."""
    loop = MAIN_LOOP
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            print("[teams] No event loop available — notification skipped.")
            return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        fn()
    else:
        loop.call_soon_threadsafe(fn)


async def _send_teams_message(content: str, teams_chat_id: str | None = None) -> bool:
    """Returns True only when the post was accepted, so callers mark things notified only on success."""
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
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
        print(f"[teams] Notification sent to {label}.")
        return True
    except Exception as e:
        print(f"[teams] ERROR sending Teams notification: {e}")
        return False


async def notify_flag(
    folder_name: str,
    sender_phone: str,
    body: str,
    teams_chat_id: str | None = None,
):
    """
    Send an INSTANT (no debounce) Teams alert to the assigned VA that a
    customer flagged a message. Flags are act-now, so unlike uploads this
    is not batched.
    """
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
    clean_phone = sender_phone.replace("@c.us", "").replace("@s.whatsapp.net", "")
    quoted = body.strip() if body and body.strip() else "(no text)"
    content = (
        f"<b>🚩 Flagged message — {folder_name}</b><br>"
        f"<i>&ldquo;{quoted}&rdquo;</i><br>"
        f"From: {clean_phone}<br>"
        f"Time: {now}"
    )
    return await _send_teams_message(content, teams_chat_id=teams_chat_id)


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
    sent = await _send_teams_message(content, teams_chat_id=teams_chat_id)
    if sent:
        mark_fn(client_id)  # only when the VA was actually told; otherwise the next upload re-announces them
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
    def schedule():
        existing = _pending.get(client_id)
        if existing and not existing.done():
            existing.cancel()
        task = asyncio.get_running_loop().create_task(
            _debounced_notify(
                client_id, client_name, folder_name, sender_phone, count_fn, mark_fn,
                teams_chat_id=teams_chat_id,
            )
        )
        _pending[client_id] = task

    run_in_loop(schedule)  # safe from the worker thread that processes the message
    print(f"[teams] Notification scheduled for '{client_name}' in {DEBOUNCE_SECONDS}s")
