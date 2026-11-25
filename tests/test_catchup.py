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
config.DATE_FOLDERS_FROM = ""  # date folders are tested below; this check stays in the client folder on any day
puts: list[str] = []
sp.httpx.put = lambda url, **k: (puts.append(url), Resp(409))[1]
out = sp.save_media("Alpha", "https://x/y", "bill.pdf", if_exists="skip")
ok("catch-up upload uses 'fail if it exists'", puts and "conflictBehavior=fail" in puts[-1])
ok("409 returns ALREADY_EXISTS and uploads nothing new", out == sp.ALREADY_EXISTS)

# ---------------------------------------------------------------- date-wise folders
config.DATE_FOLDERS_FROM = "2026-11-18"
ok("before the switch date: client folder itself", naming.dated_subfolder("2026-11-17T12:00:00Z") == "")
ok("from the switch date: YYYY-MM-DD (IST)", naming.dated_subfolder("2026-11-20T10:00:00Z") == "2026-11-20")
ok("IST date is used (17 Nov 19:00 UTC is 18 Nov in India)", naming.dated_subfolder("2026-11-17T19:00:00Z") == "2026-11-18")
config.DATE_FOLDERS_FROM = ""
ok("blank setting turns date folders off", naming.dated_subfolder("2026-11-20T10:00:00Z") == "")
config.DATE_FOLDERS_FROM = "2026-11-18"
created: list[tuple] = []
sp._create_folder_if_missing = lambda parent, name: created.append((parent, name))
puts.clear()
sp.httpx.put = lambda url, **k: (puts.append(url), Resp(201, js={"webUrl": "u", "id": "i"}))[1]
sp.save_media("Alpha", "https://x/y", "bill.pdf", message_timestamp="2026-11-20T10:00:00Z", if_exists="skip")
ok("new file goes into <client>/2026-11-20/", created[-1] == ("/Client Files/Alpha", "2026-11-20") and "/Alpha/2026-11-20/bill.pdf" in puts[-1])
sp.save_media("Alpha", "https://x/y", "old.pdf", message_timestamp="2026-11-10T10:00:00Z", if_exists="skip")
ok("old file stays in the client folder", "/Alpha/old.pdf" in puts[-1] and "/Alpha/2026" not in puts[-1])
seen_urls: list[str] = []
backfill_httpx.get = lambda url, **k: (seen_urls.append(url), Resp(404))[1]
backfill._already_downloaded("Alpha", "bill.pdf", None, "2026-11-20T10:00:00Z")
ok("catch-up looks in the same date folder", "/Alpha/2026-11-20/bill.pdf" in seen_urls[-1])
backfill_httpx.get = real_get

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
    {"unique_id": "E", "body": "", "timestamp": "2026-11-17T01:04:00Z", "sender_phone": "918888888888",
     "media": {"path": "/x/bill.pdf", "mimetype": "application/pdf", "filename": "bill.pdf"}},
    {"unique_id": "F", "body": "", "timestamp": "2026-11-17T01:05:00Z", "sender_phone": "918888888888", "from_me": True,
     "media": {"path": "/x/recon.xlsx", "mimetype": "application/vnd.ms-excel", "filename": "recon.xlsx"}},
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
rf.backfill._already_downloaded = lambda folder, name, parent=None, ts=None: name == "old.jpg"
saved_msgs: list[dict] = []
saved_files: list[tuple] = []
db.save_message = lambda **k: (saved_msgs.append(k), "id")[1]
outcomes = {"bill.pdf": "saved"}
rf.save_missing_file = lambda g, m, name: (saved_files.append((g["client_id"], name)), outcomes.get(name, "saved"))[1]
sent: list[str] = []


async def fake_send(text, chat=None):
    sent.append(text)
    return True


teams._send_teams_message = fake_send
NOW = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)

s = catchup.run(48, apply=False, now=NOW)
ok("dry run finds 5 missing messages (2 texts + 3 caption-less files) and 1 missing file",
   (s["messages_missing"], s["files_missing"]) == (5, 1))
ok("dry run writes nothing", saved_msgs == [] and saved_files == [])
ok("a failing group is retried 4 times, then reported, others still run", calls["fetch"] == 5 and len(s["errors"]) == 1)
ok("error text sent to Teams has no URL (exception type only)", s["errors"] == ["Beta: RuntimeError"])

s = catchup.run(48, apply=True, now=NOW)
d = {k["unique_id"]: k for k in saved_msgs}
ok("apply saves exactly the missing messages", set(d) == {"B", "C", "D", "E", "F"} and s["messages_saved"] == 5)
ok("team caption saved as outgoing, client text as incoming", d["C"]["direction"] == "outgoing" and d["B"]["direction"] == "incoming")
ok("catch-up stores files as '[File shared: name]' plus the caption",
   d["C"]["body"] == "[File shared: arn.pdf] Filed, ARN attached" and d["E"]["body"] == "[File shared: bill.pdf]")
ok("our caption-less file from our own phone is saved as outgoing", d["F"]["direction"] == "outgoing" and d["F"]["body"] == "[File shared: recon.xlsx]")
ok("apply uploads only the client file not in SharePoint", saved_files == [("c1", "bill.pdf")] and s["files_saved"] == 1)
ok("team files (team number or our own phone) are never picked up", [m["unique_id"] for m in rf.media_messages(MSGS)] == ["D", "E"])

saved_files.clear()
outcomes["bill.pdf"] = "present"
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

# ---------------------------------------------------------------- live bot: only client files are saved
uploads: list[str] = []
kb: list[dict] = []
main.db.find_client_by_group = lambda chat_id: {"client_id": "c1", "client_name": "Alpha"}
main.db.add_contact = lambda *a, **k: None
logged: list[dict] = []
main.db.log_upload = lambda **k: logged.append(k)
main.db.get_va_for_client = lambda cid: None
main.teams.notify = lambda **k: None
main.storage.save_media = lambda folder, url, name, parent=None, **k: (uploads.append(name), "https://sp/x")[1]
main._save_to_knowledge_base = lambda client, chat_id, sender, body, msg, uid: kb.append({"uid": uid, "body": body})


def live(uid, sender, name, body="", from_me=False):
    return main._process_message({"chat_id": "g1@g.us", "sender_phone": sender, "body": body, "from_me": from_me,
                                  "message_type": "document", "timestamp": "2026-11-24T05:00:00Z",
                                  "media": {"path": "/storage/" + name, "mimetype": "application/pdf", "filename": name}}, uid)


r = live("T1", "919741428606", "recon.pdf", body="Here is the recon")
ok("live: a file from a team number is not saved, but the message is kept with its caption",
   r == {"status": "ok"} and uploads == [] and kb == [{"uid": "T1", "body": "[File shared: recon.pdf] Here is the recon"}])
live("T2", "918888888888", "ICICI letter Signed.pdf", from_me=True)
ok("live: a file from our own connected phone is not saved", uploads == [])
ok("live: our file with NO caption still reaches the timeline (Forelife, 25 Nov)",
   kb[-1] == {"uid": "T2", "body": "[File shared: ICICI letter Signed.pdf]"})
live("T3", "918888888888", "bill.pdf")
ok("live: a client file is saved", uploads == ["bill.pdf"])
ok("live: a client file with no caption is on the timeline too", kb[-1] == {"uid": "T3", "body": "[File shared: bill.pdf]"})
live("T4", "918888888888", "oct.pdf", body="October bill, please book it")
ok("live: the text typed under a client file is logged as its caption", logged[-1]["caption"] == "October bill, please book it")
ok("live: the timeline line has the file name and the caption", kb[-1]["body"] == "[File shared: oct.pdf] October bill, please book it")

# ---------------------------------------------------------------- message text rule (shared by live bot and catch-up)
ok("text: a plain message is unchanged", naming.message_text({"body": "  hello  "}) == "hello")
ok("text: no body and no file is empty", naming.message_text({"body": None}) == "")
ok("text: a file with no name gets the message-time name",
   naming.message_text({"media": {"path": "/x/", "mimetype": "application/pdf"}, "timestamp": "2026-11-25T07:30:00Z"})
   == "[File shared: file_20261125_073000.pdf]")
real_hashes = catchup.existing_hashes
catchup.existing_hashes = lambda uids: {"HAVE"}
picked = catchup.missing_messages([
    {"unique_id": "F1", "body": None, "media": {"filename": "signed.pdf", "mimetype": "application/pdf"}, "timestamp": "2026-11-25T07:30:00Z"},
    {"unique_id": "HAVE", "body": "already saved", "timestamp": "2026-11-25T07:00:00Z"},
    {"unique_id": "E1", "body": "   ", "timestamp": "2026-11-25T07:40:00Z"},
])
ok("catch-up: a missed file with no caption is picked up; saved and empty messages are not",
   [m["unique_id"] for m in picked] == ["F1"])
catchup.existing_hashes = real_hashes

# ---------------------------------------------------------------- caption column: insert, and fallback while it is missing
inserted: list[dict] = []
missing_column = {"on": True}


class Ins:
    def insert(self, row):
        self.row = row
        return self

    def execute(self):
        if missing_column["on"] and "caption" in self.row:
            raise RuntimeError("Could not find the 'caption' column of 'document_uploads' in the schema cache")
        inserted.append(self.row)

        class R:
            data = [{"upload_id": "u1"}]
        return R()


db._supabase.table = lambda name: Ins()
db.insert_upload({"file_name": "a.pdf"}, "  please book  ")
ok("no caption column yet: the file is still logged, without the caption", inserted == [{"file_name": "a.pdf"}])
inserted.clear()
missing_column["on"] = False
db.insert_upload({"file_name": "a.pdf"}, "  please book  ")
ok("with the column: the caption is trimmed and stored", inserted == [{"file_name": "a.pdf", "caption": "please book"}])
inserted.clear()
db.insert_upload({"file_name": "b.jpg"}, "   ")
ok("a blank caption is not sent", inserted == [{"file_name": "b.jpg"}])

print("ALL PASS" if all(res) else f"{res.count(False)} FAILED")
sys.exit(0 if all(res) else 1)
