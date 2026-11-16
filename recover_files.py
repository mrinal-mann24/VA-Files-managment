"""
recover_files.py — recover WhatsApp files the live bot never saved (outages such as 2-4 and 11-15 Nov 2026).

Scope: only groups already linked to an ACTIVE client, and only messages inside the given IST date windows.
Dry run by default: lists, per group, the files Periskope has that are not in SharePoint.
With --apply it downloads each missing file to the same SharePoint folder the live bot uses (backdated to
the WhatsApp message time) and logs it in document_uploads with uploaded_at = the message time and
notified = true (so old files never trigger a Teams "uploaded" alert).

It does NOT: link new groups, add contacts, write client_messages or notes.txt, or touch inactive clients.
A file already in SharePoint (same name, same folder) is skipped, so re-running is safe.

Usage:
  python recover_files.py --window 2026-11-02:2026-11-04 --window 2026-11-11:2026-11-15            # dry run
  python recover_files.py --window 2026-11-11:2026-11-15 --group 120363431375892494                  # one group
  python recover_files.py --window 2026-11-02:2026-11-04 --window 2026-11-11:2026-11-15 --apply      # write
"""

import argparse
import time
from datetime import datetime, timedelta, timezone

import httpx

import backfill  # helpers only: _resolve_filename, _already_downloaded, _build_media_url (main() is guarded)
import config
import db
import storage_sharepoint as storage

IST = timezone(timedelta(hours=5, minutes=30))


def parse_window(text: str) -> tuple[datetime, datetime]:
    """'2026-11-11:2026-11-15' -> [11 Nov 00:00 IST, 16 Nov 00:00 IST) in UTC."""
    a, b = text.split(":")
    start = datetime.strptime(a, "%Y-%m-%d").replace(tzinfo=IST)
    end = datetime.strptime(b, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def linked_active_groups(only_group: str | None) -> list[dict]:
    q = (
        db._supabase.table("clients")
        .select("client_id, client_name, whatsapp_group_id, whatsapp_group_name, parent_client_id, status")
        .not_.is_("whatsapp_group_id", "null")
    )
    rows = q.execute().data or []
    # A WhatsApp group linked to two clients (e.g. a parent and its subgroup) is processed once, under the
    # subgroup, so files are not uploaded to two folders.
    by_gid: dict[str, dict] = {}
    for r in rows:
        gid = db._strip(r.get("whatsapp_group_id") or "")
        if not gid or not db._is_active(r.get("status")):
            continue
        if gid in by_gid:
            keep = r if r.get("parent_client_id") and not by_gid[gid].get("parent_client_id") else by_gid[gid]
            drop = by_gid[gid] if keep is r else r
            print(f"[recover] group {gid} is linked to '{keep['client_name']}' and '{drop['client_name']}': using '{keep['client_name']}'")
            by_gid[gid] = keep
        else:
            by_gid[gid] = r
    out = []
    for gid, r in by_gid.items():
        if only_group and gid != db._strip(only_group):
            continue
        out.append({
            "client_id": r["client_id"],
            "client_name": r["client_name"],
            "chat_id": f"{gid}@g.us",
            "group_id": gid,
            "folder_name": r.get("whatsapp_group_name") or r["client_name"],
            "parent_name": db._get_parent_name(r.get("parent_client_id")),
        })
    return sorted(out, key=lambda g: g["client_name"].lower())


def fetch_window(chat_id: str, start: datetime, end: datetime) -> list[dict]:
    """Messages in [start, end). Passes the window to Periskope and also filters locally."""
    # No x-phone: Periskope then returns the group's messages from every connected org phone (the default
    # PERISKOPE_PHONE is not a member of many groups). The same message can come back once per phone, so
    # results are de-duplicated by unique_id below.
    headers = {"authorization": f"Bearer {config.PERISKOPE_API_KEY}"}
    msgs, offset, page = [], 0, 100
    with httpx.Client(timeout=30.0) as client:
        while True:
            resp = client.get(
                f"{config.PERISKOPE_BASE_URL}/chats/{chat_id}/messages",
                headers=headers,
                params={
                    "offset": offset,
                    "limit": page,
                    "start_time": start.isoformat().replace("+00:00", "Z"),
                    "end_time": end.isoformat().replace("+00:00", "Z"),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data if isinstance(data, list) else (data.get("messages") or data.get("data") or [])
            if not batch:
                break
            msgs.extend(batch)
            if len(batch) < page:
                break
            offset += page
            time.sleep(0.3)
    seen, out = set(), []
    for m in msgs:
        key = m.get("unique_id") or m.get("message_id") or m.get("id")
        ts = parse_ts(m.get("timestamp"))
        if key in seen or not ts or not (start <= ts < end):
            continue
        seen.add(key)
        out.append(m)
    return out


def logged_files(group_id: str) -> set[str]:
    rows = (
        db._supabase.table("document_uploads").select("file_name").eq("group_id", group_id).execute().data or []
    )
    return {r["file_name"] for r in rows if r.get("file_name")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", action="append", required=True, help="IST dates FROM:TO inclusive, e.g. 2026-11-11:2026-11-15")
    ap.add_argument("--group", help="only this WhatsApp group id")
    ap.add_argument("--apply", action="store_true", help="download, upload and log (default is a dry run)")
    args = ap.parse_args()

    windows = [parse_window(w) for w in args.window]
    groups = linked_active_groups(args.group)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[recover] {mode}: {len(groups)} linked active group(s), windows: {', '.join(args.window)}")

    totals = {"media": 0, "in_sharepoint": 0, "missing": 0, "recovered": 0, "failed": 0, "logged_only": 0}
    for g in groups:
        media = []
        for start, end in windows:
            try:
                media += [m for m in fetch_window(g["chat_id"], start, end) if (m.get("media") or {}).get("path") or (m.get("media") or {}).get("url")]
            except Exception as e:
                print(f"  [recover] {g['client_name']}: Periskope error {e}")
        if not media:
            continue
        have_logged = logged_files(g["group_id"])
        missing, logged_only = [], 0
        for m in media:
            mo = m.get("media") or {}
            mime = mo.get("mimetype") or mo.get("mime_type") or ""
            name = backfill._resolve_filename(mo, mime, m.get("timestamp") or "")
            in_sp = bool(name) and backfill._already_downloaded(g["folder_name"], name, g["parent_name"])
            if in_sp:
                totals["in_sharepoint"] += 1
                if name not in have_logged:
                    logged_only += 1
                continue
            missing.append((m, name, mime))
        totals["media"] += len(media)
        totals["missing"] += len(missing)
        totals["logged_only"] += logged_only
        print(f"  {g['client_name']}: {len(media)} file(s) in window, {len(media) - len(missing)} already in SharePoint, "
              f"{len(missing)} MISSING" + (f", {logged_only} in SharePoint but not logged" if logged_only else ""))

        if not args.apply:
            continue
        for m, name, mime in missing:
            mo = m.get("media") or {}
            url = backfill._build_media_url(mo.get("path") or mo.get("url") or "")
            web_url = storage.save_media(g["folder_name"], url, name, g["parent_name"], message_timestamp=m.get("timestamp"))
            if not web_url:
                totals["failed"] += 1
                continue
            try:
                db._supabase.table("document_uploads").insert({
                    "client_id": g["client_id"],
                    "client_name": g["client_name"],
                    "folder_name": g["folder_name"],
                    "group_id": g["group_id"],
                    "group_name": g["folder_name"],
                    "sender_phone": db._strip(m.get("sender_phone") or ""),
                    "file_name": storage._safe_name(name) if name else name,
                    "file_type": (m.get("message_type") or "").lower(),
                    "file_size": mo.get("size") or 0,
                    "uploaded_at": m.get("timestamp"),
                    "notified": True,
                }).execute()
                totals["recovered"] += 1
            except Exception as e:
                totals["failed"] += 1
                print(f"    [recover] uploaded but not logged ({name}): {e}")

    print(f"[recover] {mode} done: {totals}")


if __name__ == "__main__":
    main()
