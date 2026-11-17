"""
catchup.py: nightly catch-up for anything the live webhook missed (webhook down, bot restarted, Periskope retry lost).

For every WhatsApp group linked to an ACTIVE client, over the last N hours (default 48):
  1. Messages: every Periskope message with text (plain text or a caption) that is not in client_messages is saved,
     the same way the live bot saves it (dedup_hash = WhatsApp unique_id, team or client from the team list).
  2. Files: every file Periskope has that is not in the client's SharePoint folder is uploaded there (backdated to the
     WhatsApp time) and logged in document_uploads as already notified, so old files never trigger "new upload" alerts.
Both steps skip anything already saved, so a run can be repeated safely and never creates duplicates.
A Teams message is sent only when something was recovered or a group failed, so a quiet night sends nothing.

Usage:
  python catchup.py                 # dry run: counts only, writes nothing
  python catchup.py --apply         # recover now
  python catchup.py --loop          # run --apply every night at 02:30 IST (the va-bot-catchup container does this)
  python catchup.py --group <id>    # one group only
"""

import argparse
import asyncio
import time
from datetime import datetime, timedelta, timezone

import config  # noqa: F401 - settings load from .env on import
import db
import recover_files as rf
import teams

IST = timezone(timedelta(hours=5, minutes=30))
RUN_AT_IST = (2, 30)          # 02:30 IST, the quietest time in the groups
DEFAULT_HOURS = 48            # two nights of overlap, so one failed night is still covered the next night
SETTLE_MINUTES = 10           # leave the last minutes to the live bot (it may still be processing them)


def fetch_with_retry(chat_id: str, start: datetime, end: datetime, tries: int = 4, pause: float = 15.0) -> list[dict]:
    """Periskope returns 503s and timeouts under load; retry with a growing pause before giving up on a group."""
    last: Exception | None = None
    for i in range(tries):
        try:
            return rf.fetch_window(chat_id, start, end)
        except Exception as e:  # noqa: BLE001 - any network or HTTP error is retried the same way
            last = e
            if i < tries - 1:
                time.sleep(pause * (i + 1))
    raise last if last else RuntimeError("fetch failed")


def existing_hashes(uids: list[str]) -> set[str]:
    found: set[str] = set()
    uids = [u for u in uids if u]
    for i in range(0, len(uids), 200):
        part = uids[i:i + 200]
        rows = db._supabase.table("client_messages").select("dedup_hash").in_("dedup_hash", part).execute().data or []
        found |= {r["dedup_hash"] for r in rows}
    return found


def missing_messages(msgs: list[dict]) -> list[dict]:
    """Messages with text (or a caption) that client_messages does not have yet, oldest first."""
    cand: dict[str, dict] = {}
    for m in msgs:
        uid = m.get("unique_id") or m.get("message_id")
        if uid and (m.get("body") or "").strip():
            cand[uid] = m
    have = existing_hashes(list(cand))
    return sorted((m for u, m in cand.items() if u not in have), key=lambda x: x.get("timestamp") or "")


def save_message(g: dict, m: dict) -> bool:
    sender = m.get("sender_phone") or m.get("author") or ""
    outgoing = bool(m.get("from_me")) or db.is_team_number(sender)
    return bool(db.save_message(
        client_id=g["client_id"],
        whatsapp_group_id=g["chat_id"],
        sender_phone=sender,
        body=m.get("body") or "",
        message_timestamp=m.get("timestamp") or "",
        unique_id=m.get("unique_id") or m.get("message_id"),
        direction="outgoing" if outgoing else "incoming",
    ))


def run(hours: int = DEFAULT_HOURS, apply: bool = False, only_group: str | None = None, now: datetime | None = None) -> dict:
    end = (now or datetime.now(timezone.utc)) - timedelta(minutes=SETTLE_MINUTES)
    start = end - timedelta(hours=hours)
    summary = {
        "hours": hours, "apply": apply, "groups": 0,
        "messages_missing": 0, "messages_saved": 0, "files_missing": 0, "files_saved": 0, "files_failed": 0,
        "errors": [], "recovered_by_group": {},
    }
    for g in rf.linked_active_groups(only_group):
        summary["groups"] += 1
        try:
            msgs = fetch_with_retry(g["chat_id"], start, end)
            lost = missing_messages(msgs)
            missing_files, _in_sp, _logged_only = rf.find_missing_files(g, rf.media_messages(msgs))
        except Exception as e:  # noqa: BLE001 - one bad group never stops the others
            summary["errors"].append(f"{g['client_name']}: {str(e)[:120]}")
            continue
        summary["messages_missing"] += len(lost)
        summary["files_missing"] += len(missing_files)
        if not apply:
            continue
        saved_msgs = sum(1 for m in lost if save_message(g, m))
        saved_files = failed_files = 0
        for m, name, _mime in missing_files:
            if rf.save_missing_file(g, m, name):
                saved_files += 1
            else:
                failed_files += 1
        summary["messages_saved"] += saved_msgs
        summary["files_saved"] += saved_files
        summary["files_failed"] += failed_files
        if saved_msgs or saved_files or failed_files:
            summary["recovered_by_group"][g["client_name"]] = {"messages": saved_msgs, "files": saved_files, "failed": failed_files}
        time.sleep(0.2)
    return summary


def report(summary: dict) -> str | None:
    """Teams text for the run, or None when there is nothing to say (nothing missed, nothing failed)."""
    if not (summary["messages_saved"] or summary["files_saved"] or summary["files_failed"] or summary["errors"]):
        return None
    lines = [
        "<b>va-bot nightly catch-up</b>",
        f"Recovered {summary['messages_saved']} message(s) and {summary['files_saved']} file(s) the live bot missed "
        f"(last {summary['hours']} hours, {summary['groups']} groups).",
    ]
    for name, c in list(summary["recovered_by_group"].items())[:15]:
        lines.append(f"{name}: {c['messages']} message(s), {c['files']} file(s)" + (f", {c['failed']} failed" if c["failed"] else ""))
    if summary["files_failed"]:
        lines.append(f"{summary['files_failed']} file(s) could not be saved. Tomorrow night's run will try again.")
    if summary["errors"]:
        lines.append(f"{len(summary['errors'])} group(s) could not be checked (Periskope errors). Tomorrow night's run will try again.")
    if summary["messages_saved"] or summary["files_saved"]:
        lines.append("If this shows up every night, check that the Periskope webhook is connected.")
    return "<br>".join(lines)


def send_report(summary: dict) -> None:
    text = report(summary)
    if text:
        asyncio.run(teams._send_teams_message(text))


def seconds_until_next_run(now: datetime | None = None) -> float:
    now_ist = (now or datetime.now(timezone.utc)).astimezone(IST)
    nxt = now_ist.replace(hour=RUN_AT_IST[0], minute=RUN_AT_IST[1], second=0, microsecond=0)
    if nxt <= now_ist:
        nxt += timedelta(days=1)
    return (nxt - now_ist).total_seconds()


def loop(hours: int) -> None:
    print(f"[catchup] scheduler started: every day at {RUN_AT_IST[0]:02d}:{RUN_AT_IST[1]:02d} IST, last {hours} hours")
    while True:
        wait = seconds_until_next_run()
        print(f"[catchup] next run in {wait / 3600:.1f} h")
        time.sleep(wait)
        try:
            s = run(hours, apply=True)
            print(f"[catchup] done: {s['messages_saved']} message(s), {s['files_saved']} file(s) recovered, "
                  f"{s['files_failed']} failed, {len(s['errors'])} group error(s), {s['groups']} groups")
            send_report(s)
        except Exception as e:  # noqa: BLE001 - never let one bad night stop the scheduler
            print(f"[catchup] run failed: {e}")
            asyncio.run(teams._send_teams_message(f"<b>va-bot nightly catch-up failed</b><br>{str(e)[:300]}"))
        time.sleep(60)  # never run twice in the same minute


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--group")
    a = ap.parse_args()
    if a.loop:
        loop(a.hours)
        return
    s = run(a.hours, apply=a.apply, only_group=a.group)
    print(f"[catchup] {'APPLY' if a.apply else 'DRY RUN'}: {s['groups']} groups, "
          f"{s['messages_missing']} message(s) and {s['files_missing']} file(s) missing, "
          f"{s['messages_saved']} message(s) and {s['files_saved']} file(s) saved, {s['files_failed']} failed, "
          f"{len(s['errors'])} group error(s)")
    for e in s["errors"]:
        print(f"  error: {e}")
    if a.apply:
        send_report(s)


if __name__ == "__main__":
    main()
