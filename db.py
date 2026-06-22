import os, json, hashlib
from datetime import datetime
import pg8000.dbapi
from urllib.parse import urlparse

def _conn():
    url = os.environ["DATABASE_URL"]
    p = urlparse(url)
    return pg8000.dbapi.connect(
        host=p.hostname,
        port=p.port or 5432,
        database=p.path.lstrip('/'),
        user=p.username,
        password=p.password,
    )

def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def _one(cur):
    cols = [d[0] for d in cur.description]
    row  = cur.fetchone()
    return dict(zip(cols, row)) if row else None

def init_db():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    name     TEXT NOT NULL,
                    email    TEXT PRIMARY KEY,
                    password TEXT NOT NULL,
                    role     TEXT NOT NULL DEFAULT 'Counsellor'
                );
                CREATE TABLE IF NOT EXISTS applications (
                    app_id           TEXT PRIMARY KEY,
                    student_name     TEXT DEFAULT '',
                    student_email    TEXT DEFAULT '',
                    student_phone    TEXT DEFAULT '',
                    schools          TEXT DEFAULT '[]',
                    counsellor_name  TEXT DEFAULT '',
                    counsellor_email TEXT DEFAULT '',
                    counsellor_phone TEXT DEFAULT '',
                    notes            TEXT DEFAULT '',
                    documents        TEXT DEFAULT '',
                    status           TEXT DEFAULT 'Not Checked',
                    assigned_officer TEXT DEFAULT '',
                    submitted_at     TEXT DEFAULT '',
                    last_updated     TEXT DEFAULT '',
                    officer_notes    TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS files (
                    app_id       TEXT,
                    filename     TEXT,
                    data         BYTEA,
                    content_type TEXT,
                    PRIMARY KEY (app_id, filename)
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id        TEXT PRIMARY KEY,
                    recipient TEXT,
                    sender    TEXT,
                    type      TEXT,
                    message   TEXT,
                    app_id    TEXT,
                    time      TEXT,
                    read      BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
        conn.commit()

def load_universities():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universities.json")
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return []

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── Users ──────────────────────────────────────────────────────────────────────
def get_all_users():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY name")
            return _rows(cur)

def register_user(name, email, password, role):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM users WHERE email=%s", (email.lower(),))
            if cur.fetchone():
                return False, "Email already registered."
            cur.execute(
                "INSERT INTO users (name,email,password,role) VALUES (%s,%s,%s,%s)",
                (name, email.lower(), hash_pw(password), role)
            )
        conn.commit()
    return True, "Account created!"

def login_user(email, password):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email=%s", (email.lower(),))
            row = _one(cur)
            if row and row["password"] == hash_pw(password):
                return True, row
    return False, None

def update_user_role(email, role):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role=%s WHERE email=%s", (role, email))
        conn.commit()

def reset_user_password(email, new_password):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password=%s WHERE email=%s", (hash_pw(new_password), email))
        conn.commit()

def delete_user(email):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE email=%s", (email,))
        conn.commit()

def get_officers():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM users WHERE role='Application Officer'")
            return [r[0] for r in cur.fetchall()]

def get_counsellor_phone(email):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT counsellor_phone FROM applications WHERE counsellor_email=%s "
                "ORDER BY submitted_at DESC LIMIT 1",
                (email,)
            )
            row = cur.fetchone()
            return row[0] if row else ""

# ── Round-robin ────────────────────────────────────────────────────────────────
def get_next_officer():
    officers = get_officers()
    if not officers: return "Unassigned"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM state WHERE key='next_officer_index'")
            row     = cur.fetchone()
            idx     = int(row[0]) if row else 0
            officer = officers[idx % len(officers)]
            new_idx = (idx + 1) % len(officers)
            if row:
                cur.execute("UPDATE state SET value=%s WHERE key='next_officer_index'", (str(new_idx),))
            else:
                cur.execute("INSERT INTO state (key,value) VALUES ('next_officer_index',%s)", (str(new_idx),))
        conn.commit()
    return officer

# ── Applications ───────────────────────────────────────────────────────────────
def load_applications(counsellor_email=None, officer_name=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            sql, params = "SELECT * FROM applications", []
            conds = []
            if counsellor_email:
                conds.append("counsellor_email=%s"); params.append(counsellor_email)
            if officer_name:
                conds.append("assigned_officer=%s"); params.append(officer_name)
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            sql += " ORDER BY submitted_at DESC"
            cur.execute(sql, params)
            return _rows(cur)

def get_application(app_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM applications WHERE app_id=%s", (app_id,))
            return _one(cur)

def save_application(data):
    cols  = list(data.keys())
    vals  = [data[c] for c in cols]
    sql   = f"INSERT INTO applications ({','.join(cols)}) VALUES ({','.join(['%s']*len(cols))})"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
        conn.commit()

def edit_application(app_id, updated):
    updated["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    sets = ",".join([f"{k}=%s" for k in updated])
    vals = list(updated.values()) + [app_id]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE applications SET {sets} WHERE app_id=%s", vals)
        conn.commit()

def update_status(app_id, status, notes=""):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE applications SET status=%s, officer_notes=%s, last_updated=%s WHERE app_id=%s",
                (status, notes, datetime.now().strftime("%Y-%m-%d %H:%M"), app_id)
            )
        conn.commit()

def reassign(app_id, officer):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE applications SET assigned_officer=%s, last_updated=%s WHERE app_id=%s",
                (officer, datetime.now().strftime("%Y-%m-%d %H:%M"), app_id)
            )
        conn.commit()

# ── Files (stored in DB) ───────────────────────────────────────────────────────
def upload_file(app_id, filename, data, content_type):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO files (app_id,filename,data,content_type) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (app_id,filename) DO UPDATE SET data=EXCLUDED.data, content_type=EXCLUDED.content_type",
                (app_id, filename, data, content_type)
            )
        conn.commit()
    return filename

def get_file(app_id, filename):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data, content_type FROM files WHERE app_id=%s AND filename=%s",
                (app_id, filename)
            )
            return cur.fetchone()  # returns (data, content_type) or None

def get_file_url(app_id, filename):
    return f"/files/{app_id}/{filename}"

# ── Notifications ──────────────────────────────────────────────────────────────
def load_notifications(recipient=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            if recipient:
                cur.execute(
                    "SELECT * FROM notifications WHERE recipient=%s OR recipient='ALL_OFFICERS' ORDER BY time DESC",
                    (recipient,)
                )
            else:
                cur.execute("SELECT * FROM notifications ORDER BY time DESC")
            return _rows(cur)

def add_notification(notif):
    cols = list(notif.keys())
    vals = [notif[c] for c in cols]
    sql  = f"INSERT INTO notifications ({','.join(cols)}) VALUES ({','.join(['%s']*len(cols))})"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
        conn.commit()

def mark_read(recipient):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET read=TRUE WHERE recipient=%s", (recipient,))
        conn.commit()

def mark_officer_read(officer_name):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET read=TRUE WHERE recipient='ALL_OFFICERS'")
            cur.execute("UPDATE notifications SET read=TRUE WHERE recipient=%s", (officer_name,))
        conn.commit()

def unread_count(recipient, is_officer=False):
    with _conn() as conn:
        with conn.cursor() as cur:
            if is_officer:
                cur.execute("SELECT COUNT(*) FROM notifications WHERE recipient='ALL_OFFICERS' AND read=FALSE")
                c1 = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM notifications WHERE recipient=%s AND read=FALSE", (recipient,))
                c2 = cur.fetchone()[0]
                return c1 + c2
            cur.execute("SELECT COUNT(*) FROM notifications WHERE recipient=%s AND read=FALSE", (recipient,))
            return cur.fetchone()[0]

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
    return "" if s.lower() == "nan" else s

def parse_schools(raw):
    try:
        d = json.loads(raw)
        if isinstance(d, list): return d
    except: pass
    return []
