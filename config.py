import os
from dotenv import load_dotenv
load_dotenv()

# --- Supabase ---------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# --- Periskope --------------------------------------------------------------
PERISKOPE_API_KEY = os.getenv("PERISKOPE_API_KEY")
PERISKOPE_PHONE = os.getenv("PERISKOPE_PHONE")
PERISKOPE_BASE_URL = os.getenv("PERISKOPE_BASE_URL", "https://api.periskope.app/v1")
PERISKOPE_MEDIA_BASE_URL = os.getenv("PERISKOPE_MEDIA_BASE_URL", "https://api.periskope.app")

# --- Microsoft Teams --------------------------------------------------------
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL")  # legacy, no longer used
TEAMS_CHAT_ID = os.getenv("TEAMS_CHAT_ID")  # group chat ID e.g. 19:xxxx@thread.v2
TEAMS_PERSONAL_WEBHOOK_URL = os.getenv("TEAMS_PERSONAL_WEBHOOK_URL")  # n8n endpoint for VA personal chat

# --- Knowledge-base capture -------------------------------------------------
# Storing chat messages in client_messages is moving to the Client-Score
# webhook service. Unset (or anything but "false") keeps this bot writing them,
# exactly as before. Set to "false" only once Client-Score is live with
# WEBHOOK_DRY_RUN=false — turn that on first, so there is never a moment when
# neither service is writing. Files, notes.txt and flag alerts are unaffected.
SAVE_CHAT_MESSAGES_ENABLED = os.getenv("SAVE_CHAT_MESSAGES_ENABLED", "true").strip().lower() != "false"

# --- Local storage (for testing) --------------------------------------------
STORAGE_ROOT = os.getenv("STORAGE_ROOT", "./storage")

# --- SharePoint (Microsoft Graph) -------------------------------------------
SHAREPOINT_TENANT_ID = os.getenv("SHAREPOINT_TENANT_ID")
SHAREPOINT_CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID")
SHAREPOINT_CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET")
SHAREPOINT_DRIVE_ID = os.getenv("SHAREPOINT_DRIVE_ID")
SHAREPOINT_ROOT_FOLDER = os.getenv("SHAREPOINT_ROOT_FOLDER", "Client Files")
