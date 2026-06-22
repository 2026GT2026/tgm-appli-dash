import os, json, hashlib, uuid
from contextlib import contextmanager
from datetime import datetime
import psycopg2
import psycopg2.extras

# ── Connection ─────────────────────────────────────────────────────────────────
def _get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

@contextmanager
def _db():
    """Yield a (conn, cursor) pair and commit/rollback automatically."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ── Schema bootstrap ───────────────────────────────────────────────────────────
def init_db():
    """Create tables if they don't exist. Call once at startup."""
    with _db() as (conn, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       SERIAL PRIMARY KEY,
                name     TEXT NOT NULL,
                email    TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role     TEXT NOT NULL DEFAULT 'Counsellor'
            );
            CREATE TABLE IF NOT EXISTS applications (
                app_id           TEXT PRIMARY KEY,
                student_name     TEXT,
                student_email    TEXT,
                student_phone    TEXT,
                schools          TEXT,
                counsellor_name  TEXT,
                counsellor_email TEXT,
                counsellor_phone TEXT,
                notes            TEXT,
                documents        TEXT,
                status           TEXT DEFAULT 'Not Checked',
                assigned_officer TEXT,
                submitted_at     TEXT,
                last_updated     TEXT,
                officer_notes    TEXT
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

_ALLOWED_APP_COLS = {
    "app_id", "student_name", "student_email", "student_phone", "schools",
    "counsellor_name", "counsellor_email", "counsellor_phone", "notes",
    "documents", "status", "assigned_officer", "submitted_at", "last_updated",
    "officer_notes",
}
_ALLOWED_NOTIF_COLS = {
    "id", "recipient", "sender", "type", "message", "app_id", "time", "read",
}

def _validate_cols(cols, allowed):
    bad = set(cols) - allowed
    if bad:
        raise ValueError(f"Disallowed column name(s): {bad}")

def load_universities():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universities.json")
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return []

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── Users ──────────────────────────────────────────────────────────────────────
def get_all_users():
    with _db() as (conn, cur):
        cur.execute("SELECT * FROM users ORDER BY name")
        return [dict(r) for r in cur.fetchall()]

def register_user(name, email, password, role):
    with _db() as (conn, cur):
        cur.execute("SELECT email FROM users WHERE email = %s", (email.lower(),))
        if cur.fetchone():
            return False, "Email already registered."
        cur.execute(
            "INSERT INTO users (name, email, password, role) VALUES (%s, %s, %s, %s)",
            (name, email.lower(), hash_pw(password), role)
        )
    return True, "Account created!"

def login_user(email, password):
    with _db() as (conn, cur):
        cur.execute("SELECT * FROM users WHERE email = %s", (email.lower(),))
        u = cur.fetchone()
        if u and u["password"] == hash_pw(password):
            return True, dict(u)
    return False, None

def update_user_role(email, role):
    with _db() as (conn, cur):
        cur.execute("UPDATE users SET role = %s WHERE email = %s", (role, email))

def reset_user_password(email, new_password):
    with _db() as (conn, cur):
        cur.execute("UPDATE users SET password = %s WHERE email = %s",
                    (hash_pw(new_password), email))

def delete_user(email):
    with _db() as (conn, cur):
        cur.execute("DELETE FROM users WHERE email = %s", (email,))

def get_officers():
    with _db() as (conn, cur):
        cur.execute("SELECT name FROM users WHERE role = 'Application Officer' ORDER BY name")
        return [r["name"] for r in cur.fetchall()]

def get_counsellor_phone(email):
    with _db() as (conn, cur):
        cur.execute(
            "SELECT counsellor_phone FROM applications "
            "WHERE counsellor_email = %s ORDER BY submitted_at DESC LIMIT 1",
            (email,)
        )
        row = cur.fetchone()
        return row["counsellor_phone"] if row else ""

# ── Round-robin ────────────────────────────────────────────────────────────────
def get_next_officer():
    officers = get_officers()
    if not officers: return "Unassigned"
    with _db() as (conn, cur):
        cur.execute("SELECT value FROM state WHERE key = 'next_officer_index'")
        row = cur.fetchone()
        idx = int(row["value"]) if row else 0
        officer = officers[idx % len(officers)]
        new_idx = (idx + 1) % len(officers)
        if row:
            cur.execute("UPDATE state SET value = %s WHERE key = 'next_officer_index'",
                        (str(new_idx),))
        else:
            cur.execute("INSERT INTO state (key, value) VALUES ('next_officer_index', %s)",
                        (str(new_idx),))
    return officer

# ── Applications ───────────────────────────────────────────────────────────────
def load_applications(counsellor_email=None, officer_name=None):
    with _db() as (conn, cur):
        sql    = "SELECT * FROM applications"
        params = []
        where  = []
        if counsellor_email:
            where.append("counsellor_email = %s")
            params.append(counsellor_email)
        if officer_name:
            where.append("assigned_officer = %s")
            params.append(officer_name)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY submitted_at DESC"
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

def get_application(app_id):
    with _db() as (conn, cur):
        cur.execute("SELECT * FROM applications WHERE app_id = %s", (app_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def save_application(data):
    _validate_cols(data.keys(), _ALLOWED_APP_COLS)
    with _db() as (conn, cur):
        cols   = list(data.keys())
        values = [data[c] for c in cols]
        sql = "INSERT INTO applications ({}) VALUES ({})".format(
            ", ".join(cols),
            ", ".join(["%s"] * len(cols))
        )
        cur.execute(sql, values)

def edit_application(app_id, updated):
    updated["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _validate_cols(updated.keys(), _ALLOWED_APP_COLS)
    with _db() as (conn, cur):
        sets   = ", ".join(f"{k} = %s" for k in updated)
        values = list(updated.values()) + [app_id]
        cur.execute(f"UPDATE applications SET {sets} WHERE app_id = %s", values)

def update_status(app_id, status, notes=""):
    with _db() as (conn, cur):
        cur.execute(
            "UPDATE applications SET status = %s, officer_notes = %s, last_updated = %s "
            "WHERE app_id = %s",
            (status, notes, datetime.now().strftime("%Y-%m-%d %H:%M"), app_id)
        )

def reassign(app_id, officer):
    with _db() as (conn, cur):
        cur.execute(
            "UPDATE applications SET assigned_officer = %s, last_updated = %s "
            "WHERE app_id = %s",
            (officer, datetime.now().strftime("%Y-%m-%d %H:%M"), app_id)
        )

# ── Files (stubbed — Postgres does not handle binary file storage) ─────────────
def upload_file(app_id, filename, data, content_type):
    """File storage is not available with Postgres. Returns filename unchanged."""
    return filename

def get_file_url(app_id, filename):
    """File storage is not available with Postgres. Returns empty string."""
    return ""

# ── Notifications ──────────────────────────────────────────────────────────────
def load_notifications(recipient=None):
    with _db() as (conn, cur):
        if recipient:
            cur.execute(
                "SELECT * FROM notifications "
                "WHERE recipient = %s OR recipient = 'ALL_OFFICERS' "
                "ORDER BY time DESC",
                (recipient,)
            )
        else:
            cur.execute("SELECT * FROM notifications ORDER BY time DESC")
        return [dict(r) for r in cur.fetchall()]

def add_notification(notif):
    _validate_cols(notif.keys(), _ALLOWED_NOTIF_COLS)
    with _db() as (conn, cur):
        cols   = list(notif.keys())
        values = [notif[c] for c in cols]
        sql = "INSERT INTO notifications ({}) VALUES ({})".format(
            ", ".join(cols),
            ", ".join(["%s"] * len(cols))
        )
        cur.execute(sql, values)

def mark_read(recipient):
    with _db() as (conn, cur):
        cur.execute(
            "UPDATE notifications SET read = TRUE WHERE recipient = %s",
            (recipient,)
        )

def mark_officer_read(officer_name):
    with _db() as (conn, cur):
        cur.execute(
            "UPDATE notifications SET read = TRUE "
            "WHERE recipient = 'ALL_OFFICERS' OR recipient = %s",
            (officer_name,)
        )

def unread_count(recipient, is_officer=False):
    with _db() as (conn, cur):
        if is_officer:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM notifications "
                "WHERE (recipient = 'ALL_OFFICERS' OR recipient = %s) AND read = FALSE",
                (recipient,)
            )
        else:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM notifications "
                "WHERE recipient = %s AND read = FALSE",
                (recipient,)
            )
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0

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
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return []
