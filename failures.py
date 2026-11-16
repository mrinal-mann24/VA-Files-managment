"""
Failed file saves: never silent.

Every file the bot could not save (no media path, download error, SharePoint upload error, or an
unexpected exception) is:
  1. appended as one JSON line to FAILED_FILES_LOG (default ./failed_files.jsonl), and
  2. alerted to the Teams group chat (TEAMS_WEBHOOK_URL), at most once every ALERT_EVERY_SECONDS,
     with the number of failures since the last alert, so an outage shows up the same day.

Recover the files afterwards with:
    python recover_files.py --window <YYYY-MM-DD>:<YYYY-MM-DD> --apply
"""

import asyncio
import json
import time
from datetime import datetime, timezone

import config
import teams

ALERT_EVERY_SECONDS = 600

_last_alert_at = 0.0
_since_last_alert = 0


def record(kind: str, *, client_name: str, chat_id: str, unique_id: str, filename: str | None, reason: str) -> None:
    """Log one failure to the JSON-lines file and schedule a (throttled) Teams alert. Never raises."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "client_name": client_name,
        "chat_id": chat_id,
        "unique_id": unique_id,
        "filename": filename,
        "reason": str(reason)[:300],
    }
    try:
        with open(config.FAILED_FILES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[failures] ERROR writing {config.FAILED_FILES_LOG}: {e}")
    print(f"[failures] {kind} not saved for '{client_name}' ({filename}): {reason}")
    _alert(client_name, str(reason)[:200])


def _alert(client_name: str, reason: str) -> None:
    global _last_alert_at, _since_last_alert
    _since_last_alert += 1
    now = time.monotonic()
    if _last_alert_at and now - _last_alert_at < ALERT_EVERY_SECONDS:
        return
    count, _since_last_alert = _since_last_alert, 0
    _last_alert_at = now
    content = (
        f"<b>⚠️ va-bot could not save {count} file(s)</b><br>"
        f"Latest: {client_name}: {reason}<br>"
        f"Details: {config.FAILED_FILES_LOG} on the va-bot server. "
        f"Recover with recover_files.py once the cause is fixed."
    )
    # Message processing runs in a worker thread; hand the async post to the server's event loop.
    teams.run_in_loop(lambda: asyncio.get_running_loop().create_task(teams._send_teams_message(content)))


_unlinked_alerted: dict[str, float] = {}
UNLINKED_ALERT_EVERY_SECONDS = 6 * 3600


def record_unlinked_group(chat_id: str, group_name: str, unique_id: str) -> None:
    """A message arrived in a group that looks like a VA client group but is linked to no client, so it
    was not saved. Logged every time; alerted at most once per group every 6 hours. Once someone links the
    group (clients.whatsapp_group_id), recover its history with backfill.py / recover_files.py."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "unlinked_group",
        "client_name": group_name,
        "chat_id": chat_id,
        "unique_id": unique_id,
        "filename": None,
        "reason": "VA-looking group is not linked to any client; message not saved",
    }
    try:
        with open(config.FAILED_FILES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[failures] ERROR writing {config.FAILED_FILES_LOG}: {e}")
    now = time.monotonic()
    last = _unlinked_alerted.get(chat_id)
    if last and now - last < UNLINKED_ALERT_EVERY_SECONDS:
        return
    _unlinked_alerted[chat_id] = now
    content = (
        f"<b>🔗 WhatsApp group not linked to a client</b><br>"
        f"Group: {group_name} ({chat_id})<br>"
        f"Messages here are not being saved. Link it by setting clients.whatsapp_group_id, "
        f"or add the client's WhatsApp number as a contact so the bot links it on the next message."
    )
    teams.run_in_loop(lambda: asyncio.get_running_loop().create_task(teams._send_teams_message(content)))
