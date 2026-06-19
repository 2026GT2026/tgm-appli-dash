import os, json, hashlib, uuid, logging, sys
from datetime import datetime
from functools import lru_cache
from supabase import create_client

logger = logging.getLogger(__name__)

def get_sb():
    url = os.environ.get("SUPABASE_URL","https://kqpxvaizyticiffdnugs.supabase.co")
    key = os.environ.get("SUPABASE_KEY","eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtxcHh2YWl6eXRpY2lmZmRudWdzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODE1MDgxMjMsImV4cCI6MjA5NzA4NDEyM30.lrFHqhl6uQBbIBqrX7frdmQ0jIagBCCiQPmfiAJpiYY")
    try:
        return create_client(url, key)
    except Exception as e:
        logger.error("Failed to initialise Supabase client: %s", e, exc_info=True)
        print(f"[db] FATAL: Supabase client init failed: {e}", file=sys.stderr, flush=True)
        raise

def load_universities():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universities.json")
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return []

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── Users ──────────────────────────────────────────────────────────────────────
def get_all_users():
    return get_sb().table("users").select("*").execute().data or []

def register_user(name, email, password, role):
    sb = get_sb()
    if sb.table("users").select("email").eq("email", email.lower()).execute().data:
        return False, "Email already registered."
    sb.table("users").insert({"name":name,"email":email.lower(),"password":hash_pw(password),"role":role}).execute()
    return True, "Account created!"

def login_user(email, password):
    res = get_sb().table("users").select("*").eq("email", email.lower()).execute()
    if res.data:
        u = res.data[0]
        if u["password"] == hash_pw(password): return True, u
    return False, None

def update_user_role(email, role):
    get_sb().table("users").update({"role":role}).eq("email",email).execute()

def reset_user_password(email, new_password):
    get_sb().table("users").update({"password":hash_pw(new_password)}).eq("email",email).execute()

def delete_user(email):
    get_sb().table("users").delete().eq("email",email).execute()

def get_officers():
    res = get_sb().table("users").select("name").eq("role","Application Officer").execute()
    return [r["name"] for r in (res.data or [])]

def get_counsellor_phone(email):
    res = get_sb().table("applications").select("counsellor_phone") \
        .eq("counsellor_email", email).order("submitted_at", desc=True).limit(1).execute()
    return res.data[0]["counsellor_phone"] if res.data else ""

# ── Round-robin ────────────────────────────────────────────────────────────────
def get_next_officer():
    officers = get_officers()
    if not officers: return "Unassigned"
    sb  = get_sb()
    res = sb.table("state").select("*").eq("key","next_officer_index").execute()
    idx = int(res.data[0]["value"]) if res.data else 0
    officer  = officers[idx % len(officers)]
    new_idx  = (idx+1) % len(officers)
    if res.data: sb.table("state").update({"value":str(new_idx)}).eq("key","next_officer_index").execute()
    else:        sb.table("state").insert({"key":"next_officer_index","value":str(new_idx)}).execute()
    return officer

# ── Applications ───────────────────────────────────────────────────────────────
def load_applications(counsellor_email=None, officer_name=None):
    sb = get_sb()
    q  = sb.table("applications").select("*").order("submitted_at", desc=True)
    if counsellor_email: q = q.eq("counsellor_email", counsellor_email)
    if officer_name:     q = q.eq("assigned_officer", officer_name)
    return q.execute().data or []

def get_application(app_id):
    res = get_sb().table("applications").select("*").eq("app_id",app_id).execute()
    return res.data[0] if res.data else None

def save_application(data):
    get_sb().table("applications").insert(data).execute()

def edit_application(app_id, updated):
    updated["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    get_sb().table("applications").update(updated).eq("app_id",app_id).execute()

def update_status(app_id, status, notes=""):
    get_sb().table("applications").update({
        "status":status, "officer_notes":notes,
        "last_updated":datetime.now().strftime("%Y-%m-%d %H:%M")
    }).eq("app_id",app_id).execute()

def reassign(app_id, officer):
    get_sb().table("applications").update({
        "assigned_officer":officer,
        "last_updated":datetime.now().strftime("%Y-%m-%d %H:%M")
    }).eq("app_id",app_id).execute()

# ── Files ──────────────────────────────────────────────────────────────────────
def upload_file(app_id, filename, data, content_type):
    path = f"{app_id}/{filename}"
    get_sb().storage.from_("documents").upload(path, data,
        {"content-type":content_type,"x-upsert":"true"})
    return filename

def get_file_url(app_id, filename):
    res = get_sb().storage.from_("documents").create_signed_url(f"{app_id}/{filename}", 3600)
    return res.get("signedURL") or res.get("signedUrl","")

# ── Notifications ──────────────────────────────────────────────────────────────
def load_notifications(recipient=None):
    sb = get_sb()
    if recipient:
        res = sb.table("notifications").select("*") \
            .or_(f"recipient.eq.{recipient},recipient.eq.ALL_OFFICERS") \
            .order("time", desc=True).execute()
    else:
        res = sb.table("notifications").select("*").order("time", desc=True).execute()
    return res.data or []

def add_notification(notif):
    get_sb().table("notifications").insert(notif).execute()

def mark_read(recipient):
    sb = get_sb()
    sb.table("notifications").update({"read":True}).eq("recipient",recipient).execute()

def mark_officer_read(officer_name):
    sb = get_sb()
    sb.table("notifications").update({"read":True}).eq("recipient","ALL_OFFICERS").execute()
    sb.table("notifications").update({"read":True}).eq("recipient",officer_name).execute()

def unread_count(recipient, is_officer=False):
    sb = get_sb()
    if is_officer:
        r1 = sb.table("notifications").select("id",count="exact").eq("recipient","ALL_OFFICERS").eq("read",False).execute()
        r2 = sb.table("notifications").select("id",count="exact").eq("recipient",recipient).eq("read",False).execute()
        return (r1.count or 0)+(r2.count or 0)
    res = sb.table("notifications").select("id",count="exact").eq("recipient",recipient).eq("read",False).execute()
    return res.count or 0

# ── Excel export ───────────────────────────────────────────────────────────────
def export_excel(rows):
    import io, pandas as pd
    df  = pd.DataFrame(rows)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Applications")
    return out.getvalue()

# ── Helpers ────────────────────────────────────────────────────────────────────
def clean(v):
    if v is None: return ""
    s = str(v).strip()
    return "" if s.lower()=="nan" else s

def parse_schools(raw):
    try:
        d = json.loads(raw)
        if isinstance(d, list): return d
    except: pass
    return []
