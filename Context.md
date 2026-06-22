# va-bot — Project Context

A WhatsApp file-collection bot. Files/messages sent by anyone in a client WhatsApp
group are automatically fetched and stored in a per-client folder on the Network
File Share. Built on FastAPI + Periskope.

_Last updated: 2026-06-22 — Teams notifications now LIVE: `TEAMS_WEBHOOK_URL` set in
`.env` and verified (test POST returned HTTP 202). Storage in `main.py` is now
`storage_sharepoint` (SharePoint via Graph). Earlier notes (upload logging, group-name
auto-mapping, folder name = group name, office server + Cloudflare Tunnel plan) still apply._

> **Status:** Bot working with Periskope webhooks. Sender/group lookup works, files
> download and save. Upload logging to Supabase `document_uploads` works.
> **Teams notifications are LIVE** — `teams.py` posts an Adaptive Card to a Power
> Automate **Workflows** webhook (`TEAMS_WEBHOOK_URL`), debounced 60s. The webhook
> URL is set and a direct test POST succeeded (HTTP 202). NO Azure app / Graph /
> admin-consent was needed for Teams — the Workflows webhook URL alone is enough.
> **Note on storage:** `main.py` imports `storage_sharepoint as storage` (SharePoint
> via Microsoft Graph), with `SHAREPOINT_*` creds in `.env` — the local-disk
> `./storage/<Group Name>/` notes below describe an earlier/testing backend.
>
> **Production target:** Run bot on the **office server** (24/7, on LAN) → files
> write directly to the **Network File Share** (`\\Server\ClientDocs\...`). Public
> webhook URL via **Cloudflare Tunnel** (permanent, free, runs as Windows service).

---

## What it does (end to end)

1. Anyone sends a file/message in a `<> VA` or `<> Virtual Accounting` WhatsApp group.
   Periskope fires `POST /webhook` with a `message.created` event.
2. Bot reads `sender_phone` (always present) and `chat_id` (the group ID).
3. **3-step client lookup:**
   - **Step 1 — known number:** look up `sender_phone` in `client_contacts`. Found →
     identify client. If group ID not saved yet → save it now (self-learning).
   - **Step 2 — known group:** `chat_id` matches `whatsapp_group_id` in `clients` →
     identify client. Auto-add `sender_phone` to `client_contacts` (self-learning).
   - **Step 3 — new group:** fetch group from Periskope API. Name contains `<> VA`
     or `<> Virtual Accounting`? → fetch all member numbers → cross-reference against
     `client_contacts` → first match = client. Save group ID + group name permanently
     (never overwritten). Auto-add sender to contacts.
4. **Folder name = WhatsApp group name** (e.g. `"Eiffel Landmarks <> VA"`).
5. **Route by message type:**
   - **DOCUMENT / IMAGE / VIDEO / AUDIO** → download from Periskope's public Google
     Storage URL → save to `./storage/<Group Name>/<filename>`. Duplicates get `_1`,
     `_2` suffix — never overwritten.
   - **TEXT / CHAT** → append as timestamped line to `./storage/<Group Name>/notes.txt`.
6. **Log upload** to Supabase `document_uploads` table (client, group, sender, filename, size, timestamp).
7. **Teams notification** — debounced 60s — sends one message per burst to the VA
   channel: `"Eiffel Landmarks <> VA — uploaded 3 document(s)"`.

---

## Files

| File | Role |
|------|------|
| `main.py` | FastAPI webhook. 3-step lookup → save file → log → notify Teams. |
| `config.py` | All credentials/settings. Env vars first, hardcoded fallback. |
| `db.py` | Supabase: lookups by number/group/members, update group, add contact, log upload, notify helpers. |
| `storage.py` | Local disk backend (test). Same 3 signatures for easy swap to network share. |
| `messaging.py` | Periskope outbound API (not used in current flow — no replies needed). |
| `teams.py` | Teams Incoming Webhook notifications with 60s debounce. |
| `requirements.txt` | fastapi, uvicorn, supabase, httpx |

### Key function signatures
- `db.find_client_by_number(whatsapp_number)` → `{client_id, client_name, folder_name, whatsapp_group_id}` or `None`
- `db.find_client_by_group(chat_id)` → same dict or `None`
- `db.find_client_by_group_members(chat_id)` → same dict or `None` (fetches Periskope API)
- `db.update_group_id(client_id, group_id, group_name)` → saves both (only if null)
- `db.add_contact(client_id, whatsapp_number)` → inserts to client_contacts (if not exists)
- `db.log_upload(...)` → inserts row to document_uploads
- `db.get_unnotified_count(client_id)` → int
- `db.mark_notified(client_id)` → flips notified=true
- `storage.save_media(folder_name, url, filename)` → local file path or `None`
- `storage.save_text_note(folder_name, text)` → local file path or `None`
- `teams.notify(client_id, client_name, folder_name, sender_phone, count_fn, mark_fn)` → schedules debounced notification

---

## Supabase schema

### clients
`client_id` uuid, `client_name`, `assigned_va`, `whatsapp_group_id` (text),
`whatsapp_group_name` (text) — both added via:
```sql
ALTER TABLE clients ADD COLUMN IF NOT EXISTS whatsapp_group_id text;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS whatsapp_group_name text;
```

### client_contacts
`contact_id` uuid, `client_id` uuid FK, `whatsapp_number`, `contact_name`, `role`,
`is_primary`. Auto-populated by the bot as new senders are discovered.

### document_uploads (create once)
```sql
CREATE TABLE IF NOT EXISTS public.document_uploads (
  upload_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id      uuid REFERENCES public.clients(client_id),
  client_name    text NOT NULL,
  folder_name    text NOT NULL,
  group_id       text,
  group_name     text,
  sender_phone   text,
  file_name      text,
  file_type      text,
  file_size      bigint,
  uploaded_at    timestamptz DEFAULT now(),
  notified       boolean DEFAULT false
);
CREATE INDEX ON public.document_uploads (client_id, notified);
CREATE INDEX ON public.document_uploads (uploaded_at);
```

---

## Periskope

### Inbound webhook payload (real observed structure)
```json
{
  "event_type": "message.created",
  "data": {
    "chat_id": "120363408058769914@g.us",
    "sender_phone": "916372161101@c.us",
    "author": null,
    "from_me": false,
    "message_type": "document",
    "body": "caption or null",
    "has_media": null,
    "media": {
      "path": "https://storage.googleapis.com/periskope-attachments/...",
      "filename": "NDA Copy.pdf",
      "mimetype": "application/pdf",
      "size": 368416
    },
    "org_phone": "918904987623@c.us",
    "timestamp": "2026-06-18T12:07:27+00:00"
  }
}
```

### Key payload notes
- `event_type` (not `event`) — code handles both
- `sender_phone` always present — use this, not `author` (often null)
- `has_media` often null — check `media` object directly
- Media URL is full public Google Storage URL — no auth needed to download
- Group: `chat_id` ends `@g.us` | 1:1: ends `@c.us`
- Periskope fires webhook once per connected GM in the group — same message
  can arrive 2–3× (dedup on `unique_id` is a future improvement)

### Auth headers (outbound API)
```
authorization: Bearer <PERISKOPE_API_KEY>
x-phone: <GM_phone_digits_only>
```

### Group members API
`GET /v1/chats/{chat_id}` returns `chat_name` + `members` object:
```json
{
  "chat_name": "Eiffel Landmarks <> VA",
  "members": {
    "919876543210@c.us": {"is_admin": false},
    "918904987623@c.us": {"is_admin": true}
  }
}
```
Used in Step 3 to cross-reference members against `client_contacts`.

---

## Self-learning group/contact mapping

```
Message arrives in a group
      │
      ▼
Step 1: sender_phone in client_contacts?
      ├── YES → client found + save group ID if missing → save file ✓
      └── NO ↓

Step 2: group ID in clients.whatsapp_group_id?
      ├── YES → client found + auto-add sender to contacts → save file ✓
      └── NO ↓

Step 3: fetch group from Periskope API
      Name contains "<> VA" or "<> Virtual Accounting"?
      ├── NO  → skip (internal/non-client group)
      └── YES → fetch all members → cross-ref against client_contacts
                ├── match found → save group ID + name (once, never overwrite)
                │                  auto-add sender → save file ✓
                └── no match → log it, skip
```

**Only runs Step 3 once per group ever.** After that, Step 1 or 2 handles it instantly.

---

## Teams notifications

- **Trigger:** every successful file upload
- **Debounce:** 60s — burst of uploads → one Teams message
- **Message format:**
  ```
  Eiffel Landmarks <> VA
  Uploaded 3 document(s)
  Sender: 916372161101
  Time: 2026-06-19 10:32 UTC
  ```
- **Config:** set `TEAMS_WEBHOOK_URL` in `config.py`
  (Teams channel → `...` → Connectors → Incoming Webhook → create → copy URL)
- **Supabase tracking:** `notified=false` rows → send → flip to `notified=true`

---

## config.py settings

- **Supabase:** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- **Periskope:** `PERISKOPE_API_KEY` (Bearer token), `PERISKOPE_PHONE` (one GM's
  number digits only), `PERISKOPE_BASE_URL`, `PERISKOPE_MEDIA_BASE_URL`
- **Teams:** `TEAMS_WEBHOOK_URL`
- **Local storage (test):** `STORAGE_ROOT` (`./storage`)
- **OneDrive (future):** `ONEDRIVE_TENANT_ID`, `ONEDRIVE_CLIENT_ID`,
  `ONEDRIVE_CLIENT_SECRET`, `ONEDRIVE_DRIVE_ID`, `ONEDRIVE_ROOT_FOLDER`

⚠️ Move keys to `.env` / env vars before deploying. Add `.gitignore`.

---

## Running & testing locally

```powershell
# 1. Activate venv
.\venv\Scripts\activate

# 2. Start the bot
uvicorn main:app --reload --port 8000

# 3. Expose publicly (second terminal)
ngrok http 8000
# → copy https://xxxx.ngrok-free.app

# 4. Set in Periskope → Settings → Webhooks
#    URL: https://xxxx.ngrok-free.app/webhook
#    Event: message.created
```

Files land at: `va-bot/storage/<Group Name>/filename.pdf`

---

## Production deployment — Office Server + Cloudflare Tunnel

**Decided approach (2026-06-19):** Run the bot on the **office server** (always-on,
on the LAN) so it can write directly to the **Network File Share** (`\\Server\ClientDocs\...`).
Public webhook reachability via **Cloudflare Tunnel**.

### Why office server (not VPS)
- Network File Share is SMB — only reachable on office LAN
- VPS on public internet cannot reach it
- Office server is already 24/7 and on the LAN — no new hardware needed

### Cloudflare Tunnel (replaces ngrok for production)
```
Periskope → https://va-bot.yourdomain.com (permanent)
                ↓
        Cloudflare Tunnel (Windows service on office server)
                ↓
        http://localhost:8000 (bot running on same machine)
```
- **Free forever**, permanent URL, no port forwarding, no firewall changes
- Runs as a **Windows service** — auto-starts on server reboot
- Set webhook URL in Periskope once, never change it

### Setup steps (one-time)
1. Create free Cloudflare account, add a domain
2. Download `cloudflared.exe` on the office server
3. `cloudflared tunnel login`
4. `cloudflared tunnel create va-bot`
5. `cloudflared tunnel route dns va-bot va-bot.yourdomain.com`
6. `cloudflared service install` — runs forever as Windows service
7. Set `https://va-bot.yourdomain.com/webhook` in Periskope webhooks

### storage.py swap (when moving to network share)
Replace local disk writes with UNC path writes — same 3 function signatures,
`main.py` unchanged:
```python
STORAGE_ROOT = r"\\Server\ClientDocs"
# ensure_client_folder / save_media / save_text_note → os.makedirs + open()
```
Bot's Windows account needs write permission on the share.

---

## Planned / pending

- [ ] Run SQL to create `document_uploads` table in Supabase
- [ ] Add `whatsapp_group_id` and `whatsapp_group_name` columns to `clients`
- [ ] Paste Teams Incoming Webhook URL into `config.py`
- [ ] Swap `storage.py` to write to `\\Server\ClientDocs` when on office server
- [ ] Set up Cloudflare Tunnel on office server
- [ ] Dedup webhook fires (Periskope fires once per connected GM — same file arrives 2-3×)

---

## History / decisions

- Started: local disk → Google Drive → local disk (test) → Network File Share (production target)
- Messaging provider: Infobip → **Periskope** (2026-06-19)
- No outbound replies in current flow — not needed
- Sender identification: `author` field (unreliable) → **`sender_phone`** (always present)
- Group lookup: number-first → group-ID fallback → **group-member cross-reference** (Step 3)
- Folder name: was `client_name` → now **WhatsApp group name** (e.g. `"Eiffel Landmarks <> VA"`)
- Group patterns: originally included AI Accountant → **narrowed to `<> VA` and `<> Virtual Accounting` only**
- Auth header: Infobip used `App <key>` → Periskope uses **`Bearer <key>`**
- `has_media` flag unreliable in Periskope payloads → check `media` object directly
