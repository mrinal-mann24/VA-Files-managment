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
    try:
        asyncio.get_event_loop().create_task(teams._send_teams_message(content))
    except RuntimeError:
        print("[failures] No running event loop; Teams alert skipped.")
