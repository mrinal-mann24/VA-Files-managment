"""
Offline tests for the duplicate protections (no network, no database, no SharePoint).
Run from the repo root:  python tests/test_catchup.py      (prints PASS/FAIL per check, exits 1 on any failure)
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backfill  # noqa: E402
import catchup  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import naming  # noqa: E402
import recover_files as rf  # noqa: E402
import storage_sharepoint as sp  # noqa: E402
import teams  # noqa: E402

res: list[bool] = []


def ok(name: str, cond) -> None:
    res.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


class Resp:
    def __init__(self, code, content=b"", js=None):
        self.status_code, self.content, self._js = code, content, js or {}

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ---------------------------------------------------------------- naming: one rule for live and catch-up
media = {"path": "/storage/abc", "mimetype": "image/jpeg"}
ts = "2026-11-17T01:02:03Z"
ok("nameless file is named by MESSAGE time, the same on every run", naming.resolve_filename(media, "image/jpeg", ts) == "file_20261117_010203.jpg")
ok("backfill and recovery use the same function", backfill._resolve_filename is naming.resolve_filename)
ok("real WhatsApp file name wins", naming.resolve_filename({"filename": "GSTR3B"}, "application/pdf", ts) == "GSTR3B.pdf")
ok("media from a foreign host is refused", naming.media_url("https://evil.example/x.pdf") == "")
ok("Periskope paths become full URLs", naming.media_url("/storage/x.pdf").endswith("/storage/x.pdf"))
ok("logs keep host and path only, never the query token", naming.url_for_log("https://h.example/a/b.pdf?sig=SECRET") == "h.example/a/b.pdf")

# ---------------------------------------------------------------- C1: a SharePoint error is never "missing"
backfill._get_token = lambda: "t"
backfill_httpx = __import__("httpx")
real_get = backfill_httpx.get
try:
    backfill_httpx.get = lambda *a, **k: Resp(503)
    try:
        backfill._already_downloaded("Alpha", "bill.pdf")
        ok("503 on the file check raises (not 'missing')", False)
    except RuntimeError:
        ok("503 on the file check raises (not 'missing')", True)
    backfill_httpx.get = lambda *a, **k: Resp(404)
    ok("404 means missing", backfill._already_downloaded("Alpha", "bill.pdf") is False)
    backfill_httpx.get = lambda *a, **k: Resp(200)
    ok("200 means present", backfill._already_downloaded("Alpha", "bill.pdf") is True)
finally:
    backfill_httpx.get = real_get

# ---------------------------------------------------------------- exact-name upload for catch-up
sp._headers = lambda: {}
sp.ensure_client_folder = lambda *a, **k: "/Client Files/Alpha"
sp._set_file_dates = lambda *a, **k: None


class FakeClient:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url): return Resp(200, b"data")


sp.httpx.Client = FakeClient
puts: list[str] = []
sp.httpx.put = lambda url, **k: (puts.append(url), Resp(409))[1]
out = sp.save_media("Alpha", "https://x/y", "bill.pdf", if_exists="skip")
ok("catch-up upload uses 'fail if it exists'", puts and "conflictBehavior=fail" in puts[-1])
ok("409 returns ALREADY_EXISTS and uploads nothing new", out == sp.ALREADY_EXISTS)

# ---------------------------------------------------------------- catch-up run
db._TEAM_CACHE.update({"numbers": frozenset({"9741428606"}), "at": time.monotonic()})
G1 = {"client_id": "c1", "client_name": "Alpha", "chat_id": "g1@g.us", "group_id": "g1", "folder_name": "Alpha", "parent_name": None}
G2 = {"client_id": "c2", "client_name": "Beta", "chat_id": "g2@g.us", "group_id": "g2", "folder_name": "Beta", "parent_name": None}
rf.linked_active_groups = lambda only=None: [g for g in (G1, G2) if not only or g["group_id"] == only]
MSGS = [
    {"unique_id": "A", "body": "saved already", "timestamp": "2026-11-17T01:00:00Z", "sender_phone": "918888888888"},
    {"unique_id": "B", "body": "missed text", "timestamp": "2026-11-17T01:01:00Z", "sender_phone": "918888888888"},
    {"unique_id": "C", "body": "Filed, ARN attached", "timestamp": "2026-11-17T01:02:00Z", "sender_phone": "919741428606",
     "media": {"path": "/x/arn.pdf", "mimetype": "application/pdf", "filename": "arn.pdf"}},
    {"unique_id": "D", "body": "", "timestamp": "2026-11-17T01:03:00Z", "media": {"path": "/x/old.jpg", "mimetype": "image/jpeg", "filename": "old.jpg"}},
]
calls = {"fetch": 0}


def fake_fetch(chat_id, start, end):
    calls["fetch"] += 1
    if chat_id == "g2@g.us":
        raise RuntimeError("503 Service Unavailable")
    return list(MSGS)


rf.fetch_window = fake_fetch
catchup.time.sleep = lambda s: None


class T:
    def select(self, *a): return self
    def in_(self, c, vals): self.v = vals; return self
    def eq(self, *a): return self
    def execute(self):
        v = getattr(self, "v", [])
        class R: data = [{"dedup_hash": h} for h in v if h in {"A"}]
        return R()


db._supabase.table = lambda name: T()
rf.logged_files = lambda gid: set()
rf.backfill._already_downloaded = lambda folder, name, parent=None: name == "old.jpg"
saved_msgs: list[dict] = []
saved_files: list[tuple] = []
db.save_message = lambda **k: (saved_msgs.append(k), "id")[1]
outcomes = {"arn.pdf": "saved"}
rf.save_missing_file = lambda g, m, name: (saved_files.append((g["client_id"], name)), outcomes.get(name, "saved"))[1]
sent: list[str] = []


async def fake_send(text, chat=None):
    sent.append(text)
    return True


teams._send_teams_message = fake_send
NOW = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)

s = catchup.run(48, apply=False, now=NOW)
ok("dry run finds 2 missing messages and 1 missing file", (s["messages_missing"], s["files_missing"]) == (2, 1))
ok("dry run writes nothing", saved_msgs == [] and saved_files == [])
ok("a failing group is retried 4 times, then reported, others still run", calls["fetch"] == 5 and len(s["errors"]) == 1)
ok("error text sent to Teams has no URL (exception type only)", s["errors"] == ["Beta: RuntimeError"])

s = catchup.run(48, apply=True, now=NOW)
d = {k["unique_id"]: k for k in saved_msgs}
ok("apply saves exactly the missing messages", set(d) == {"B", "C"} and s["messages_saved"] == 2)
ok("team caption saved as outgoing, client text as incoming", d["C"]["direction"] == "outgoing" and d["B"]["direction"] == "incoming")
ok("apply uploads only the file not in SharePoint", saved_files == [("c1", "arn.pdf")] and s["files_saved"] == 1)

saved_files.clear()
outcomes["arn.pdf"] = "present"
s = catchup.run(48, apply=True, now=NOW)
ok("'present' (live bot saved it meanwhile) is neither saved nor failed", (s["files_saved"], s["files_failed"]) == (0, 0))

saved_msgs.clear()
real_flag = config.SAVE_CHAT_MESSAGES_ENABLED
config.SAVE_CHAT_MESSAGES_ENABLED = False
s = catchup.run(48, apply=True, now=NOW)
config.SAVE_CHAT_MESSAGES_ENABLED = real_flag
ok("messages are skipped when SAVE_CHAT_MESSAGES_ENABLED is off", saved_msgs == [] and s["messages_missing"] == 0)

catchup.send_report({"hours": 48, "groups": 2, "messages_saved": 2, "files_saved": 1, "files_failed": 0, "errors": [],
                     "recovered_by_group": {"Alpha": {"messages": 2, "files": 1, "failed": 0}}})
ok("Teams report sent when something was recovered", sent and "Recovered 2 message(s) and 1 file(s)" in sent[-1])
quiet = {"hours": 48, "groups": 30, "messages_saved": 0, "files_saved": 0, "files_failed": 0, "errors": [], "recovered_by_group": {}}
ok("quiet night sends nothing", catchup.report(quiet) is None)
ok("next run is 02:30 IST the same night", round(catchup.seconds_until_next_run(datetime(2026, 11, 16, 19, 30, tzinfo=timezone.utc)) / 60) == 90)
ok("after 02:30 the next run is tomorrow", round(catchup.seconds_until_next_run(datetime(2026, 11, 16, 21, 30, tzinfo=timezone.utc)) / 3600, 1) == 23.5)

# ---------------------------------------------------------------- I2: live webhook never runs one message twice at once
import main  # noqa: E402

main._IN_FLIGHT.add("BUSY")


class Req:
    headers: dict = {}
    query_params: dict = {}

    async def json(self):
        return {"event": "message.created", "data": {"unique_id": "BUSY", "chat_id": "g1@g.us"}}


r = asyncio.run(main.webhook(Req()))
ok("a copy that arrives while the first is processing is skipped", r == {"status": "duplicate"})
main._IN_FLIGHT.discard("BUSY")

print("ALL PASS" if all(res) else f"{res.count(False)} FAILED")
sys.exit(0 if all(res) else 1)
